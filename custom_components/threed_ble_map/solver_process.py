"""Run the layout solve in a separate process.

The solver is pure Python with no numpy, so it holds the interpreter lock for
as long as it runs. In a thread -- which is what an executor job is -- that
makes every await in the whole of Home Assistant wait for the GIL switch
interval. Measured on a live house, against a coroutine that should tick every
10 ms:

                            loop lag: median      p95     worst
    idle, no solve                    0.13 ms   0.20 ms   0.31 ms
    solve in an executor thread       5.22 ms   5.31 ms  10.40 ms
    solve in a subprocess             0.14 ms   0.22 ms   1.24 ms

5.22 ms is the switch interval exactly. A solve takes 5-7 s and runs every
15-20 s, so roughly a quarter of the time every websocket frame, state write
and API response in the instance was paying it -- including the API responses
the supervisor watchdog counts before it restarts core.

In a subprocess it is somebody else's core, and the event loop cannot tell the
difference between a solve running and no solve running.

A process per solve rather than a pool: the interpreter start plus this
module's imports cost about a fifth of a second against a solve of several
seconds, and in exchange there is no worker to keep alive, restart, or leak,
and a solver that crashes or wedges cannot take anything with it.

This module must stay free of Home Assistant imports. It is both the parent's
entry point and the script the child runs.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import types
from typing import Any

# Reading a whole result off a pipe: generous, because the payload is the
# solved layout plus every beacon, and truncating one is a silent wrong map.
_MAX_OUTPUT_BYTES = 64 * 1024 * 1024


def _load_geometry() -> Any:
    """Import geometry.py without importing the integration package.

    `custom_components.threed_ble_map` pulls in Home Assistant through its
    __init__, which in this process would cost seconds of import time and
    hundreds of megabytes to reach four modules that import nothing but `math`.
    So the solver modules are loaded from their own directory under a private
    package name, which is enough to make geometry's relative import of refine
    resolve.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    package = types.ModuleType("_tbm_solver")
    package.__path__ = [here]
    sys.modules["_tbm_solver"] = package
    for name in ("const", "quality", "refine", "geometry"):
        spec = importlib.util.spec_from_file_location(
            f"_tbm_solver.{name}", os.path.join(here, f"{name}.py")
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"_tbm_solver.{name}"] = module
        spec.loader.exec_module(module)
    return sys.modules["_tbm_solver.geometry"]


def solve(payload: dict[str, Any]) -> dict[str, Any]:
    """Do the solve. Runs in the child, and in the parent as the fallback."""
    geometry = _load_geometry()
    # JSON has no tuple keys, so the direct links travel as pairs.
    direct = {
        (listener, advertiser): values
        for listener, advertiser, values in payload["direct"]
    }
    return geometry.solve_layout(
        payload["anchors"],
        direct,
        payload["observations"],
        payload.get("levels"),
        beacon_weights=payload.get("beacon_weights"),
        previous=payload.get("previous"),
    )


def encode(
    anchors: list[str],
    direct: dict[tuple[str, str], list[float]],
    observations: dict[str, dict[str, float]],
    levels: dict[str, float] | None,
    beacon_weights: dict[str, float] | None = None,
    previous: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Everything the solver needs, in a shape that survives a pipe."""
    return {
        "anchors": list(anchors),
        "direct": [[a, b, list(v)] for (a, b), v in direct.items()],
        "observations": observations,
        "levels": levels or {},
        "beacon_weights": beacon_weights,
        "previous": previous,
    }


class SolverProcessError(RuntimeError):
    """The child could not produce a result."""


async def async_solve(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Solve in a child process, or raise so the caller can fall back."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        os.path.abspath(__file__),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=_MAX_OUTPUT_BYTES,
    )
    try:
        out, err = await asyncio.wait_for(
            process.communicate(json.dumps(payload).encode()), timeout
        )
    except asyncio.TimeoutError as exc:
        # A wedged solver is the case this design exists to survive: kill it and
        # let the caller decide, rather than leaving it burning a core forever.
        process.kill()
        await process.wait()
        raise SolverProcessError(f"solve did not finish within {timeout:.0f}s") from exc
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise

    if process.returncode != 0 or not out:
        detail = (err or b"").decode(errors="replace").strip().splitlines()
        raise SolverProcessError(
            f"solver exited {process.returncode}: {detail[-1] if detail else 'no output'}"
        )
    try:
        return json.loads(out)
    except ValueError as exc:
        raise SolverProcessError("solver produced unreadable output") from exc


def _main() -> None:
    payload = json.loads(sys.stdin.read())
    json.dump(solve(payload), sys.stdout)
    sys.stdout.flush()


if __name__ == "__main__":
    _main()
