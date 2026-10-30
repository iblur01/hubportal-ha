"""Lecture des entités Portal et arbitrage du Portal maître."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_MASTER_TIMEOUT,
    CONF_PRIORITY,
    DEFAULT_MASTER_TIMEOUT,
    DOMAIN,
    SOURCES,
    UPDATE_INTERVAL,
)
from .transport import MobileAppTransport, PortalTransport

_LOGGER = logging.getLogger(__name__)


class PortalCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Met en cache les capteurs mobile_app configurés pour un Portal."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.entry = entry
        self.transport: PortalTransport = MobileAppTransport(hass, entry)
        super().__init__(hass, logger=_LOGGER, name=f"HubPortal {entry.title}", update_interval=UPDATE_INTERVAL)

    async def _async_update_data(self) -> dict[str, Any]:
        result: dict[str, Any] = {"last_seen": None, "source_states": {}}
        for key, option in SOURCES.items():
            entity_id = self.entry.data.get(option)
            if not entity_id:
                result[key] = None
                continue
            state = self.hass.states.get(entity_id)
            if state is None or state.state in ("unknown", "unavailable"):
                result[key] = None
                continue
            result[key] = state.state
            result["source_states"][entity_id] = state
        updated = [state.last_updated for state in result["source_states"].values()]
        if updated:
            result["last_seen"] = max(updated)
        end_value = result.get("timer_end")
        end_at = dt_util.parse_datetime(end_value or "")
        if end_at:
            result["timer_remaining"] = max(0, int((end_at - dt_util.utcnow()).total_seconds() + 0.999))
        return result

    @property
    def portal_name(self) -> str:
        return self.entry.data["portal_name"]

    @property
    def priority(self) -> int:
        return int(self.entry.data.get(CONF_PRIORITY, 100))

    @property
    def timeout(self) -> int:
        return int(self.entry.data.get(CONF_MASTER_TIMEOUT, DEFAULT_MASTER_TIMEOUT))

    @property
    def last_seen(self) -> datetime | None:
        return self.data.get("last_seen") if self.data else None

    @property
    def is_available(self) -> bool:
        seen = self.last_seen
        return bool(seen and (dt_util.utcnow() - seen).total_seconds() <= self.timeout)

    @property
    def availability_estimated(self) -> bool:
        """Le build mobile_app ne publie pas de heartbeat dédié."""
        return True

    async def async_send_command(self, message: str, command: Any = None) -> None:
        """Appelle le service mobile_app avec délai et erreur explicite."""
        if message == "command_action_lock" and command in (False, "off"):
            raise HomeAssistantError("Déverrouillage refusé : confirmez cette action depuis le Portal.")
        if message != "command_action_lock" and self.data and self.data.get("action_lock") == "on":
            raise HomeAssistantError("Commandes refusées : le verrou d’actions du Portal est activé.")

        await self.transport.async_send_command(message, command)


@dataclass
class FoyerGroup:
    """État commun du maître pour un foyer configuré."""

    foyer: str
    portals: dict[str, PortalCoordinator] = field(default_factory=dict)
    selected: str | None = None
    master: str | None = None
    last_auto_master: str | None = None
    selector_owner: str | None = None

    def add(self, portal: PortalCoordinator) -> None:
        self.portals[portal.entry.entry_id] = portal
        if self.selector_owner is None:
            self.selector_owner = portal.entry.entry_id
        self.recompute()

    def remove(self, portal: PortalCoordinator) -> None:
        self.portals.pop(portal.entry.entry_id, None)
        if self.selector_owner == portal.entry.entry_id:
            self.selector_owner = None
        self.recompute()

    def ordered(self) -> list[PortalCoordinator]:
        def registration_order(portal: PortalCoordinator) -> tuple[int, float, str]:
            created_at = getattr(portal.entry, "created_at", None)
            registered = created_at.timestamp() if isinstance(created_at, datetime) else float("inf")
            return portal.priority, registered, portal.entry.entry_id

        return sorted(
            self.portals.values(),
            key=registration_order,
        )

    def recompute(self) -> None:
        ordered = self.ordered()
        if not ordered:
            self.master = None
            self._notify_selector()
            return
        if self.selected in self.portals and self.portals[self.selected].is_available:
            self.master = self.selected
            self._notify_selector()
            return
        available = [portal for portal in ordered if portal.is_available]
        if self.master in self.portals and self.portals[self.master].is_available:
            self._notify_selector()
            return
        self.master = (available or ordered)[0].entry.entry_id
        self.last_auto_master = self.master
        self._notify_selector()

    def _notify_selector(self) -> None:
        selector = getattr(self, "selector_entity", None)
        if selector and selector.hass is not None and selector.entity_id is not None:
            selector.async_write_ha_state()

    @property
    def master_name(self) -> str | None:
        portal = self.portals.get(self.master or "")
        return portal.portal_name if portal else None

    def options(self) -> list[str]:
        return [portal.portal_name for portal in self.ordered()]

    def choose(self, name: str) -> None:
        match = next((portal for portal in self.portals.values() if portal.portal_name == name), None)
        if match is None:
            raise HomeAssistantError(f"Portal inconnu : {name}.")
        self.selected = match.entry.entry_id
        self.master = self.selected

    def is_master(self, entry_id: str) -> bool:
        self.recompute()
        return self.master == entry_id

    @property
    def master_available(self) -> bool:
        self.recompute()
        return bool(self.master in self.portals and self.portals[self.master].is_available)


def get_group(hass: HomeAssistant, foyer: str) -> FoyerGroup:
    """Retourne le groupe en mémoire associé à un foyer."""
    root = hass.data.setdefault(DOMAIN, {"groups": {}, "coordinators": {}})
    return root["groups"].setdefault(foyer, FoyerGroup(foyer))
