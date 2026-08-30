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

# A second, much slower average of the same RSSI. The gap between the fast and
# slow averages is what a beacon *moving* looks like: a real displacement shows
# up as a sustained offset between them, while radio noise jitters the fast
# average around the slow one and cancels. One extra float per link buys a
# motion detector without keeping any history.
RECORDER_SLOW_SMOOTHING = 0.03

# A beacon needs a few readings before any of its statistics mean anything.
BEACON_MIN_SAMPLES = 4

# Motion, in dB, at which a beacon is trusted half as much. Roughly the shift a
# metre of movement produces at mid-range under the path-loss model.
BEACON_MOTION_SCALE_DB = 3.0

# No beacon is discarded outright -- weighting is continuous, and even a phone
# in a pocket says something about which radios can hear each other. This is the
# least a beacon can be trusted.
BEACON_MIN_WEIGHT = 0.05

# The layout solve takes hundreds of milliseconds, which must not happen on the
# event loop. It runs in an executor and the result is cached: the geometry
# changes far more slowly than the panel polls.
SOLVE_CACHE_SECONDS = 15

WS_RAW_OBSERVATIONS = f"{DOMAIN}/raw_observations"
