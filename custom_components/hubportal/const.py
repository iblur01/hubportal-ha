"""Constantes de l’intégration HubPortal."""

from datetime import timedelta

DOMAIN = "hubportal"
PLATFORMS = ("switch", "number", "button", "sensor", "select")
CONF_FOYER = "foyer"
CONF_DEVICE_ID = "mobile_device_id"
CONF_PORTAL_NAME = "portal_name"
CONF_NOTIFY_SERVICE = "notify_service"
CONF_PRIORITY = "priority"
CONF_MASTER_TIMEOUT = "master_timeout"
CONF_SCREEN_ENTITY = "screen_entity"
CONF_LOCK_ENTITY = "lock_entity"
CONF_BRIGHTNESS_ENTITY = "brightness_entity"
CONF_TIMER_ENTITY = "timer_entity"
CONF_TIMER_END_ENTITY = "timer_end_entity"
CONF_TIMER_REMAINING_ENTITY = "timer_remaining_entity"
CONF_ORDER = "order"
CONF_MASTER = "master"
DEFAULT_MASTER_TIMEOUT = 90
DEFAULT_COMMAND_TIMEOUT = 10
UPDATE_INTERVAL = timedelta(seconds=5)
LEGACY_AVAILABILITY_MAX_AGE = 300
STORAGE_VERSION = 1

STATE_IDLE = "idle"
STATE_ACTIVE = "active"
STATE_RINGING = "ringing"

SOURCES = {
    "screen": CONF_SCREEN_ENTITY,
    "action_lock": CONF_LOCK_ENTITY,
    "brightness": CONF_BRIGHTNESS_ENTITY,
    "timer": CONF_TIMER_ENTITY,
    "timer_end": CONF_TIMER_END_ENTITY,
    "timer_remaining": CONF_TIMER_REMAINING_ENTITY,
}

# Maillage (contrat v2)
CONF_NETWORK_ID = "network_id"
CONF_NETWORK_KEY = "network_key"
CONF_HA_ID = "ha_portal_id"
CONF_HA_NAME = "ha_name"
CONF_HA_CERT = "ha_cert_pem"
CONF_HA_KEY = "ha_key_pem"
CONF_HA_FP = "ha_fp"
CONF_MEMBERS = "members"
CONF_EPOCH = "epoch"
CONF_MESH_HOST = "mesh_host"
MESH_PORT = 8447
MESH_PATH = "/mesh"
MESH_PROTO = 1
MESH_SERVICE_TYPE = "_hubportal._tcp.local."
MESH_TS_WINDOW = 60
MESH_LEASE_SECONDS = 3.0
MESH_HANDSHAKE_TIMEOUT = 10.0
MESH_CONNECT_TIMEOUT = 10.0
MESH_SEND_TIMEOUT = 5.0
MESH_BACKOFF_BASE = 1.0
MESH_BACKOFF_CAP = 60.0
MESH_ADMISSION_TIMEOUT = 120
MESH_ALERT_EVENT = "hubportal_mesh_alert"
