"""Score a solved layout against the floor-plan yardstick.

Usage:
    python3 validation/score.py raw.json        # re-solve a raw dump and score it

`raw.json` is the output of the threed_ble_map/raw_observations websocket
command. Re-solving from raw means the solver can be changed and re-scored in
milliseconds, instead of restarting Home Assistant for every attempt.

The solved layout is relative -- arbitrary origin, rotation and handedness -- so
it is Procrustes-aligned onto the truth before scoring. Scale is reported but
NOT fitted away: getting the scale right is part of the job.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
import types
from pathlib import Path

import ground_truth as GT

SRC = Path(__file__).resolve().parents[1] / "custom_components" / "threed_ble_map"


def _load(name: str):
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
_load("refine")


def solve(raw: dict) -> tuple[dict[str, dict[str, float]], dict]:
    labels = {a["source"]: a["label"] for a in raw["anchors"]}
    levels = {
        a["source"]: a["level"] for a in raw["anchors"] if a.get("level") is not None
    }
    ordered = [a["source"] for a in raw["anchors"]]
    direct: dict[tuple[str, str], list[float]] = {}
    for link in raw["direct_links"]:
        direct.setdefault((link["listener"], link["advertiser"]), []).extend(
            link["rssi"] if isinstance(link["rssi"], list) else [link["rssi"]]
        )
    result = geometry.solve_layout(ordered, direct, raw["observations"], levels)
    named = {
        labels[src]: pos for src, pos in result.get("positions", {}).items()
    }
    return named, result


def kabsch(P: list[list[float]], Q: list[list[float]]) -> list[list[float]]:
    """Rotate P onto Q (both already centred), allowing reflection."""
    H = [[sum(P[k][i] * Q[k][j] for k in range(len(P))) for j in range(3)] for i in range(3)]
    R = [row[:] for row in H]
    for _ in range(300):
        det = (
            R[0][0] * (R[1][1] * R[2][2] - R[1][2] * R[2][1])
            - R[0][1] * (R[1][0] * R[2][2] - R[1][2] * R[2][0])
            + R[0][2] * (R[1][0] * R[2][1] - R[1][1] * R[2][0])
        )
        if abs(det) < 1e-12:
            break
        inv = [[0.0] * 3 for _ in range(3)]
        for i in range(3):
            for j in range(3):
                minor = [[R[r][c] for c in range(3) if c != j] for r in range(3) if r != i]
                inv[j][i] = (minor[0][0] * minor[1][1] - minor[0][1] * minor[1][0]) * (-1) ** (i + j) / det
        R = [[(R[i][j] + inv[j][i]) / 2 for j in range(3)] for i in range(3)]
    return [[sum(R[i][j] * p[i] for i in range(3)) for j in range(3)] for p in P]


def score(named: dict[str, dict[str, float]]) -> dict:
    matched = []
    for label, pos in named.items():
        key = GT.resolve(label)
        if key:
            matched.append((key, [pos["x"], pos["y"], pos["z"]], list(GT.TRUTH[key])))
    if len(matched) < 3:
        return {"error": f"only {len(matched)} anchors matched ground truth"}

    def centre(rows):
        c = [sum(r[i] for r in rows) / len(rows) for i in range(3)]
        return [[r[i] - c[i] for i in range(3)] for r in rows]

    P = centre([m[1] for m in matched])
    Q = centre([m[2] for m in matched])
    aligned = kabsch(P, Q)
    errors = [math.dist(aligned[i], Q[i]) for i in range(len(matched))]

    # Scale, measured on pairwise distances so it is independent of the fit.
    num = den = 0.0
    pair_rows = []
    for i in range(len(matched)):
        for j in range(i + 1, len(matched)):
            got = math.dist(matched[i][1], matched[j][1])
            true = math.dist(matched[i][2], matched[j][2])
            num += got
            den += true
            pair_rows.append((matched[i][0], matched[j][0], got, true))

    return {
        "per_anchor": {matched[i][0]: errors[i] for i in range(len(matched))},
        "rms": math.sqrt(sum(e * e for e in errors) / len(errors)),
        "scale": num / den,
        "pairs": pair_rows,
    }


def main() -> int:
    raw = json.load(open(sys.argv[1]))
    named, result = solve(raw)
    if not named:
        print("solver returned no positions:", result.get("error"))
        return 1
    s = score(named)
    if "error" in s:
        print(s["error"])
        return 1

    print(f"residual {result.get('residual_db')} dB | beacons {result.get('beacons_used')} "
          f"| shadowing {result.get('shadowing_db')} dB | bias {result.get('bias_correction')}")
    print(f"\nSCALE {s['scale']:.2f}x   POSITION RMS {s['rms']:.2f} m "
          f"(yardstick itself is +/- ~1.5 m)\n")
    for name, err in sorted(s["per_anchor"].items(), key=lambda kv: -kv[1]):
        print(f"  {name:26} {err:5.2f} m")
    print(f"\n{'pair':>56} {'solved':>8} {'true':>7} {'err':>7}")
    for a, b, got, true in sorted(s["pairs"], key=lambda r: r[3]):
        print(f"  {a[:24]:>26} - {b[:24]:<24} {got:>7.2f} {true:>7.2f} {got-true:>+7.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
