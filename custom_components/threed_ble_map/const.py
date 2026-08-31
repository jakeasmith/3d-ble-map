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

# Hard ceiling on how many beacons enter the solve, most-heard first.
#
# This is a backstop, not the fix. Solve cost grows with radios x beacons and
# every radio added to a house also admits more beacons past
# MIN_RADIOS_PER_BEACON, so the two multiply -- but a long solve is only
# dangerous when solves can overlap, and _async_cached_solve now runs at most
# one at a time. Measured at 5.3 s for 8 radios and 51 beacons, comfortably
# inside the cache interval; this bounds the pathological case, not the normal
# one.
#
# Set at the point where accuracy stops improving, measured on 120 synthetic
# beacons at realistic noise: 2.71 m at 30, 2.40 at 60, 2.04 at 90, 1.97 at 120
# and unchanged above. Capping lower would trade real accuracy for a cost
# problem that the single-flight guard already solves.
MAX_SOLVE_BEACONS = 120

# Warn when a solve takes longer than this. It runs in an executor and only one
# runs at a time, so a slow solve is a stale map rather than a hung core -- but
# it is the number to watch, because everything that arrives while one is
# running waits on it.
SOLVE_SLOW_SECONDS = 5.0

# The share of each new solve that reaches the published layout once the map has
# settled. Radios are infrastructure: they do not move between one solve and the
# next, but the solver returns a slightly different answer every time and the map
# visibly contorts. Measured, consecutive solves moved it 2.02 m RMS with nothing
# in the house having changed.
#
# So a solve is evidence, not the answer. Measured over 150 solves of one static
# capture, against the exact step response of the same filter:
#
#     rate    resting jitter    63% of a real move    95%
#     today         2.023 m          immediate        immediate
#     0.20          0.332 m           1.8 min          5.1 min
#     0.10          0.163 m           3.7 min         10.6 min
#     0.05          0.082 m           7.3 min         21.6 min
#     0.03          0.049 m          12.1 min         36.3 min
#     0.01          0.056 m          36.3 min        109.6 min
#
# 0.03 is where the curve stops paying. Below it the jitter does not improve --
# 0.01 is slightly worse, because what remains is not per-solve noise but the
# occasional solve landing in a different basin, which averaging cannot remove --
# while the time to notice a radio that genuinely moved triples. Above it,
# stillness is given up for responsiveness the problem does not need: radios are
# screwed to shelves, and twelve minutes to follow one that really moved is not
# a cost anyone pays often.
#
# It is a 41x reduction in per-update movement, and 5 cm on a house-sized map
# reads as still.
CALIBRATION_RATE = 0.03

# A solve that has not finished by now is wedged rather than slow, and is killed.
# The normal figure is 5-7 s; this is far enough above it that only a genuine
# hang trips it, and a killed child costs one stale map rather than a stuck core.
SOLVE_TIMEOUT_SECONDS = 120.0

WS_RAW_OBSERVATIONS = f"{DOMAIN}/raw_observations"

WS_SUBSCRIBE = f"{DOMAIN}/subscribe"

# How often the map is recomputed and pushed to whoever is watching. Matched to
# the recorder's own sampling interval: the readings underneath cannot change
# faster than they are taken, so publishing faster would only resend the same
# numbers.
LIVE_INTERVAL = timedelta(seconds=5)

# With nobody watching, a full solve still runs this often. Not for the map --
# there is nobody to show it to -- but because calibration only advances when a
# solve happens, so a house that is never looked at would show a cold, unsettled
# layout the moment someone opened the panel. Five minutes keeps it converging
# for about one percent of a core.
IDLE_SOLVE_SECONDS = 300
