"""Boutons de commande du minuteur."""

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import HubPortalEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN]["coordinators"][entry.entry_id]
    async_add_entities(
        [
            PortalTimerButton(coordinator, "demarrer", "Minuteur démarrer"),
            PortalTimerButton(coordinator, "annuler", "Minuteur annuler"),
        ]
    )


class PortalTimerButton(HubPortalEntity, ButtonEntity):
    """Démarrage ou annulation du minuteur du Portal."""

    def __init__(self, coordinator, action: str, name: str) -> None:
        super().__init__(coordinator, f"minuteur_{action}", name)
        self.action = action

    async def async_press(self) -> None:
        if self.action == "annuler":
            await self.coordinator.async_send_command("command_timer_cancel")
        else:
            seconds = self.coordinator.hass.data[DOMAIN]["timer_durations"].get(self.coordinator.entry.entry_id, 300)
            if not 1 <= seconds <= 86399:
                raise HomeAssistantError("La durée doit être comprise entre 1 et 86 399 secondes.")
            await self.coordinator.async_send_command("command_timer_start", seconds)
        await self.coordinator.async_request_refresh()
