"""Flux de configuration HubPortal avec jumelage de foyer par acceptation locale."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
import unicodedata
from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .const import (
    CONF_DEVICE_ID,
    CONF_EPOCH,
    CONF_FOYER,
    CONF_HA_CERT,
    CONF_HA_FP,
    CONF_HA_ID,
    CONF_HA_KEY,
    CONF_HA_NAME,
    CONF_MASTER_TIMEOUT,
    CONF_MEMBERS,
    CONF_MESH_HOST,
    CONF_NETWORK_ID,
    CONF_NETWORK_KEY,
    CONF_NOTIFY_SERVICE,
    CONF_PORTAL_NAME,
    CONF_PRIORITY,
    DEFAULT_MASTER_TIMEOUT,
    DOMAIN,
    MESH_ADMISSION_TIMEOUT,
    MESH_PORT,
    MESH_PROTO,
    SOURCES,
)
from .mesh import AdmissionRefused, MeshAdmission, MeshRejected, build_ssl_context, generate_identity, open_socket

_LOGGER = logging.getLogger(__name__)
SUPPORTED_PROTO = {MESH_PROTO}

PAIRING_DECISION_EVENT = "hubportal_pairing_decision"
PAIRING_CONTRACT_VERSION = 1
PAIRING_TIMEOUT = timedelta(seconds=120)
NOTIFY_TIMEOUT = 10

PORTAL_SUFFIXES = {
    "screen_on": "screen",
    "action_lock": "action_lock",
    "brightness": "brightness",
    "portal_timer": "timer",
    "portal_timer_end": "timer_end",
    "portal_timer_remaining": "timer_remaining",
}


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")


def discover_portals(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """Reconstruit les Portals depuis les registres de mobile_app."""
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    found: dict[str, dict[str, Any]] = {}
    for mobile_entry in hass.config_entries.async_entries("mobile_app"):
        for device in dr.async_entries_for_config_entry(devices, mobile_entry.entry_id):
            mobile_ids = [identifier for domain, identifier in device.identifiers if domain == "mobile_app"]
            if not mobile_ids:
                continue
            device_id = mobile_ids[0]
            mapping = {}
            for entity in er.async_entries_for_device(entities, device.id):
                if entity.disabled_by:
                    continue
                for suffix, key in PORTAL_SUFFIXES.items():
                    if entity.unique_id.endswith(f"_{suffix}"):
                        mapping[SOURCES[key]] = entity.entity_id
            if any(not mapping.get(entity_id) for entity_id in SOURCES.values()):
                continue
            name = device.name_by_user or device.name or "Portal"
            service = f"mobile_app_{slugify(device.name or name)}"
            found[device_id] = {
                CONF_DEVICE_ID: device_id,
                CONF_PORTAL_NAME: name,
                CONF_NOTIFY_SERVICE: service,
                **{key: value for key, value in mapping.items()},
            }
    return found


def _unique_id(foyer: str, device_id: str) -> str:
    return f"{foyer}:{device_id}"


def _entry_data(foyer: str, portal: dict[str, Any], priority: int, timeout: int) -> dict[str, Any]:
    """Construit une entrée sans conserver de secret ou de jeton."""
    return {
        CONF_FOYER: foyer,
        CONF_PORTAL_NAME: portal[CONF_PORTAL_NAME],
        CONF_NOTIFY_SERVICE: portal[CONF_NOTIFY_SERVICE],
        CONF_DEVICE_ID: portal[CONF_DEVICE_ID],
        CONF_PRIORITY: priority,
        CONF_MASTER_TIMEOUT: timeout,
        **{key: value for key, value in portal.items() if key in SOURCES.values()},
    }


class HubPortalConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Jumeler tous les Portals d'un foyer après une acceptation physique."""

    VERSION = 1

    def __init__(self) -> None:
        self._portals: dict[str, dict[str, Any]] = {}
        self._foyer = ""
        self._priority = 100
        self._master_timeout = DEFAULT_MASTER_TIMEOUT
        self._request_id: str | None = None
        self._expires_at: datetime | None = None
        self._pairing_result: dict[str, str] | None = None
        # maillage (contrat v2)
        self._host = ""
        self._port = MESH_PORT
        self._portal_label = ""
        self._network_id: str | None = None
        self._identity: dict[str, str] | None = None
        self._admission: MeshAdmission | None = None
        self._mesh_task: asyncio.Task | None = None
        self._mesh_error: str | None = None
        self._mesh_code = ""
        self._mesh_confirmed = False
        self._mesh_result: dict[str, Any] | None = None

    async def async_step_user(self, user_input=None):
        """Demande le foyer et les paramètres partagés avant d'envoyer le défi."""
        errors = {}
        if user_input is not None and user_input.get(CONF_MESH_HOST, "").strip():
            self._host = user_input[CONF_MESH_HOST].strip()
            self._port = MESH_PORT
            return await self.async_step_mesh_connect()
        portals = discover_portals(self.hass)
        # Saisie v1 sans Portal mobile_app : abandon ; le formulaire reste ouvert pour l'adresse du maillage.
        if not portals and user_input is not None:
            return self.async_abort(reason="no_portals_found")

        missing_notify = any(
            not self.hass.services.has_service("notify", portal[CONF_NOTIFY_SERVICE]) for portal in portals.values()
        )
        if missing_notify and all(
            not self.hass.services.has_service("notify", portal[CONF_NOTIFY_SERVICE]) for portal in portals.values()
        ):
            return self.async_abort(reason="no_notify_services")
        if missing_notify:
            errors["base"] = "service_mobile_app_requis"

        if user_input is not None:
            foyer = _slug(user_input.get(CONF_FOYER, ""))
            if not foyer:
                errors[CONF_FOYER] = "foyer_invalide"
            elif missing_notify:
                errors["base"] = "service_mobile_app_requis"
            else:
                self._portals = portals
                self._foyer = foyer
                self._priority = user_input[CONF_PRIORITY]
                self._master_timeout = user_input[CONF_MASTER_TIMEOUT]
                configured = {
                    entry.unique_id
                    for entry in self.hass.config_entries.async_entries(DOMAIN)
                    if entry.unique_id is not None
                }
                if all(_unique_id(foyer, device_id) in configured for device_id in portals):
                    return self.async_abort(reason="already_configured")
                self._request_id = secrets.token_urlsafe(18)
                self._expires_at = dt_util.utcnow() + PAIRING_TIMEOUT
                progress_task = self.hass.async_create_task(self._async_pairing_progress())
                return self.async_show_progress(
                    step_id="pairing",
                    progress_action="waiting_for_portal",
                    description_placeholders={"portal_count": str(len(self._portals))},
                    progress_task=progress_task,
                )

        schema = vol.Schema(
            {
                vol.Optional(CONF_MESH_HOST): str,
                vol.Required(CONF_FOYER, default="maison"): str,
                vol.Required(CONF_PRIORITY, default=100): vol.All(int, vol.Range(min=0, max=10000)),
                vol.Required(CONF_MASTER_TIMEOUT, default=DEFAULT_MASTER_TIMEOUT): vol.All(
                    int, vol.Range(min=15, max=3600)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    # ------------------------------------------------------------ maillage (contrat v2)

    async def async_step_zeroconf(self, discovery_info):
        """Découverte `_hubportal._tcp` : seuls les Portals qui publient `proto` rejoignent le maillage."""
        props = discovery_info.properties
        portal_id, network_id, proto = props.get("portal_id"), props.get("network_id"), props.get("proto")
        if not portal_id or not network_id or not proto:
            return self.async_abort(reason="not_mesh")
        try:
            if int(proto) not in SUPPORTED_PROTO:
                return self.async_abort(reason="proto_unsupported")
        except ValueError:
            return self.async_abort(reason="proto_unsupported")
        port = discovery_info.port or MESH_PORT
        host = str(discovery_info.host)
        known = next((e for e in self.hass.config_entries.async_entries(DOMAIN) if e.unique_id == network_id), None)
        if known is not None:
            await self._note_announcement(known, str(portal_id), host, port)
            return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(network_id)
        self._abort_if_unique_id_configured()
        self._host, self._port, self._network_id = host, port, network_id
        self._portal_label = str(portal_id)[:8]
        self.context["title_placeholders"] = {"name": self._portal_label}
        return await self.async_step_mesh_confirm()

    async def _note_announcement(self, entry, portal_id: str, host: str, port: int) -> None:
        """Annonce mDNS d'un membre connu : la nouvelle adresse n'est retenue que si elle présente l'empreinte épinglée.

        HA essaie l'adresse annoncée; si la connexion TLS montre l'empreinte épinglée, l'adresse est enregistrée.
        Sinon l'ancienne adresse est conservée et l'annonce est signalée (journal, événement). L'empreinte n'est
        jamais modifiée par une annonce.
        """
        members = {pid: dict(m) for pid, m in entry.data.get(CONF_MEMBERS, {}).items()}
        current = members.get(portal_id)
        if current is None or (current.get("host"), current.get("port")) == (host, port):
            return
        verified = False
        try:
            context = await self.hass.async_add_executor_job(
                build_ssl_context, entry.data[CONF_HA_CERT], entry.data[CONF_HA_KEY]
            )
            ws, fingerprint = await open_socket(async_get_clientsession(self.hass), host, port, context)
            await ws.close()
            verified = fingerprint == current.get("fp")
        except Exception:  # noqa: BLE001 - adresse injoignable ou TLS invalide : annonce non retenue
            verified = False
        if verified:
            current.update(host=host, port=port)
            self.hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_MEMBERS: members})
            return
        _LOGGER.info("Annonce mDNS non vérifiée pour le Portal %s : adresse inchangée", portal_id)
        self.hass.bus.async_fire("hubportal_mesh_announce", {"portal_id": portal_id, "host": host, "port": port})

    async def async_step_mesh_confirm(self, user_input=None):
        if user_input is not None:
            return await self.async_step_mesh_connect()
        return self.async_show_form(
            step_id="mesh_confirm", description_placeholders={"name": self._portal_label or self._host}
        )

    async def _async_open_admission(self) -> None:
        try:
            self._identity = await self.hass.async_add_executor_job(generate_identity)
            context = await self.hass.async_add_executor_job(
                build_ssl_context, self._identity["cert_pem"], self._identity["key_pem"]
            )
            self._admission = MeshAdmission(
                async_get_clientsession(self.hass), self._host, self._port, self._identity, "Home Assistant"
            )
            self._mesh_code = await self._admission.async_connect(context)
        except AdmissionRefused:
            self._mesh_error = "mesh_refused"
        except MeshRejected:
            self._mesh_error = "mesh_mismatch"
        except Exception:  # noqa: BLE001 - jamais le détail d'une exception réseau
            self._mesh_error = "mesh_unreachable"

    async def async_step_mesh_connect(self, user_input=None):
        if self._mesh_task is None:
            self._mesh_task = self.hass.async_create_task(self._async_open_admission())
        if not self._mesh_task.done():
            return self.async_show_progress(
                step_id="mesh_connect", progress_action="connecting_portal", progress_task=self._mesh_task
            )
        self._mesh_task = None
        return self.async_show_progress_done(next_step_id="mesh_failed" if self._mesh_error else "mesh_code")

    async def _async_wait_accept(self) -> None:
        assert self._admission is not None
        try:
            await self._admission.async_confirm()
            remaining = MESH_ADMISSION_TIMEOUT - (time.monotonic() - self._admission.started)
            self._mesh_result = await self._admission.async_wait_accept(max(1.0, remaining))
        except AdmissionRefused:
            self._mesh_error = "mesh_refused"
        except TimeoutError:
            self._mesh_error = "mesh_timeout"
        except MeshRejected:
            self._mesh_error = "mesh_mismatch"
        except Exception:  # noqa: BLE001
            self._mesh_error = "mesh_unreachable"

    async def async_step_mesh_code(self, user_input=None):
        """Affiche le code (non secret) et exige que l'utilisateur confirme qu'il est identique à celui du Portal."""
        if user_input is not None:
            if user_input.get("codes_match") is not True:
                return self.async_abort(reason="mesh_codes_differ")
            self._mesh_confirmed = True
            return await self.async_step_mesh_wait()
        return self.async_show_form(
            step_id="mesh_code",
            data_schema=vol.Schema({vol.Required("codes_match", default=False): bool}),
            description_placeholders={"code": self._mesh_code, "portal": self._portal_label or self._host},
        )

    async def async_step_mesh_wait(self, user_input=None):
        """Attend l'acceptation sur un Portal; jamais avant la confirmation du code par l'utilisateur."""
        if not self._mesh_confirmed:
            return self.async_abort(reason="mesh_codes_differ")
        if self._mesh_task is None:
            self._mesh_task = self.hass.async_create_task(self._async_wait_accept())
        if not self._mesh_task.done():
            return self.async_show_progress(
                step_id="mesh_wait",
                progress_action="waiting_for_accept",
                description_placeholders={"code": self._mesh_code, "portal": self._portal_label or self._host},
                progress_task=self._mesh_task,
            )
        self._mesh_task = None
        return self.async_show_progress_done(next_step_id="mesh_failed" if self._mesh_error else "mesh_finish")

    async def async_step_mesh_failed(self, user_input=None):
        return self.async_abort(reason=self._mesh_error or "mesh_unreachable")

    async def async_step_mesh_finish(self, user_input=None):
        result, identity = self._mesh_result, self._identity
        if not self._mesh_confirmed:
            return self.async_abort(reason="mesh_codes_differ")
        assert result is not None and identity is not None
        if self._network_id and result["network_id"] != self._network_id:
            return self.async_abort(reason="mesh_mismatch")
        await self.async_set_unique_id(result["network_id"])
        self._abort_if_unique_id_configured()
        data = {
            CONF_NETWORK_ID: result["network_id"],
            CONF_NETWORK_KEY: result["key"],
            CONF_EPOCH: result["epoch"],
            CONF_HA_ID: identity["portal_id"],
            CONF_HA_NAME: "Home Assistant",
            CONF_HA_CERT: identity["cert_pem"],
            CONF_HA_KEY: identity["key_pem"],
            CONF_HA_FP: identity["fp"],
            CONF_MEMBERS: result["members"],
        }
        return self.async_create_entry(title="Maillage HubPortal", data=data)

    @callback
    def async_remove(self) -> None:
        if self._admission is not None:
            self.hass.async_create_task(self._admission.async_close())
        if self._mesh_task is not None:
            self._mesh_task.cancel()

    async def _async_pairing_progress(self) -> dict[str, str]:
        """Conserve le résultat du progress task avant la reprise du flow."""
        self._pairing_result = await self._async_wait_for_acceptance()
        return self._pairing_result

    async def _async_wait_for_acceptance(self) -> dict[str, str]:
        """Écoute uniquement la décision portant l'identifiant aléatoire de ce flow."""
        assert self._request_id is not None
        assert self._expires_at is not None
        loop = asyncio.get_running_loop()
        accepted: asyncio.Future[dict[str, str]] = loop.create_future()
        deadline = loop.time() + max(0.0, (self._expires_at - dt_util.utcnow()).total_seconds())

        @callback
        def handle_decision(event) -> None:
            data = event.data
            if type(data.get("contract_version")) is not int or data["contract_version"] != PAIRING_CONTRACT_VERSION:
                return
            request_id = data.get("request_id")
            decision = data.get("decision")
            if (
                not isinstance(request_id, str)
                or request_id != self._request_id
                or not isinstance(decision, str)
                or decision != "accepted"
                or dt_util.utcnow() >= self._expires_at
            ):
                # Un refus ne ferme que l'interface du Portal qui l'a émis.
                return
            device_id = data.get("device_id")
            if not isinstance(device_id, str) or device_id not in self._portals:
                return
            if not accepted.done():
                accepted.set_result({"device_id": device_id})

        remove_listener = self.hass.bus.async_listen(PAIRING_DECISION_EVENT, handle_decision)
        try:
            command = {
                "request_id": self._request_id,
                "foyer": self._foyer,
                "expires_at": dt_util.as_timestamp(self._expires_at),
            }
            service_data = {
                "message": "command_hubportal_pairing_request",
                "data": {"contract_version": PAIRING_CONTRACT_VERSION, "command": command},
            }
            delivered = 0
            for portal in self._portals.values():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(
                        self.hass.services.async_call(
                            "notify", portal[CONF_NOTIFY_SERVICE], service_data, blocking=True
                        ),
                        timeout=min(NOTIFY_TIMEOUT, remaining),
                    )
                    delivered += 1
                except TimeoutError:
                    _LOGGER.warning("Délai dépassé pendant l'envoi de la demande de jumelage à un Portal")
                except Exception:  # noqa: BLE001 - ne jamais consigner le contenu d'une exception de transport
                    _LOGGER.warning("Échec de transport pendant l'envoi d'une demande de jumelage")

            if not delivered:
                return {"status": "transport_error"}

            remaining = max(0.0, deadline - loop.time())
            try:
                async with asyncio.timeout(remaining):
                    return await accepted
            except TimeoutError:
                return {"status": "timeout"}
        finally:
            remove_listener()

    async def async_step_pairing(self, user_input=None):
        """Annonce à Home Assistant que l'attente est terminée."""
        return self.async_show_progress_done(next_step_id="complete")

    async def async_step_complete(self, user_input=None):
        """Termine le groupe après une décision acceptée ou explique l'expiration."""
        result = self._pairing_result
        if result is None:
            return self.async_abort(reason="pairing_cancelled")
        if result.get("status") == "timeout":
            return self.async_abort(reason="pairing_timeout")
        if result.get("status") == "transport_error":
            return self.async_abort(reason="pairing_transport_error")

        accepted_id = result["device_id"]
        assert self._request_id is not None
        resolved = {
            "message": "command_hubportal_pairing_resolved",
            "data": {
                "contract_version": PAIRING_CONTRACT_VERSION,
                "command": {"request_id": self._request_id, "device_id": accepted_id, "decision": "accepted"},
            },
        }
        await self._async_notify_all(resolved)

        # L'ordre part du Portal qui a accepté, puis d'un ordre stable par identifiant mobile_app.
        ordered_ids = [accepted_id, *sorted(device_id for device_id in self._portals if device_id != accepted_id)]
        existing_ids = {
            entry.unique_id for entry in self.hass.config_entries.async_entries(DOMAIN) if entry.unique_id is not None
        }
        entries = [
            _entry_data(self._foyer, self._portals[device_id], self._priority + index, self._master_timeout)
            for index, device_id in enumerate(ordered_ids)
            if _unique_id(self._foyer, device_id) not in existing_ids
        ]
        if not entries:
            return self.async_abort(reason="already_configured")

        # Les entrées secondaires sont créées par des flows import standards pour que
        # Home Assistant assure leur validation, leur persistance et leur setup.
        for data in entries[1:]:
            imported = await self.hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IMPORT}, data=data
            )
            if imported["type"] != "create_entry" and imported.get("reason") != "already_configured":
                return self.async_abort(reason="group_creation_failed")

        primary = entries[0]
        await self.async_set_unique_id(_unique_id(self._foyer, primary[CONF_DEVICE_ID]))
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"{primary[CONF_PORTAL_NAME]} · {self._foyer}",
            data=primary,
        )

    async def _async_notify_all(self, service_data: dict[str, Any]) -> None:
        """Diffuse la fermeture aux Portals; les erreurs partielles ne masquent pas l'acceptation."""
        for portal in self._portals.values():
            try:
                await asyncio.wait_for(
                    self.hass.services.async_call("notify", portal[CONF_NOTIFY_SERVICE], service_data, blocking=True),
                    timeout=NOTIFY_TIMEOUT,
                )
            except TimeoutError:
                _LOGGER.warning("Délai dépassé pendant la fermeture des feuilles de jumelage")
            except Exception:  # noqa: BLE001 - le flow accepté ne doit pas divulguer une erreur contenant des secrets
                _LOGGER.warning("Échec de transport pendant la fermeture des feuilles de jumelage")

    async def async_step_import(self, user_input=None):
        """Crée une entrée Portal secondaire lors du jumelage d'un foyer."""
        if user_input is None:
            return self.async_abort(reason="import_data_missing")
        foyer = user_input[CONF_FOYER]
        await self.async_set_unique_id(_unique_id(foyer, user_input[CONF_DEVICE_ID]))
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"{user_input[CONF_PORTAL_NAME]} · {foyer}",
            data=user_input,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HubPortalOptionsFlow(config_entry)


class HubPortalOptionsFlow(config_entries.OptionsFlow):
    """Options du délai de commande."""

    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "command_timeout", default=self.config_entry.options.get("command_timeout", 10)
                    ): vol.All(int, vol.Range(min=1, max=60))
                }
            ),
        )
