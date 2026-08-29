"""Joint solve for radio positions, per-radio gain, and beacon positions.

This is a force-directed layout, in the metric sense. Every RSSI reading is a
spring between a radio and a beacon whose rest length is the distance that
reading implies. Relaxing all the springs at once is stress majorization
(SMACOF): each point moves to the average of where its springs want it, which is
guaranteed never to increase the total stress. There is no learning rate and no
line search to tune -- the update is the algorithm.

The reason to do this at all is calibration. geometry.py's pairwise fit assumes
every radio converts RSSI to distance identically. They do not: antenna gain,
shielding and enclosure differ per board, so -70 dBm on one radio is not the same
distance as -70 dBm on another, and treating that offset as zero bakes a
per-radio bias into the layout. The offset is only identifiable if it is solved
for, and each beacon heard by three or more radios adds observations faster than
it adds unknowns, so positions and gains can be recovered together:

    rssi(radio, beacon) = TX + gain[radio] - 10 n log10(|p_radio - q_beacon|)

Gain is linear in that model, so it has a closed form -- a radio's best offset is
just how far its readings sit from the model, taken as a median so one
wall-blocked beacon cannot drag a whole radio's calibration. Positions are
non-linear and get the SMACOF sweeps. The two alternate.

Two degeneracies are fixed explicitly: adding a constant to every gain is
indistinguishable from scaling every distance, so gains are held to zero mean;
and the whole solution is free to rotate, so orientation is applied afterwards by
geometry._orient_by_level.
"""

from __future__ import annotations

import math
import random
from typing import Any, Sequence

from .geometry import (
    MAX_DISTANCE_M,
    MIN_DISTANCE_M,
    PATH_LOSS_EXPONENT,
    TX_POWER_AT_1M,
)

# A beacon costs three unknowns -- its own position -- and supplies one
# observation per radio that hears it, so at exactly three it nets out to zero
# constraint on the radios: it only pins itself.
#
# That argument says such beacons are neutral, not harmful, and raising this to
# 4 was tried and reverted: on a five-radio house it halved the usable beacons
# (60 to 32) and the layout got noisier, not cleaner. Neutral data is worth
# keeping when the alternative is half as much of it.
MIN_RADIOS_PER_BEACON = 3

# Radios on one storey share a floor, so their heights should agree. Home
# Assistant knows which floor each radio is on, and that is a much stronger fact
# than anything RSSI can say about the vertical axis -- a floor between two
# radios eats signal that the path-loss model books as distance, which is what
# stretches the layout. This weights the "same floor, same height" spring
# against a radio's observations; 1.0 makes it worth as much as everything else
# that radio hears put together.
FLOOR_COHESION = 0.8

# Residuals beyond this many dB are treated as outliers -- a wall, a reflection,
# a beacon that moved mid-window -- and are downweighted rather than chased.
HUBER_DELTA_DB = 6.0

# A radio's gain offset realistically lives within this band. Anything larger is
# the fit laundering a geometry error into a calibration term.
MAX_GAIN_DB = 12.0

# Below this two points are indistinguishable and the spring direction is noise.
MIN_SEPARATION_M = 0.3

# Shadowing is Gaussian in dB, so inverting the path-loss law gives a *log*-normal
# distance whose mean sits above the true distance by exp(a^2 sigma^2 / 2), where
# a = ln10 / (10 n). At 2 dB that is under 2% and ignorable; at the 8 dB a real
# house produces it is 30%, and it inflates the entire map. The estimate is
# capped because the fit residual also carries model error, and over-correcting
# would shrink the map instead.
MAX_SHADOWING_DB = 12.0

ROUNDS = 40
SWEEPS_PER_ROUND = 8

# SMACOF is a local method, so the answer depends on where it starts. The
# convex-relaxation literature exists precisely because of this. Short of
# solving an SDP, restarting from perturbed seeds and keeping the best fit
# recovers most of the benefit: without it roughly one run in ten settles in a
# badly wrong minimum, which is what produced 13m outliers against a 2m median.
RESTARTS = 6
RESTART_JITTER = 0.4


def shadowing_bias(shadowing_db: float) -> float:
    """How much the naive path-loss inversion overestimates distance.

    Dividing an estimated distance by this returns the median of the log-normal
    rather than its mean, which is the unbiased choice.
    """
    sigma = max(0.0, min(MAX_SHADOWING_DB, shadowing_db))
    a = math.log(10) / (10 * PATH_LOSS_EXPONENT)
    return math.exp((a * sigma) ** 2 / 2)


def refine_layout(
    anchors: Sequence[str],
    positions: dict[str, dict[str, float]],
    observations: dict[str, dict[str, float]],
    direct_rssi: dict[tuple[str, str], list[float]],
    levels: dict[str, float] | None = None,
    shadowing_db: float = 0.0,
) -> dict[str, Any] | None:
    """Refine an initial layout, solving per-radio gain at the same time.

    Returns None when there is not enough shared data, or when refinement fails
    to improve on the input, so the caller can fall back to the pairwise fit.
    """
    radios = list(anchors)
    index = {radio: i for i, radio in enumerate(radios)}

    beacons = _shared_beacons(radios, observations)
    if len(beacons) < len(radios):
        return None

    seed_points = [
        [positions[radio]["x"], positions[radio]["y"], positions[radio]["z"]]
        for radio in radios
    ]

    readings = [
        (index[radio], b, observations[radio][beacon])
        for b, beacon in enumerate(beacons)
        for radio in radios
        if beacon in observations[radio]
    ]
    links = [
        (index[listener], index[advertiser], sum(values) / len(values))
        for (listener, advertiser), values in direct_rssi.items()
        if listener in index and advertiser in index
    ]
    if not readings:
        return None

    floor_groups = _floor_groups(radios, levels or {})
    bias = shadowing_bias(shadowing_db)
    scale = _scene_scale(seed_points)

    before = _rms_residual(
        seed_points,
        [list(p) for p in seed_points[: len(beacons)]] or [[0.0, 0.0, 0.0]],
        [0.0] * len(radios),
        [],
        links,
    )
    baseline = _rms_residual(
        seed_points,
        _seed_beacons(radios, observations, beacons, seed_points, random.Random(0)),
        [0.0] * len(radios),
        readings,
        links,
    )

    best: tuple[float, list[list[float]], list[float]] | None = None
    for attempt in range(RESTARTS):
        rng = random.Random(attempt)
        points = [
            point[:]
            if attempt == 0
            else [
                value + rng.gauss(0, RESTART_JITTER * scale) for value in point
            ]
            for point in seed_points
        ]
        gains = [0.0] * len(radios)
        beacon_points = _seed_beacons(radios, observations, beacons, points, rng)

        for _ in range(ROUNDS):
            gains = _solve_gains(
                points, beacon_points, gains, readings, links, len(radios)
            )
            for _ in range(SWEEPS_PER_ROUND):
                _majorize(
                    points, beacon_points, gains, readings, links, floor_groups, bias
                )

        residual = _rms_residual(points, beacon_points, gains, readings, links)
        if best is None or residual < best[0]:
            best = (residual, [p[:] for p in points], gains[:])

    current, points, gains = best

    if current >= baseline:
        # Refinement is an optimisation, not an article of faith. If it did not
        # beat the seed layout, say so and let the caller keep the seed.
        return None

    return {
        "positions": [list(point) for point in points],
        "gains": {radio: round(gains[i], 1) for i, radio in enumerate(radios)},
        "residual_db": round(current, 2),
        "seed_residual_db": round(baseline, 2),
        "beacons_used": len(beacons),
        "observations": len(readings) + len(links),
        "shadowing_db": round(shadowing_db, 2),
        "bias_correction": round(bias, 3),
        "restarts": RESTARTS,
    }


def _seed_beacons(
    radios: Sequence[str],
    observations: dict[str, dict[str, float]],
    beacons: Sequence[str],
    points: list[list[float]],
    rng: random.Random,
) -> list[list[float]]:
    return [
        _initial_beacon_position(radios, observations, beacon, points, rng)
        for beacon in beacons
    ]


def _scene_scale(points: list[list[float]]) -> float:
    """Rough extent of the seed layout, so jitter is proportional to the house."""
    spans = [
        max(p[axis] for p in points) - min(p[axis] for p in points)
        for axis in range(3)
    ]
    return max(1.0, max(spans))


def _floor_groups(
    radios: Sequence[str], levels: dict[str, float]
) -> list[list[int]]:
    """Indices of the radios sharing each building floor, groups of 2 or more."""
    by_level: dict[float, list[int]] = {}
    for i, radio in enumerate(radios):
        if (level := levels.get(radio)) is not None:
            by_level.setdefault(level, []).append(i)
    return [group for group in by_level.values() if len(group) > 1]


def _majorize(
    points: list[list[float]],
    beacon_points: list[list[float]],
    gains: list[float],
    readings: list[tuple[int, int, float]],
    links: list[tuple[int, int, float]],
    floor_groups: list[list[int]],
    bias: float,
) -> None:
    """One SMACOF sweep: move every point to where its springs want it.

    For each edge, the neighbour contributes the position this point would sit
    at if that spring alone were satisfied -- the neighbour's position, offset by
    the spring's rest length along the current direction. Averaging those over a
    point's edges is the Guttman transform, and it cannot increase stress.
    """
    targets = [[0.0] * 3 for _ in points]
    weights = [0.0] * len(points)
    beacon_targets = [[0.0] * 3 for _ in beacon_points]
    beacon_weights = [0.0] * len(beacon_points)

    for radio, beacon, observed in readings:
        rest = _rest_length(observed - gains[radio], bias)
        if rest is None:
            continue
        _pull(points[radio], beacon_points[beacon], rest, targets[radio], weights, radio)
        _pull(
            beacon_points[beacon],
            points[radio],
            rest,
            beacon_targets[beacon],
            beacon_weights,
            beacon,
        )

    for listener, advertiser, observed in links:
        rest = _rest_length(observed - gains[listener] - gains[advertiser], bias)
        if rest is None:
            continue
        _pull(points[listener], points[advertiser], rest, targets[listener], weights, listener)
        _pull(points[advertiser], points[listener], rest, targets[advertiser], weights, advertiser)

    # Radios on one storey are at the same height. Pull each towards its floor's
    # mean, as a spring alongside the observations rather than a hard clamp, so
    # a genuine height difference can still show through.
    for group in floor_groups:
        mean_z = sum(points[i][2] for i in group) / len(group)
        for i in group:
            pull = FLOOR_COHESION * max(1.0, weights[i])
            targets[i][0] += pull * points[i][0]
            targets[i][1] += pull * points[i][1]
            targets[i][2] += pull * mean_z
            weights[i] += pull

    for i, weight in enumerate(weights):
        if weight > 0:
            points[i] = [value / weight for value in targets[i]]
    for b, weight in enumerate(beacon_weights):
        if weight > 0:
            beacon_points[b] = [value / weight for value in beacon_targets[b]]


def _pull(
    point: list[float],
    neighbour: list[float],
    rest: float,
    target: list[float],
    weights: list[float],
    index: int,
) -> None:
    """Accumulate one spring's preferred position for `point`."""
    delta = [point[axis] - neighbour[axis] for axis in range(3)]
    distance = math.sqrt(sum(value * value for value in delta))
    if distance < MIN_SEPARATION_M:
        # Degenerate direction; any unit vector will do and this one is stable.
        delta, distance = [1.0, 0.0, 0.0], 1.0

    # Downweight edges the model already disagrees with badly, so a wall-blocked
    # reading bends the layout less than a clean one. This is the Huber weight
    # applied to the residual in dB, in IRLS form.
    residual_db = abs(
        10 * PATH_LOSS_EXPONENT * math.log10(max(distance, MIN_SEPARATION_M) / rest)
    )
    weight = 1.0 if residual_db <= HUBER_DELTA_DB else HUBER_DELTA_DB / residual_db

    # Shadowing is Gaussian in dB, not in metres, so a fixed dB error is a
    # *proportional* distance error. Weighting by 1/d^2 makes minimising this sum
    # equivalent to minimising squared dB error, the maximum likelihood
    # objective. Without it a distant, weak link outweighs a close reliable one
    # simply for being further away.
    #
    # The weight must use the *model* distance, not the measured rest length.
    # Weighting by the measurement lets any link that noise happens to make look
    # short dominate everything, and the whole map collapses inward -- measured
    # at 0.57x true scale before this was corrected.
    weight /= distance * distance

    for axis in range(3):
        target[axis] += weight * (neighbour[axis] + rest * delta[axis] / distance)
    weights[index] += weight


def _rest_length(rssi: float, bias: float = 1.0) -> float | None:
    """Spring rest length: the distance this reading implies, or None if absurd."""
    distance = 10 ** ((TX_POWER_AT_1M - rssi) / (10 * PATH_LOSS_EXPONENT)) / bias
    if MIN_DISTANCE_M <= distance <= MAX_DISTANCE_M:
        return distance
    return None


def _solve_gains(
    points: list[list[float]],
    beacon_points: list[list[float]],
    gains: list[float],
    readings: list[tuple[int, int, float]],
    links: list[tuple[int, int, float]],
    radio_count: int,
) -> list[float]:
    """Closed-form gain update: each radio's median residual against the model.

    RSSI is linear in the gain term, so the best offset for a radio is how far
    its readings sit from the model. A median rather than a mean keeps one
    wall-blocked beacon from dragging a radio's whole calibration.
    """
    per_radio: list[list[float]] = [[] for _ in range(radio_count)]

    for radio, beacon, observed in readings:
        distance = max(
            MIN_SEPARATION_M, math.dist(points[radio], beacon_points[beacon])
        )
        predicted = TX_POWER_AT_1M - 10 * PATH_LOSS_EXPONENT * math.log10(distance)
        per_radio[radio].append(observed - predicted)

    for listener, advertiser, observed in links:
        distance = max(
            MIN_SEPARATION_M, math.dist(points[listener], points[advertiser])
        )
        predicted = TX_POWER_AT_1M - 10 * PATH_LOSS_EXPONENT * math.log10(distance)
        # A link carries both radios' gain, so split the discrepancy between them.
        half = (observed - predicted) / 2
        per_radio[listener].append(half)
        per_radio[advertiser].append(half)

    updated = [
        _median(values) if values else gains[i] for i, values in enumerate(per_radio)
    ]
    updated = [max(-MAX_GAIN_DB, min(MAX_GAIN_DB, value)) for value in updated]

    # Hold the gains to zero mean: a constant added to all of them is
    # indistinguishable from scaling every distance.
    mean_gain = sum(updated) / len(updated)
    return [value - mean_gain for value in updated]


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _shared_beacons(
    radios: Sequence[str], observations: dict[str, dict[str, float]]
) -> list[str]:
    """Beacons heard by enough radios to constrain anything."""
    counts: dict[str, int] = {}
    for radio in radios:
        for beacon in observations.get(radio, {}):
            counts[beacon] = counts.get(beacon, 0) + 1
    return sorted(b for b, count in counts.items() if count >= MIN_RADIOS_PER_BEACON)


def _initial_beacon_position(
    radios: Sequence[str],
    observations: dict[str, dict[str, float]],
    beacon: str,
    points: list[list[float]],
    rng: random.Random,
) -> list[float]:
    """Start a beacon at the signal-weighted centroid of the radios hearing it."""
    total = 0.0
    position = [0.0, 0.0, 0.0]
    for i, radio in enumerate(radios):
        if (rssi := observations.get(radio, {}).get(beacon)) is None:
            continue
        # Louder means closer, so weight by strength above the noise floor.
        weight = max(0.1, rssi + 100.0)
        total += weight
        for axis in range(3):
            position[axis] += weight * points[i][axis]
    if total <= 0:
        return [rng.uniform(-1, 1) for _ in range(3)]
    # Jitter breaks the symmetry of beacons that start on top of each other.
    return [position[axis] / total + rng.uniform(-0.5, 0.5) for axis in range(3)]


def _rms_residual(
    points: list[list[float]],
    beacon_points: list[list[float]],
    gains: list[float],
    readings: list[tuple[int, int, float]],
    links: list[tuple[int, int, float]],
) -> float:
    """RMS of the dB residuals: fit quality in the units it was measured in."""
    total = 0.0
    count = 0
    for radio, beacon, observed in readings:
        distance = max(
            MIN_SEPARATION_M, math.dist(points[radio], beacon_points[beacon])
        )
        predicted = (
            TX_POWER_AT_1M
            + gains[radio]
            - 10 * PATH_LOSS_EXPONENT * math.log10(distance)
        )
        total += (observed - predicted) ** 2
        count += 1
    for listener, advertiser, observed in links:
        distance = max(
            MIN_SEPARATION_M, math.dist(points[listener], points[advertiser])
        )
        predicted = (
            TX_POWER_AT_1M
            + gains[listener]
            + gains[advertiser]
            - 10 * PATH_LOSS_EXPONENT * math.log10(distance)
        )
        total += (observed - predicted) ** 2
        count += 1
    return math.sqrt(total / count) if count else 0.0
