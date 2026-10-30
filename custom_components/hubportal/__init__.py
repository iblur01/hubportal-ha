"""Intégration des écrans Portal Hub à Home Assistant."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN, PLATFORMS
from .coordinator import PortalCoordinator, get_group


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Initialise le domaine."""
    hass.data.setdefault(DOMAIN, {"groups": {}, "coordinators": {}, "timer_durations": {}})
    hass.services.async_register(
        DOMAIN,
        "timer_start",
        _async_timer_start,
        schema=vol.Schema(
            {
                vol.Required("foyer"): str,
                vol.Required("seconds"): vol.All(int, vol.Range(min=1, max=86399)),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN, "timer_cancel", _async_timer_cancel, schema=vol.Schema({vol.Required("foyer"): str})
    )
    return True


async def _async_timer_start(call) -> None:
    group = call.hass.data[DOMAIN]["groups"].get(call.data["foyer"])
    if group is None or group.master is None or not group.master_available:
        raise HomeAssistantError(f"Aucun Portal maître pour le foyer {call.data['foyer']}.")
    await group.portals[group.master].async_send_command("command_timer_start", call.data["seconds"])


async def _async_timer_cancel(call) -> None:
    group = call.hass.data[DOMAIN]["groups"].get(call.data["foyer"])
    if group is None or group.master is None or not group.master_available:
        raise HomeAssistantError(f"Aucun Portal maître pour le foyer {call.data['foyer']}.")
    await group.portals[group.master].async_send_command("command_timer_cancel")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Charge un Portal et l’ajoute à son groupe de foyer."""
    hass.data.setdefault(DOMAIN, {"groups": {}, "coordinators": {}, "timer_durations": {}})
    hass.data[DOMAIN]["timer_durations"].setdefault(entry.entry_id, 300)
    coordinator = PortalCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data[DOMAIN]["coordinators"][entry.entry_id] = coordinator
    group = get_group(hass, entry.data["foyer"])
    group.add(coordinator)
    coordinator.async_add_listener(group.recompute)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Recharge les entités lorsque les options changent."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Retire un Portal du groupe."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = hass.data[DOMAIN]["coordinators"].pop(entry.entry_id, None)
        if coordinator:
            get_group(hass, entry.data["foyer"]).remove(coordinator)
            hass.data[DOMAIN]["timer_durations"].pop(entry.entry_id, None)
            await coordinator.async_shutdown()
        group = get_group(hass, entry.data["foyer"])
        if not group.portals:
            hass.data[DOMAIN]["groups"].pop(entry.data["foyer"], None)
    return unloaded
