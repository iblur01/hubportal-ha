"""Sélecteur du Portal maître par foyer."""

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import FoyerGroup


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    if "network_id" in entry.data:
        return
    foyer_group = hass.data[DOMAIN]["groups"][entry.data["foyer"]]
    if foyer_group.selector_owner != entry.entry_id:
        return
    entity = HubPortalMasterSelect(entry, foyer_group)
    foyer_group.selector_entity = entity
    async_add_entities([entity])


class HubPortalMasterSelect(SelectEntity, RestoreEntity):
    """Une seule entité de sélection est créée par foyer configuré."""

    _attr_has_entity_name = False
    _attr_name = "HubPortal maître"

    def __init__(self, entry: ConfigEntry, foyer_group: FoyerGroup) -> None:
        self.foyer_group = foyer_group
        self._attr_unique_id = f"foyer_{entry.data['foyer']}_maitre"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.state in self.options:
            self.foyer_group.choose(last_state.state)

    @property
    def current_option(self) -> str | None:
        return self.foyer_group.master_name

    @property
    def options(self) -> list[str]:
        return self.foyer_group.options()

    async def async_select_option(self, option: str) -> None:
        self.foyer_group.choose(option)
        self.async_write_ha_state()
