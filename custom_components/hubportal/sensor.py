"""Capteurs d’état et de disponibilité Portal."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .entity import HubPortalEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN]["coordinators"][entry.entry_id]
    async_add_entities(
        [
            PortalSensor(coordinator, "minuteur", "Minuteur"),
            PortalSensor(coordinator, "minuteur_fin", "Minuteur fin"),
            PortalSensor(coordinator, "minuteur_restant", "Minuteur restant"),
            PortalSensor(coordinator, "disponibilite", "Disponibilité"),
        ]
    )


class PortalSensor(HubPortalEntity, SensorEntity):
    """État du minuteur ou estimation de disponibilité."""

    def __init__(self, coordinator, kind: str, name: str) -> None:
        super().__init__(coordinator, kind, name)
        self.kind = kind
        if kind == "minuteur":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["idle", "active", "ringing"]
        elif kind == "minuteur_fin":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        elif kind == "minuteur_restant":
            self._attr_native_unit_of_measurement = "s"

    @property
    def native_value(self):
        data = self.coordinator.data or {}
        if self.kind == "minuteur":
            return data.get("timer") if data.get("timer") in ("idle", "active", "ringing") else "idle"
        if self.kind == "minuteur_fin":
            return dt_util.parse_datetime(data.get("timer_end") or "")
        if self.kind == "minuteur_restant":
            try:
                return max(0, int(float(data.get("timer_remaining") or 0)))
            except (TypeError, ValueError):
                return 0
        return "connecté" if self.coordinator.is_available else "indisponible"

    @property
    def extra_state_attributes(self):
        if self.kind != "disponibilite":
            return None
        seen = self.coordinator.last_seen
        return {
            "dernier_battement": seen.isoformat() if seen else None,
            "derniere_mise_a_jour_capteur": seen.isoformat() if seen else None,
            "estimation": "fraîcheur des capteurs mobile_app; battement dédié indisponible",
        }
