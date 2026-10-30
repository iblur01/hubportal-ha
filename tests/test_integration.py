"""Tests d’intégration avec le harnais Home Assistant."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hubportal.config_flow import discover_portals
from custom_components.hubportal.const import DOMAIN
from custom_components.hubportal.coordinator import FoyerGroup


def _entry_data(name="Salon", notify="mobile_app_portal_salon", foyer="maison", priority=10):
    return {
        "foyer": foyer,
        "portal_name": name,
        "notify_service": notify,
        "priority": priority,
        "master_timeout": 90,
        "screen_entity": f"binary_sensor.{name.lower()}_screen",
        "lock_entity": f"binary_sensor.{name.lower()}_lock",
        "brightness_entity": f"sensor.{name.lower()}_brightness",
        "timer_entity": f"sensor.{name.lower()}_timer",
        "timer_end_entity": f"sensor.{name.lower()}_timer_end",
        "timer_remaining_entity": f"sensor.{name.lower()}_timer_remaining",
    }


def _register_mobile_app_portal(hass: HomeAssistant):
    """Crée des entrées de registre semblables à celles créées par mobile_app."""
    mobile_entry = MockConfigEntry(domain="mobile_app", data={}, title="Mobile App")
    mobile_entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=mobile_entry.entry_id,
        identifiers={("mobile_app", "device-portal-01")},
        name="Portal Salon",
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
        registry.async_get_or_create(
            domain,
            "mobile_app",
            f"webhook-device-portal-01_{suffix}",
            device_id=device.id,
            suggested_object_id=f"portal_salon_{suffix}",
        )
    return device


async def _setup(hass: HomeAssistant, name="Salon", notify="mobile_app_portal_salon", priority=10, options=None):
    data = _entry_data(name, notify, priority=priority)
    values = {
        data["screen_entity"]: "on",
        data["lock_entity"]: "off",
        data["brightness_entity"]: "40",
        data["timer_entity"]: "idle",
        data["timer_end_entity"]: "",
        data["timer_remaining_entity"]: "0",
    }
    for entity_id, value in values.items():
        hass.states.async_set(entity_id, value)
    entry = MockConfigEntry(
        domain=DOMAIN, data=data, title=name, unique_id=f"maison:{name.casefold()}", options=options or {}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.mark.asyncio
async def test_entites_creees_pour_le_portal(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    entities = [state.entity_id for state in hass.states.async_all()]
    assert any(entity.startswith("switch.") and "ecran" in entity for entity in entities)
    assert any(entity.startswith("number.") and "luminosite" in entity for entity in entities)
    assert any(entity.startswith("button.") and "minuteur_demarrer" in entity for entity in entities)
    assert any(entity.startswith("sensor.") and "disponibilite" in entity for entity in entities)
    assert hass.states.get("select.hubportal_maitre") is not None
    assert hass.data[DOMAIN]["coordinators"][entry.entry_id].is_available


@pytest.mark.asyncio
async def test_commandes_notify_et_minuteur(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    calls = []

    async def notify(call):
        calls.append(call.data)

    hass.services.async_register("notify", "mobile_app_portal_salon", notify)
    coordinator = hass.data[DOMAIN]["coordinators"][entry.entry_id]
    await coordinator.async_send_command("command_screen_on")
    assert calls[-1] == {"message": "command_screen_on", "data": {"contract_version": 1}}
    await hass.services.async_call(DOMAIN, "timer_start", {"foyer": "maison", "seconds": 600}, blocking=True)
    await hass.services.async_call(DOMAIN, "timer_cancel", {"foyer": "maison"}, blocking=True)
    assert calls[-2]["message"] == "command_timer_start"
    assert calls[-2]["data"]["command"] == 600
    assert calls[-1]["message"] == "command_timer_cancel"


@pytest.mark.asyncio
async def test_verrou_empeche_action_sensible_et_deverrouillage(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN]["coordinators"][entry.entry_id]
    coordinator.data = {"action_lock": "on"}
    with pytest.raises(HomeAssistantError, match="verrou d’actions"):
        await coordinator.async_send_command("command_timer_start", 90)
    with pytest.raises(HomeAssistantError, match="confirmez"):
        await coordinator.async_send_command("command_action_lock", "off")


@pytest.mark.asyncio
async def test_erreur_transport_service_absent(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN]["coordinators"][entry.entry_id]
    with pytest.raises(HomeAssistantError, match="introuvable"):
        await coordinator.async_send_command("command_timer_cancel")


@pytest.mark.asyncio
async def test_delai_transport_remonte_sans_retry(hass: HomeAssistant) -> None:
    entry = await _setup(hass, options={"command_timeout": 1})
    coordinator = hass.data[DOMAIN]["coordinators"][entry.entry_id]
    calls = 0

    async def slow_notify(call):
        nonlocal calls
        calls += 1
        await asyncio.sleep(1.1)

    hass.services.async_register("notify", "mobile_app_portal_salon", slow_notify)
    with pytest.raises(HomeAssistantError, match="Délai dépassé"):
        await coordinator.async_send_command("command_timer_cancel")
    assert calls == 1


@pytest.mark.asyncio
async def test_minuteur_compte_et_est_termine_depuis_la_fin(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    data = entry.data
    coordinator = hass.data[DOMAIN]["coordinators"][entry.entry_id]
    hass.states.async_set(data["timer_entity"], "active")
    end = dt_util.utcnow() + timedelta(seconds=25)
    hass.states.async_set(data["timer_end_entity"], end.isoformat())
    await coordinator.async_refresh()
    assert 0 < coordinator.data["timer_remaining"] <= 25
    hass.states.async_set(data["timer_entity"], "ringing")
    hass.states.async_set(data["timer_end_entity"], (dt_util.utcnow() - timedelta(seconds=1)).isoformat())
    await coordinator.async_refresh()
    assert coordinator.data["timer_remaining"] == 0
    assert hass.states.get("sensor.salon_minuteur").state == "ringing"


def test_bascule_automatique_apres_silence() -> None:
    class FakePortal:
        def __init__(self, entry_id, name, priority, available):
            self.entry = SimpleNamespace(entry_id=entry_id)
            self.portal_name = name
            self.priority = priority
            self.is_available = available

    group = FoyerGroup("maison")
    preferred = FakePortal("a", "Salon", 1, True)
    fallback = FakePortal("b", "Cuisine", 2, True)
    group.portals = {"a": preferred, "b": fallback}
    group.recompute()
    assert group.master == "a"
    preferred.is_available = False
    group.recompute()
    assert group.master == "b"
    assert group.master_name == "Cuisine"


@pytest.mark.asyncio
async def test_decouverte_mobile_app_et_flux_automatique(hass: HomeAssistant) -> None:
    _register_mobile_app_portal(hass)
    notify_calls = []

    async def notify(call):
        notify_calls.append(call.data)

    hass.services.async_register("notify", "mobile_app_portal_salon", notify)
    discovered = discover_portals(hass)
    assert discovered["device-portal-01"]["screen_entity"] == "binary_sensor.portal_salon_screen_on"
    assert discovered["device-portal-01"]["timer_entity"] == "sensor.portal_salon_portal_timer"
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] == "form"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"foyer": "maison", "priority": 1, "master_timeout": 90},
    )
    assert result["type"] == "progress"
    pairing_request = notify_calls[0]["data"]["command"]
    assert notify_calls[0]["message"] == "command_hubportal_pairing_request"
    hass.bus.async_fire(
        "hubportal_pairing_decision",
        {
            "contract_version": 1,
            "request_id": pairing_request["request_id"],
            "device_id": "device-portal-01",
            "decision": "accepted",
        },
    )
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] == "create_entry"
    assert result["data"]["notify_service"] == "mobile_app_portal_salon"
    assert result["data"]["screen_entity"] == "binary_sensor.portal_salon_screen_on"
    assert notify_calls[-1]["message"] == "command_hubportal_pairing_resolved"


@pytest.mark.asyncio
async def test_config_flow_sans_portal_affiche_une_explication(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] == "form"  # le formulaire reste ouvert : l'adresse du maillage y est saisissable
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"foyer": "maison"})
    assert result["type"] == "abort"
    assert result["reason"] == "no_portals_found"
