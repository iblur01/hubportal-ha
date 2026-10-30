"""Client membre du maillage HubPortal : HA est un membre authentifié et non votant.

Ce module n'élit jamais de maître, ne compte pas dans le quorum et ne réécrit pas l'epoch.
La clé réseau ne doit apparaître dans aucun journal, attribut ou diagnostic.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import secrets
import ssl
import tempfile
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import (
    MESH_ALERT_EVENT,
    MESH_BACKOFF_BASE,
    MESH_BACKOFF_CAP,
    MESH_CONNECT_TIMEOUT,
    MESH_HANDSHAKE_TIMEOUT,
    MESH_LEASE_SECONDS,
    MESH_PATH,
    MESH_PORT,
    MESH_PROTO,
    MESH_SEND_TIMEOUT,
    MESH_TS_WINDOW,
)

_LOGGER = logging.getLogger(__name__)

STATE_KEYS = ("screen", "action_lock", "brightness", "timer", "timer_end", "timer_remaining")
LOCK_OFF_VALUES = (False, "off")


class MeshRejected(Exception):
    """Message ou session rejetés ; `reason` est un code stable sans contenu de message."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class MeshClosed(Exception):
    """Connexion fermée par le pair."""


class PinMismatch(MeshRejected):
    """Empreinte TLS différente de celle épinglée (identité clonée ou usurpée)."""

    def __init__(self) -> None:
        super().__init__("fingerprint")


class AdmissionRefused(Exception):
    """Le Portal admetteur a refusé ou fermé la demande."""


# ---------------------------------------------------------------- primitives de fil


def json_compact(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def message_mac(key: bytes, t: str, sender: str, seq: int, ts: int, epoch: int, p: dict[str, Any]) -> str:
    body = "|".join([t, sender, str(seq), str(ts), str(epoch), sha256_hex(json_compact(p))])
    return hmac.new(key, body.encode(), hashlib.sha256).hexdigest()


def hello_auth(key: bytes, nonce_peer: str, portal_id: str, fp_local: str, fp_peer: str) -> str:
    body = "|".join(["hello", nonce_peer, portal_id, fp_local, fp_peer])
    return hmac.new(key, body.encode(), hashlib.sha256).hexdigest()


def nonce_commit(nonce: str) -> str:
    """Engagement sur un nonce d'admission : SHA-256 hex des octets décodés du nonce (16 octets)."""
    return hashlib.sha256(bytes.fromhex(nonce)).hexdigest()


def comparison_code(fp_requester: str, fp_admitter: str, nonce_requester: str, nonce_admitter: str) -> str:
    """Code à 6 chiffres : SHA-256("hubportal-sas" ‖ fp_dem ‖ fp_adm ‖ n_dem ‖ n_adm) mod 10^6."""
    digest = hashlib.sha256(
        b"hubportal-sas"
        + bytes.fromhex(fp_requester)
        + bytes.fromhex(fp_admitter)
        + bytes.fromhex(nonce_requester)
        + bytes.fromhex(nonce_admitter)
    ).digest()
    return f"{int.from_bytes(digest, 'big') % 1_000_000:06d}"


def cert_fingerprint(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def build_envelope(
    key: bytes | None, t: str, sender: str, seq: int, epoch: int, p: dict[str, Any], ts: int | None = None
) -> dict[str, Any]:
    ts = int(time.time()) if ts is None else ts
    mac = message_mac(key, t, sender, seq, ts, epoch, p) if key else ""
    return {"t": t, "from": sender, "seq": seq, "ts": ts, "epoch": epoch, "p": p, "mac": mac}


class EnvelopeGuard:
    """Valide les enveloppes reçues d'un pair : émetteur, fenêtre ts, mac, seq strictement croissant."""

    def __init__(self, key: bytes, peer_id: str, clock: Callable[[], float] = time.time) -> None:
        self._key = key
        self.peer_id = peer_id
        self._clock = clock
        self.last_seq = -1

    def check(self, env: Any) -> dict[str, Any]:
        if not isinstance(env, dict):
            raise MeshRejected("format")
        t, sender, seq, ts, epoch, p, mac = (env.get(k) for k in ("t", "from", "seq", "ts", "epoch", "p", "mac"))
        ints = all(type(v) is int for v in (seq, ts, epoch))
        if not (
            isinstance(t, str) and isinstance(sender, str) and ints and isinstance(p, dict) and isinstance(mac, str)
        ):
            raise MeshRejected("format")
        if sender != self.peer_id:
            raise MeshRejected("sender")
        if abs(self._clock() - ts) > MESH_TS_WINDOW:
            raise MeshRejected("ts")
        # Le hello n'est pas signé (mac vide) : son champ `auth` lie clé, nonces et empreintes.
        if not (t == "hello" and mac == ""):
            expected = message_mac(self._key, t, sender, seq, ts, epoch, p)
            if not hmac.compare_digest(expected, mac):
                raise MeshRejected("mac")
        if seq <= self.last_seq:
            raise MeshRejected("seq")
        self.last_seq = seq
        return env


# ---------------------------------------------------------------- identité et TLS


def generate_identity() -> dict[str, str]:
    """Génère l'identité de HA : portal_id et certificat client auto-signé (bloquant, pour un executor)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    portal_id = str(uuid.uuid4())
    private = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, portal_id)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .sign(private, hashes.SHA256())
    )
    return {
        "portal_id": portal_id,
        "cert_pem": cert.public_bytes(serialization.Encoding.PEM).decode(),
        "key_pem": private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        "fp": cert_fingerprint(cert.public_bytes(serialization.Encoding.DER)),
    }


def build_ssl_context(cert_pem: str, key_pem: str) -> ssl.SSLContext:
    """Contexte client sans vérification d'autorité (l'empreinte est épinglée ensuite). Bloquant."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with tempfile.TemporaryDirectory() as directory:
        cert_path = os.path.join(directory, "c.pem")
        key_path = os.path.join(directory, "k.pem")
        for path, content in ((cert_path, cert_pem), (key_path, key_pem)):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(content)
        context.load_cert_chain(cert_path, key_path)
    return context


def peer_fingerprint(ws: aiohttp.ClientWebSocketResponse) -> str | None:
    """Empreinte SHA-256 du certificat présenté par le Portal."""
    transport = getattr(getattr(ws, "_writer", None), "transport", None)
    if transport is None:
        connection = getattr(getattr(ws, "_response", None), "connection", None)
        transport = getattr(connection, "transport", None)
    ssl_object = transport.get_extra_info("ssl_object") if transport is not None else None
    der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
    return cert_fingerprint(der) if der else None


def _url(host: str, port: int) -> str:
    bracket = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"wss://{bracket}:{port}{MESH_PATH}"


async def open_socket(
    session: aiohttp.ClientSession, host: str, port: int, context: ssl.SSLContext
) -> tuple[aiohttp.ClientWebSocketResponse, str]:
    async with asyncio.timeout(MESH_CONNECT_TIMEOUT):
        ws = await session.ws_connect(_url(host, port), ssl=context, heartbeat=15, max_msg_size=1_048_576)
    fingerprint = peer_fingerprint(ws)
    if fingerprint is None:
        await ws.close()
        raise MeshRejected("tls")
    return ws, fingerprint


async def _next_text(ws: aiohttp.ClientWebSocketResponse) -> str | None:
    msg = await ws.receive()
    if msg.type in (
        aiohttp.WSMsgType.CLOSE,
        aiohttp.WSMsgType.CLOSING,
        aiohttp.WSMsgType.CLOSED,
        aiohttp.WSMsgType.ERROR,
    ):
        raise MeshClosed
    return msg.data if msg.type is aiohttp.WSMsgType.TEXT else None


# ---------------------------------------------------------------- admission


class MeshAdmission:
    """Admission de HA : join_request, code de comparaison, attente de join_accept."""

    def __init__(
        self, session: aiohttp.ClientSession, host: str, port: int, identity: dict[str, str], name: str
    ) -> None:
        self._session = session
        self.host = host
        self.port = port
        self._identity = identity
        self._name = name
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self.portal_fp: str | None = None
        self.code: str | None = None
        self.started = time.monotonic()
        self.admitter_id: str | None = None

    async def _send(self, t: str, p: dict[str, Any]) -> None:
        assert self._ws is not None
        envelope = build_envelope(None, t, self._identity["portal_id"], int(time.time() * 1000), 0, p)
        await self._ws.send_str(json.dumps(envelope))

    async def _expect(self, wanted: str) -> dict[str, Any]:
        """Attend un message d'admission (non signé : la clé n'est pas encore connue)."""
        assert self._ws is not None
        async with asyncio.timeout(MESH_HANDSHAKE_TIMEOUT):
            while True:
                try:
                    raw = await _next_text(self._ws)
                except MeshClosed as err:
                    raise AdmissionRefused from err
                try:
                    env = json.loads(raw) if raw is not None else None
                except ValueError:
                    continue
                if not isinstance(env, dict) or not isinstance(env.get("p"), dict):
                    continue
                if env.get("t") == "join_reject":
                    raise AdmissionRefused
                if env.get("t") == wanted:
                    if isinstance(env.get("from"), str):
                        self.admitter_id = env["from"]
                    return env["p"]

    async def async_connect(self, context: ssl.SSLContext) -> str:
        """Engagement puis révélation des nonces (commit-reveal) ; retourne le code à comparer.

        HA envoie join_commit {c = H(nHA)}, attend join_ack {c = H(nA)}, révèle nHA (join_request),
        reçoit nA (join_reveal) et vérifie son engagement avant de calculer le code.
        """
        self._ws, self.portal_fp = await open_socket(self._session, self.host, self.port, context)
        self.started = time.monotonic()
        # L'admetteur identifie le pair par son hello (auth vide : la clé n'est pas encore connue).
        await self._send(
            "hello",
            {"proto": MESH_PROTO, "nonce": secrets.token_hex(16), "fp": self._identity["fp"], "auth": ""},
        )
        nonce = secrets.token_hex(16)
        await self._send("join_commit", {"c": nonce_commit(nonce)})
        commit = (await self._expect("join_ack")).get("c")
        await self._send("join_request", {"nonce": nonce, "name": self._name, "kind": "ha"})
        revealed = (await self._expect("join_reveal")).get("nonce")
        try:
            valid = isinstance(commit, str) and isinstance(revealed, str) and len(revealed) == 32
            valid = valid and hmac.compare_digest(nonce_commit(revealed), commit)
        except ValueError:
            valid = False
        if not valid:
            await self.async_close()
            raise MeshRejected("commit")
        self.code = comparison_code(self._identity["fp"], self.portal_fp, nonce, revealed)
        return self.code

    async def async_confirm(self) -> None:
        """L'utilisateur a confirmé le code dans HA : sans ce message l'admetteur n'envoie pas join_accept."""
        await self._send("join_confirm", {})

    async def async_wait_accept(self, timeout: float) -> dict[str, Any]:
        """Attend join_accept ; retourne network_id, key, epoch, members, admitter_id."""
        assert self._ws is not None and self.portal_fp is not None
        try:
            async with asyncio.timeout(timeout):
                while True:
                    try:
                        raw = await _next_text(self._ws)
                    except MeshClosed as err:
                        raise AdmissionRefused from err
                    if raw is None:
                        continue
                    try:
                        env = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(env, dict) and env.get("t") == "join_reject":
                        raise AdmissionRefused
                    if isinstance(env, dict) and env.get("t") == "join_accept":
                        return self._parse_accept(env)
        finally:
            await self.async_close()

    def _parse_accept(self, env: dict[str, Any]) -> dict[str, Any]:
        p = env.get("p")
        if not isinstance(p, dict) or not isinstance(env.get("from"), str):
            raise MeshRejected("format")
        key_hex, network_id, members = p.get("key"), p.get("network_id"), p.get("members")
        if not (isinstance(key_hex, str) and isinstance(network_id, str) and isinstance(members, list)):
            raise MeshRejected("format")
        try:
            key = bytes.fromhex(key_hex)
        except ValueError as err:
            raise MeshRejected("format") from err
        epoch, seq, ts = env.get("epoch"), env.get("seq"), env.get("ts")
        if type(epoch) is not int or type(seq) is not int or type(ts) is not int:
            raise MeshRejected("format")
        if abs(time.time() - ts) > MESH_TS_WINDOW:
            raise MeshRejected("ts")
        expected = message_mac(key, "join_accept", env["from"], seq, ts, epoch, p)
        if not hmac.compare_digest(expected, str(env.get("mac"))):
            raise MeshRejected("mac")
        admitter = env["from"]
        result: dict[str, dict[str, Any]] = {}
        if self.admitter_id is not None and env["from"] != self.admitter_id:
            raise MeshRejected("sender")  # join_accept d'un autre pair que celui qui a mené l'admission
        own_id, own_fp = self._identity["portal_id"], self._identity["fp"]
        for item in members:
            if not isinstance(item, dict):
                raise MeshRejected("format")
            if item.get("portal_id") == own_id or item.get("fp") == own_fp:
                # Nos données dans la liste doivent être cohérentes avec notre demande : HA, jamais « portal ».
                if not (item.get("ha") is True and item.get("portal_id") == own_id and item.get("fp") == own_fp):
                    raise MeshRejected("identity")
                continue
            if item.get("ha"):
                continue  # les autres HA ne sont pas des Portals
            if isinstance(item.get("portal_id"), str) and isinstance(item.get("fp"), str):
                result[item["portal_id"]] = {"fp": item["fp"], "name": str(item.get("name") or item["portal_id"][:8])}
        if admitter in result and result[admitter]["fp"] != self.portal_fp:
            # L'empreinte annoncée pour l'admetteur diffère de celle vue en TLS : relais probable.
            raise PinMismatch
        entry = result.setdefault(admitter, {"fp": self.portal_fp, "name": admitter[:8]})
        entry["host"], entry["port"] = self.host, self.port
        result.pop(self._identity["portal_id"], None)
        return {"network_id": network_id, "key": key_hex, "epoch": epoch, "members": result, "admitter_id": admitter}

    async def async_close(self) -> None:
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()


# ---------------------------------------------------------------- client membre


class MeshClient:
    """Connexions vers chaque Portal membre, suivi du maître et de l'epoch, commandes."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        network_id: str,
        network_key: str,
        identity: dict[str, str],
        members: dict[str, dict[str, Any]],
        session: aiohttp.ClientSession,
        ssl_context: ssl.SSLContext,
        epoch: int = 0,
        lease: float = MESH_LEASE_SECONDS,
        backoff_base: float = MESH_BACKOFF_BASE,
        backoff_cap: float = MESH_BACKOFF_CAP,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Any] = asyncio.sleep,
        on_key_rotated: Callable[[str, int], None] | None = None,
        on_members: Callable[[dict[str, dict[str, Any]]], None] | None = None,
    ) -> None:
        self.hass = hass
        self.network_id = network_id
        self._key = bytes.fromhex(network_key)
        self._identity = identity
        self.members = members
        self._session = session
        self._ssl = ssl_context
        self.epoch = epoch
        self.leader: str | None = None
        self.index = 0
        self._lease = lease
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._rng = rng
        self._clock = clock
        self._sleep = sleep
        self._on_key_rotated = on_key_rotated
        self._on_members = on_members
        self._last_heartbeat = float("-inf")
        self._expiry: asyncio.TimerHandle | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._links: dict[str, aiohttp.ClientWebSocketResponse] = {}
        self._states: dict[str, dict[str, Any]] = {}
        self._last_seen: dict[str, datetime] = {}
        self._listeners: list[Callable[[], None]] = []
        self._seq = 0
        self._stopping = False
        self._established: set[str] = set()
        self.rejected: dict[str, int] = {}
        self.alerts: set[str] = set()
        self.sent_types: list[str] = []

    def __repr__(self) -> str:
        return f"MeshClient(network_id={self.network_id!r}, members={len(self.members)})"

    # -- état

    @callback
    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    @property
    def master(self) -> str | None:
        """Maître courant tant que son bail (mesuré à la réception) n'est pas expiré."""
        if self.leader and self.hass.loop.time() - self._last_heartbeat <= self._lease:
            return self.leader
        return None

    def master_name(self) -> str | None:
        leader = self.master
        return self.member_name(leader) if leader else None

    def member_name(self, portal_id: str) -> str:
        return str(self.members.get(portal_id, {}).get("name") or portal_id[:8])

    def is_connected(self, portal_id: str) -> bool:
        ws = self._links.get(portal_id)
        return ws is not None and not ws.closed

    def portal_data(self, portal_id: str) -> dict[str, Any]:
        data: dict[str, Any] = {key: None for key in STATE_KEYS}
        data.update(self._states.get(portal_id, {}))
        data["last_seen"] = self._last_seen.get(portal_id)
        return data

    # -- cycle de vie

    def async_start(self, create_task: Callable[..., asyncio.Task] | None = None) -> None:
        self._stopping = False
        for portal_id in self.members:
            self._start_link(portal_id, create_task)

    def _start_link(self, portal_id: str, create_task: Callable[..., asyncio.Task] | None = None) -> None:
        if portal_id in self._tasks and not self._tasks[portal_id].done():
            return
        coro = self._run_link(portal_id)
        self._tasks[portal_id] = (
            create_task(coro, name=f"hubportal mesh {portal_id[:8]}")
            if create_task
            else self.hass.async_create_background_task(coro, name=f"hubportal mesh {portal_id[:8]}")
        )

    async def async_stop(self) -> None:
        """Ferme les connexions. Rien n'est envoyé : l'arrêt de HA n'a aucun effet sur le maillage."""
        self._stopping = True
        if self._expiry:
            self._expiry.cancel()
        for task in self._tasks.values():
            task.cancel()
        for ws in list(self._links.values()):
            await ws.close()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._links.clear()

    # -- boucle de connexion

    async def _run_link(self, portal_id: str) -> None:
        attempt = 0
        while not self._stopping:
            try:
                await self._session_once(portal_id)
            except asyncio.CancelledError:
                raise
            except PinMismatch:
                self._alert(portal_id, "fingerprint")
            except MeshRejected as err:
                self._count_reject(portal_id, err.reason)
            except (MeshClosed, aiohttp.ClientError, TimeoutError, OSError, ssl.SSLError):
                _LOGGER.debug("Connexion maillage perdue avec %s", portal_id)
            finally:
                self._links.pop(portal_id, None)
                if portal_id in self._established:
                    self._established.discard(portal_id)
                    attempt = 0
                self._notify()
            delay = self._rng() * min(self._backoff_cap, self._backoff_base * 2**attempt)
            attempt += 1
            await self._sleep(delay)

    async def _session_once(self, portal_id: str) -> None:
        member = self.members[portal_id]
        host = member.get("host")
        if not host:
            raise MeshClosed
        ws, fingerprint = await open_socket(self._session, host, int(member.get("port", MESH_PORT)), self._ssl)
        try:
            if fingerprint != member["fp"]:
                raise PinMismatch
            guard = EnvelopeGuard(self._key, portal_id, self._clock)
            await self._handshake(ws, guard, portal_id, fingerprint)
            self._links[portal_id] = ws
            self._established.add(portal_id)
            self._notify()
            while True:
                raw = await _next_text(ws)
                if raw is None:
                    continue
                env = self._parse(raw, guard, portal_id)
                if env is not None:
                    self._dispatch(portal_id, env)
        finally:
            await ws.close()

    def _parse(self, raw: str, guard: EnvelopeGuard, portal_id: str) -> dict[str, Any] | None:
        try:
            return guard.check(json.loads(raw))
        except ValueError:
            self._count_reject(portal_id, "format")
        except MeshRejected as err:
            self._count_reject(portal_id, err.reason)
        return None

    async def _handshake(self, ws, guard: EnvelopeGuard, portal_id: str, peer_fp: str) -> None:
        my_fp = self._identity["fp"]
        my_nonce = secrets.token_hex(16)
        await self._send(ws, "hello", {"proto": MESH_PROTO, "nonce": my_nonce, "fp": my_fp, "auth": ""})
        sent_auth = peer_ok = False
        async with asyncio.timeout(MESH_HANDSHAKE_TIMEOUT):
            while not (sent_auth and peer_ok):
                raw = await _next_text(ws)
                env = self._parse(raw, guard, portal_id) if raw is not None else None
                if env is None or env["t"] != "hello":
                    continue
                p = env["p"]
                if p.get("proto") != MESH_PROTO:
                    raise MeshRejected("proto")
                if p.get("fp") != peer_fp:
                    raise PinMismatch
                auth = p.get("auth") or ""
                if not auth:
                    nonce = p.get("nonce")
                    if not isinstance(nonce, str):
                        raise MeshRejected("format")
                    reply = hello_auth(self._key, nonce, self._identity["portal_id"], my_fp, peer_fp)
                    await self._send(ws, "hello", {"proto": MESH_PROTO, "nonce": my_nonce, "fp": my_fp, "auth": reply})
                    sent_auth = True
                else:
                    expected = hello_auth(self._key, my_nonce, portal_id, peer_fp, my_fp)
                    if not hmac.compare_digest(expected, str(auth)):
                        raise MeshRejected("auth")
                    peer_ok = True

    # -- réception

    def _dispatch(self, portal_id: str, env: dict[str, Any]) -> None:
        if env["t"] == "heartbeat":
            self._on_heartbeat(portal_id, env)
        elif env["t"] == "state":
            self._on_state(portal_id, env["p"])
        elif env["t"] == "members":
            self._on_members_message(portal_id, env)
        elif env["t"] == "command":
            self._on_command(portal_id, env)

    def _on_command(self, sender: str, env: dict[str, Any]) -> None:
        """Seule commande reçue par HA : mesh.rotate_key, du maître courant, signée avec l'ancienne clé."""
        p = env["p"]
        if p.get("cmd") != "mesh.rotate_key" or p.get("target") != self._identity["portal_id"]:
            return
        if sender != self.master:
            self._count_reject(sender, "not_master")
            return
        data = p.get("data")
        key_hex = data.get("key") if isinstance(data, dict) else None
        epoch = data.get("epoch") if isinstance(data, dict) else None
        try:
            new_key = bytes.fromhex(key_hex) if isinstance(key_hex, str) else b""
        except ValueError:
            new_key = b""
        if not new_key or type(epoch) is not int or epoch < self.epoch:
            self._count_reject(sender, "format")
            return
        self._key = new_key
        self.epoch = epoch
        self._last_heartbeat = float("-inf")  # le prochain heartbeat (nouvelle clé) redonne le maître
        if self._on_key_rotated:
            self._on_key_rotated(key_hex, epoch)
        # Rouvrir toutes les sessions : les gardes et le hello utilisent la nouvelle clé.
        for ws in list(self._links.values()):
            self.hass.async_create_background_task(ws.close(), name="hubportal mesh rotate")
        self._notify()

    def _on_members_message(self, sender: str, env: dict[str, Any]) -> None:
        """Liste des membres diffusée par le maître : lecture seule, HA ne vote pas."""
        p = env["p"]
        if sender != self.master or type(p.get("epoch")) is not int or p["epoch"] < self.epoch:
            self._count_reject(sender, "not_master")
            return
        items = p.get("members")
        if not isinstance(items, list):
            self._count_reject(sender, "format")
            return
        updated: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict) or item.get("ha"):
                continue
            pid, fp = item.get("portal_id"), item.get("fp")
            if not isinstance(pid, str) or not isinstance(fp, str) or pid == self._identity["portal_id"]:
                continue
            known = self.members.get(pid, {})
            # L'empreinte déjà épinglée n'est jamais remplacée par une liste reçue.
            updated[pid] = {
                **known,
                "fp": known.get("fp", fp),
                "name": str(item.get("name") or known.get("name") or pid[:8]),
                "priority": item.get("priority", known.get("priority")),
            }
        if updated != self.members and self._on_members:
            self._on_members(updated)

    def _on_state(self, portal_id: str, p: dict[str, Any]) -> None:
        current = self._states.setdefault(portal_id, {})
        for key in STATE_KEYS:
            if key in p and (p[key] is None or isinstance(p[key], str | int | float | bool)):
                current[key] = p[key]
        self._last_seen[portal_id] = datetime.now(UTC)
        self._notify()

    def _on_heartbeat(self, sender: str, env: dict[str, Any]) -> None:
        p = env["p"]
        leader, index, epoch = p.get("leader"), p.get("index", 0), env["epoch"]
        if leader != sender or not isinstance(leader, str):
            return
        if epoch < self.epoch:
            return  # ancien maître revenu : ignoré, il se soumettra de son côté
        if epoch == self.epoch and self.master not in (None, leader):
            return  # un seul maître par epoch tant que le bail courant est valide
        changed = (leader, epoch) != (self.master, self.epoch)
        self.epoch, self.leader = epoch, leader
        self.index = index if type(index) is int else 0
        self._last_heartbeat = self.hass.loop.time()
        if self._expiry:
            self._expiry.cancel()
        self._expiry = self.hass.loop.call_later(self._lease + 0.05, self._notify)
        if changed:
            self._notify()

    def _count_reject(self, portal_id: str, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1
        _LOGGER.debug("Message maillage rejeté (%s) pour %s", reason, portal_id)

    def _alert(self, portal_id: str, reason: str) -> None:
        self._count_reject(portal_id, reason)
        if portal_id not in self.alerts:  # clone : même portal_id, autre empreinte
            self.alerts.add(portal_id)
            _LOGGER.warning("Identité maillage refusée pour le Portal %s : %s", portal_id, reason)
            self.hass.bus.async_fire(MESH_ALERT_EVENT, {"portal_id": portal_id, "reason": "clone"})

    # -- émission

    def _next_seq(self) -> int:
        self._seq = max(self._seq + 1, int(time.time() * 1000))
        return self._seq

    async def _send(self, ws, t: str, p: dict[str, Any]) -> None:
        """Envoie une enveloppe signée avec l'epoch observée (HA ne la réécrit jamais)."""
        envelope = build_envelope(
            None if t == "hello" else self._key,  # hello non signé : `auth` fait foi
            t,
            self._identity["portal_id"],
            self._next_seq(),
            self.epoch,
            p,
            int(self._clock()),
        )
        self.sent_types.append(t)
        await asyncio.wait_for(ws.send_str(json.dumps(envelope)), timeout=MESH_SEND_TIMEOUT)

    async def async_command(self, target: str, cmd: str, data: Any = None) -> None:
        """Commande ciblée (portal_id) ou de groupe ("*", routée au maître). Jamais rejouée."""
        if cmd.startswith("mesh."):
            # Les commandes de gestion du maillage (propose, remove_member, rotate_key...) ne viennent jamais de HA.
            raise HomeAssistantError("Commande de maillage interdite pour HA.")
        if cmd == "command_action_lock" and data in LOCK_OFF_VALUES:
            raise HomeAssistantError("Déverrouillage refusé : confirmez cette action depuis le Portal.")
        if target == "*":
            destination = self.master
            if destination is None:
                raise HomeAssistantError("Pas de maître : commande de groupe refusée.")
        else:
            destination = target
        ws = self._links.get(destination)
        if ws is None or ws.closed:
            name = self.member_name(destination)
            raise HomeAssistantError(f"Portal {name} injoignable : commande non envoyée.")
        payload = {"target": target, "cmd": cmd, "data": data}
        try:
            await self._send(ws, "command", payload)
        except Exception as err:
            # Ne pas recopier les détails : aucune relecture automatique d'une commande sensible.
            raise HomeAssistantError(f"Échec de transport pour {cmd}.") from err
