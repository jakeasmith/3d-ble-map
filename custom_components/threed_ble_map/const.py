"""Constants for the 3D BLE Map integration."""

from datetime import timedelta

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

WS_ANCHOR_MAP = f"{DOMAIN}/anchor_map"

# Recorder: how often to fold a scan into the smoothed view, how heavily to
# weight each new reading, and when to forget a beacon nobody hears any more.
RECORDER_INTERVAL = timedelta(seconds=5)
RECORDER_SMOOTHING = 0.25
RECORDER_STALE_AFTER = timedelta(minutes=10)

# The layout is only worth showing once a little history has accumulated.
MIN_RECORDING_SECONDS = 30
