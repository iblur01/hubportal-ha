"""Tests du membre HA du maillage (contrat v2) contre un faux serveur de maillage TLS."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import subprocess
import sys
import time
from functools import partial
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER, SOURCE_ZEROCONF
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hubportal import mesh
from custom_components.hubportal.const import DOMAIN

from .mesh_fake import FakePortal, wait_for

NETWORK_ID = "net-0123456789abcdef"
KEY = secrets.token_bytes(32)
KEY_HEX = KEY.hex()


@pytest.fixture(autouse=True)
def _sockets(socket_enabled):
    """Les tests réseau parlent à 127.0.0.1 uniquement."""


@pytest.fixture
def expected_lingering_timers() -> bool:
    """aiohttp réarme un minuteur de ping si une trame arrive pendant la fermeture d'un WebSocket (inoffensif)."""
    return True


@pytest.fixture
async def portals(tmp_path):
    created: list[FakePortal] = []

    async def make(epoch: int = 1, key: bytes = KEY, identity=None) -> FakePortal:
        portal = FakePortal(key, identity, epoch)
        await portal.start(tmp_path)
        created.append(portal)
        return portal

    yield make
    for portal in created:
        await portal.stop()


def _entry(hass: HomeAssistant, servers: list[FakePortal]) -> MockConfigEntry:
    identity = mesh.generate_identity()
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=NETWORK_ID,
        title="Maillage HubPortal",
        data={
            "network_id": NETWORK_ID,
            "network_key": KEY_HEX,
            "epoch": 1,
            "ha_portal_id": identity["portal_id"],
            "ha_name": "Home Assistant",
            "ha_cert_pem": identity["cert_pem"],
            "ha_key_pem": identity["key_pem"],
            "ha_fp": identity["fp"],
            "members": {
                s.portal_id: {"fp": s.fp, "name": f"Portal {i}", "host": "127.0.0.1", "port": s.port}
                for i, s in enumerate(servers)
            },
        },
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass, servers, lease: float = 0.4):
    entry = _entry(hass, servers)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = hass.data[DOMAIN]["meshes"][entry.entry_id]["client"]
    client._lease = lease
    await wait_for(lambda: all(client.is_connected(s.portal_id) for s in servers))
    return entry, client


def _entity(hass, platform, portal_id, key):
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{portal_id}_{key}")
    assert entity_id is not None, (platform, key)
    return entity_id


# ------------------------------------------------------------ format de fil


def test_mac_conforme_a_la_specification() -> None:
    """Recalcul indépendant de la formule du contrat, sans passer par mesh.message_mac."""
    p = {"target": "*", "cmd": "command_screen_on", "data": None}
    compact = json.dumps(p, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    inner = hashlib.sha256(compact.encode()).hexdigest()
    body = "|".join(["command", "abc", "7", "1700000000", "3", inner])
    expected = hmac.new(KEY, body.encode(), hashlib.sha256).hexdigest()
    assert mesh.message_mac(KEY, "command", "abc", 7, 1700000000, 3, p) == expected
    hello = hmac.new(KEY, b"hello|nn|pid|fpl|fpp", hashlib.sha256).hexdigest()
    assert mesh.hello_auth(KEY, "nn", "pid", "fpl", "fpp") == hello


def test_code_de_comparaison_six_chiffres_et_depend_des_empreintes_et_des_nonces() -> None:
    a, b, c = (hashlib.sha256(x).hexdigest() for x in (b"a", b"b", b"c"))
    n1, n2 = "00" * 16, "11" * 16
    code = mesh.comparison_code(a, b, n1, n2)
    assert len(code) == 6 and code.isdigit()
    assert code == mesh.comparison_code(a, b, n1, n2)
    assert code != mesh.comparison_code(a, c, n1, n2)  # homme du milieu : autre empreinte, autre code
    assert code != mesh.comparison_code(a, b, n1, "22" * 16)
    assert mesh.nonce_commit(n1) == hashlib.sha256(bytes(16)).hexdigest()


def test_garde_rejette_rejeu_mac_et_fenetre() -> None:
    guard = mesh.EnvelopeGuard(KEY, "p1")
    ok = mesh.build_envelope(KEY, "state", "p1", 10, 1, {"screen": "on"})
    guard.check(ok)
    for envelope, reason in (
        (ok, "seq"),
        (mesh.build_envelope(KEY, "state", "p1", 9, 1, {}), "seq"),
        (mesh.build_envelope(b"x" * 32, "state", "p1", 11, 1, {}), "mac"),
        (mesh.build_envelope(KEY, "state", "p1", 12, 1, {}, ts=int(time.time()) - 61), "ts"),
        (mesh.build_envelope(KEY, "state", "p1", 13, 1, {}, ts=int(time.time()) + 61), "ts"),
        (mesh.build_envelope(KEY, "state", "autre", 14, 1, {}), "sender"),
    ):
        with pytest.raises(mesh.MeshRejected) as err:
            guard.check(envelope)
        assert err.value.reason == reason


# ------------------------------------------------------------ entités et lecture


async def test_entites_par_portal_et_diagnostic(hass: HomeAssistant, portals) -> None:
    a, b = await portals(), await portals()
    entry, client = await _setup(hass, [a, b])
    await a.send("state", {"screen": "on", "action_lock": "off", "brightness": 40, "timer": "idle"})
    await b.send("state", {"screen": "off", "action_lock": "off", "brightness": 10, "timer": "idle"})
    await a.send("heartbeat", {"leader": a.portal_id, "index": 5}, epoch=4)
    await wait_for(lambda: client.master == a.portal_id)

    devices = dr.async_get(hass)
    for server in (a, b):
        assert devices.async_get_device_by_identifier((DOMAIN, server.portal_id), entry.entry_id) is not None
    switch_a = _entity(hass, "switch", a.portal_id, "ecran")
    switch_b = _entity(hass, "switch", b.portal_id, "ecran")
    await wait_for(lambda: hass.states.get(switch_a).state == "on" and hass.states.get(switch_b).state == "off")
    registry = er.async_get(hass)
    assert registry.async_get(switch_a).unique_id.startswith(a.portal_id)
    assert registry.async_get(_entity(hass, "number", b.portal_id, "luminosite")).unique_id.startswith(b.portal_id)
    master = hass.states.get(_entity(hass, "sensor", NETWORK_ID, "maitre"))
    epoch = hass.states.get(_entity(hass, "sensor", NETWORK_ID, "epoch"))
    assert master.state == "Portal 0" and epoch.state == "4"
    assert registry.async_get(master.entity_id).entity_category.value == "diagnostic"


async def test_cle_reseau_absente_des_etats_attributs_et_journaux(hass: HomeAssistant, portals, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    a = await portals()
    entry, client = await _setup(hass, [a])
    await a.send("state", {"screen": "on"})
    await a.send("state", {"screen": "on"}, key=b"y" * 32)  # mac invalide pour provoquer un journal de rejet
    await wait_for(lambda: client.rejected.get("mac"))
    for state in hass.states.async_all():
        assert KEY_HEX not in json.dumps(dict(state.attributes)) and KEY_HEX not in state.state
    assert KEY_HEX not in caplog.text and entry.data["ha_key_pem"] not in caplog.text
    from custom_components.hubportal.diagnostics import async_get_config_entry_diagnostics

    diagnostics = json.dumps(await async_get_config_entry_diagnostics(hass, entry))
    assert KEY_HEX not in diagnostics and "PRIVATE KEY" not in diagnostics
    assert KEY_HEX not in repr(client)


# ------------------------------------------------------------ commandes


async def test_commande_ciblee_envoyee_au_portal_designe(hass: HomeAssistant, portals) -> None:
    a, b = await portals(), await portals()
    entry, client = await _setup(hass, [a, b])
    await b.send("state", {"screen": "off", "action_lock": "off"})
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _entity(hass, "switch", b.portal_id, "ecran")}, blocking=True
    )
    await wait_for(lambda: b.commands())
    assert b.commands()[0]["p"] == {"target": b.portal_id, "cmd": "command_screen_on", "data": None}
    assert a.commands() == []
    guard = mesh.EnvelopeGuard(KEY, client._identity["portal_id"])
    guard.check(b.commands()[0])  # signée avec la clé réseau, seq/ts valides


async def test_commande_de_groupe_routee_au_maitre(hass: HomeAssistant, portals) -> None:
    a, b = await portals(), await portals()
    entry, client = await _setup(hass, [a, b])
    await b.send("heartbeat", {"leader": b.portal_id, "index": 1}, epoch=2)
    await wait_for(lambda: client.master == b.portal_id)
    await hass.services.async_call(DOMAIN, "timer_start", {"foyer": NETWORK_ID, "seconds": 90}, blocking=True)
    await wait_for(lambda: b.commands())
    assert b.commands()[0]["p"] == {"target": "*", "cmd": "command_timer_start", "data": 90}
    assert a.commands() == []
    await hass.services.async_call(DOMAIN, "timer_cancel", {"foyer": "maillage"}, blocking=True)
    await wait_for(lambda: len(b.commands()) == 2)
    assert b.commands()[1]["p"]["cmd"] == "command_timer_cancel"


async def test_commande_de_groupe_refusee_sans_maitre(hass: HomeAssistant, portals) -> None:
    a = await portals()
    entry, client = await _setup(hass, [a])
    with pytest.raises(HomeAssistantError, match="Pas de maître"):
        await hass.services.async_call(DOMAIN, "timer_start", {"foyer": NETWORK_ID, "seconds": 10}, blocking=True)
    assert a.commands() == []
    # La commande ciblée reste possible en minorité.
    await a.send("state", {"screen": "off", "action_lock": "off"})
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _entity(hass, "switch", a.portal_id, "ecran")}, blocking=True
    )
    await wait_for(lambda: a.commands())


async def test_maitre_disparu_puis_successeur_suivi(hass: HomeAssistant, portals) -> None:
    a, b = await portals(), await portals()
    entry, client = await _setup(hass, [a, b])
    await a.send("heartbeat", {"leader": a.portal_id, "index": 1}, epoch=1)
    await wait_for(lambda: client.master == a.portal_id)
    await wait_for(lambda: client.master is None)  # plus de heartbeat : bail expiré (0,4 s)
    sensor = _entity(hass, "sensor", NETWORK_ID, "maitre")
    await wait_for(lambda: hass.states.get(sensor).state == "unknown")
    with pytest.raises(HomeAssistantError, match="Pas de maître"):
        await client.async_command("*", "command_screen_on")
    await b.send("heartbeat", {"leader": b.portal_id, "index": 2}, epoch=2)
    await wait_for(lambda: client.master == b.portal_id and client.epoch == 2)
    await wait_for(lambda: hass.states.get(sensor).state == "Portal 1")
    # Ancien maître de retour avec une epoch inférieure : ignoré.
    await a.send("heartbeat", {"leader": a.portal_id, "index": 9}, epoch=1)
    await asyncio.sleep(0.1)
    assert client.master == b.portal_id and client.epoch == 2
    await client.async_command("*", "command_screen_on")
    await wait_for(lambda: b.commands())
    assert a.commands() == []


# ------------------------------------------------------------ rejets


async def test_rejeu_mac_invalide_et_ts_hors_fenetre_rejetes(hass: HomeAssistant, portals) -> None:
    a = await portals()
    entry, client = await _setup(hass, [a])
    switch = _entity(hass, "switch", a.portal_id, "ecran")
    good = await a.send("state", {"screen": "on"})
    await wait_for(lambda: hass.states.get(switch).state == "on")
    # rejeu : même enveloppe (seq identique), puis nouveau contenu avec un seq déjà vu
    await a.send_raw(good)
    await a.send("state", {"screen": "off"}, seq=good["seq"])
    await wait_for(lambda: client.rejected.get("seq") == 2)
    await a.send("state", {"screen": "off"}, key=b"z" * 32)
    await wait_for(lambda: client.rejected.get("mac") == 1)
    await a.send("state", {"screen": "off"}, ts=int(time.time()) - 120)
    await wait_for(lambda: client.rejected.get("ts") == 1)
    await asyncio.sleep(0.05)
    assert hass.states.get(switch).state == "on"
    assert client.is_connected(a.portal_id)


async def test_portal_clone_rejete(hass: HomeAssistant, portals) -> None:
    a = await portals()
    # Même portal_id, autre certificat : le fp épinglé ne correspond plus.
    clone_identity = mesh.generate_identity()
    clone_identity["portal_id"] = a.portal_id
    clone = await portals(identity=clone_identity)
    events = []
    hass.bus.async_listen("hubportal_mesh_alert", events.append)
    entry = _entry(hass, [a])
    entry.data["members"][a.portal_id]["port"] = clone.port
    assert await hass.config_entries.async_setup(entry.entry_id)
    client = hass.data[DOMAIN]["meshes"][entry.entry_id]["client"]
    await wait_for(lambda: client.alerts)
    await hass.async_block_till_done()
    assert a.portal_id in client.alerts and client.rejected["fingerprint"] >= 1
    assert not client.is_connected(a.portal_id)
    assert clone.authed == [] and clone.commands() == []
    assert events and events[0].data["reason"] == "clone"


async def test_reconnexion_backoff_full_jitter(hass: HomeAssistant, portals) -> None:
    a = await portals()
    entry = _entry(hass, [a])
    entry.data["members"][a.portal_id]["port"] = 1  # rien n'écoute : échecs de connexion
    delays: list[float] = []

    async def fake_sleep(delay):
        delays.append(delay)
        await asyncio.sleep(0.01)

    with patch("custom_components.hubportal.MeshClient", partial(mesh.MeshClient, sleep=fake_sleep)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await wait_for(lambda: len(delays) >= 4)
    # full jitter : 0 <= délai <= min(cap, base * 2**essai)
    for attempt, delay in enumerate(delays[:4]):
        assert 0 <= delay <= min(60.0, 1.0 * 2**attempt)


async def test_hello_mauvaise_cle_reseau_refuse(hass: HomeAssistant, portals) -> None:
    a = await portals(key=b"k" * 32)  # le Portal a une autre clé : son hello n'est pas authentique
    entry, client = await _setup_expect_no_link(hass, a)
    assert not client.is_connected(a.portal_id)
    assert client.rejected.get("auth", 0) >= 1  # hello non signé : `auth` incorrecte avec une autre clé


async def _setup_expect_no_link(hass, server):
    entry = _entry(hass, [server])
    assert await hass.config_entries.async_setup(entry.entry_id)
    client = hass.data[DOMAIN]["meshes"][entry.entry_id]["client"]
    await wait_for(lambda: client.rejected)
    return entry, client


# ------------------------------------------------------------ verrou, v1, arrêt de HA


async def test_deverrouillage_refuse_et_commandes_bloquees_sous_verrou(hass: HomeAssistant, portals) -> None:
    a = await portals()
    entry, client = await _setup(hass, [a])
    await a.send("state", {"screen": "on", "action_lock": "on"})
    lock = _entity(hass, "switch", a.portal_id, "verrou_actions")
    await wait_for(lambda: hass.states.get(lock).state == "on")
    with pytest.raises(HomeAssistantError, match="Déverrouillage refusé"):
        await hass.services.async_call("switch", "turn_off", {"entity_id": lock}, blocking=True)
    with pytest.raises(HomeAssistantError, match="Déverrouillage refusé"):
        await client.async_command("*", "command_action_lock", "off")
    with pytest.raises(HomeAssistantError, match="Déverrouillage refusé"):
        await client.async_command(a.portal_id, "command_action_lock", False)
    with pytest.raises(HomeAssistantError, match="verrou d’actions"):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": _entity(hass, "switch", a.portal_id, "ecran")}, blocking=True
        )
    await asyncio.sleep(0.05)
    assert a.commands() == []


async def test_commande_non_rejouee_apres_perte_de_transport(hass: HomeAssistant, portals) -> None:
    a = await portals()
    entry, client = await _setup(hass, [a])
    await a.stop()
    await wait_for(lambda: not client.is_connected(a.portal_id))
    with pytest.raises(HomeAssistantError, match="injoignable"):
        await client.async_command(a.portal_id, "command_timer_start", 30)
    assert a.commands() == []


async def test_portal_v1_reste_servi_par_notify(hass: HomeAssistant, portals) -> None:
    from .test_integration import _setup as setup_v1

    a = await portals()
    await _setup(hass, [a])
    v1 = await setup_v1(hass)
    calls = []

    async def notify(call):
        calls.append(call.data)

    hass.services.async_register("notify", "mobile_app_portal_salon", notify)
    await hass.data[DOMAIN]["coordinators"][v1.entry_id].async_send_command("command_screen_on")
    assert calls == [{"message": "command_screen_on", "data": {"contract_version": 1}}]
    assert a.commands() == []


async def test_arret_de_ha_sans_effet_sur_le_portal(hass: HomeAssistant, portals) -> None:
    """Côté HA uniquement : à l'arrêt, HA ne parle plus et n'a jamais touché à l'epoch."""
    a = await portals(epoch=7)
    entry, client = await _setup(hass, [a])
    await a.send("heartbeat", {"leader": a.portal_id, "index": 1}, epoch=7)
    await wait_for(lambda: client.master == a.portal_id)
    sent_before_stop = len(a.received)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await wait_for(lambda: a.closed_by_peer >= 1)
    await asyncio.sleep(0.2)
    assert len(a.received) == sent_before_stop and a.connections == 1  # pas de reconnexion
    assert {env["t"] for env in a.received} <= {"hello"}  # ni vote, ni élection, ni heartbeat
    assert all(env["epoch"] <= 7 for env in a.received)  # epoch jamais augmentée par HA
    assert not client._tasks


# ------------------------------------------------------------ découverte et admission


def _info(props, host="127.0.0.1", port=8447):
    return ZeroconfServiceInfo(
        ip_address=__import__("ipaddress").ip_address(host),
        ip_addresses=[__import__("ipaddress").ip_address(host)],
        port=port,
        hostname="portal.local.",
        type="_hubportal._tcp.local.",
        name="Portal._hubportal._tcp.local.",
        properties=props,
    )


async def test_decouverte_d_un_portal_maillage(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_ZEROCONF},
        data=_info({"portal_id": "p-1", "network_id": NETWORK_ID, "proto": "1", "ver": "1.2.0"}),
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "mesh_confirm"


async def test_portal_sans_proto_ou_proto_inconnu_non_maille(hass: HomeAssistant) -> None:
    for props, reason in (
        ({"portal_id": "p-1", "network_id": NETWORK_ID, "ver": "1.0"}, "not_mesh"),
        ({"portal_id": "p-1", "network_id": NETWORK_ID, "proto": "99", "ver": "9"}, "proto_unsupported"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_ZEROCONF}, data=_info(props)
        )
        assert result["type"] is FlowResultType.ABORT and result["reason"] == reason


async def _annonce(hass, portal_id, host, port):
    await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_ZEROCONF},
        data=_info({"portal_id": portal_id, "network_id": NETWORK_ID, "proto": "1"}, host=host, port=port),
    )
    await hass.async_block_till_done()


async def test_annonce_injoignable_ne_modifie_pas_un_membre(hass: HomeAssistant, portals) -> None:
    a = await portals()
    entry, client = await _setup(hass, [a])
    events = []
    hass.bus.async_listen("hubportal_mesh_announce", events.append)
    before = dict(entry.data["members"][a.portal_id])
    await _annonce(hass, a.portal_id, "127.0.0.1", 1)  # rien n'écoute
    assert entry.data["members"][a.portal_id] == before
    assert events and events[0].data["port"] == 1


async def test_changement_d_ip_repris_si_l_empreinte_est_epinglee(hass: HomeAssistant, portals) -> None:
    a = await portals()
    entry, client = await _setup(hass, [a])
    # Même identité (même certificat) sur une autre adresse : le Portal a changé d'IP.
    moved = await portals(identity=a.identity)
    await _annonce(hass, a.portal_id, "127.0.0.1", moved.port)
    assert entry.data["members"][a.portal_id]["port"] == moved.port
    assert entry.data["members"][a.portal_id]["fp"] == a.fp


async def test_annonce_sur_une_autre_empreinte_rejetee_et_signalee(hass: HomeAssistant, portals) -> None:
    a, intrus = await portals(), await portals()
    entry, client = await _setup(hass, [a])
    events = []
    hass.bus.async_listen("hubportal_mesh_announce", events.append)
    await _annonce(hass, a.portal_id, "127.0.0.1", intrus.port)
    assert entry.data["members"][a.portal_id]["port"] == a.port and entry.data["members"][a.portal_id]["fp"] == a.fp
    assert events and events[0].data["port"] == intrus.port


async def test_adresse_absente_renseignee_seulement_avec_l_empreinte_epinglee(hass: HomeAssistant, portals) -> None:
    a, b, intrus = await portals(), await portals(), await portals()
    entry, client = await _setup(hass, [a])
    members = {
        **entry.data["members"],
        "portal-b": {"fp": b.fp, "name": "B"},
        "portal-c": {"fp": "cd" * 32, "name": "C"},
    }
    hass.config_entries.async_update_entry(entry, data={**entry.data, "members": members})
    await hass.async_block_till_done()
    await _annonce(hass, "portal-b", "127.0.0.1", b.port)
    await _annonce(hass, "portal-c", "127.0.0.1", intrus.port)
    assert entry.data["members"]["portal-b"]["port"] == b.port
    assert "host" not in entry.data["members"]["portal-c"]


async def _admission_jusqu_au_code(hass, server):
    """Découverte, confirmation, commit-reveal : retourne le résultat du formulaire de code."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_ZEROCONF},
        data=_info({"portal_id": server.portal_id, "network_id": NETWORK_ID, "proto": "1"}, port=server.port),
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await wait_for(lambda: server.join_requests)
    await hass.async_block_till_done()
    return await hass.config_entries.flow.async_configure(result["flow_id"])


async def _start_admission(hass, server):
    """Comme ci-dessus puis confirmation explicite du code par l'utilisateur."""
    form = await _admission_jusqu_au_code(hass, server)
    assert form["type"] is FlowResultType.FORM and form["step_id"] == "mesh_code"
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {"codes_match": True})
    assert result["type"] is FlowResultType.SHOW_PROGRESS and result["step_id"] == "mesh_wait"
    result["code"] = form["description_placeholders"]["code"]
    return result


async def test_admission_code_affiche_et_join_accept(hass: HomeAssistant, portals, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    a = await portals()
    result = await _start_admission(hass, a)
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    code = result["code"]
    request = a.join_requests[0]
    assert request["mac"] == "" and request["p"]["kind"] == "ha" and request["from"] == flow._identity["portal_id"]
    # Le même code est calculé côté Portal à partir des deux empreintes et du nonce.
    assert a.commit_ok is True  # HA s'était engagé sur son nonce avant de le révéler
    assert code == mesh.comparison_code(flow._identity["fp"], a.fp, request["p"]["nonce"], a.admission_nonce)
    assert len(code) == 6 and code.isdigit() and KEY_HEX not in code

    other = hashlib.sha256(b"autre portal").hexdigest()
    accept_p = {
        "network_id": NETWORK_ID,
        "key": KEY_HEX,
        "epoch": 3,
        "members": [
            {"portal_id": a.portal_id, "fp": a.fp, "name": "Salon", "ha": False},
            {"portal_id": "p-autre", "fp": other, "name": "Cuisine", "ha": False},
            {"portal_id": "ha-ancien", "fp": other, "name": "HA", "ha": True},
        ],
    }
    await wait_for(lambda: a.confirmed)  # join_confirm envoyé après la confirmation du code
    await a.send("join_accept", accept_p, epoch=3)
    await wait_for(lambda: flow._mesh_result is not None)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data["network_key"] == KEY_HEX and data["network_id"] == NETWORK_ID and data["epoch"] == 3
    assert set(data["members"]) == {a.portal_id, "p-autre"}
    assert data["members"][a.portal_id]["host"] == "127.0.0.1" and data["members"][a.portal_id]["port"] == a.port
    assert (
        data["ha_fp"]
        == flow._identity["fp"]
        == hashlib.sha256(__import__("ssl").PEM_cert_to_DER_cert(data["ha_cert_pem"])).hexdigest()
    )
    assert KEY_HEX not in caplog.text
    # L'entrée créée démarre seule et se connecte au Portal admetteur avec l'empreinte épinglée à l'admission.
    client = hass.data[DOMAIN]["meshes"][result["result"].entry_id]["client"]
    await wait_for(lambda: client.is_connected(a.portal_id))
    await hass.config_entries.async_unload(result["result"].entry_id)


async def test_admission_refusee_par_le_portal(hass: HomeAssistant, portals) -> None:
    a = await portals()
    result = await _start_admission(hass, a)
    await a.send("join_reject", {"reason": "refused"})
    await wait_for(lambda: hass.config_entries.flow._progress[result["flow_id"]]._mesh_error)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "mesh_refused"


async def test_admission_rejette_relais_d_empreinte(hass: HomeAssistant, portals) -> None:
    a = await portals()
    result = await _start_admission(hass, a)
    faux = hashlib.sha256(b"intercepteur").hexdigest()
    await a.send(
        "join_accept",
        {
            "network_id": NETWORK_ID,
            "key": KEY_HEX,
            "epoch": 1,
            "members": [{"portal_id": a.portal_id, "fp": faux, "name": "x"}],
        },
    )
    await wait_for(lambda: hass.config_entries.flow._progress[result["flow_id"]]._mesh_error)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "mesh_mismatch"
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_admission_manuelle_par_adresse(hass: HomeAssistant, portals) -> None:
    """Saisie d'adresse (Docker/VLAN sans mDNS) : le formulaire utilisateur accepte mesh_host, même sans mobile_app."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    # Soumettre le formulaire v1 sans Portal mobile_app et sans adresse : abandon explicite (comportement v1 conservé).
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"foyer": "maison"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "no_portals_found"


# ------------------------------------------------------------ rotation de clé et liste des membres


async def _make_master(server, client, epoch=1):
    await server.send("heartbeat", {"leader": server.portal_id, "index": 1}, epoch=epoch)
    await wait_for(lambda: client.master == server.portal_id)


async def test_rotate_key_acceptee_du_maitre(hass: HomeAssistant, portals) -> None:
    a = await portals()
    entry, client = await _setup(hass, [a])
    await _make_master(a, client)
    new_key = secrets.token_bytes(32)
    ha_id = client._identity["portal_id"]
    await a.send("command", {"target": ha_id, "cmd": "mesh.rotate_key", "data": {"key": new_key.hex(), "epoch": 5}})
    a.key = new_key  # le Portal bascule après l'envoi, signé avec l'ancienne clé
    await wait_for(lambda: entry.data["network_key"] == new_key.hex() and entry.data["epoch"] == 5)
    assert client._key == new_key and client.epoch == 5
    await wait_for(lambda: a.connections >= 2 and client.is_connected(a.portal_id))  # sessions rouvertes
    await a.send("state", {"screen": "on"})  # lisible avec la nouvelle clé
    switch = _entity(hass, "switch", a.portal_id, "ecran")
    await wait_for(lambda: hass.states.get(switch).state == "on")


async def test_rotate_key_refusee_hors_maitre_ou_mac_invalide(hass: HomeAssistant, portals) -> None:
    a, b = await portals(), await portals()
    entry, client = await _setup(hass, [a, b])
    await _make_master(a, client)
    ha_id = client._identity["portal_id"]
    payload = {"target": ha_id, "cmd": "mesh.rotate_key", "data": {"key": "ab" * 32, "epoch": 9}}
    await b.send("command", payload)  # B n'est pas le maître
    await wait_for(lambda: client.rejected.get("not_master"))
    await a.send("command", payload, key=b"w" * 32)  # mac invalide
    await wait_for(lambda: client.rejected.get("mac"))
    await a.send("command", {**payload, "target": "autre"})  # destiné à un autre membre
    await asyncio.sleep(0.1)
    assert client._key == KEY and entry.data["network_key"] == KEY_HEX and client.epoch == 1
    assert client.is_connected(a.portal_id) and a.connections == 1


async def test_membre_absent_rattrape_la_rotation_sans_readmission(hass: HomeAssistant, portals) -> None:
    a = await portals()
    entry, client = await _setup(hass, [a])
    await wait_for(lambda: client.is_connected(a.portal_id))
    new_key = secrets.token_bytes(32)
    ha_id = client._identity["portal_id"]
    # HA absent pendant la rotation : a connaît la nouvelle clé et prouve l'ancienne (auth_prev) au retour de HA.
    a.prev_key, a.key = KEY, new_key
    for ws in list(a._all):
        await ws.close()
    await wait_for(lambda: a.connections >= 2)
    await wait_for(lambda: client.is_connected(a.portal_id) and len(a.authed) == 1)  # hello accepté via auth_prev
    assert entry.data["network_key"] == KEY_HEX  # pas encore la nouvelle clé
    # Sans maître courant, a relaie la rotation signée avec l'ancienne clé : HA la prend.
    assert client.master is None
    await a.send(
        "command",
        {"target": ha_id, "cmd": "mesh.rotate_key", "data": {"key": new_key.hex(), "epoch": 2}},
        key=KEY,
        epoch=1,
    )
    await wait_for(lambda: entry.data["network_key"] == new_key.hex() and entry.data["epoch"] == 2)
    # sessions rouvertes avec la nouvelle clé
    await wait_for(lambda: client.is_connected(a.portal_id) and len(a.authed) == 1)
    assert client.rejected.get("not_master") is None


async def test_members_ajout_puis_retrait_sans_readmission(hass: HomeAssistant, portals) -> None:
    a = await portals()
    entry, client = await _setup(hass, [a])
    await _make_master(a, client)
    nouveau = hashlib.sha256(b"portal b").hexdigest()
    liste = [
        {"portal_id": a.portal_id, "fp": a.fp, "name": "Salon", "ha": False, "priority": 0},
        {"portal_id": "portal-b", "fp": nouveau, "name": "Cuisine", "ha": False, "priority": 1},
        {"portal_id": client._identity["portal_id"], "fp": client._identity["fp"], "name": "HA", "ha": True},
    ]
    await a.send("members", {"epoch": 1, "members": liste})
    await wait_for(lambda: "portal-b" in entry.data["members"])
    await hass.async_block_till_done()
    members = entry.data["members"]
    assert members["portal-b"]["fp"] == nouveau and members["portal-b"]["name"] == "Cuisine"
    assert members[a.portal_id]["port"] == a.port  # l'adresse existante est conservée
    assert set(members) == {a.portal_id, "portal-b"}  # HA n'est pas un Portal
    devices = dr.async_get(hass)
    assert devices.async_get_device_by_identifier((DOMAIN, "portal-b"), entry.entry_id) is not None
    _entity(hass, "switch", "portal-b", "ecran")
    # Retrait : le maître (reconnecté après le rechargement) republie la liste sans B.
    client = hass.data[DOMAIN]["meshes"][entry.entry_id]["client"]
    await wait_for(lambda: client.is_connected(a.portal_id))
    await _make_master(a, client)
    await a.send("members", {"epoch": 1, "members": liste[:1]})
    await wait_for(lambda: set(entry.data["members"]) == {a.portal_id})
    await hass.async_block_till_done()
    assert devices.async_get_device_by_identifier((DOMAIN, "portal-b"), entry.entry_id) is None
    assert er.async_get(hass).async_get_entity_id("switch", DOMAIN, "portal-b_ecran") is None or (
        hass.states.get(er.async_get(hass).async_get_entity_id("switch", DOMAIN, "portal-b_ecran")) is None
    )


async def test_members_ignore_hors_maitre_et_ne_remplace_pas_l_empreinte(hass: HomeAssistant, portals) -> None:
    a, b = await portals(), await portals()
    entry, client = await _setup(hass, [a, b])
    await _make_master(a, client)
    await b.send("members", {"epoch": 1, "members": []})  # B n'est pas le maître
    await wait_for(lambda: client.rejected.get("not_master"))
    faux = hashlib.sha256(b"autre").hexdigest()
    await a.send(
        "members",
        {
            "epoch": 1,
            "members": [
                {"portal_id": a.portal_id, "fp": faux, "name": "Renommé", "ha": False},
                {"portal_id": b.portal_id, "fp": b.fp, "name": "Portal 1", "ha": False},
            ],
        },
    )
    await wait_for(lambda: entry.data["members"][a.portal_id]["name"] == "Renommé")
    assert entry.data["members"][a.portal_id]["fp"] == a.fp  # empreinte épinglée conservée
    assert set(entry.data["members"]) == {a.portal_id, b.portal_id}


async def test_admission_nonce_revele_different_de_l_engagement_rejete(hass: HomeAssistant, portals) -> None:
    a = await portals()
    a.reveal_override = "11" * 16  # l'admetteur change de nonce après avoir vu celui de HA
    result = await _admission_jusqu_au_code(hass, a)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "mesh_mismatch"
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_admission_sans_confirmation_du_code_ne_cree_rien(hass: HomeAssistant, portals) -> None:
    a = await portals()
    form = await _admission_jusqu_au_code(hass, a)
    assert form["type"] is FlowResultType.FORM and form["step_id"] == "mesh_code"
    # join_accept arrive avant toute confirmation : rien n'est consommé.
    await a.send("join_accept", {"network_id": NETWORK_ID, "key": KEY_HEX, "epoch": 1, "members": []})
    await asyncio.sleep(0.1)
    assert a.confirmed is False  # join_confirm n'est jamais envoyé avant la confirmation
    assert not hass.config_entries.async_entries(DOMAIN)
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {"codes_match": False})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "mesh_codes_differ"
    assert not hass.config_entries.async_entries(DOMAIN)


# ------------------------------------------------------------ vecteurs dorés partagés avec le dépôt Qt

VECTORS_PATH = Path(__file__).parent / "fixtures" / "mesh_vectors.json"
VECTORS = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


def test_vecteurs_json_compact() -> None:
    for case in VECTORS["json_compact"]:
        assert mesh.json_compact(case["input"]) == case["expected"]


def test_vecteurs_mac() -> None:
    key = bytes.fromhex(VECTORS["key_hex"])
    for case in VECTORS["mac"]:
        assert mesh.json_compact(case["p"]) == case["json_compact"]
        assert mesh.sha256_hex(case["json_compact"]) == case["p_sha256"]
        mac = mesh.message_mac(key, case["t"], case["from"], case["seq"], case["ts"], case["epoch"], case["p"])
        assert mac == case["mac"]


def test_vecteurs_hello_sas_et_commit() -> None:
    key = bytes.fromhex(VECTORS["key_hex"])
    for case in VECTORS["hello_auth"]:
        assert (
            mesh.hello_auth(key, case["nonce_peer"], case["portal_id"], case["fp_local"], case["fp_peer"])
            == case["expected"]
        )
    for case in VECTORS["sas"]:
        args = (case["fp_requester"], case["fp_admitter"], case["nonce_requester"], case["nonce_admitter"])
        assert mesh.comparison_code(*args) == case["expected"]
    for case in VECTORS["commit"]:
        assert mesh.nonce_commit(case["nonce"]) == case["expected"]


def test_fichier_de_vecteurs_a_jour() -> None:
    """Le fichier commité correspond à la sortie du script générateur (référence indépendante)."""
    script = Path(__file__).parents[1] / "scripts" / "gen_mesh_vectors.py"
    assert subprocess.run([sys.executable, str(script), "--check"], check=False).returncode == 0


# ------------------------------------------------------------ M7, mesh.* interdit, groupe au maître valide


async def _accept_avec(hass, a, mutate):
    result = await _start_admission(hass, a)
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    ha = {"portal_id": flow._identity["portal_id"], "fp": flow._identity["fp"], "name": "HA", "ha": True}
    members = [{"portal_id": a.portal_id, "fp": a.fp, "name": "Salon", "ha": False}, ha]
    p = {"network_id": NETWORK_ID, "key": KEY_HEX, "epoch": 1, "members": mutate(members, ha)}
    await wait_for(lambda: a.confirmed)
    await a.send("join_accept", p)
    return result, flow


async def test_join_accept_avec_ha_coherent_est_accepte_et_ha_se_declare_ha(hass: HomeAssistant, portals) -> None:
    a = await portals()
    result, flow = await _accept_avec(hass, a, lambda members, ha: members)
    assert a.join_requests[0]["p"]["kind"] == "ha"
    await wait_for(lambda: flow._mesh_result is not None)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    client = hass.data[DOMAIN]["meshes"][result["result"].entry_id]["client"]
    await wait_for(lambda: client.is_connected(a.portal_id))
    await hass.config_entries.async_unload(result["result"].entry_id)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda members, ha: [members[0], {**ha, "ha": False}],  # HA listé comme Portal
        lambda members, ha: [members[0], {**ha, "fp": "ef" * 32}],  # empreinte de HA incohérente
        lambda members, ha: [members[0], {**ha, "portal_id": "autre-id"}],  # notre fp sous une autre identité
        lambda members, ha: [members[0], {**ha, "fp": "ef" * 32}, {**ha, "ha": False}],
    ],
)
async def test_join_accept_incoherent_avec_notre_demande_rejete(hass: HomeAssistant, portals, mutate) -> None:
    a = await portals()
    result, flow = await _accept_avec(hass, a, mutate)
    await wait_for(lambda: flow._mesh_error)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "mesh_mismatch"
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_join_accept_d_un_autre_pair_que_l_admetteur_rejete(hass: HomeAssistant, portals) -> None:
    a = await portals()
    result = await _start_admission(hass, a)
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    await wait_for(lambda: a.confirmed)
    p = {"network_id": NETWORK_ID, "key": KEY_HEX, "epoch": 1, "members": []}
    await a.send_raw(mesh.build_envelope(KEY, "join_accept", "autre-portal", a.next_seq(), 1, p))
    await wait_for(lambda: flow._mesh_error)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "mesh_mismatch"


async def test_ha_n_emet_jamais_de_commande_de_gestion_du_maillage(hass: HomeAssistant, portals) -> None:
    sources = "".join(path.read_text() for path in (Path(__file__).parents[1] / "custom_components").rglob("*.py"))
    for forbidden in ("mesh.propose", "mesh.remove_member"):
        # Seules des mentions de documentation sont tolérées, jamais un envoi.
        assert f'"{forbidden}"' not in sources and f"'{forbidden}'" not in sources
    a = await portals()
    entry, client = await _setup(hass, [a])
    await _make_master(a, client)
    for cmd in ("mesh.propose", "mesh.remove_member", "mesh.rotate_key"):
        with pytest.raises(HomeAssistantError, match="interdite"):
            await client.async_command(a.portal_id, cmd, {})
        with pytest.raises(HomeAssistantError, match="interdite"):
            await client.async_command("*", cmd, {})
    await asyncio.sleep(0.05)
    assert a.commands() == []
    assert set(client.sent_types) <= {"hello", "command"}


async def test_groupe_refuse_quand_le_bail_du_maitre_a_expire_meme_connecte(hass: HomeAssistant, portals) -> None:
    a = await portals()
    entry, client = await _setup(hass, [a])
    await _make_master(a, client)
    await wait_for(lambda: client.master is None)  # bail de 0,4 s expiré, la connexion reste ouverte
    assert client.is_connected(a.portal_id)
    with pytest.raises(HomeAssistantError, match="Pas de maître"):
        await hass.services.async_call(DOMAIN, "timer_start", {"foyer": NETWORK_ID, "seconds": 10}, blocking=True)
    assert a.commands() == []
    # Un heartbeat « maître » d'un Portal qui n'est pas le leader annoncé ne crée pas de maître.
    await a.send("heartbeat", {"leader": "autre-portal", "index": 1}, epoch=3)
    await asyncio.sleep(0.1)
    assert client.master is None
