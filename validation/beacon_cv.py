"""Ask whether beacon positions carry information, without any floor plan.

Usage:
    python3 validation/beacon_cv.py raw.json

The floor-plan yardstick locates the five radios and nothing else, so it cannot
say whether a beacon dot is in the right place. This does it by cross-validation
instead: hide one radio's reading of a beacon, fit the beacon from the radios
that remain, and see how well the hidden reading is predicted. A position that
generalises to a radio it was not fitted against is a position that means
something.

Two references make the number readable:

  null      the beacon parked at the centroid of the radios that hear it, which
            is the answer available for free, without solving anything.
  in-sample the fit's own residual. Cross-validated error must be worse than
            this; how much worse is how much of the fit was memorised noise.

Unlike score.py this needs no ground truth at all, so it works in any house --
which is the point, since the shipped integration has no floor plan either.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from score import _load, solve  # noqa: E402

geometry = _load("geometry")
refine = _load("refine")

FIT_STEPS = 60


def _rest_length(rssi: float, gain: float, bias: float) -> float:
    """The distance one reading implies, given that radio's solved gain."""
    exponent = (geometry.TX_POWER_AT_1M + gain - rssi) / (
        10 * geometry.PATH_LOSS_EXPONENT
    )
    return max(geometry.MIN_DISTANCE_M, (10**exponent) / bias)


def _predict(distance: float, gain: float) -> float:
    return (
        geometry.TX_POWER_AT_1M
        + gain
        - 10 * geometry.PATH_LOSS_EXPONENT * math.log10(max(0.3, distance))
    )


def _fit_beacon(
    seats: list[list[float]], rests: list[float], start: list[float]
) -> list[float]:
    """Place one beacon by the Guttman transform, the single-point SMACOF step.

    Each radio wants the beacon at its own position offset by the rest length,
    along the current direction. Averaging those is the update, and it cannot
    increase stress -- the same guarantee the full solver relies on.
    """
    point = start[:]
    for _ in range(FIT_STEPS):
        target = [0.0, 0.0, 0.0]
        for seat, rest in zip(seats, rests):
            delta = [point[axis] - seat[axis] for axis in range(3)]
            norm = math.sqrt(sum(d * d for d in delta)) or 1e-6
            for axis in range(3):
                target[axis] += seat[axis] + rest * delta[axis] / norm
        point = [value / len(seats) for value in target]
    return point


def main() -> int:
    raw = json.load(open(sys.argv[1]))
    _named, result = solve(raw)
    if result.get("error"):
        print("solver failed:", result["error"])
        return 1

    positions = result["positions"]
    gains = result.get("gains") or {}
    bias = result.get("bias_correction") or 1.0
    observations = raw["observations"]
    sources = [a["source"] for a in raw["anchors"] if a["source"] in positions]

    held_out: list[float] = []
    null_out: list[float] = []
    in_sample = [beacon["residual_db"] for beacon in result["beacons"]]
    eligible = 0

    for beacon in result["beacons"]:
        address = beacon["address"]
        heard = [s for s in sources if address in observations[s]]
        # Three radios and three unknowns is an exact fit: drop one and the
        # beacon is no longer determined, so there is nothing to validate.
        if len(heard) < 4:
            continue
        eligible += 1

        for hidden in heard:
            kept = [s for s in heard if s != hidden]
            seats = [[positions[s][axis] for axis in "xyz"] for s in kept]
            rests = [
                _rest_length(observations[s][address], gains.get(s, 0.0), bias)
                for s in kept
            ]
            centroid = [sum(p[axis] for p in seats) / len(seats) for axis in range(3)]
            point = _fit_beacon(seats, rests, centroid)

            seat = [positions[hidden][axis] for axis in "xyz"]
            gain = gains.get(hidden, 0.0)
            observed = observations[hidden][address]
            held_out.append(observed - _predict(math.dist(point, seat), gain))
            null_out.append(observed - _predict(math.dist(centroid, seat), gain))

    if not held_out:
        print(f"no beacon is heard by 4+ radios, so nothing can be held out "
              f"({len(result['beacons'])} beacons placed)")
        return 1

    rms = lambda values: math.sqrt(sum(v * v for v in values) / len(values))
    cv = rms(held_out)
    null = rms(null_out)
    fit = rms(in_sample)

    print(f"{len(result['beacons'])} beacons placed, {eligible} testable "
          f"(4+ radios), {len(held_out)} held-out predictions\n")
    print(f"  in-sample residual   {fit:6.2f} dB   the fit's own error")
    print(f"  cross-validated      {cv:6.2f} dB   predicting an unseen radio")
    print(f"  null (centroid)      {null:6.2f} dB   no solve at all\n")

    gained = 1 - cv / null
    verdict = (
        "positions carry real information"
        if gained > 0.1
        else "positions are barely better than a free guess"
        if gained > 0
        else "positions are NOT better than a free guess -- stop drawing them"
    )
    print(f"  cross-validation beats the null by {gained:+.0%} -- {verdict}")
    print(f"  overfit gap: {cv - fit:+.2f} dB between fitted and held-out")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
