"""Capteurs d’état et de disponibilité Portal."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_NETWORK_ID, DOMAIN
from .coordinator import portal_coordinators
from .entity import HubPortalEntity
from .mesh import MeshClient


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinators = portal_coordinators(hass, entry)
    entities: list[SensorEntity] = [
        PortalSensor(coordinator, kind, name)
        for coordinator in coordinators
        for kind, name in (
            ("minuteur", "Minuteur"),
            ("minuteur_fin", "Minuteur fin"),
            ("minuteur_restant", "Minuteur restant"),
            ("disponibilite", "Disponibilité"),
        )
    ]
    if CONF_NETWORK_ID in entry.data:
        client = hass.data[DOMAIN]["meshes"][entry.entry_id]["client"]
        entities += [MeshDiagnosticSensor(client, "maitre"), MeshDiagnosticSensor(client, "epoch")]
    async_add_entities(entities)


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
            "estimation": (
                "fraîcheur des capteurs mobile_app; battement dédié indisponible"
                if self.coordinator.availability_estimated
                else "connexion maillage"
            ),
        }


class MeshDiagnosticSensor(SensorEntity):
    """Maître courant et epoch observés par HA (diagnostic, jamais de secret)."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, client: MeshClient, kind: str) -> None:
        self._client = client
        self._kind = kind
        self._attr_translation_key = f"maillage_{kind}"
        self._attr_unique_id = f"{client.network_id}_{kind}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"mesh_{client.network_id}")}, name="Maillage HubPortal", manufacturer="Portal Hub"
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._client.add_listener(self.async_write_ha_state))

    @property
    def native_value(self):
        return self._client.master_name() if self._kind == "maitre" else self._client.epoch

    @property
    def extra_state_attributes(self):
        if self._kind != "maitre":
            return None
        return {"portal_id": self._client.master}
