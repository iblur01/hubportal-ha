"""Test croisé contre le serveur d'interopérabilité Qt (`mesh_interop_server`).

Skippé sauf si HUBPORTAL_QT_INTEROP_BIN désigne l'exécutable. Le protocole minimal exact (arguments, lignes de
sortie, noms de champs) est fourni par le dépôt Qt : les noms ci-dessous suivent le message de l'orchestrateur
(`--auto-accept`, port libre en argument, ligne JSON {"ready": true, "fp", "portal_id"} sur stdout) et sont à
confirmer avec ce contrat avant la première exécution réelle.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess

import pytest
from homeassistant.core import HomeAssistant

from custom_components.hubportal import mesh

from .mesh_fake import wait_for

BIN = os.environ.get("HUBPORTAL_QT_INTEROP_BIN")

pytestmark = pytest.mark.skipif(not BIN, reason="HUBPORTAL_QT_INTEROP_BIN non défini")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def expected_lingering_timers() -> bool:
    """aiohttp réarme un minuteur de ping à la fermeture d'un WebSocket (inoffensif, voir test_mesh.py)."""
    return True


@pytest.fixture
async def qt_server(socket_enabled):
    port = _free_port()
    process = subprocess.Popen(  # noqa: S603 - binaire fourni explicitement par l'environnement de test
        [BIN, "--auto-accept", "--port", str(port)], stdout=subprocess.PIPE, text=True
    )
    try:
        line = await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, process.stdout.readline), 15)
        ready = json.loads(line)
        assert ready.get("ready") is True
        events: list[dict] = []

        def pump() -> None:
            for raw in process.stdout:
                try:
                    events.append(json.loads(raw))
                except ValueError:
                    pass

        asyncio.get_running_loop().run_in_executor(None, pump)
        yield {"port": port, "fp": ready["fp"], "portal_id": ready["portal_id"], "events": events}
    finally:
        process.terminate()
        process.wait(timeout=10)


async def test_admission_puis_session_contre_le_serveur_qt(hass: HomeAssistant, qt_server) -> None:
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    identity = await hass.async_add_executor_job(mesh.generate_identity)
    context = await hass.async_add_executor_job(mesh.build_ssl_context, identity["cert_pem"], identity["key_pem"])
    session = async_get_clientsession(hass)

    # Admission complète : commit-reveal, code, join_confirm, join_accept.
    admission = mesh.MeshAdmission(session, "127.0.0.1", qt_server["port"], identity, "HA test")
    code = await admission.async_connect(context)
    assert len(code) == 6 and admission.portal_fp == qt_server["fp"]
    await admission.async_confirm()
    accepted = await admission.async_wait_accept(15)
    assert accepted["admitter_id"] == qt_server["portal_id"]

    # Session de membre : hello en deux temps, empreinte épinglée, heartbeat, commande ciblée.
    members = accepted["members"]
    client = mesh.MeshClient(
        hass,
        network_id=accepted["network_id"],
        network_key=accepted["key"],
        identity={"portal_id": identity["portal_id"], "fp": identity["fp"]},
        members=members,
        session=session,
        ssl_context=context,
        epoch=accepted["epoch"],
    )
    client.async_start()
    try:
        await wait_for(lambda: client.is_connected(qt_server["portal_id"]), timeout=10)
        await wait_for(lambda: client.master == qt_server["portal_id"], timeout=10)
        await client.async_command(qt_server["portal_id"], "command_screen_on")
        assert not client.rejected, client.rejected
        events = qt_server["events"]
        await wait_for(lambda: any(e.get("event") == "command" for e in events), timeout=10)
        assert not [e for e in events if e.get("event") == "message_rejected"], events
    finally:
        await client.async_stop()
