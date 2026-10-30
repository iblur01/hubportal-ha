"""Diagnostics HubPortal : jamais la clé réseau ni l'identité de HA."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_HA_CERT, CONF_HA_KEY, CONF_NETWORK_KEY

TO_REDACT = {CONF_NETWORK_KEY, CONF_HA_KEY, CONF_HA_CERT}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    return {"entry": async_redact_data(dict(entry.data), TO_REDACT)}
