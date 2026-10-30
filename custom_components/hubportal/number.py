"""Nombres de luminosité et de durée du minuteur."""

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import portal_coordinators
from .entity import HubPortalEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinators = portal_coordinators(hass, entry)
    async_add_entities(
        [
            PortalNumber(coordinator, *spec)
            for coordinator in coordinators
            for spec in (("luminosite", "Luminosité", 0, 100, 1), ("minuteur_duree", "Minuteur durée", 1, 86399, 1))
        ]
    )


class PortalNumber(HubPortalEntity, NumberEntity):
    """Luminosité (%) ou durée saisie du minuteur (secondes)."""

    def __init__(self, coordinator, kind: str, name: str, minimum: int, maximum: int, step: int) -> None:
        super().__init__(coordinator, kind, name)
        self.kind = kind
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
        self._attr_native_step = step
        self._attr_mode = NumberMode.BOX
        self._attr_native_unit_of_measurement = "%" if kind == "luminosite" else "s"

    @property
    def native_value(self) -> float:
        if self.kind == "luminosite":
            try:
                return float(self.coordinator.data.get("brightness") or 0)
            except (TypeError, ValueError):
                return 0
        return self.coordinator.hass.data[DOMAIN]["timer_durations"].get(self.coordinator.unique_prefix, 300)

    async def async_set_native_value(self, value: float) -> None:
        bounded = int(min(self._attr_native_max_value, max(self._attr_native_min_value, value)))
        if self.kind == "minuteur_duree":
            self.coordinator.hass.data[DOMAIN]["timer_durations"][self.coordinator.unique_prefix] = bounded
            self.async_write_ha_state()
            return
        await self.coordinator.async_send_command("command_screen_brightness_level", round(bounded * 255 / 100))
        await self.coordinator.async_request_refresh()
