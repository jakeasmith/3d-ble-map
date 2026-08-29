"""Checks for the layout solver, run against synthetic ground truth.

There is no way to check the solver against the real house -- nobody has surveyed
it -- so instead place anchors at known coordinates, simulate the RSSI they would
report, and measure how well the recovered shape matches.

Run with: python3 tests/test_geometry.py
"""

from __future__ import annotations

import importlib.util
import math
import random
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "custom_components" / "threed_ble_map"


def _load(name: str):
    """Load one module of the integration without importing Home Assistant.

    geometry and refine import each other relatively, so they need a package to
    live in; importing the real package would pull in __init__.py and all of HA.
    """
    if "tbm" not in sys.modules:
        package = types.ModuleType("tbm")
        package.__path__ = [str(SRC)]
        sys.modules["tbm"] = package
    spec = importlib.util.spec_from_file_location(f"tbm.{name}", SRC / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"tbm.{name}"] = module
    spec.loader.exec_module(module)
    return module


geometry = _load("geometry")
refine = _load("refine")

# A two-storey house, metres. Mirrors the real deployment: five anchors, three
# of which advertise and so can be heard directly by the others.
#
# Radios on one storey sit at deliberately different heights -- on a shelf, on a
# desk, on the floor. The solver leans on "same floor, same height" as a prior,
# so the truth must not satisfy it exactly or the tests would only prove the
# prior agrees with itself.
TRUTH = {
    "garage": (0.0, 0.0, 0.0),
    "office": (8.0, 1.0, 0.9),
    "dining": (5.0, 7.0, 1.6),
    "mainbed": (7.0, 5.0, 3.2),
    "stretch": (2.0, 6.0, 4.0),
}
LEVELS = {"garage": 1, "office": 1, "dining": 1, "mainbed": 2, "stretch": 2}
ADVERTISERS = ["office", "dining", "stretch"]
NOISE_DB = 2.0
BEACON_COUNT = 120

# A real two-storey building attenuates between floors, so a simulation without
# it is unphysical and lets the solver look better than it is. Deliberately set
# to a different figure from the solver's own prior (refine.FLOOR_PENALTY_DB),
# so these tests measure how the solver copes with a prior that is approximately
# right rather than exactly right.
#
# The solver corrects floor loss only on the radio-to-radio links, so loss on the
# beacon paths is uncorrected and heavy attenuation degrades the layout: at 18 dB
# the shape error is several metres. 6 dB is a reasonable residential figure and
# the regime the solver is built for.
TRUE_FLOOR_PENALTY_DB = 6.0


def simulate(
    penalties: dict[tuple[str, str], float] | None = None,
    gains: dict[str, float] | None = None,
):
    """Build direct links and shared-beacon observations for the truth layout.

    `gains` simulates uncalibrated radios: each board reading a few dB off the
    others, which is the normal case and not something to be assumed away.

    Also returns where each beacon really was, so beacon recovery can be scored.
    """
    penalties = penalties or {}
    gains = gains or {anchor: 0.0 for anchor in TRUTH}

    def rssi(distance: float) -> float:
        return (
            geometry.TX_POWER_AT_1M
            - 10 * geometry.PATH_LOSS_EXPONENT * math.log10(max(distance, 0.3))
            + random.gauss(0, NOISE_DB)
        )

    direct = {}
    for advertiser in ADVERTISERS:
        for listener in TRUTH:
            if listener == advertiser:
                continue
            penalty = penalties.get((listener, advertiser), 0.0) + penalties.get(
                (advertiser, listener), 0.0
            )
            distance = math.dist(TRUTH[listener], TRUTH[advertiser])
            crossed = abs(LEVELS[listener] - LEVELS[advertiser])
            direct[(listener, advertiser)] = [
                rssi(distance)
                - penalty
                - crossed * TRUE_FLOOR_PENALTY_DB
                + gains[listener]
                + gains[advertiser]
            ]

    beacons: dict[str, tuple[float, float, float]] = {}
    observations = {anchor: {} for anchor in TRUTH}
    for index in range(BEACON_COUNT):
        beacon = (
            random.uniform(-2, 12),
            random.uniform(-2, 10),
            random.uniform(0, 3.5),
        )
        for anchor, position in TRUTH.items():
            # Beacons are scattered through both storeys; a reading crossing a
            # floor loses signal to it.
            beacon_level = 2 if beacon[2] > 2.0 else 1
            crossed = abs(LEVELS[anchor] - beacon_level)
            reading = (
                rssi(math.dist(position, beacon))
                + gains[anchor]
                - crossed * TRUE_FLOOR_PENALTY_DB
            )
            if reading > -100:  # radio sensitivity floor
                observations[anchor][f"b{index}"] = reading
        beacons[f"b{index}"] = beacon

    return direct, observations, beacons


def shape_error(positions: dict[str, dict[str, float]]) -> float:
    """RMS distance between the recovered shape and the truth, best-aligned.

    Compares pairwise distances rather than coordinates, which sidesteps the
    rotation and reflection that MDS leaves arbitrary.
    """
    anchors = list(TRUTH)
    residual = 0.0
    count = 0
    for i, first in enumerate(anchors):
        for second in anchors[i + 1 :]:
            truth = math.dist(TRUTH[first], TRUTH[second])
            got = math.dist(
                [positions[first][axis] for axis in "xyz"],
                [positions[second][axis] for axis in "xyz"],
            )
            residual += (truth - got) ** 2
            count += 1
    return math.sqrt(residual / count)


def check(name: str, condition: bool, detail: str) -> bool:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}: {detail}")
    return condition


def test_recovers_shape() -> bool:
    random.seed(7)
    direct, observations, _beacons = simulate()
    result = geometry.solve_layout(list(TRUTH), direct, observations, LEVELS)

    ok = check("no error", result["error"] is None, str(result["error"]))
    ok &= check("stress under 0.15", result["stress"] < 0.15, f"stress={result['stress']}")

    error = shape_error(result["positions"])
    ok &= check("shape error under 1.5m", error < 1.5, f"{error:.2f}m RMS")

    lower = [result["positions"][a]["z"] for a in TRUTH if LEVELS[a] == 1]
    upper = [result["positions"][a]["z"] for a in TRUTH if LEVELS[a] == 2]
    mean_lower = sum(lower) / len(lower)
    mean_upper = sum(upper) / len(upper)
    ok &= check(
        "upstairs sits above downstairs",
        mean_upper > mean_lower,
        f"downstairs z={mean_lower:.2f}, upstairs z={mean_upper:.2f}",
    )
    return ok


def test_rejects_attenuated_link() -> bool:
    """A wall-attenuated link must not be trusted as a distance.

    Without the reliability floor this is what wrecked the real house: a -96 dBm
    reading between two anchors became a 26m estimate and broke the embedding.
    """
    random.seed(11)
    direct, observations, _beacons = simulate(penalties={("mainbed", "office"): 22.0})
    result = geometry.solve_layout(list(TRUTH), direct, observations, LEVELS)

    pair = next(
        p for p in result["pairs"] if {p["a"], p["b"]} == {"mainbed", "office"}
    )
    ok = check(
        "attenuated link not treated as direct",
        pair["method"] == "inferred",
        f"method={pair['method']}",
    )

    # The pairwise distance is only a seed for the refinement, and a link buried
    # under 22 dB of wall on top of the floor loss makes a poor one. What has to
    # survive is the finished layout, so assert on that rather than on the seed.
    error = shape_error(result["positions"])
    ok &= check(
        "layout survives a badly attenuated link",
        error < 2.5,
        f"{error:.2f}m RMS",
    )
    return ok


def test_recovers_uncalibrated_gains() -> bool:
    """Radios read a few dB apart for the same distance; solve for that.

    Without this the offset is silently absorbed into the geometry, pushing a
    hot radio outward and a quiet one inward.
    """
    gains = {
        "garage": -6.0,
        "office": 5.0,
        "dining": 0.0,
        "mainbed": 3.0,
        "stretch": -2.0,
    }
    random.seed(5)
    direct, observations, _beacons = simulate(gains=gains)
    result = geometry.solve_layout(list(TRUTH), direct, observations, LEVELS)

    ok = check("refinement ran", result["refined"], f"residual={result['residual_db']} dB")

    # The solver holds gains to zero mean, so compare on the same footing.
    mean_truth = sum(gains.values()) / len(gains)
    errors = [
        abs(result["gains"].get(anchor, 0.0) - (gains[anchor] - mean_truth))
        for anchor in TRUTH
    ]
    mean_error = sum(errors) / len(errors)
    ok &= check(
        "per-radio gain recovered within 1.5 dB",
        mean_error < 1.5,
        f"{mean_error:.2f} dB mean absolute error",
    )

    error = shape_error(result["positions"])
    ok &= check(
        "shape survives uncalibrated radios",
        error < 2.0,
        f"{error:.2f}m RMS",
    )
    return ok


# What the real house measures, from the fit residual on two live captures.
# The other tests in this file predate that measurement and run at NOISE_DB;
# beacon recovery is checked here at the real figure as well, because it is the
# regime where the answer changes qualitatively rather than just getting worse.
REALISTIC_NOISE_DB = 7.85


def _score_beacons(noise_db: float, seed: int = 11) -> dict[str, float]:
    """Recover beacons at a given noise level and score them against truth.

    Scored on each beacon's distance to each radio rather than its coordinates,
    which sidesteps the rotation and reflection MDS leaves arbitrary -- the same
    trick shape_error uses.

    Scored against a null model too: a beacon parked at the centroid of the
    radios that hear it. That is the layout you get for free, without solving
    anything, so the ratio between the two is the only number that says whether
    the solve is carrying information.
    """
    global NOISE_DB
    previous = NOISE_DB
    NOISE_DB = noise_db
    try:
        random.seed(seed)
        direct, observations, truth = simulate()
        result = geometry.solve_layout(list(TRUTH), direct, observations, LEVELS)
    finally:
        NOISE_DB = previous

    positions = result["positions"]
    anchors = list(TRUTH)
    errors, null_errors, within = [], [], 0

    for beacon in result["beacons"]:
        real = truth[beacon["address"]]
        heard = [a for a in anchors if beacon["address"] in observations[a]]
        centroid = [
            sum(positions[a][axis] for a in heard) / len(heard) for axis in "xyz"
        ]
        point = [beacon[axis] for axis in "xyz"]

        residual = null_residual = 0.0
        for anchor in anchors:
            true_range = math.dist(real, TRUTH[anchor])
            seat = [positions[anchor][axis] for axis in "xyz"]
            residual += (true_range - math.dist(point, seat)) ** 2
            null_residual += (true_range - math.dist(centroid, seat)) ** 2

        error = math.sqrt(residual / len(anchors))
        errors.append(error)
        null_errors.append(math.sqrt(null_residual / len(anchors)))
        # The reported radius ignores dilution of precision, so it is a lower
        # bound on the error and is checked as a floor, not a promise.
        if error <= 2 * beacon["uncertainty_m"]:
            within += 1

    median = sorted(errors)[len(errors) // 2]
    null_median = sorted(null_errors)[len(null_errors) // 2]
    return {
        "placed": len(result["beacons"]),
        "median": median,
        "null_median": null_median,
        "ratio": median / null_median,
        "covered": within / len(errors),
        "residual_db": result["residual_db"],
        "fewest_radios": min(b["radios"] for b in result["beacons"]),
    }


def test_places_beacons() -> bool:
    """Beacon positions must beat a free guess, and be honest about their error."""
    clean = _score_beacons(NOISE_DB)
    real = _score_beacons(REALISTIC_NOISE_DB)

    ok = check(
        "beacons are returned",
        clean["placed"] > BEACON_COUNT // 2,
        f"{clean['placed']} of {BEACON_COUNT} placed",
    )
    ok &= check(
        "every placed beacon had at least 3 radios",
        clean["fewest_radios"] >= refine.MIN_RADIOS_PER_BEACON,
        f"fewest radios on any beacon: {clean['fewest_radios']}",
    )
    ok &= check(
        f"at {NOISE_DB} dB, beacons clearly beat the centroid guess",
        clean["ratio"] < 0.6,
        f"{clean['median']:.2f}m vs {clean['null_median']:.2f}m null "
        f"({clean['ratio']:.2f}x)",
    )

    # The house is not a 2 dB environment. At its real noise the solve still
    # wins, but only just, and that margin is the honest description of what a
    # beacon dot on the map is worth. If this ever tightens below ~0.6 something
    # has genuinely improved; if it reaches 1.0 the dots are decoration and
    # should stop being drawn.
    ok &= check(
        f"at a realistic {REALISTIC_NOISE_DB} dB, beacons still beat it",
        real["ratio"] < 0.95,
        f"{real['median']:.2f}m vs {real['null_median']:.2f}m null "
        f"({real['ratio']:.2f}x) -- a thin margin, by design of physics",
    )
    ok &= check(
        "realistic noise reproduces the house's fit residual",
        4.5 < real["residual_db"] < 7.0,
        f"{real['residual_db']} dB simulated vs 5.75-5.99 dB measured",
    )
    ok &= check(
        "reported uncertainty brackets the real error at both noise levels",
        0.5 < clean["covered"] <= 1.0 and 0.5 < real["covered"] <= 1.0,
        f"{clean['covered']:.0%} and {real['covered']:.0%} inside 2x the radius",
    )
    return ok


def test_edge_cases() -> bool:
    ok = check(
        "too few anchors is an error",
        geometry.solve_layout(["a", "b"], {}, {})["error"] is not None,
        "2 anchors rejected",
    )
    ok &= check(
        "no data is an error",
        geometry.solve_layout(["a", "b", "c"], {}, {})["error"] is not None,
        "3 anchors, no readings, rejected",
    )
    ok &= check(
        "implausible RSSI rejected",
        geometry.rssi_to_distance(-200) is None and geometry.rssi_to_distance(0) is None,
        "out-of-range readings return None",
    )
    ok &= check(
        "1m reference",
        abs(geometry.rssi_to_distance(geometry.TX_POWER_AT_1M) - 1.0) < 0.01,
        "reference RSSI maps to 1.0m",
    )
    return ok


def main() -> int:
    passed = True
    for test in (
        test_recovers_shape,
        test_rejects_attenuated_link,
        test_recovers_uncalibrated_gains,
        test_places_beacons,
        test_edge_cases,
    ):
        print(f"\n{test.__name__}")
        passed &= test()
    print("\nALL PASSED" if passed else "\nFAILURES ABOVE")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
