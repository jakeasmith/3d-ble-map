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
) -> dict[str, Any]:
    """Estimate a relative 3D position for each anchor.

    anchors: anchor ids, in the order positions should be returned.
    direct_rssi: (listener, advertiser) -> RSSI readings for anchors that hear
        each other. Both directions are averaged when both exist.
    observations: anchor id -> {beacon address: RSSI} for shared beacons.
    levels: anchor id -> building floor level, used only to orient the result.
    """
    if len(anchors) < 3:
        return {
            "positions": {},
            "pairs": [],
            "stress": None,
            "error": "At least 3 anchors are needed to estimate a layout.",
        }

    pairs = _estimate_pairs(anchors, direct_rssi, observations)
    matrix, missing = _distance_matrix(anchors, pairs)
    if missing:
        return {
            "positions": {},
            "pairs": pairs,
            "stress": None,
            "error": (
                f"No distance estimate for {missing} anchor pair(s). They neither "
                "hear each other nor share enough beacons."
            ),
        }

    coords = _classical_mds(matrix, dimensions=3)
    coords = _orient_by_level(anchors, coords, levels or {})

    return {
        "positions": {
            anchor: {"x": round(p[0], 2), "y": round(p[1], 2), "z": round(p[2], 2)}
            for anchor, p in zip(anchors, coords)
        },
        "pairs": pairs,
        "stress": _stress(matrix, coords),
        "error": None,
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


def _orient_by_level(
    anchors: Sequence[str], coords: list[list[float]], levels: dict[str, float]
) -> list[list[float]]:
    """Rotate so the axis separating building floors points up.

    MDS returns an arbitrary rotation. Home Assistant knows which floor each
    anchor is on, so use that to pick one -- it does not change the geometry,
    only which way up it is drawn.
    """
    coords = _centre(coords)
    grouped: dict[float, list[list[float]]] = {}
    for anchor, position in zip(anchors, coords):
        if (level := levels.get(anchor)) is not None:
            grouped.setdefault(level, []).append(position)

    if len(grouped) < 2:
        return coords

    ordered = sorted(grouped)
    lower = _centroid(grouped[ordered[0]])
    upper = _centroid(grouped[ordered[-1]])
    up = [upper[axis] - lower[axis] for axis in range(3)]

    if (norm := math.sqrt(sum(value * value for value in up))) < 1e-9:
        return coords
    up = [value / norm for value in up]

    right = _orthonormal_to(up)
    forward = _cross(up, right)
    return [
        [_dot(position, right), _dot(position, forward), _dot(position, up)]
        for position in coords
    ]


def _centre(coords: list[list[float]]) -> list[list[float]]:
    centroid = _centroid(coords)
    return [[p[axis] - centroid[axis] for axis in range(3)] for p in coords]


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
