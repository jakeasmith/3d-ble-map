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


def simulate(
    penalties: dict[tuple[str, str], float] | None = None,
    gains: dict[str, float] | None = None,
):
    """Build direct links and shared-beacon observations for the truth layout.

    `gains` simulates uncalibrated radios: each board reading a few dB off the
    others, which is the normal case and not something to be assumed away.
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
            direct[(listener, advertiser)] = [
                rssi(distance) - penalty + gains[listener] + gains[advertiser]
            ]

    observations = {anchor: {} for anchor in TRUTH}
    for index in range(BEACON_COUNT):
        beacon = (
            random.uniform(-2, 12),
            random.uniform(-2, 10),
            random.uniform(0, 3.5),
        )
        for anchor, position in TRUTH.items():
            reading = rssi(math.dist(position, beacon)) + gains[anchor]
            if reading > -100:  # radio sensitivity floor
                observations[anchor][f"b{index}"] = reading

    return direct, observations


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
    direct, observations = simulate()
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
    direct, observations = simulate(penalties={("mainbed", "office"): 22.0})
    result = geometry.solve_layout(list(TRUTH), direct, observations, LEVELS)

    pair = next(
        p for p in result["pairs"] if {p["a"], p["b"]} == {"mainbed", "office"}
    )
    truth = math.dist(TRUTH["mainbed"], TRUTH["office"])

    ok = check(
        "attenuated link not treated as direct",
        pair["method"] == "inferred",
        f"method={pair['method']}",
    )
    ok &= check(
        "attenuated pair within 4m of truth",
        abs(pair["distance"] - truth) < 4.0,
        f"est={pair['distance']}m truth={truth:.2f}m",
    )
    ok &= check("stress under 0.15", result["stress"] < 0.15, f"stress={result['stress']}")
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
    direct, observations = simulate(gains=gains)
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
        test_edge_cases,
    ):
        print(f"\n{test.__name__}")
        passed &= test()
    print("\nALL PASSED" if passed else "\nFAILURES ABOVE")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
