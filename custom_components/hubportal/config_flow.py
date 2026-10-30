"""Flux de configuration HubPortal."""

from __future__ import annotations

import re
import unicodedata

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify

from .const import (
    CONF_DEVICE_ID,
    CONF_FOYER,
    CONF_MASTER_TIMEOUT,
    CONF_NOTIFY_SERVICE,
    CONF_PORTAL_NAME,
    CONF_PRIORITY,
    DEFAULT_MASTER_TIMEOUT,
    DOMAIN,
    SOURCES,
)

PORTAL_SUFFIXES = {
    "screen_on": "screen",
    "action_lock": "action_lock",
    "brightness": "brightness",
    "portal_timer": "timer",
    "portal_timer_end": "timer_end",
    "portal_timer_remaining": "timer_remaining",
}


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")


def discover_portals(hass) -> dict[str, dict]:
    """Reconstruit automatiquement les Portals depuis le registre mobile_app."""
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    found = {}
    for device in devices.devices.values():
        mobile_ids = [identifier for domain, identifier in device.identifiers if domain == "mobile_app"]
        if not mobile_ids:
            continue
        device_id = mobile_ids[0]
        mapping = {}
        for entity in er.async_entries_for_device(entities, device.id):
            if entity.disabled_by:
                continue
            for suffix, key in PORTAL_SUFFIXES.items():
                if entity.unique_id.endswith(f"_{suffix}"):
                    mapping[SOURCES[key]] = entity.entity_id
        if any(not mapping.get(entity_id) for entity_id in SOURCES.values()):
            continue
        name = device.name_by_user or device.name or "Portal"
        service = f"mobile_app_{slugify(device.name or name)}"
        found[device_id] = {
            CONF_DEVICE_ID: device_id,
            "portal_name": name,
            "notify_service": service,
            **mapping,
        }
    return found


class HubPortalConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configurer un Portal à partir de ses entités mobile_app."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Propose les Portals déjà enregistrés par mobile_app."""
        errors = {}
        portals = discover_portals(self.hass)
        if not portals:
            return self.async_abort(reason="no_portals_found")
        if user_input is not None:
            foyer = _slug(user_input[CONF_FOYER]) or "maison"
            device_id = user_input[CONF_DEVICE_ID]
            portal = portals.get(device_id)
            if portal is None:
                return self.async_abort(reason="device_not_found")
            unique_id = f"{foyer}:{device_id}"
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()
            if not self.hass.services.has_service("notify", portal[CONF_NOTIFY_SERVICE]):
                errors[CONF_NOTIFY_SERVICE] = "service_mobile_app_requis"
            else:
                data = {
                    CONF_FOYER: foyer,
                    CONF_PORTAL_NAME: portal[CONF_PORTAL_NAME],
                    CONF_NOTIFY_SERVICE: portal[CONF_NOTIFY_SERVICE],
                    CONF_DEVICE_ID: device_id,
                    CONF_PRIORITY: user_input.get(CONF_PRIORITY, 100),
                    CONF_MASTER_TIMEOUT: user_input.get(CONF_MASTER_TIMEOUT, DEFAULT_MASTER_TIMEOUT),
                    **{key: value for key, value in portal.items() if key in SOURCES.values()},
                }
                return self.async_create_entry(title=f"{data[CONF_PORTAL_NAME]} · {foyer}", data=data)

        schema = vol.Schema(
            {
                vol.Required(CONF_FOYER, default="maison"): str,
                vol.Required(CONF_DEVICE_ID): vol.In(
                    {
                        device_id: f"{portal[CONF_PORTAL_NAME]} · {device_id[-6:]}"
                        for device_id, portal in portals.items()
                    }
                ),
                vol.Optional(CONF_PRIORITY, default=100): vol.All(int, vol.Range(min=0, max=10000)),
                vol.Optional(CONF_MASTER_TIMEOUT, default=DEFAULT_MASTER_TIMEOUT): vol.All(
                    int, vol.Range(min=15, max=3600)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HubPortalOptionsFlow(config_entry)


class HubPortalOptionsFlow(config_entries.OptionsFlow):
    """Options du délai de commande."""

    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "command_timeout", default=self.config_entry.options.get("command_timeout", 10)
                    ): vol.All(int, vol.Range(min=1, max=60))
                }
            ),
        )
