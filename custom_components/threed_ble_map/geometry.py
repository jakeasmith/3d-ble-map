"""Turn RSSI observations into a rough relative 3D layout of the anchors.

The pipeline is:

1. Estimate a distance for every anchor pair. Anchors that advertise are heard
   directly by their neighbours, which is the good measurement. Pairs with no
   direct link fall back to how similarly they hear the beacons they share,
   scaled against the pairs that do have a direct link.
2. Embed those distances in 3D with classical multidimensional scaling.
3. Rotate the result so the axis separating the building's floors is vertical,
   because MDS itself returns an arbitrary orientation.

Everything here is pure Python: the matrices are one row per anchor, so a
hand-rolled Jacobi eigensolver is cheaper than taking on a numpy dependency.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

# Log-distance path loss. -59 dBm at 1 m is the usual BLE reference; 2.5 is a
# reasonable indoor exponent and matches what this house measured.
TX_POWER_AT_1M = -59.0
PATH_LOSS_EXPONENT = 2.5

# Distances outside this range are treated as unusable rather than clamped
# silently, so a bad link shows up as a missing pair instead of a wrong one.
MIN_DISTANCE_M = 0.3
MAX_DISTANCE_M = 60.0

# A co-observation estimate needs enough shared beacons to mean anything.
MIN_SHARED_BEACONS = 5

# Below this, a direct reading says more about what is in the way than about
# distance: a wall or a floor costs tens of dB, and the path-loss model books
# all of it as range. Measured here, a -96 dBm link between two anchors came out
# at 26 m in a house whose longest span is nowhere near that, violating the
# triangle inequality against its neighbours by 15 m. Weak links fall back to
# the co-observation estimate, which is calibrated on the strong ones.
RELIABLE_RSSI_FLOOR = -90.0

# Fallback metres-per-dB if there are too few direct links to calibrate against.
DEFAULT_DB_TO_M = 0.45


def rssi_to_distance(rssi: float) -> float | None:
    """Convert an RSSI reading to a distance in metres, or None if implausible."""
    distance = 10 ** ((TX_POWER_AT_1M - rssi) / (10 * PATH_LOSS_EXPONENT))
    if MIN_DISTANCE_M <= distance <= MAX_DISTANCE_M:
        return distance
    return None


def solve_layout(
    anchors: Sequence[str],
    direct_rssi: dict[tuple[str, str], list[float]],
    observations: dict[str, dict[str, float]],
    levels: dict[str, float] | None = None,
    beacon_weights: dict[str, float] | None = None,
    previous: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Estimate a relative 3D position for each anchor.

    anchors: anchor ids, in the order positions should be returned.
    direct_rssi: (listener, advertiser) -> RSSI readings for anchors that hear
        each other. Both directions are averaged when both exist.
    observations: anchor id -> {beacon address: RSSI} for shared beacons.
    levels: anchor id -> building floor level, used only to orient the result.
    beacon_weights: beacon address -> how far to trust it as a fixed landmark,
        from quality.py. Absent or missing entries are trusted fully.
    previous: the last layout this house solved to, offered back to the refiner
        as one more starting point so an unchanged house stops jumping between
        equally-good minima. Ignored unless it covers exactly these anchors.
    """
    if len(anchors) < 3:
        return _empty("At least 3 anchors are needed to estimate a layout.")

    pairs = _estimate_pairs(anchors, direct_rssi, observations)
    matrix, missing = _distance_matrix(anchors, pairs)
    if missing:
        return _empty(
            f"No distance estimate for {missing} anchor pair(s). They neither "
            "hear each other nor share enough beacons.",
            pairs=pairs,
        )

    coords = _classical_mds(matrix, dimensions=3)
    # Stress describes the pairwise stage, so measure it before refinement
    # moves the points off the pairwise estimates and onto the raw readings.
    stress = _stress(matrix, coords)

    # Orient *before* refining, not just after. MDS returns an arbitrary
    # rotation, so until this runs the third coordinate is just some axis --
    # and refine.py constrains that coordinate against how tall a storey is,
    # which is only meaningful once it actually points up. Rotating first costs
    # nothing (it changes no distance) and makes the constraint real.
    coords = _apply_orientation(_orientation(anchors, coords, levels or {}), coords)

    # The pairwise fit assumes every radio reads RSSI the same way. Refine it
    # against the raw observations, solving each radio's gain at the same time.
    from .refine import refine_layout

    floors = _censor_floors(anchors, observations)

    seed = {
        anchor: {"x": p[0], "y": p[1], "z": p[2]}
        for anchor, p in zip(anchors, coords)
    }
    # Two passes. The first measures how much shadowing there is; the second
    # uses that to undo the log-normal bias, which at a real house's noise level
    # inflates every distance by about 30%. Estimating it from the fit is the
    # only option -- nothing else in the system knows the shadowing figure.
    warm_seed = None
    if previous and all(anchor in previous for anchor in anchors):
        warm_seed = [
            [previous[a]["x"], previous[a]["y"], previous[a]["z"]] for a in anchors
        ]

    refined = refine_layout(
        anchors, seed, observations, direct_rssi, levels,
        beacon_weights=beacon_weights,
        warm_seed=warm_seed,
        censor_floors=floors,
    )
    if refined is not None:
        corrected = refine_layout(
            anchors,
            seed,
            observations,
            direct_rssi,
            levels,
            shadowing_db=_shadowing_sigma(refined),
            beacon_weights=beacon_weights,
            # The first pass measures how far each kind of observation actually
            # scatters; the second weights them by it. Same shape as the
            # shadowing correction above -- nothing else knows these figures
            # until a fit has been run once.
            links_worth=refined["links_worth"],
            warm_seed=warm_seed,
            censor_floors=floors,
        )
        if corrected is not None:
            refined = corrected
        coords = refined["positions"]

    # Only centre here -- do NOT rotate again. Refinement holds the storeys
    # apart along z, so the layout comes back already the right way up, and
    # _orientation would undo that: it takes "up" to be the vector between floor
    # centroids, which is mostly *horizontal* whenever the two storeys are not
    # stacked directly on top of each other. That re-rotation turned a layout
    # whose floors were flat to 0.02 m into one spread over 10 m.
    transform = (_centroid(coords), None)
    coords = _apply_orientation(transform, coords)
    beacons = _oriented_beacons(refined, transform)

    return {
        "positions": {
            anchor: {"x": round(p[0], 2), "y": round(p[1], 2), "z": round(p[2], 2)}
            for anchor, p in zip(anchors, coords)
        },
        "beacons": beacons,
        "pairs": pairs,
        "stress": stress,
        "gains": refined["gains"] if refined else {},
        "residual_db": refined["residual_db"] if refined else None,
        "beacons_used": refined["beacons_used"] if refined else 0,
        "shadowing_db": refined["shadowing_db"] if refined else None,
        "beacon_sigma_db": refined["beacon_sigma_db"] if refined else None,
        "link_sigma_db": refined["link_sigma_db"] if refined else None,
        "links_worth": refined["links_worth"] if refined else None,
        "floor_penalty_db": refined["floor_penalty_db"] if refined else None,
        "bias_correction": refined["bias_correction"] if refined else None,
        "reused_previous": bool(refined and refined["reused_previous"]),
        "refined": refined is not None,
        "error": None,
    }


# A radio needs this many readings before the shape of its own RSSI histogram
# means anything.
MIN_READINGS_FOR_FLOOR = 20


def _censor_floors(
    anchors: Sequence[str], observations: dict[str, dict[str, float]]
) -> list[float] | None:
    """Estimate, per radio, the RSSI below which its readings stop being fair.

    A receiver has a sensitivity limit. Packets weaker than it are not heard at
    all, so the weak readings that do arrive are the ones a favourable fade
    lifted over the bar -- biased strong, and read as closer than true.

    The limit shows up as the mode of the radio's own RSSI histogram: an
    uncensored population tails off smoothly, while a censored one piles up
    against the wall and then cliffs. Measured here the mode sits at -94 to
    -98 dBm across eight radios of three different kinds, tightly clustered
    despite completely different placements, which is what makes it a property
    of the receivers rather than of where they happen to sit.

    Taking it from each radio's own data rather than fixing a number keeps this
    working on hardware with a different sensitivity -- and on synthetic data,
    which models no receiver floor at all and where the mode lands mid-histogram
    with a long tail below it.
    """
    floors = []
    for anchor in anchors:
        values = list(observations.get(anchor, {}).values())
        if len(values) < MIN_READINGS_FOR_FLOOR:
            floors.append(-999.0)
            continue
        histogram: dict[int, int] = {}
        for value in values:
            bucket = int(value // 2) * 2
            histogram[bucket] = histogram.get(bucket, 0) + 1
        floors.append(float(max(histogram, key=lambda b: (histogram[b], b))))
    return floors if any(f > -999.0 for f in floors) else None


def _shadowing_sigma(refined: dict[str, Any]) -> float:
    """Estimate the shadowing sigma the layout was actually generated with.

    The bias correction needs the *shadowing* -- how far a reading strays from
    the path-loss model -- but the only thing measurable after a fit is the
    residual, which is smaller. A fit spends its parameters absorbing exactly
    the deviations it is being asked to measure: every beacon carries three free
    coordinates, so a beacon heard by four radios can move to soak up most of
    its own shadowing and report almost none.

    That gap is not small here. This house fits 171 parameters against 308
    observations, leaving 137 degrees of freedom, so the residual understates
    sigma by sqrt(308/137) = 1.5x. Under-estimating sigma under-corrects the
    log-normal bias, which is why the map solved 1.74x too large while the
    correction meant to prevent that was running.

    The unbiased estimator is the textbook one, sigma^2 = RSS/(n - p):

        3 per radio and 3 per beacon for position, minus the 6 rigid motions
        that no amount of data can pin, plus one gain per radio less the one
        removed by holding the gains to zero mean.

    It is an approximation. The storey projections in refine.py remove freedom
    that this does not count, so p is a slight over-estimate and the correction
    slightly aggressive; MAX_SHADOWING_DB is what stops that running away.
    """
    n = refined["observations"]
    radios = len(refined["positions"])
    beacons = refined["beacons_used"]
    p = 3 * (radios + beacons) - 6 + (radios - 1)
    if n - p <= 1:
        # Too little redundancy to say anything; the raw residual is all there is.
        return refined["residual_db"]
    return refined["residual_db"] * math.sqrt(n / (n - p))


def _oriented_beacons(
    refined: dict[str, Any] | None,
    transform: tuple[list[float], list[list[float]] | None],
) -> list[dict[str, Any]]:
    """Move the solved beacons into the same frame as the radios."""
    if not refined:
        return []
    reported = refined.get("beacons") or []
    moved = _apply_orientation(
        transform, [[b["x"], b["y"], b["z"]] for b in reported]
    )
    return [
        {**beacon, "x": round(p[0], 2), "y": round(p[1], 2), "z": round(p[2], 2)}
        for beacon, p in zip(reported, moved)
    ]


def _empty(error: str, pairs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A result carrying the same keys as a solved one, so callers need no guards."""
    return {
        "positions": {},
        "beacons": [],
        "pairs": pairs or [],
        "stress": None,
        "gains": {},
        "residual_db": None,
        "beacons_used": 0,
        "shadowing_db": None,
        "beacon_sigma_db": None,
        "link_sigma_db": None,
        "links_worth": None,
        "floor_penalty_db": None,
        "bias_correction": None,
        "reused_previous": False,
        "refined": False,
        "error": error,
    }


def _estimate_pairs(
    anchors: Sequence[str],
    direct_rssi: dict[tuple[str, str], list[float]],
    observations: dict[str, dict[str, float]],
) -> list[dict[str, Any]]:
    """Build one distance estimate per anchor pair, direct link preferred."""
    similarity: dict[tuple[str, str], tuple[float, int]] = {}
    for index, first in enumerate(anchors):
        for second in anchors[index + 1 :]:
            result = _rssi_similarity(
                observations.get(first, {}), observations.get(second, {})
            )
            if result:
                similarity[(first, second)] = result

    direct: dict[tuple[str, str], float] = {}
    for index, first in enumerate(anchors):
        for second in anchors[index + 1 :]:
            readings = [
                *direct_rssi.get((first, second), []),
                *direct_rssi.get((second, first), []),
            ]
            # Links are asymmetric by a few dB, so average the directions
            # rather than trusting whichever one happened to be louder.
            usable = [r for r in readings if r >= RELIABLE_RSSI_FLOOR]
            if not usable:
                continue
            if (distance := rssi_to_distance(sum(usable) / len(usable))) is not None:
                direct[(first, second)] = distance

    scale = _calibrate(direct, similarity)

    pairs = []
    for index, first in enumerate(anchors):
        for second in anchors[index + 1 :]:
            key = (first, second)
            shared = similarity.get(key)
            if key in direct:
                pairs.append(
                    {
                        "a": first,
                        "b": second,
                        "distance": round(direct[key], 2),
                        "method": "direct",
                        "shared_beacons": shared[1] if shared else 0,
                    }
                )
            elif shared:
                pairs.append(
                    {
                        "a": first,
                        "b": second,
                        "distance": round(shared[0] * scale, 2),
                        "method": "inferred",
                        "shared_beacons": shared[1],
                    }
                )
    return pairs


def _rssi_similarity(
    first: dict[str, float], second: dict[str, float]
) -> tuple[float, int] | None:
    """RMS RSSI difference over the beacons two anchors both hear.

    Two anchors standing next to each other hear the same beacon at nearly the
    same strength, so this rises with separation. It is a dissimilarity in dB,
    not a distance; _calibrate turns it into metres.
    """
    shared = first.keys() & second.keys()
    if len(shared) < MIN_SHARED_BEACONS:
        return None
    total = sum((first[address] - second[address]) ** 2 for address in shared)
    return math.sqrt(total / len(shared)), len(shared)


def _calibrate(
    direct: dict[tuple[str, str], float],
    similarity: dict[tuple[str, str], tuple[float, int]],
) -> float:
    """Metres per dB of dissimilarity, fitted on pairs that have both measures."""
    points = [
        (similarity[key][0], distance)
        for key, distance in direct.items()
        if key in similarity
    ]
    if len(points) < 3:
        return DEFAULT_DB_TO_M

    # Least squares through the origin: at zero dissimilarity the anchors are
    # in the same place, so an intercept would only add a free parameter.
    numerator = sum(dissim * distance for dissim, distance in points)
    denominator = sum(dissim * dissim for dissim, _ in points)
    if denominator <= 0:
        return DEFAULT_DB_TO_M
    return numerator / denominator


def _distance_matrix(
    anchors: Sequence[str], pairs: Iterable[dict[str, Any]]
) -> tuple[list[list[float]], int]:
    """Square distance matrix, plus a count of pairs we could not estimate."""
    index = {anchor: position for position, anchor in enumerate(anchors)}
    size = len(anchors)
    matrix = [[0.0] * size for _ in range(size)]
    known = set()

    for pair in pairs:
        first, second = index[pair["a"]], index[pair["b"]]
        matrix[first][second] = matrix[second][first] = pair["distance"]
        known.add((first, second))

    expected = size * (size - 1) // 2
    return matrix, expected - len(known)


def _classical_mds(matrix: list[list[float]], dimensions: int) -> list[list[float]]:
    """Classical MDS: double-centre the squared distances, then eigendecompose."""
    size = len(matrix)
    squared = [[value * value for value in row] for row in matrix]

    row_means = [sum(row) / size for row in squared]
    grand_mean = sum(row_means) / size
    centred = [
        [
            -0.5 * (squared[i][j] - row_means[i] - row_means[j] + grand_mean)
            for j in range(size)
        ]
        for i in range(size)
    ]

    values, vectors = _jacobi_eigen(centred)
    order = sorted(range(size), key=lambda i: values[i], reverse=True)

    coords = []
    for point in range(size):
        position = []
        for axis in range(dimensions):
            if axis >= len(order):
                position.append(0.0)
                continue
            index = order[axis]
            # A negative eigenvalue means the distances are not quite Euclidean,
            # which is normal for noisy RSSI. That axis carries no real extent.
            value = values[index]
            position.append(vectors[index][point] * math.sqrt(value) if value > 0 else 0.0)
        coords.append(position)
    return coords


def _jacobi_eigen(
    matrix: list[list[float]], sweeps: int = 100, tolerance: float = 1e-10
) -> tuple[list[float], list[list[float]]]:
    """Eigenvalues and eigenvectors of a small symmetric matrix.

    Returns (values, vectors) where vectors[k] is the eigenvector for values[k].
    """
    size = len(matrix)
    work = [row[:] for row in matrix]
    basis = [[1.0 if i == j else 0.0 for j in range(size)] for i in range(size)]

    for _ in range(sweeps):
        pivot_row, pivot_col, largest = 0, 1, 0.0
        for i in range(size):
            for j in range(i + 1, size):
                if abs(work[i][j]) > largest:
                    largest, pivot_row, pivot_col = abs(work[i][j]), i, j
        if largest < tolerance:
            break

        p, q = pivot_row, pivot_col
        theta = (work[q][q] - work[p][p]) / (2 * work[p][q])
        sign = 1.0 if theta >= 0 else -1.0
        t = sign / (abs(theta) + math.sqrt(theta * theta + 1))
        cos = 1 / math.sqrt(t * t + 1)
        sin = t * cos

        for k in range(size):
            kp, kq = work[k][p], work[k][q]
            work[k][p] = cos * kp - sin * kq
            work[k][q] = sin * kp + cos * kq
        for k in range(size):
            pk, qk = work[p][k], work[q][k]
            work[p][k] = cos * pk - sin * qk
            work[q][k] = sin * pk + cos * qk
        for k in range(size):
            kp, kq = basis[k][p], basis[k][q]
            basis[k][p] = cos * kp - sin * kq
            basis[k][q] = sin * kp + cos * kq

    values = [work[i][i] for i in range(size)]
    vectors = [[basis[row][col] for row in range(size)] for col in range(size)]
    return values, vectors


def _orientation(
    anchors: Sequence[str], coords: list[list[float]], levels: dict[str, float]
) -> tuple[list[float], list[list[float]] | None]:
    """Pick a rigid motion that puts the building's floors roughly the right way up.

    MDS returns an arbitrary rotation, and refine.py constrains the third
    coordinate against how tall a storey is, which is only meaningful once that
    coordinate points up. This runs once, on the seed, to get it close.

    It is only an approximation: "up" is taken as the vector between the floors'
    centroids, so it tilts by however far the storeys are offset horizontally.
    That is good enough as a starting frame, because the storey constraint then
    takes over and effectively defines up for the rest of the solve. It is not
    good enough to apply *after* refinement, which is why it no longer is.
    """
    centroid = _centroid(coords)
    centred = [[p[axis] - centroid[axis] for axis in range(3)] for p in coords]

    grouped: dict[float, list[list[float]]] = {}
    for anchor, position in zip(anchors, centred):
        if (level := levels.get(anchor)) is not None:
            grouped.setdefault(level, []).append(position)

    if len(grouped) < 2:
        return centroid, None

    ordered = sorted(grouped)
    lower = _centroid(grouped[ordered[0]])
    upper = _centroid(grouped[ordered[-1]])
    up = [upper[axis] - lower[axis] for axis in range(3)]

    if (norm := math.sqrt(sum(value * value for value in up))) < 1e-9:
        return centroid, None
    up = [value / norm for value in up]

    right = _orthonormal_to(up)
    return centroid, [right, _cross(up, right), up]


def _apply_orientation(
    transform: tuple[list[float], list[list[float]] | None],
    points: list[list[float]],
) -> list[list[float]]:
    centroid, basis = transform
    centred = [[p[axis] - centroid[axis] for axis in range(3)] for p in points]
    if basis is None:
        return centred
    return [[_dot(position, axis) for axis in basis] for position in centred]


def _centroid(points: list[list[float]]) -> list[float]:
    count = len(points) or 1
    return [sum(p[axis] for p in points) / count for axis in range(3)]


def _orthonormal_to(vector: list[float]) -> list[float]:
    """Any unit vector perpendicular to the given one."""
    seed = [1.0, 0.0, 0.0] if abs(vector[0]) < 0.9 else [0.0, 1.0, 0.0]
    projection = _dot(seed, vector)
    candidate = [seed[axis] - projection * vector[axis] for axis in range(3)]
    norm = math.sqrt(sum(value * value for value in candidate))
    return [value / norm for value in candidate]


def _cross(a: list[float], b: list[float]) -> list[float]:
    return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _stress(matrix: list[list[float]], coords: list[list[float]]) -> float | None:
    """Kruskal stress-1: how far the embedded distances are from the estimates."""
    residual = 0.0
    total = 0.0
    for i in range(len(matrix)):
        for j in range(i + 1, len(matrix)):
            observed = matrix[i][j]
            fitted = math.dist(coords[i], coords[j])
            residual += (observed - fitted) ** 2
            total += observed**2
    if total <= 0:
        return None
    return round(math.sqrt(residual / total), 3)
