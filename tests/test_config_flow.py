"""Tests du jumelage asynchrone des Portals."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hubportal import config_flow
from custom_components.hubportal.const import DOMAIN


def _ajouter_portal(hass: HomeAssistant, device_id: str, nom: str) -> str:
    """Ajoute les capteurs mobile_app attendus par la découverte."""
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id="mobile_app_test",
        identifiers={("mobile_app", device_id)},
        name=f"Portal {nom}",
        manufacturer="Portal Hub",
    )
    registry = er.async_get(hass)
    suffixes = {
        "screen_on": "binary_sensor",
        "action_lock": "binary_sensor",
        "brightness": "sensor",
        "portal_timer": "sensor",
        "portal_timer_end": "sensor",
        "portal_timer_remaining": "sensor",
    }
    for suffix, domain in suffixes.items():
        entity = registry.async_get_or_create(
            domain,
            "mobile_app",
            f"webhook-{device_id}_{suffix}",
            device_id=device.id,
            suggested_object_id=f"portal_{nom.lower()}_{suffix}",
        )
        hass.states.async_set(entity.entity_id, "idle" if suffix == "portal_timer" else "0")
    return f"mobile_app_portal_{nom.lower()}"


def _mobile_app(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain="mobile_app", data={}, title="Mobile App", entry_id="mobile_app_test")
    entry.add_to_hass(hass)


async def _demarrer_jumelage(hass: HomeAssistant, *, priority: int = 25, timeout: int = 75):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] == "form"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"foyer": "maison étage", "priority": priority, "master_timeout": timeout},
    )
    assert result["type"] == "progress"
    return result


async def _continuer_apres_progress(hass: HomeAssistant, flow_id: str):
    """Simule le retour du frontend après la notification de progression terminée."""
    await hass.async_block_till_done()
    flow = hass.config_entries.flow._progress[flow_id]
    assert flow.cur_step is not None
    assert flow.cur_step["type"] is FlowResultType.SHOW_PROGRESS_DONE
    return await hass.config_entries.flow.async_configure(flow_id)


@pytest.mark.asyncio
async def test_jumelage_accepte_cree_toutes_les_entrees_et_resout_tous_les_portals(
    hass: HomeAssistant,
) -> None:
    _mobile_app(hass)
    services = [_ajouter_portal(hass, "portal-a", "Salon"), _ajouter_portal(hass, "portal-b", "Cuisine")]
    calls: dict[str, list[dict[str, Any]]] = {service: [] for service in services}

    for service in services:
        async def notify(call, *, service=service):
            calls[service].append(call.data)

        hass.services.async_register("notify", service, notify)

    flow = await _demarrer_jumelage(hass)
    request = calls[services[0]][0]
    payload = request["data"]["command"]
    assert request["message"] == "command_hubportal_pairing_request"
    assert request["data"]["contract_version"] == 1
    assert set(payload) == {"request_id", "foyer", "expires_at"}
    assert payload["foyer"] == "maison_etage"
    assert isinstance(payload["expires_at"], (int, float))
    assert 0 < payload["expires_at"] - dt_util.as_timestamp(dt_util.utcnow()) <= 120
    assert all(calls[service][0]["data"]["command"] == payload for service in services)

    hass.bus.async_fire(
        "hubportal_pairing_decision",
        {
            "contract_version": 1,
            "request_id": payload["request_id"],
            "device_id": "portal-a",
            "decision": "accepted",
        },
    )
    result = await _continuer_apres_progress(hass, flow["flow_id"])
    assert result["type"] == "create_entry"

    entries = [
        entry for entry in hass.config_entries.async_entries(DOMAIN) if entry.data.get("foyer") == "maison_etage"
    ]
    assert len(entries) == 2
    by_device = {entry.data["mobile_device_id"]: entry for entry in entries}
    assert by_device["portal-a"].data["priority"] == 25
    assert by_device["portal-b"].data["priority"] == 26
    assert all(entry.data["master_timeout"] == 75 for entry in entries)
    for service in services:
        resolved = calls[service][-1]
        assert resolved["message"] == "command_hubportal_pairing_resolved"
        assert resolved["data"]["command"] == {
            "request_id": payload["request_id"],
            "device_id": "portal-a",
            "decision": "accepted",
        }
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []
    assert "hubportal_pairing_decision" not in hass.bus.async_listeners()
    assert flow["flow_id"]


@pytest.mark.asyncio
async def test_refus_d_un_portal_ne_ferme_pas_le_flow_et_un_autre_peut_accepter(hass: HomeAssistant) -> None:
    _mobile_app(hass)
    services = [_ajouter_portal(hass, "portal-a", "Salon"), _ajouter_portal(hass, "portal-b", "Cuisine")]
    calls = []

    async def notify(call):
        calls.append(call.data)

    for service in services:
        hass.services.async_register("notify", service, notify)
    flow = await _demarrer_jumelage(hass)
    payload = calls[0]["data"]["command"]

    hass.bus.async_fire(
        "hubportal_pairing_decision",
        {
            "contract_version": 1,
            "request_id": payload["request_id"],
            "device_id": "portal-a",
            "decision": "refused",
        },
    )
    await asyncio.sleep(0)
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    hass.bus.async_fire(
        "hubportal_pairing_decision",
        {
            "contract_version": 1,
            "request_id": payload["request_id"],
            "device_id": "portal-b",
            "decision": "accepted",
        },
    )
    result = await _continuer_apres_progress(hass, flow["flow_id"])
    assert result["type"] == "create_entry"
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 2
    assert any(entry.data["mobile_device_id"] == "portal-b" and entry.data["priority"] == 25 for entry in entries)
    assert sum(call["message"] == "command_hubportal_pairing_resolved" for call in calls) == len(services)
    assert flow["flow_id"]


@pytest.mark.asyncio
async def test_timeout_expire_et_nettoie_le_listener(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    _mobile_app(hass)
    service = _ajouter_portal(hass, "portal-a", "Salon")
    calls = []

    async def notify(call):
        calls.append(call.data)

    hass.services.async_register("notify", service, notify)
    monkeypatch.setattr(config_flow, "PAIRING_TIMEOUT", timedelta(milliseconds=5))

    flow = await _demarrer_jumelage(hass)
    calls_for_portal = calls[0]["data"]["command"]
    hass.bus.async_fire(
        "hubportal_pairing_decision",
        {
            "contract_version": 1,
            "request_id": calls_for_portal["request_id"],
            "device_id": "portal-a",
            "decision": "refused",
        },
    )
    await asyncio.sleep(0)
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    await asyncio.sleep(0.02)
    result = await _continuer_apres_progress(hass, flow["flow_id"])

    assert result["type"] == "abort"
    assert result["reason"] == "pairing_timeout"
    assert hass.config_entries.async_entries(DOMAIN) == []
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []
    assert "hubportal_pairing_decision" not in hass.bus.async_listeners()
    assert flow["flow_id"]


@pytest.mark.asyncio
async def test_annulation_ferme_le_listener_evenementiel(hass: HomeAssistant) -> None:
    _mobile_app(hass)
    service = _ajouter_portal(hass, "portal-a", "Salon")
    hass.services.async_register("notify", service, lambda call: None)
    flow = await _demarrer_jumelage(hass)
    assert hass.bus.async_listeners().get("hubportal_pairing_decision") == 1
    hass.config_entries.flow.async_abort(flow["flow_id"])
    await hass.async_block_till_done()

    assert "hubportal_pairing_decision" not in hass.bus.async_listeners()
    hass.bus.async_fire(
        "hubportal_pairing_decision",
        {"contract_version": 1, "request_id": "any", "device_id": "portal-a", "decision": "accepted"},
    )
    await hass.async_block_till_done()
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.asyncio
async def test_foyer_completement_configure_refuse_un_jumelage_inutile(hass: HomeAssistant) -> None:
    _mobile_app(hass)
    services = [_ajouter_portal(hass, "portal-a", "Salon"), _ajouter_portal(hass, "portal-b", "Cuisine")]
    for service in services:
        hass.services.async_register("notify", service, lambda call: None)
    for device_id, service, name in (
        ("portal-a", services[0], "Salon"),
        ("portal-b", services[1], "Cuisine"),
    ):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={"foyer": "maison_etage", "mobile_device_id": device_id},
            title=name,
            unique_id=f"maison_etage:{device_id}",
        )
        entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] == "form"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"foyer": "maison étage", "priority": 100, "master_timeout": 90}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"
