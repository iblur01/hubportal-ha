"""Classes de base des entités Portal."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PortalCoordinator


class HubPortalEntity(CoordinatorEntity[PortalCoordinator]):
    """Entité rattachée à l’appareil Portal configuré."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PortalCoordinator, key: str, name: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=coordinator.portal_name,
            manufacturer="Portal Hub",
            model="Portal",
        )

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success
