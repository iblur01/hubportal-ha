"""Commutateurs de l’écran et du verrou du Portal."""

from homeassistant.components.switch import SwitchEntity
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
            PortalSwitch(coordinator, "ecran", "Écran"),
            PortalSwitch(coordinator, "verrou_actions", "Verrou d’actions"),
        ]
    )


class PortalSwitch(HubPortalEntity, SwitchEntity):
    """État et commande d’un commutateur Portal."""

    def __init__(self, coordinator, kind: str, name: str) -> None:
        super().__init__(coordinator, kind, name)
        self.kind = kind

    @property
    def is_on(self) -> bool:
        key = "screen" if self.kind == "ecran" else "action_lock"
        return self.coordinator.data.get(key) == "on"

    async def async_turn_on(self, **kwargs) -> None:
        if self.kind == "ecran":
            await self.coordinator.async_send_command("command_screen_on")
        else:
            await self.coordinator.async_send_command("command_action_lock", "on")
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        if self.kind == "ecran":
            await self.coordinator.async_send_command("command_screen_off")
        else:
            raise HomeAssistantError("Déverrouillage refusé : utilisez la confirmation locale du Portal.")
        await self.coordinator.async_request_refresh()
