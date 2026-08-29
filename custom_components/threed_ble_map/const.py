"""Constants for the 3D BLE Map integration."""

DOMAIN = "threed_ble_map"

PANEL_URL_PATH = "threed-ble-map"
PANEL_TITLE = "3D BLE Map"
PANEL_ICON = "mdi:cube-scan"

STATIC_URL_BASE = f"/{DOMAIN}_frontend"
PANEL_JS_FILENAME = "panel.js"

WS_LIST_ADAPTERS = f"{DOMAIN}/adapters"
WS_LIST_SIGNALS = f"{DOMAIN}/signals"

# How many signals the panel asks for by default.
DEFAULT_SIGNAL_LIMIT = 20
MAX_SIGNAL_LIMIT = 200
