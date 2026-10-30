"""Transports de commande, séparés de l’arbitrage et des entités."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound

from .const import CONF_NOTIFY_SERVICE, DEFAULT_COMMAND_TIMEOUT


class PortalTransport(Protocol):
    """Interface des transports remplaçables de commande Portal."""

    async def async_send_command(self, message: str, command: Any = None) -> None:
        """Envoie une commande et signale les erreurs de transport."""


class MobileAppTransport:
    """Transport v1 via notify.mobile_app_<portal>."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        self.entry = entry

    async def async_send_command(self, message: str, command: Any = None) -> None:
        """Appelle notify avec un délai et un contrat versionné."""
        service = self.entry.data[CONF_NOTIFY_SERVICE]
        if not self.hass.services.has_service("notify", service):
            raise HomeAssistantError(f"Service notify.mobile_app_{service.removeprefix('mobile_app_')} introuvable.")
        service_data: dict[str, Any] = {"message": message, "data": {"contract_version": 1}}
        if command is not None:
            service_data["data"]["command"] = command
        timeout = int(self.entry.options.get("command_timeout", DEFAULT_COMMAND_TIMEOUT))
        try:
            await asyncio.wait_for(
                self.hass.services.async_call("notify", service, service_data, blocking=True), timeout=timeout
            )
        except TimeoutError as err:
            raise HomeAssistantError(f"Délai dépassé ({timeout} s) pour {message}.") from err
        except ServiceNotFound as err:
            raise HomeAssistantError(f"Service notify.{service} introuvable.") from err
        except Exception as err:
            # Ne pas recopier les détails d’exception : certains transports les remplissent de secrets.
            raise HomeAssistantError(f"Échec de transport pour {message}.") from err
