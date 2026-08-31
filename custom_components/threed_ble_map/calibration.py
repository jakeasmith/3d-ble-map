"""Hold the radios still, and let consistent evidence move them anyway.

Radios are infrastructure. They sit on shelves and in cupboards and they do not
move between one solve and the next -- but the solver does not know that, so
every solve returns a slightly different answer and the map visibly contorts.
Measured before this: consecutive solves moved the layout 1.0 to 2.2 m RMS with
nothing in the house having changed at all.

The fix is to stop treating each solve as the answer and start treating it as
evidence. A calibrated layout is kept, and each new solve is blended into it at
a low rate, so noise averages away while a radio that genuinely moved is tracked
over minutes rather than instantly.

Two things have to happen in that order, and the first is easy to forget: the
solver has no preferred orientation about the vertical axis and no preferred
handedness, so consecutive solves come back arbitrarily spun and mirrored.
Averaging two layouts in different frames is meaningless -- it would shrink the
map toward its own centroid. So a candidate is rotated onto the calibrated frame
first, and only then blended.

Only yaw and handedness are free. The vertical axis is pinned by the storey
constraints in refine.py and oriented before refinement, so it is already
consistent between solves and does not need solving for here. That keeps the
alignment a closed form and avoids taking on an SVD.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

# The share of a new solve that reaches the published layout. See
# CALIBRATION_RATE in const.py for how it was chosen.
Point = dict[str, float]
Layout = dict[str, Point]

AXES = ("x", "y", "z")


def learning_rate(solves: int, floor: float) -> float:
    """How much the next solve is allowed to move the calibrated layout.

    A plain fixed rate would take a very long time to establish a map from
    nothing -- at 3% the first solve would barely register. So the early solves
    are a running mean (1, 1/2, 1/3 ...), which converges quickly, and the rate
    stiffens to `floor` once enough evidence has accumulated. `solves` is how
    many have already been folded in, so the very first returns 1.0 and is
    adopted whole.
    """
    return max(floor, 1.0 / max(1, solves + 1))


def _centroid(layout: Layout, keys: Iterable[str]) -> Point:
    keys = list(keys)
    if not keys:
        return {axis: 0.0 for axis in AXES}
    return {
        axis: sum(layout[key][axis] for key in keys) / len(keys) for axis in AXES
    }


def alignment(candidate: Layout, reference: Layout) -> dict[str, Any]:
    """The spin and mirror that best puts `candidate` in `reference`'s frame.

    Returned rather than applied, because the beacons have to travel with the
    radios. They are solved in the same frame, so publishing calibrated radios
    without moving the beacons by the identical transform would leave the
    beacons floating in the previous solve's orientation.

    Rotation about the vertical axis has a closed form: the angle that best
    matches two sets of paired points is atan2 of their summed cross and dot
    products. Handedness has no closed form because it is discrete, so both are
    tried and the better fit kept.
    """
    shared = [key for key in candidate if key in reference]
    if len(shared) < 2:
        return {"angle": 0.0, "mirror": 1, "from": None, "to": None}

    here = _centroid(candidate, shared)
    there = _centroid(reference, shared)

    best: tuple[float, float, int] | None = None
    for mirror in (1, -1):
        cross = 0.0
        dot = 0.0
        for key in shared:
            ax = (candidate[key]["x"] - here["x"]) * mirror
            ay = candidate[key]["y"] - here["y"]
            bx = reference[key]["x"] - there["x"]
            by = reference[key]["y"] - there["y"]
            cross += ax * by - ay * bx
            dot += ax * bx + ay * by
        angle = math.atan2(cross, dot)
        cos, sin = math.cos(angle), math.sin(angle)
        residual = 0.0
        for key in shared:
            ax = (candidate[key]["x"] - here["x"]) * mirror
            ay = candidate[key]["y"] - here["y"]
            residual += (
                (ax * cos - ay * sin - (reference[key]["x"] - there["x"])) ** 2
                + (ax * sin + ay * cos - (reference[key]["y"] - there["y"])) ** 2
            )
        if best is None or residual < best[0]:
            best = (residual, angle, mirror)

    _, angle, mirror = best
    return {"angle": angle, "mirror": mirror, "from": here, "to": there}


def apply(transform: dict[str, Any], point: Point) -> Point:
    """Move one point into the calibrated frame."""
    here, there = transform["from"], transform["to"]
    if here is None:
        return dict(point)
    cos, sin = math.cos(transform["angle"]), math.sin(transform["angle"])
    ax = (point["x"] - here["x"]) * transform["mirror"]
    ay = point["y"] - here["y"]
    return {
        "x": ax * cos - ay * sin + there["x"],
        "y": ax * sin + ay * cos + there["y"],
        "z": point["z"] - here["z"] + there["z"],
    }


def align(candidate: Layout, reference: Layout) -> Layout:
    """Spin and mirror a whole layout onto `reference`."""
    transform = alignment(candidate, reference)
    return {key: apply(transform, point) for key, point in candidate.items()}


def blend(reference: Layout, candidate: Layout, rate: float) -> Layout:
    """Move the calibrated layout `rate` of the way toward a new solve.

    A radio the calibrated layout has never seen is adopted outright -- there is
    nothing to average it against, and holding a new radio at the origin while
    it creeps into place would be worse than a single jump. A radio that has
    gone missing from this solve keeps its calibrated position rather than being
    forgotten, so a proxy dropping off Wi-Fi for a minute does not erase it.
    """
    blended: Layout = {key: dict(point) for key, point in reference.items()}
    for key, point in candidate.items():
        if key not in blended:
            blended[key] = dict(point)
            continue
        blended[key] = {
            axis: (1.0 - rate) * blended[key][axis] + rate * point[axis]
            for axis in AXES
        }
    return blended


def calibrate(
    reference: Layout | None, candidate: Layout, solves: int, floor: float
) -> tuple[Layout, float, dict[str, Any]]:
    """Fold one solve into the calibrated layout.

    Returns the calibrated layout, the rate used, and the transform that put the
    candidate in its frame -- the caller needs that last one to bring the
    beacons along.
    """
    identity = {"angle": 0.0, "mirror": 1, "from": None, "to": None}
    if not reference or not candidate:
        return {key: dict(point) for key, point in candidate.items()}, 1.0, identity
    transform = alignment(candidate, reference)
    moved = {key: apply(transform, point) for key, point in candidate.items()}
    rate = learning_rate(solves, floor)
    return blend(reference, moved, rate), rate, transform


def movement(before: Layout | None, after: Layout) -> float | None:
    """RMS distance each radio moved between two published layouts."""
    if not before:
        return None
    shared = [key for key in after if key in before]
    if not shared:
        return None
    total = sum(
        sum((after[key][axis] - before[key][axis]) ** 2 for axis in AXES)
        for key in shared
    )
    return math.sqrt(total / len(shared))
