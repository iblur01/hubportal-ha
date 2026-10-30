"""Intégration des écrans Portal Hub à Home Assistant."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_EPOCH,
    CONF_HA_CERT,
    CONF_HA_FP,
    CONF_HA_ID,
    CONF_HA_KEY,
    CONF_MEMBERS,
    CONF_NETWORK_ID,
    CONF_NETWORK_KEY,
    DOMAIN,
    MESH_PORT,
    PLATFORMS,
)
from .coordinator import MeshPortalCoordinator, PortalCoordinator, get_group
from .mesh import MeshClient, build_ssl_context


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Initialise le domaine."""
    hass.data.setdefault(DOMAIN, {"groups": {}, "coordinators": {}, "timer_durations": {}, "meshes": {}})
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


def _find_mesh(hass: HomeAssistant, foyer: str):
    """Maillage visé par le champ foyer : network_id, ou « maillage » s'il n'y en a qu'un."""
    meshes = hass.data[DOMAIN].get("meshes", {})
    for runtime in meshes.values():
        if runtime["client"].network_id == foyer:
            return runtime["client"]
    if foyer == "maillage" and len(meshes) == 1:
        return next(iter(meshes.values()))["client"]
    return None


async def _async_timer_start(call) -> None:
    group = call.hass.data[DOMAIN]["groups"].get(call.data["foyer"])
    mesh = _find_mesh(call.hass, call.data["foyer"]) if group is None else None
    if mesh is not None:
        await mesh.async_command("*", "command_timer_start", call.data["seconds"])
        return
    if group is None or group.master is None or not group.master_available:
        raise HomeAssistantError(f"Aucun Portal maître pour le foyer {call.data['foyer']}.")
    await group.portals[group.master].async_send_command("command_timer_start", call.data["seconds"])


async def _async_timer_cancel(call) -> None:
    group = call.hass.data[DOMAIN]["groups"].get(call.data["foyer"])
    mesh = _find_mesh(call.hass, call.data["foyer"]) if group is None else None
    if mesh is not None:
        await mesh.async_command("*", "command_timer_cancel")
        return
    if group is None or group.master is None or not group.master_available:
        raise HomeAssistantError(f"Aucun Portal maître pour le foyer {call.data['foyer']}.")
    await group.portals[group.master].async_send_command("command_timer_cancel")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Charge un Portal (v1) ou un maillage (v2)."""
    if CONF_NETWORK_ID in entry.data:
        return await _async_setup_mesh(hass, entry)
    hass.data.setdefault(DOMAIN, {"groups": {}, "coordinators": {}, "timer_durations": {}, "meshes": {}})
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


async def _async_setup_mesh(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Démarre le client membre : une connexion et un device par portal_id."""
    hass.data.setdefault(DOMAIN, {"groups": {}, "coordinators": {}, "timer_durations": {}, "meshes": {}})
    data = entry.data
    identity = {"portal_id": data[CONF_HA_ID], "fp": data[CONF_HA_FP]}
    context = await hass.async_add_executor_job(build_ssl_context, data[CONF_HA_CERT], data[CONF_HA_KEY])
    members = {pid: dict(member) for pid, member in data[CONF_MEMBERS].items()}
    client = MeshClient(
        hass,
        network_id=data[CONF_NETWORK_ID],
        network_key=data[CONF_NETWORK_KEY],
        identity=identity,
        members=members,
        session=async_get_clientsession(hass),
        ssl_context=context,
        epoch=int(data.get(CONF_EPOCH, 0)),
        on_key_rotated=lambda key, epoch: hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_NETWORK_KEY: key, CONF_EPOCH: epoch}
        ),
        on_members=lambda updated: hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_MEMBERS: updated}
        ),
    )
    _remove_stale_devices(hass, entry, set(members))
    portals = {pid: MeshPortalCoordinator(hass, entry, client, pid) for pid in members}
    for pid in members:
        hass.data[DOMAIN]["timer_durations"].setdefault(pid, 300)

    @callback
    def dispatch() -> None:
        for portal in portals.values():
            portal.handle_mesh_update()

    hass.data[DOMAIN]["meshes"][entry.entry_id] = {"client": client, "portals": portals}
    entry.async_on_unload(client.add_listener(dispatch))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    client.async_start(lambda coro, name: entry.async_create_background_task(hass, coro, name))
    entry.async_on_unload(entry.add_update_listener(_async_mesh_updated))
    return True


def _remove_stale_devices(hass: HomeAssistant, entry: ConfigEntry, portal_ids: set[str]) -> None:
    """Retire les devices des Portals qui ne sont plus membres du maillage."""
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        ids = {identifier for domain, identifier in device.identifiers if domain == DOMAIN}
        if ids and not any(i in portal_ids or i.startswith("mesh_") for i in ids):
            registry.async_update_device(device.id, remove_config_entry_id=entry.entry_id)


async def _async_mesh_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Applique les nouvelles adresses sans couper les connexions ; recharge si les membres changent."""
    runtime = hass.data[DOMAIN]["meshes"].get(entry.entry_id)
    if runtime is None:
        return
    client = runtime["client"]
    if set(entry.data[CONF_MEMBERS]) != set(client.members):
        await hass.config_entries.async_reload(entry.entry_id)
        return
    for pid, member in entry.data[CONF_MEMBERS].items():
        client.members[pid].update(
            host=member.get("host"),
            port=member.get("port", MESH_PORT),
            name=member.get("name"),
            priority=member.get("priority"),
        )


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Recharge les entités lorsque les options changent."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Retire un Portal du groupe ou arrête le client maillage (sans effet sur le maillage)."""
    if CONF_NETWORK_ID in entry.data:
        unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        if unloaded:
            runtime = hass.data[DOMAIN]["meshes"].pop(entry.entry_id, None)
            if runtime:
                await runtime["client"].async_stop()
        return unloaded
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
