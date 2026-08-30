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
geometry._orientation.
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
# than anything RSSI can say about the vertical axis. This weights the "same
# floor, same height" spring against a radio's observations; 1.0 makes it worth
# as much as everything else that radio hears put together.
#
# This is the soft half of the vertical model. CEILING_HEIGHT_M below is the
# hard half: the spring shapes where radios sit inside their storey, the clamp
# forbids them from leaving it. Chosen at 0.5 by a 12-seed sweep at realistic
# noise -- it beat every other value on both mean shape error (2.11 m) and, more
# tellingly, on the worst case (2.79 m against 3.83 m at 0.8), so it is mainly
# buying protection from bad solves rather than a better typical one.
#
# Note what this costs: within-floor height detail is flattened to a few
# centimetres against a real 1.6 m spread. That detail is not recoverable from
# RSSI anyway -- desk height versus shelf height is far below the noise -- and
# giving it up buys a materially better horizontal layout.
FLOOR_COHESION = 0.5

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

# A radio-to-radio link and a beacon reading are not the same measurement, and
# the objective was treating them as if they were. Weighting every observation
# by 1/d^2 is the maximum-likelihood choice only when they all share one sigma;
# they do not. A link has no unknown transmit power, both ends are mains-powered
# and cannot walk off, and it is averaged over two directions -- measured on this
# house it scatters 5.8 dB against 8.5 dB for a beacon reading.
#
# Generalised least squares says weight by 1/sigma^2, so the ratio of the two
# variances is the link's weight. That figure is measured from each fit rather
# than fixed here: an empirical sweep on one capture liked 3.0 and the measured
# ratio came out at 2.1, close enough that hard-coding either would be tuning to
# one house. The bounds only stop a degenerate fit running away -- with 18 links
# against 290 readings the ratio is estimated from few residuals.
MIN_LINK_TRUST = 1.0
MAX_LINK_TRUST = 6.0

ROUNDS = 40
SWEEPS_PER_ROUND = 8

# SMACOF is a local method, so the answer depends on where it starts. The
# convex-relaxation literature exists precisely because of this. Short of
# solving an SDP, restarting from perturbed seeds and keeping the best fit
# recovers most of the benefit: without it roughly one run in ten settles in a
# badly wrong minimum, which is what produced 13m outliers against a 2m median.
RESTARTS = 6
RESTART_JITTER = 0.4

# The previous layout is offered back as one more starting point. It is not a
# ratchet: the cold multi-restart search still runs in full every solve and can
# take over whenever it genuinely fits better, so nothing can latch onto a stale
# answer when a radio actually moves. No release condition is needed because the
# escape is always running.
#
# What it buys is stability. SMACOF is a local method, and two minima a hundredth
# of a dB apart are indistinguishable as fits but metres apart as layouts, so the
# best-of-six pick was free to alternate between them and the house visibly
# jumped. Measured before this: nudging the smoothed RSSI by a quarter of a dB
# -- less than the recorder smooths away between polls -- moved pairwise
# distances 1.03 m RMS, and the response was not even monotone in the size of the
# nudge, which is the signature of minimum-hopping rather than of sensitivity to
# the data.
#
# Hence the margin: keep last solve's answer unless a fresh one beats it by more
# than this fraction. Ties go to not moving.
WARM_HYSTERESIS = 0.02

# A floor between transmitter and receiver costs signal that the path-loss model
# would otherwise book as distance, pushing storeys apart. Measured on a real
# two-storey house, ignoring it left cross-floor pairs +3.0 m too far apart while
# same-floor pairs were unbiased.
#
# This is a fixed prior, NOT fitted, and that is deliberate. The penalty and the
# vertical separation are degenerate: raising the penalty while collapsing the
# storey gap produces an identical fit, so a free fit always over-attributes to
# attenuation. Left uncapped it settles at 8 dB and over-corrects the layout to
# 0.83x true scale.
#
# A beacon's own storey is unknown, so it is taken from whichever radio floor its
# solved height sits nearest. That is circular -- the vertical axis feeding the
# term meant to fix the vertical axis -- and correcting only the radio-to-radio
# links, where both storeys are certain, avoids it. That variant was tried: it
# scores marginally better on this house and far worse on synthetic data,
# because beacon paths outnumber links by an order of magnitude and leaving
# their floor loss uncorrected wrecks the gain estimates. The circular version
# wins on the evidence available.
#
# CAVEAT: validated against one house and one synthetic model that demonstrably
# disagree with each other. Treat 3 dB as a working figure, not a measured
# constant -- distinguishing candidates needs a better yardstick than room
# centres, which are only good to about +/- 1.5 m.
FLOOR_PENALTY_DB = 3.0

# Two facts about how houses are built, used as hard bounds on the vertical axis.
# They are generic American construction, not facts about any particular home, so
# they cost nothing in portability -- unlike a floor plan, which would make the
# solver accurate in one house and useless everywhere else.
#
# Both are applied as dead-zone projections: they do nothing until the layout
# leaves the range a building could actually occupy, and then move it only as far
# as the nearest edge. Nothing is asserted beyond "that answer is impossible".
#
# CEILING_HEIGHT_M caps how far apart two radios on one storey can sit
# vertically. 8 ft is the traditional US ceiling and the floor-to-ceiling
# distance is the absolute limit -- in practice these sit on desks and shelves
# and differ by well under half of it, so this only ever removes nonsense.
CEILING_HEIGHT_M = 2.4

# STOREY_PITCH_M is floor-to-floor: ceiling height plus the floor assembly
# (joists, subfloor, finish, ceiling drywall, about a foot). Two independent
# derivations agree. By component build-up: 8 ft ceiling + 2x10 joists = 2.73 m,
# 9 ft + deep I-joists = 3.11 m. By stair geometry -- a flight's total rise *is*
# the floor-to-floor height, no assumptions needed -- 14 to 15 risers at 7 to
# 7.75 in (the IRC cap) gives 2.7 to 2.95 m.
#
# The tolerance is deliberately wide. Real houses vary by about this much, and
# pinning the pitch exactly would assert more than the construction data
# supports. Treat 2.9 m as a working centre, not a measured constant.
STOREY_PITCH_M = 2.9
STOREY_PITCH_TOLERANCE_M = 0.4


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
    beacon_weights: dict[str, float] | None = None,
    links_worth: float = MIN_LINK_TRUST,
    warm_seed: list[list[float]] | None = None,
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
    # How far each beacon is trusted as a fixed landmark. Uniform when nothing
    # is known, which is what the tests and the offline scorer run with.
    weights_by_beacon = [
        (beacon_weights or {}).get(beacon, 1.0) for beacon in beacons
    ]
    links = [
        (index[listener], index[advertiser], sum(values) / len(values))
        for (listener, advertiser), values in direct_rssi.items()
        if listener in index and advertiser in index
    ]
    if not readings:
        return None

    levels = levels or {}
    floor_groups = _floor_groups(radios, levels)
    storeys = _storeys(radios, levels)
    radio_levels = [levels.get(radio) for radio in radios]
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

    starts: list[tuple[str, list[list[float]]]] = [("cold", seed_points)]
    if warm_seed is not None and len(warm_seed) == len(radios):
        starts.append(("warm", [point[:] for point in warm_seed]))
    for attempt in range(1, RESTARTS):
        rng = random.Random(attempt)
        starts.append((
            "jitter",
            [
                [value + rng.gauss(0, RESTART_JITTER * scale) for value in point]
                for point in seed_points
            ],
        ))

    best: tuple[
        float, list[list[float]], list[float], float, list[list[float]]
    ] | None = None
    warm_result: tuple[
        float, list[list[float]], list[float], float, list[list[float]]
    ] | None = None
    for attempt, (origin, start) in enumerate(starts):
        rng = random.Random(attempt)
        points = [point[:] for point in start]
        gains = [0.0] * len(radios)
        _constrain_storeys(points, storeys)
        beacon_points = _seed_beacons(radios, observations, beacons, points, rng)
        penalty = FLOOR_PENALTY_DB if any(
            level is not None for level in radio_levels
        ) else 0.0

        for _ in range(ROUNDS):
            gains = _solve_gains(
                points, beacon_points, gains, readings, links,
                len(radios), radio_levels, penalty,
            )
            for _ in range(SWEEPS_PER_ROUND):
                _majorize(
                    points, beacon_points, gains, readings, links,
                    floor_groups, bias, radio_levels, penalty,
                    weights_by_beacon, links_worth,
                )
                _constrain_storeys(points, storeys)
                _constrain_beacons(beacon_points, points, storeys)

        residual = _rms_residual(
            points, beacon_points, gains, readings, links, radio_levels, penalty
        )
        outcome = (
            residual,
            [p[:] for p in points],
            gains[:],
            penalty,
            [b[:] for b in beacon_points],
        )
        if best is None or residual < best[0]:
            best = outcome
        if origin == "warm":
            warm_result = outcome

    # Ties go to standing still. A fresh minimum has to be meaningfully better
    # than where the layout already is before the house is allowed to move.
    reused = False
    if warm_result is not None and warm_result[0] <= best[0] * (1 + WARM_HYSTERESIS):
        best = warm_result
        reused = True

    current, points, gains, penalty, beacon_points = best
    beacon_sigma, link_sigma = _class_sigmas(
        points, beacon_points, gains, readings, links, radio_levels, penalty
    )
    radios_per_beacon = len(readings) / len(beacons) if beacons else 0.0

    if current >= baseline:
        # Refinement is an optimisation, not an article of faith. If it did not
        # beat the seed layout, say so and let the caller keep the seed.
        return None

    return {
        "positions": [list(point) for point in points],
        "beacons": _beacon_report(
            beacons, beacon_points, points, readings, gains,
            current, radio_levels, penalty,
        ),
        "gains": {radio: round(gains[i], 1) for i, radio in enumerate(radios)},
        "residual_db": round(current, 2),
        "seed_residual_db": round(baseline, 2),
        "beacons_used": len(beacons),
        "beacon_sigma_db": round(beacon_sigma, 2),
        "link_sigma_db": round(link_sigma, 2),
        "links_worth": round(
            link_trust(beacon_sigma, link_sigma, radios_per_beacon), 2
        ),
        "observations": len(readings) + len(links),
        "shadowing_db": round(shadowing_db, 2),
        "bias_correction": round(bias, 3),
        "floor_penalty_db": round(penalty, 1),
        "restarts": len(starts),
        "reused_previous": reused,
    }


def _floors_apart(
    radio_level: float | None, other_level: float | None
) -> float:
    """Storeys between two endpoints, or 0 when either storey is unknown."""
    if radio_level is None or other_level is None:
        return 0.0
    return abs(radio_level - other_level)


def _beacon_levels(
    beacon_points: list[list[float]],
    points: list[list[float]],
    radio_levels: list[float | None],
) -> list[float | None]:
    """Assign each beacon the storey of whichever radio floor it sits nearest."""
    heights: dict[float, list[float]] = {}
    for point, level in zip(points, radio_levels):
        if level is not None:
            heights.setdefault(level, []).append(point[2])
    if len(heights) < 2:
        return [None] * len(beacon_points)
    means = {level: sum(v) / len(v) for level, v in heights.items()}
    return [
        min(means, key=lambda level: abs(means[level] - beacon[2]))
        for beacon in beacon_points
    ]




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


def _storeys(
    radios: Sequence[str], levels: dict[str, float]
) -> list[tuple[float, list[int]]]:
    """(level, radio indices) for every storey, lowest first.

    Unlike _floor_groups this keeps storeys holding a single radio, because one
    radio still has to sit a storey's height above the floor below it.
    """
    by_level: dict[float, list[int]] = {}
    for i, radio in enumerate(radios):
        if (level := levels.get(radio)) is not None:
            by_level.setdefault(level, []).append(i)
    return sorted(by_level.items())


def _constrain_storeys(
    points: list[list[float]], storeys: list[tuple[float, list[int]]]
) -> None:
    """Force the vertical axis into a shape a building could have.

    RSSI says almost nothing reliable about height: a floor between two radios
    eats signal that the path-loss model books as distance, so the vertical axis
    inflates and there is no term anywhere that knows how tall a storey is. Left
    alone this house solved to radios 6.3 m apart *on one floor* and storeys
    7.4 m apart -- both about 2.5x impossible.

    Two projections fix that, applied after every sweep:

    1. Radios on one storey are pulled inside a ceiling's height of their
       median. The median rather than the mean because the failure case is two
       outliers dragging a group, which is exactly what the mean follows.
    2. Adjacent storeys are held a storey's pitch apart, within tolerance.

    Only the vertical coordinate is touched. The horizontal spread that a link
    needs is then recovered by the next sweep on its own: a spring whose rest
    length is unchanged but whose vertical component just shrank must grow
    horizontally, since h = sqrt(d^2 - dz^2). Doing that redirection by hand
    would risk overshoot and lose the monotone convergence that makes projected
    majorization safe.
    """
    if not storeys:
        return

    half = CEILING_HEIGHT_M / 2
    for _level, group in storeys:
        centre = _median([points[i][2] for i in group])
        for i in group:
            points[i][2] = min(centre + half, max(centre - half, points[i][2]))

    # Walk upwards, carrying the shift already applied to the storeys below so
    # correcting one gap cannot silently break the one above it.
    carried = 0.0
    for index in range(1, len(storeys)):
        lower_level, lower = storeys[index - 1]
        upper_level, upper = storeys[index]
        for i in upper:
            points[i][2] += carried

        gap = _median([points[i][2] for i in upper]) - _median(
            [points[i][2] for i in lower]
        )
        # Levels need not be consecutive, so scale the target by how many
        # storeys actually separate them.
        between = max(1.0, abs(upper_level - lower_level))
        lowest = (STOREY_PITCH_M - STOREY_PITCH_TOLERANCE_M) * between
        highest = (STOREY_PITCH_M + STOREY_PITCH_TOLERANCE_M) * between

        correction = 0.0
        if gap > highest:
            correction = highest - gap
        elif gap < lowest:
            correction = lowest - gap
        if correction:
            for i in upper:
                points[i][2] += correction
            carried += correction


def _constrain_beacons(
    beacon_points: list[list[float]],
    points: list[list[float]],
    storeys: list[tuple[float, list[int]]],
) -> None:
    """Hold beacons inside the building's vertical extent.

    Constraining only the radios moves the problem rather than solving it. The
    path-loss model still books cross-floor attenuation as distance, and once
    the radios are pinned that excess goes somewhere else -- into the beacons,
    which were landing 8 m below the ground floor and 10 m above the roof.
    Measured effect: flattening the radios alone made beacon positions *worse*
    than parking each beacon at the centroid of the radios hearing it.

    A beacon heard by three radios is inside the house, so it lies between the
    lowest storey's floor and the highest storey's ceiling. Radios sit somewhere
    within their own storey, so their median height is half a ceiling below that
    storey's ceiling and half above its floor, which sets the envelope.
    """
    if not storeys:
        return
    half = CEILING_HEIGHT_M / 2
    floors = [_median([points[i][2] for i in group]) for _level, group in storeys]
    lowest = min(floors) - half
    highest = max(floors) + half
    for beacon in beacon_points:
        beacon[2] = min(highest, max(lowest, beacon[2]))


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
    radio_levels: list[float | None] | None = None,
    floor_penalty: float = 0.0,
    beacon_weights: list[float] | None = None,
    links_worth: float = 1.0,
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
    beacon_pulls = [0.0] * len(beacon_points)

    levels = radio_levels or [None] * len(points)
    beacon_levels = (
        _beacon_levels(beacon_points, points, levels)
        if floor_penalty
        else [None] * len(beacon_points)
    )

    for radio, beacon, observed in readings:
        # Add the floor loss back before inverting: it is attenuation, not range.
        crossed = floor_penalty * _floors_apart(levels[radio], beacon_levels[beacon])
        rest = _rest_length(observed - gains[radio] + crossed, bias)
        if rest is None:
            continue
        trust = beacon_weights[beacon] if beacon_weights else 1.0
        _pull(
            points[radio], beacon_points[beacon], rest,
            targets[radio], weights, radio, trust,
        )
        # Scaling the beacon's own side too is a no-op for where the beacon
        # lands -- every one of its springs carries the same factor, so it
        # cancels in its weighted average -- but it keeps the two sides
        # symmetric and costs nothing.
        _pull(
            beacon_points[beacon], points[radio], rest,
            beacon_targets[beacon], beacon_pulls, beacon, trust,
        )

    for listener, advertiser, observed in links:
        crossed = floor_penalty * _floors_apart(
            levels[listener], levels[advertiser]
        )
        rest = _rest_length(
            observed - gains[listener] - gains[advertiser] + crossed, bias
        )
        if rest is None:
            continue
        _pull(
            points[listener], points[advertiser], rest,
            targets[listener], weights, listener, links_worth,
        )
        _pull(
            points[advertiser], points[listener], rest,
            targets[advertiser], weights, advertiser, links_worth,
        )

    # Radios on one storey are at the same height. Pull each towards its floor's
    # mean as a spring alongside the observations. _constrain_storeys applies the
    # hard bound after the sweep; this only shapes the distribution inside it.
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
    for b, weight in enumerate(beacon_pulls):
        if weight > 0:
            beacon_points[b] = [value / weight for value in beacon_targets[b]]


def _pull(
    point: list[float],
    neighbour: list[float],
    rest: float,
    target: list[float],
    weights: list[float],
    index: int,
    trust: float = 1.0,
) -> None:
    """Accumulate one spring's preferred position for `point`.

    `trust` scales the whole spring by how good a landmark the other end is, so
    a mains-powered light fitting bends the layout more than a tracker in
    someone's pocket. See quality.py for where the number comes from.
    """
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
    weight *= trust

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
    radio_levels: list[float | None] | None = None,
    floor_penalty: float = 0.0,
) -> list[float]:
    """Closed-form gain update: each radio's median residual against the model.

    RSSI is linear in the gain term, so the best offset for a radio is how far
    its readings sit from the model. A median rather than a mean keeps one
    wall-blocked beacon from dragging a radio's whole calibration.
    """
    per_radio: list[list[float]] = [[] for _ in range(radio_count)]
    levels = radio_levels or [None] * radio_count
    beacon_levels = (
        _beacon_levels(beacon_points, points, levels)
        if floor_penalty
        else [None] * len(beacon_points)
    )

    for radio, beacon, observed in readings:
        distance = max(
            MIN_SEPARATION_M, math.dist(points[radio], beacon_points[beacon])
        )
        predicted = (
            TX_POWER_AT_1M
            - 10 * PATH_LOSS_EXPONENT * math.log10(distance)
            - floor_penalty * _floors_apart(levels[radio], beacon_levels[beacon])
        )
        per_radio[radio].append(observed - predicted)

    for listener, advertiser, observed in links:
        distance = max(
            MIN_SEPARATION_M, math.dist(points[listener], points[advertiser])
        )
        predicted = (
            TX_POWER_AT_1M
            - 10 * PATH_LOSS_EXPONENT * math.log10(distance)
            - floor_penalty * _floors_apart(levels[listener], levels[advertiser])
        )
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


def _class_sigmas(
    points, beacon_points, gains, readings, links,
    radio_levels=None, floor_penalty=0.0,
):
    """RMS dB residual of beacon readings and of direct links, separately."""
    levels = radio_levels or [None] * len(points)
    beacon_levels = (
        _beacon_levels(beacon_points, points, levels)
        if floor_penalty
        else [None] * len(beacon_points)
    )
    beacon_sq = []
    for radio, beacon, observed in readings:
        distance = max(
            MIN_SEPARATION_M, math.dist(points[radio], beacon_points[beacon])
        )
        predicted = (
            TX_POWER_AT_1M
            + gains[radio]
            - 10 * PATH_LOSS_EXPONENT * math.log10(distance)
            - floor_penalty * _floors_apart(levels[radio], beacon_levels[beacon])
        )
        beacon_sq.append((observed - predicted) ** 2)

    link_sq = []
    for listener, advertiser, observed in links:
        distance = max(
            MIN_SEPARATION_M, math.dist(points[listener], points[advertiser])
        )
        predicted = (
            TX_POWER_AT_1M
            + gains[listener]
            + gains[advertiser]
            - 10 * PATH_LOSS_EXPONENT * math.log10(distance)
            - floor_penalty * _floors_apart(levels[listener], levels[advertiser])
        )
        link_sq.append((observed - predicted) ** 2)

    def rms(values):
        return math.sqrt(sum(values) / len(values)) if values else 0.0

    return rms(beacon_sq), rms(link_sq)


def link_trust(
    beacon_sigma: float, link_sigma: float, radios_per_beacon: float
) -> float:
    """How much more a direct link is worth than a beacon reading.

    Two independent reasons, multiplied, both measured rather than chosen.

    A link scatters less. No unknown transmit power, both ends mains-powered
    and immobile, and it is averaged over two directions. Generalised least
    squares weights by 1/sigma^2, so the variance ratio is the first factor --
    about 1.7 on this house.

    A beacon reading is also worth less than it looks, which is the larger
    effect. A beacon carries its own three unknown coordinates, so of the k
    readings it contributes, three are spent pinning the beacon itself and only
    k - 3 say anything about where the radios are. At k = 5.8 that is 2.8 of
    5.8, a factor of 2.1. A link spends nothing: both of its endpoints are
    already in the solve.

    Together they derive ~3.5 on this capture. An empirical sweep independently
    liked 3.0, which is the agreement worth having -- the constant is computed
    from each fit, so a house with more radios per beacon gets a smaller one.
    """
    if link_sigma <= 0 or beacon_sigma <= 0 or radios_per_beacon <= 3:
        return MIN_LINK_TRUST
    scatter = (beacon_sigma / link_sigma) ** 2
    redundancy = radios_per_beacon / (radios_per_beacon - 3)
    return max(MIN_LINK_TRUST, min(MAX_LINK_TRUST, scatter * redundancy))


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _beacon_report(
    beacons: Sequence[str],
    beacon_points: list[list[float]],
    points: list[list[float]],
    readings: list[tuple[int, int, float]],
    gains: list[float],
    sigma_db: float,
    radio_levels: list[float | None],
    floor_penalty: float,
) -> list[dict[str, Any]]:
    """Describe each solved beacon, with how far it can be trusted.

    The position itself is a by-product: the joint solve has to place beacons in
    order to use them, and until now it threw them away. What it does not come
    with is any statement of confidence, and beacons need one far more than
    radios do. A radio is pinned by every beacon in the house -- sixty-odd
    observations -- whereas a beacon is pinned by however many radios happen to
    hear it, often exactly three. Three ranges and three unknowns is a fit with
    no redundancy at all: it will pass through the data exactly and tell you
    nothing about whether it is right.

    So each beacon carries a radius. Patwari's bound gives the range error as a
    constant *fraction* of distance, sigma_d/d = (ln10/10)(sigma_dB/n), and
    trilaterating k of them recovers at best a sqrt(3/k) improvement over one.
    Hence radius ~ fraction * mean_distance * sqrt(3/k).

    Two honest caveats. The bound assumes favourable geometry: a beacon outside
    the hull of the radios that hear it has a dilution of precision above one,
    sometimes far above, and this does not compute that. And sigma comes from
    the global fit rather than the beacon's own residual, because three samples
    cannot estimate a standard deviation. The radius is therefore a lower bound
    on the error -- the beacon is at least this uncertain.
    """
    by_beacon: dict[int, list[tuple[int, float]]] = {}
    for radio, beacon, observed in readings:
        by_beacon.setdefault(beacon, []).append((radio, observed))

    levels = _beacon_levels(beacon_points, points, radio_levels)
    fraction = (math.log(10) / (10 * PATH_LOSS_EXPONENT)) * sigma_db

    report = []
    for b, address in enumerate(beacons):
        heard = by_beacon.get(b, [])
        if not heard:
            continue
        point = beacon_points[b]

        distances = []
        squared = 0.0
        for radio, observed in heard:
            distance = max(MIN_SEPARATION_M, math.dist(points[radio], point))
            distances.append(distance)
            predicted = (
                TX_POWER_AT_1M
                + gains[radio]
                - 10 * PATH_LOSS_EXPONENT * math.log10(distance)
                - floor_penalty * _floors_apart(radio_levels[radio], levels[b])
            )
            squared += (observed - predicted) ** 2

        k = len(heard)
        mean_distance = sum(distances) / k
        report.append(
            {
                "address": address,
                "x": round(point[0], 2),
                "y": round(point[1], 2),
                "z": round(point[2], 2),
                "radios": k,
                "nearest_m": round(min(distances), 2),
                "residual_db": round(math.sqrt(squared / k), 2),
                "uncertainty_m": round(
                    fraction * mean_distance * math.sqrt(3.0 / k), 2
                ),
            }
        )
    return report


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
    radio_levels: list[float | None] | None = None,
    floor_penalty: float = 0.0,
) -> float:
    """RMS of the dB residuals: fit quality in the units it was measured in."""
    total = 0.0
    count = 0
    levels = radio_levels or [None] * len(points)
    beacon_levels = (
        _beacon_levels(beacon_points, points, levels)
        if floor_penalty
        else [None] * len(beacon_points)
    )
    for radio, beacon, observed in readings:
        distance = max(
            MIN_SEPARATION_M, math.dist(points[radio], beacon_points[beacon])
        )
        predicted = (
            TX_POWER_AT_1M
            + gains[radio]
            - 10 * PATH_LOSS_EXPONENT * math.log10(distance)
            - floor_penalty * _floors_apart(levels[radio], beacon_levels[beacon])
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
            - floor_penalty * _floors_apart(levels[listener], levels[advertiser])
        )
        total += (observed - predicted) ** 2
        count += 1
    return math.sqrt(total / count) if count else 0.0
