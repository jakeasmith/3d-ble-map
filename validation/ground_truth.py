"""Approximate real-world anchor positions, for VALIDATION ONLY.

    *** THIS FILE MUST NEVER BE IMPORTED BY custom_components/. ***

The whole point of the integration is that it works in any home with no floor
plan and no surveyed positions. Feeding measured positions back into the solver
would make it look accurate here and useless everywhere else. These coordinates
exist so the maths can be scored against reality, and for nothing else.

Derived by reading room centres off a two-storey floor plan drawn with both
floors in registration (the garage sits directly beneath the upstairs COZ room),
scaled by the one room whose dimensions are recorded: the Stretch Room at
10 x 12 ft measures 164 x 187 px, giving ~16 px/ft in both axes.

Accuracy of these figures: the anchor is placed at its room's centre, but the
hardware sits on a wall or in a corner, so treat each as +/- 1.5 m. That is the
noise floor of this yardstick -- a solver scoring better than about 1.5 m
against it is not measurably better.
"""

from __future__ import annotations

import math

PIXELS_PER_FOOT = 16.0
FEET_PER_METRE = 3.28084
METRES_PER_PIXEL = 1.0 / (PIXELS_PER_FOOT * FEET_PER_METRE)

# The upstairs plan starts at y=5 px, the downstairs at y=415 px; subtracting
# those puts both floors in one frame.
UPSTAIRS_Y_ORIGIN = 5
DOWNSTAIRS_Y_ORIGIN = 415

# Typical storey pitch. Only used for the vertical axis of the yardstick.
STOREY_HEIGHT_M = 2.9

# Room centres in plan pixels, with the floor each sits on.
_ROOMS = {
    "BLE Anchor Garage": (159, 648, "downstairs"),
    "Office EPL": (382, 708, "downstairs"),
    "EP1 OG Dining Room": (516, 500, "downstairs"),
    "BLE Anchor Main Bedroom": (698, 89, "upstairs"),
    "Stretch Presence": (775, 264, "upstairs"),
}


def _to_metres(x_px: float, y_px: float, floor: str) -> tuple[float, float, float]:
    origin = UPSTAIRS_Y_ORIGIN if floor == "upstairs" else DOWNSTAIRS_Y_ORIGIN
    return (
        x_px * METRES_PER_PIXEL,
        (y_px - origin) * METRES_PER_PIXEL,
        STOREY_HEIGHT_M if floor == "upstairs" else 0.0,
    )


TRUTH = {name: _to_metres(*values) for name, values in _ROOMS.items()}

# Anchors may be named differently in Home Assistant over time; match loosely.
ALIASES = {
    "BLE Anchor 9fbb0c": "BLE Anchor Garage",
    "BLE Anchor c55180": "BLE Anchor Main Bedroom",
    "EP1 OG": "EP1 OG Dining Room",
}


def resolve(label: str) -> str | None:
    """Map a Home Assistant anchor label onto a ground-truth key."""
    if label in TRUTH:
        return label
    if label in ALIASES:
        return ALIASES[label]
    for key in TRUTH:
        if key.lower().startswith(label.lower()[:12]):
            return key
    return None


def pairwise() -> dict[tuple[str, str], float]:
    """True distances between every pair, the scale-free part of the yardstick."""
    names = sorted(TRUTH)
    return {
        (a, b): math.dist(TRUTH[a], TRUTH[b])
        for i, a in enumerate(names)
        for b in names[i + 1 :]
    }


if __name__ == "__main__":
    print("Ground truth in metres (validation only):\n")
    for name, (x, y, z) in sorted(TRUTH.items()):
        print(f"  {name:26} x={x:6.2f}  y={y:6.2f}  z={z:5.2f}")
    print("\nTrue pairwise distances:\n")
    for (a, b), d in sorted(pairwise().items(), key=lambda kv: kv[1]):
        print(f"  {a:26} - {b:26} {d:6.2f} m")
