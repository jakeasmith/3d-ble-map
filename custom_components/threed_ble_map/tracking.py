"""Move the beacons without re-solving the house.

A full solve searches for everything at once: eight radio positions, eight
gains, and every beacon, from twelve different starting points. That is the
right way to establish a frame, and the wrong way to answer "where is this
tracker now" -- it costs seconds, so a beacon's position is only ever as fresh
as the last full solve.

But the radios are infrastructure. Once calibration has settled they move about
5 cm per update, and with them held still each beacon becomes an independent
three-unknown problem: a handful of ranges to known points. Measured on a live
capture of this house, against the joint solve's own answer:

    full joint solve, 92 beacons        7060 ms
    tracked, 2 sweeps                      3.5 ms
        drift from the joint answer: median 0.32 m, p90 1.40 m

0.32 m against a map whose own accuracy is around 2.4 m, for a two-thousandth
of the cost.

The physics is not reimplemented here. This calls refine's own majorization
sweep and then puts the radios back where they were, so the objective, the
Huber weighting, the 1/d^2 weighting, the floor-crossing penalty and the
vertical envelope are all necessarily the same ones the full solve used. A
hand-rolled version that dropped the floor penalty and the envelope was tried
first and disagreed with the joint answer by 7 m.
"""

from __future__ import annotations

import math
from typing import Any

from .refine import (
    MIN_RADIOS_PER_BEACON,
    _constrain_beacons,
    _majorize,
    _storeys,
    shadowing_bias,
)

# Sweeps for a beacon already sitting near its answer. The drift from the joint
# solve is flat from one sweep to about four and then slowly grows, because a
# handful of beacons with degenerate geometry wander once nothing else pins
# them, so more work here is not better work.
SWEEPS = 2

# A beacon nobody has placed yet starts from the strongest radio that hears it
# and needs a real descent rather than a nudge. Still under a millisecond each.
COLD_SWEEPS = 24

# How much of each computed step is actually taken. A full Guttman step
# overshoots on beacons whose geometry barely determines them -- heard by
# exactly three radios, where the answer is a mirror pair -- and they settle
# into a two-cycle a couple of metres wide rather than converging. Twelve of
# ninety-two did. Taking a fraction of the step lands on the midpoint of that
# cycle instead, and costs the well-conditioned majority nothing, because a
# tick happens every few seconds either way.
#
# Measured on this house, holding everything else fixed:
#
#     step   settled jitter   follows a real 5 m move to 95%
#     1.00        0.478 m       1 tick   ( 5 s)
#     0.70        0.319 m       2.5 ticks (12 s)
#     0.50        0.224 m       4.3 ticks (22 s)
#     0.35        0.149 m       7.0 ticks (35 s)
#     0.20        0.077 m      13.4 ticks (67 s)
#
# 0.7 keeps the jitter well inside the map's own ~2.4 m accuracy while
# responding in about the time the recorder's own smoothing takes to reflect a
# move at all. Damping harder would buy stillness the eye cannot see, at the
# price of the responsiveness this path exists for.
STEP = 0.7

# How far a tracked beacon is allowed to move in one update. Ranging from RSSI
# is noisy enough to throw a badly-conditioned beacon across the house between
# ticks; a person does not walk 15 m in five seconds and a step limit is a
# cheaper defence than a filter with state.
MAX_STEP_M = 4.0


def frame_from(result: dict[str, Any], levels: dict[str, float]) -> dict[str, Any]:
    """Everything the tracker needs from a full solve, in the published frame.

    Gains and shadowing are scalars and rest lengths are distances, so none of
    them care which way the map is facing. That is what lets tracking run
    directly in the calibrated frame the panel is shown, with no transform to
    apply afterwards and no chance of the beacons ending up in a different
    orientation from the radios.
    """
    positions = result.get("positions") or {}
    if not positions:
        return {}
    return {
        "anchors": list(positions),
        "radios": {a: dict(p) for a, p in positions.items()},
        "gains": dict(result.get("gains") or {}),
        "shadowing_db": float(result.get("shadowing_db") or 0.0),
        "floor_penalty_db": float(result.get("floor_penalty_db") or 0.0),
        "levels": {a: levels[a] for a in positions if a in levels},
    }


def track(
    frame: dict[str, Any],
    observations: dict[str, dict[str, float]],
    seeds: dict[str, dict[str, float]] | None = None,
) -> dict[str, dict[str, float]]:
    """Place every beacon three or more radios can hear, radios held fixed."""
    anchors = frame.get("anchors") or []
    if len(anchors) < MIN_RADIOS_PER_BEACON:
        return {}

    radios = frame["radios"]
    points = [[radios[a]["x"], radios[a]["y"], radios[a]["z"]] for a in anchors]
    gains = [float(frame["gains"].get(a, 0.0)) for a in anchors]
    levels = frame.get("levels") or {}
    radio_levels = [levels.get(a) for a in anchors]
    bias = shadowing_bias(frame.get("shadowing_db") or 0.0)
    penalty = float(frame.get("floor_penalty_db") or 0.0)
    storeys = _storeys(anchors, levels)

    heard: dict[str, list[tuple[int, float]]] = {}
    for i, anchor in enumerate(anchors):
        for address, rssi in (observations.get(anchor) or {}).items():
            heard.setdefault(address, []).append((i, rssi))
    placeable = [a for a, rows in heard.items() if len(rows) >= MIN_RADIOS_PER_BEACON]
    if not placeable:
        return {}

    seeds = seeds or {}
    warm = [a for a in placeable if a in seeds]
    cold = [a for a in placeable if a not in seeds]

    tracked: dict[str, dict[str, float]] = {}
    for group, sweeps in ((warm, SWEEPS), (cold, COLD_SWEEPS)):
        if not group:
            continue
        starts = [
            [seeds[a]["x"], seeds[a]["y"], seeds[a]["z"]]
            if a in seeds
            else _cold_start(heard[a], points)
            for a in group
        ]
        readings = [
            (radio, b, rssi)
            for b, address in enumerate(group)
            for radio, rssi in heard[address]
        ]
        moved = [list(p) for p in starts]
        for _ in range(sweeps):
            # _majorize moves the radios too. Putting them straight back is what
            # makes this a beacon-only solve while still using the real sweep.
            _majorize(
                points, moved, gains, readings, [], [], bias,
                radio_levels=radio_levels, floor_penalty=penalty,
            )
            for i, anchor in enumerate(anchors):
                points[i] = [radios[anchor][axis] for axis in ("x", "y", "z")]
            _constrain_beacons(moved, points, storeys)

        for address, start, end in zip(group, starts, moved):
            if address in seeds:
                end = [s + (e - s) * STEP for s, e in zip(start, end)]
            tracked[address] = _limit(start, end, address in seeds)
    return tracked


def _cold_start(rows: list[tuple[int, float]], points: list[list[float]]) -> list[float]:
    """Start a new beacon beside whichever radio hears it loudest."""
    loudest = max(rows, key=lambda row: row[1])[0]
    return [points[loudest][0] + 0.5, points[loudest][1] + 0.5, points[loudest][2]]


def _limit(start: list[float], end: list[float], warm: bool) -> dict[str, float]:
    """Cap how far one update may move a beacon that already had a position."""
    if warm and (distance := math.dist(start, end)) > MAX_STEP_M:
        share = MAX_STEP_M / distance
        end = [s + (e - s) * share for s, e in zip(start, end)]
    return {axis: round(value, 2) for axis, value in zip(("x", "y", "z"), end)}
