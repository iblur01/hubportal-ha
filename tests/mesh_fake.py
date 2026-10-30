"""Faux Portal de maillage pour les tests réseau : serveur WebSocket TLS qui parle le format de fil v2."""

from __future__ import annotations

import asyncio
import json
import secrets
import ssl
import time
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from custom_components.hubportal.mesh import build_envelope, generate_identity, hello_auth, nonce_commit


async def wait_for(condition, timeout: float = 3.0) -> None:
    """Attend une condition ; échoue explicitement au lieu de masquer un délai."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition non atteinte avant le délai")
        await asyncio.sleep(0.02)


class FakePortal:
    def __init__(self, key: bytes, identity: dict[str, str] | None = None, epoch: int = 1) -> None:
        self.key = key
        self.identity = identity or generate_identity()
        self.portal_id = self.identity["portal_id"]
        self.fp = self.identity["fp"]
        self.epoch = epoch
        self.port = 0
        self.received: list[dict[str, Any]] = []
        self.join_requests: list[dict[str, Any]] = []
        self.commit: str | None = None
        self.commit_ok: bool | None = None
        self.confirmed = False
        self.admission_nonce = secrets.token_hex(16)
        self.reveal_override: str | None = None
        self.authed: list[web.WebSocketResponse] = []
        self._all: list[web.WebSocketResponse] = []
        self.connections = 0
        self.closed_by_peer = 0
        self._runner: web.AppRunner | None = None
        self._seq = int(time.time() * 1000)

    async def start(self, directory: Path) -> None:
        cert, key = directory / f"{self.portal_id}.crt", directory / f"{self.portal_id}.key"
        cert.write_text(self.identity["cert_pem"])
        key.write_text(self.identity["key_pem"])
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        app = web.Application()
        app.router.add_get("/mesh", self._handler)
        self._runner = web.AppRunner(app, shutdown_timeout=0.2)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0, ssl_context=context)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for ws in list(self._all):
            await ws.close()
        if self._runner:
            await self._runner.cleanup()

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def envelope(
        self,
        t: str,
        p: dict[str, Any],
        *,
        epoch: int | None = None,
        key: bytes | None = None,
        seq: int | None = None,
        ts: int | None = None,
    ) -> dict[str, Any]:
        return build_envelope(
            None if t == "hello" else (key or self.key),
            t,
            self.portal_id,
            seq or self.next_seq(),
            self.epoch if epoch is None else epoch,
            p,
            ts,
        )

    async def send(self, t: str, p: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        envelope = self.envelope(t, p, **kwargs)
        for ws in list(self.authed):
            await ws.send_str(json.dumps(envelope))
        return envelope

    async def send_raw(self, envelope: dict[str, Any]) -> None:
        for ws in list(self.authed):
            await ws.send_str(json.dumps(envelope))

    def commands(self) -> list[dict[str, Any]]:
        return [env for env in self.received if env["t"] == "command"]

    async def _handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connections += 1
        self._all.append(ws)
        nonce = secrets.token_hex(16)
        await ws.send_str(json.dumps(self.envelope("hello", {"proto": 1, "nonce": nonce, "fp": self.fp, "auth": ""})))
        async for msg in ws:
            if msg.type is not WSMsgType.TEXT:
                continue
            env = json.loads(msg.data)
            self.received.append(env)
            if env["t"] == "join_commit" and not any(e["t"] == "hello" for e in self.received[:-1]):
                continue  # comme Qt : un pair inconnu doit d'abord se présenter par un hello
            if env["t"] == "join_commit":
                self.commit = env["p"]["c"]
                self.authed.append(ws)  # permet d'envoyer join_accept / join_reject sur cette session
                ack = build_envelope(
                    None,
                    "join_ack",
                    self.portal_id,
                    self.next_seq(),
                    self.epoch,
                    {"c": nonce_commit(self.admission_nonce)},
                )
                await ws.send_str(json.dumps(ack))
            elif env["t"] == "join_confirm":
                self.confirmed = True
            elif env["t"] == "join_request":
                self.join_requests.append(env)
                self.commit_ok = nonce_commit(env["p"]["nonce"]) == self.commit
                reveal = {"nonce": self.reveal_override or self.admission_nonce}
                await ws.send_str(
                    json.dumps(build_envelope(None, "join_reveal", self.portal_id, self.next_seq(), self.epoch, reveal))
                )
            elif env["t"] == "hello":
                p = env["p"]
                if not p["auth"]:
                    auth = hello_auth(self.key, p["nonce"], self.portal_id, self.fp, p["fp"])
                    await ws.send_str(
                        json.dumps(self.envelope("hello", {"proto": 1, "nonce": nonce, "fp": self.fp, "auth": auth}))
                    )
                else:
                    expected = hello_auth(self.key, nonce, env["from"], p["fp"], self.fp)
                    if expected == p["auth"]:
                        self.authed.append(ws)
        if ws in self.authed:
            self.authed.remove(ws)
        self.closed_by_peer += 1
        return ws
