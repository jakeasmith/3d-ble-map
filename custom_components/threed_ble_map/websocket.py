"""Websocket API for 3D BLE Map."""

from __future__ import annotations

import asyncio
import logging
import time
from functools import partial
from typing import Any

import voluptuous as vol

from homeassistant.components import bluetooth, websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    floor_registry as fr,
)

from . import calibration, geometry, quality
from .const import (
    DEFAULT_SIGNAL_LIMIT,
    DOMAIN,
    MAX_SIGNAL_LIMIT,
    CALIBRATION_RATE,
    MIN_RECORDING_SECONDS,
    SOLVE_CACHE_SECONDS,
    SOLVE_SLOW_SECONDS,
    WS_ANCHOR_MAP,
    WS_LIST_ADAPTERS,
    WS_LIST_SIGNALS,
    WS_RAW_OBSERVATIONS,
)

_LOGGER = logging.getLogger(__name__)


@callback
def async_register_websocket_api(hass: HomeAssistant) -> None:
    """Register this integration's websocket commands."""
    websocket_api.async_register_command(hass, ws_list_adapters)
    websocket_api.async_register_command(hass, ws_list_signals)
    websocket_api.async_register_command(hass, ws_anchor_map)
    websocket_api.async_register_command(hass, ws_raw_observations)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): WS_LIST_ADAPTERS}
)
@callback
def ws_list_adapters(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return every Bluetooth scanner the bluetooth integration currently has."""
    device_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)

    adapters = [
        _describe_scanner(scanner, device_reg, area_reg)
        for scanner in bluetooth.async_current_scanners(hass)
    ]
    # Stable ordering so the panel does not reshuffle rows between polls.
    adapters.sort(key=lambda item: (item["name"] or "", item["source"]))

    connection.send_result(msg["id"], {"adapters": adapters})


def _describe_scanner(
    scanner: Any,
    device_reg: dr.DeviceRegistry,
    area_reg: ar.AreaRegistry,
) -> dict[str, Any]:
    """Flatten one scanner into a JSON-serialisable row."""
    source = getattr(scanner, "source", None) or ""

    # discovered_devices is a list of BLEDevice, not a mapping. Counting the
    # mapping variant instead silently reports 0 for remote scanners.
    devices = getattr(scanner, "discovered_devices", None) or []

    device_entry = _find_device(device_reg, source)
    area_name = None
    if device_entry and device_entry.area_id:
        if area := area_reg.async_get_area(device_entry.area_id):
            area_name = area.name

    return {
        "source": source,
        "name": getattr(scanner, "name", None) or source,
        "adapter": getattr(scanner, "adapter", None),
        "connectable": bool(getattr(scanner, "connectable", False)),
        "scanning": bool(getattr(scanner, "scanning", False)),
        "device_count": len(devices),
        "seconds_since_last_detection": _last_detection(scanner),
        "scanner_class": type(scanner).__name__,
        "device_name": device_entry.name_by_user or device_entry.name
        if device_entry
        else None,
        "area": area_name,
    }


def _last_detection(scanner: Any) -> float | None:
    """Seconds since this scanner last saw anything, if it reports that."""
    if not (method := getattr(scanner, "time_since_last_detection", None)):
        return None
    try:
        return round(float(method()), 1)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


def _find_device(
    device_reg: dr.DeviceRegistry, source: str
) -> dr.DeviceEntry | None:
    """Map a scanner source MAC back to its HA device, if there is one.

    An ESPHome node advertises BLE on a MAC a few digits off the network MAC
    it registers with, so an exact match misses. Fall back to matching the
    first five octets with a small tolerance on the last.
    """
    if not source:
        return None
    mac = dr.format_mac(source)
    for domain in (dr.CONNECTION_BLUETOOTH, dr.CONNECTION_NETWORK_MAC):
        if device := device_reg.async_get_device(connections={(domain, mac)}):
            return device
    return _find_device_by_near_mac(device_reg, mac)


# Largest observed gap between an ESPHome node's BLE MAC and its network MAC.
_MAC_TOLERANCE = 4


def _find_device_by_near_mac(
    device_reg: dr.DeviceRegistry, mac: str
) -> dr.DeviceEntry | None:
    """Find a device whose MAC is within a few digits of this scanner's."""
    if (target := _split_mac(mac)) is None:
        return None
    prefix, last = target

    for device in device_reg.devices.values():
        for domain, value in device.connections:
            if domain not in (dr.CONNECTION_BLUETOOTH, dr.CONNECTION_NETWORK_MAC):
                continue
            if (other := _split_mac(value)) is None:
                continue
            if other[0] == prefix and abs(other[1] - last) <= _MAC_TOLERANCE:
                return device
    return None


def _split_mac(value: str) -> tuple[str, int] | None:
    """Split a MAC into its first five octets and its last octet as an int."""
    parts = dr.format_mac(value).split(":")
    if len(parts) != 6:
        return None
    try:
        return ":".join(parts[:5]), int(parts[5], 16)
    except ValueError:
        return None


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_LIST_SIGNALS,
        vol.Optional("limit", default=DEFAULT_SIGNAL_LIMIT): vol.All(
            int, vol.Range(min=1, max=MAX_SIGNAL_LIMIT)
        ),
    }
)
@callback
def ws_list_signals(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the BLE addresses heard by the most scanners.

    An address seen by three or more scanners is one a multilateration solver
    could actually place, so heard-by count is the useful sort order here.
    """
    device_reg = dr.async_get(hass)
    scanners = list(bluetooth.async_current_scanners(hass))

    labels = {
        getattr(scanner, "source", ""): _scanner_label(scanner, device_reg)
        for scanner in scanners
    }

    signals: dict[str, dict[str, Any]] = {}
    for scanner in scanners:
        source = getattr(scanner, "source", "")
        for address, name, rssi in _scanner_readings(scanner):
            signal = signals.setdefault(
                address,
                {"address": address, "name": None, "heard_by": []},
            )
            if name and not signal["name"]:
                signal["name"] = name
            signal["heard_by"].append(
                {"source": source, "label": labels.get(source, source), "rssi": rssi}
            )

    rows = [_finish_signal(signal) for signal in signals.values()]
    # Most-heard first; break ties on the strongest single reading so the top of
    # the list is the set a solver would have the best shot at.
    rows.sort(key=lambda row: (-row["scanner_count"], -(row["best_rssi"] or -999)))

    connection.send_result(
        msg["id"], {"signals": rows[: msg["limit"]], "total": len(rows)}
    )


def _finish_signal(signal: dict[str, Any]) -> dict[str, Any]:
    """Add the derived fields the panel sorts and renders on."""
    heard_by = sorted(
        signal["heard_by"],
        key=lambda entry: (entry["rssi"] is None, -(entry["rssi"] or 0)),
    )
    rssis = [entry["rssi"] for entry in heard_by if entry["rssi"] is not None]
    return {
        **signal,
        "heard_by": heard_by,
        "scanner_count": len(heard_by),
        "best_rssi": max(rssis) if rssis else None,
    }


def _scanner_readings(scanner: Any) -> list[tuple[str, str | None, int | None]]:
    """Pull (address, name, rssi) for everything one scanner currently hears."""
    # Unlike discovered_devices, this one IS a mapping -- address to a
    # (BLEDevice, AdvertisementData) pair -- and it is the only place the
    # per-scanner RSSI is available.
    paired = getattr(scanner, "discovered_devices_and_advertisement_data", None)
    if not paired:
        return [
            (device.address, device.name, None)
            for device in getattr(scanner, "discovered_devices", None) or []
        ]

    readings = []
    for address, (device, adv) in paired.items():
        name = getattr(adv, "local_name", None) or getattr(device, "name", None)
        readings.append((address, name, getattr(adv, "rssi", None)))
    return readings


def _scanner_label(scanner: Any, device_reg: dr.DeviceRegistry) -> str:
    """Shortest useful human name for a scanner."""
    source = getattr(scanner, "source", "") or ""
    if device := _find_device(device_reg, source):
        return device.name_by_user or device.name or source
    return getattr(scanner, "adapter", None) or source


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): WS_ANCHOR_MAP})
@websocket_api.async_response
async def ws_anchor_map(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Estimate where each anchor sits in 3D, relative to the others."""
    recorder = hass.data.get(DOMAIN, {}).get("recorder")
    if recorder is None:
        connection.send_result(
            msg["id"],
            {
                "anchors": [],
                "positions": {},
                "pairs": [],
                "stress": None,
                "elapsed": 0,
                "ready": False,
                "error": "The signal recorder is not running.",
            },
        )
        return

    device_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)
    floor_reg = fr.async_get(hass)

    scanners = list(bluetooth.async_current_scanners(hass))
    sources = [s for scanner in scanners if (s := getattr(scanner, "source", None))]

    anchors = []
    levels: dict[str, float] = {}
    counts = recorder.sample_counts(sources)
    for scanner in scanners:
        source = getattr(scanner, "source", None)
        if not source:
            continue
        device = _find_device(device_reg, source)
        area = area_reg.async_get_area(device.area_id) if device and device.area_id else None
        floor = floor_reg.async_get_floor(area.floor_id) if area and area.floor_id else None
        if floor is not None and floor.level is not None:
            levels[source] = float(floor.level)
        anchors.append(
            {
                "source": source,
                "label": _scanner_label(scanner, device_reg),
                "area": area.name if area else None,
                "floor": floor.name if floor else None,
                "level": levels.get(source),
                "tracked_beacons": counts.get(source, 0),
            }
        )
    anchors.sort(key=lambda anchor: anchor["label"])

    ordered = [anchor["source"] for anchor in anchors]
    elapsed = round(recorder.elapsed)
    ready = elapsed >= MIN_RECORDING_SECONDS

    if not ready:
        result = {"positions": {}, "pairs": [], "stress": None, "error": None}
    else:
        result = await _async_cached_solve(
            hass, recorder, ordered, levels,
            {anchor["source"]: anchor for anchor in anchors},
        )

    connection.send_result(
        msg["id"],
        {
            "anchors": anchors,
            "elapsed": elapsed,
            "ready": ready,
            "min_seconds": MIN_RECORDING_SECONDS,
            **result,
        },
    )


async def _async_cached_solve(
    hass: HomeAssistant,
    recorder: Any,
    ordered: list[str],
    levels: dict[str, float],
    anchors: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Solve the layout off the event loop, reusing a recent result.

    The solve is expensive and the geometry changes far more slowly than the
    panel polls, so it runs in an executor behind a cache.

    A stale cache alone is not enough. Checking it and then starting a solve is
    two steps with an await between them, so every request that arrived during
    those seconds used to miss the cache and start its own solve.

    Measured on this house: 5.3 s per solve at 8 radios and 51 beacons, against
    a 15 s cache. So no single solve outran its cache -- the damage came from
    what arrived *while one was running*. Several open panels, or one panel and
    a script, land together in that window and each start a full solve on the
    executor. Enough of those and core stops answering, and Home Assistant's
    supervisor restarts a core that stops answering. It did, three times.

    So a solve in flight is recorded as a future and concurrent callers await
    that instead of starting their own. At most one solve exists at any moment,
    which makes the pile-up impossible rather than merely unlikely.
    """
    cache = hass.data.setdefault(DOMAIN, {}).setdefault("solve_cache", {})
    key = tuple(ordered)
    now = time.monotonic()

    cached = cache.get("result")
    if (
        cached is not None
        and cache.get("key") == key
        and now - cache.get("at", 0.0) < SOLVE_CACHE_SECONDS
    ):
        return cached

    # Someone is already solving this: wait for their answer instead of
    # starting a second one.
    pending = cache.get("pending")
    if pending is not None and cache.get("pending_key") == key and not pending.done():
        return await asyncio.shield(pending)

    # The solve is a task rather than a bare await so that a client
    # disconnecting cannot cancel it out from under everyone waiting on it.
    # Every caller, this one included, awaits it shielded.
    task = hass.async_create_task(
        _async_solve(hass, recorder, ordered, levels, anchors)
    )
    cache["pending"] = task
    cache["pending_key"] = key

    def _finished(done: asyncio.Task) -> None:
        # Clear the slot from the task's own callback, not from a finally in
        # whichever caller happened to start it -- that caller may be cancelled
        # long before the solve finishes.
        if cache.get("pending") is done:
            cache["pending"] = None
            cache["pending_key"] = None
        if done.cancelled() or done.exception() is not None:
            return
        result = done.result()
        cache.update({"result": result, "key": key, "at": time.monotonic()})
        if result.get("positions"):
            cache["positions"] = result["positions"]

    task.add_done_callback(_finished)
    return await asyncio.shield(task)


async def _async_solve(
    hass: HomeAssistant,
    recorder: Any,
    ordered: list[str],
    levels: dict[str, float],
    anchors: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Do the actual work. Only ever called with no other solve in flight."""

    # Snapshot the recorder on the event loop, then do the arithmetic off it.
    direct = recorder.direct_links(ordered)
    observations = recorder.observations(ordered)
    evidence = recorder.beacon_quality(ordered)
    known = _known_addresses(dr.async_get(hass))
    weights = {
        address: quality.beacon_weight(
            facts["motion"], facts["persistence"], address, address in known
        )
        for address, facts in evidence.items()
    }
    tracked = len(evidence)

    # The trust score is reported, not applied. Passing it to solve_layout as a
    # spring multiplier was built, measured, and rejected: downweighting suspect
    # beacons made the layout monotonically worse (shape error 2.75 m trusting
    # them, 2.98 m at 0.7x, 4.02 m at 0.4x, with 15 of ~120 beacons caught
    # mid-move), and at heavier contamination it did not help on a single seed.
    #
    # The reason is that _pull already applies a Huber weight to each reading's
    # own residual. That is evidence from the fit itself about whether a reading
    # is consistent, which strictly beats a prior guess about whether it might
    # be -- and a prior on top only removes constraint mass the solver needs.
    #
    # solve_layout still accepts weights and the path is tested, so this is one
    # argument away if better evidence turns up.
    # Hand the previous layout back to the solver. It competes against the cold
    # search rather than replacing it, so a house that has genuinely changed
    # still gets re-solved from scratch; see WARM_HYSTERESIS in refine.py.
    started = time.monotonic()
    result = await hass.async_add_executor_job(
        partial(
            geometry.solve_layout,
            ordered,
            direct,
            observations,
            levels,
            previous=hass.data[DOMAIN]["solve_cache"].get("calibrated"),
        )
    )
    elapsed = time.monotonic() - started
    result["solve_seconds"] = round(elapsed, 2)
    if elapsed > SOLVE_SLOW_SECONDS:
        _LOGGER.warning(
            "Layout solve took %.1fs for %d radios and %d beacons. It runs off "
            "the event loop and only one runs at a time, so this is slow rather "
            "than harmful, but lower MAX_SOLVE_BEACONS if it approaches the "
            "%ds cache interval",
            elapsed,
            len(ordered),
            result.get("beacons_used") or 0,
            SOLVE_CACHE_SECONDS,
        )
    _apply_calibration(hass, result)
    result["beacons"] = _describe_beacons(
        result.get("beacons") or [], observations, recorder.names(), anchors,
        evidence, known, weights,
    )
    result["weighted_beacons"] = sum(1 for w in weights.values() if w >= 0.5)
    result["trust"] = weights
    result["tracked_beacons"] = tracked

    return result


def _apply_calibration(hass: HomeAssistant, result: dict[str, Any]) -> None:
    """Publish a calibrated layout rather than this solve's raw answer.

    The solve is evidence, not the answer. Each one is spun into the calibrated
    frame and blended in at CALIBRATION_RATE, so noise averages away while a
    radio that has genuinely moved is followed over minutes.

    The beacons are moved by the same transform. They are solved in the same
    frame as the radios, so publishing calibrated radios without carrying the
    beacons along would leave them in the previous solve's orientation.
    """
    solved = result.get("positions")
    if not solved:
        return
    cache = hass.data.setdefault(DOMAIN, {}).setdefault("solve_cache", {})
    reference = cache.get("calibrated")
    solves = cache.get("solves", 0)

    calibrated, rate, transform = calibration.calibrate(
        reference, solved, solves, CALIBRATION_RATE
    )
    moved = calibration.movement(reference, calibrated)

    cache["calibrated"] = calibrated
    cache["solves"] = solves + 1

    result["positions"] = {
        anchor: {axis: round(value, 2) for axis, value in point.items()}
        for anchor, point in calibrated.items()
    }
    result["beacons"] = [
        {**beacon, **{
            axis: round(value, 2)
            for axis, value in calibration.apply(transform, beacon).items()
        }}
        for beacon in (result.get("beacons") or [])
    ]
    result["calibration"] = {
        "rate": round(rate, 4),
        "solves": solves + 1,
        "settled": rate <= CALIBRATION_RATE,
        "moved_m": None if moved is None else round(moved, 3),
    }


def _known_addresses(device_reg: dr.DeviceRegistry) -> set[str]:
    """Every MAC Home Assistant already associates with a device it manages.

    A beacon Home Assistant knows about is installed kit -- a light, a sensor, a
    television -- rather than something passing through, which is exactly the
    distinction the weighting wants. Built as one reverse index because doing a
    registry scan per beacon would be hundreds of scans on the event loop.
    """
    return {
        value.upper()
        for device in device_reg.devices.values()
        for _kind, value in device.connections
        if isinstance(value, str)
    }


def _describe_beacons(
    beacons: list[dict[str, Any]],
    observations: dict[str, dict[str, float]],
    names: dict[str, str],
    anchors: dict[str, dict[str, Any]],
    evidence: dict[str, dict[str, float]] | None = None,
    known: set[str] | None = None,
    trust: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Attach a name and a nearest radio to each solved beacon.

    The nearest radio is taken from the strongest reading, not from the solved
    coordinates. That is deliberate: "which radio hears this loudest" is a raw
    measurement that survives any amount of geometry error, so it stays right
    even where the 3D position is barely better than a guess. It is the field to
    trust when the uncertainty radius is large.
    """
    described = []
    for beacon in beacons:
        address = beacon["address"]
        heard = {
            source: observations[source][address]
            for source in observations
            if address in observations[source]
        }
        loudest = max(heard, key=heard.get) if heard else None
        anchor = anchors.get(loudest) if loudest else None
        facts = (evidence or {}).get(address, {})
        described.append(
            {
                **beacon,
                "name": names.get(address),
                "motion": facts.get("motion"),
                "persistence": facts.get("persistence"),
                "known_device": bool(known and address.upper() in known),
                "address_kind": quality.address_kind(address),
                "trust": round((trust or {}).get(address, 1.0), 2),
                "nearest_anchor": anchor["label"] if anchor else None,
                "nearest_area": anchor["area"] if anchor else None,
                "rssi": round(heard[loudest]) if loudest else None,
            }
        )
    # Strongest first: the beacons a user can actually place are at the top.
    described.sort(key=lambda beacon: -(beacon["rssi"] or -127))
    return described


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): WS_RAW_OBSERVATIONS})
@callback
def ws_raw_observations(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Dump the recorder's raw view, so the solver can be worked on offline.

    Tuning a solver by restarting Home Assistant between attempts is unusable.
    This exposes exactly what solve_layout receives, so the same inputs can be
    replayed against a candidate algorithm on a workstation.
    """
    recorder = hass.data.get(DOMAIN, {}).get("recorder")
    if recorder is None:
        connection.send_result(msg["id"], {"error": "recorder not running"})
        return

    device_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)
    floor_reg = fr.async_get(hass)

    scanners = list(bluetooth.async_current_scanners(hass))
    sources = [s for scanner in scanners if (s := getattr(scanner, "source", None))]

    anchors = []
    for scanner in scanners:
        source = getattr(scanner, "source", None)
        if not source:
            continue
        device = _find_device(device_reg, source)
        area = area_reg.async_get_area(device.area_id) if device and device.area_id else None
        floor = floor_reg.async_get_floor(area.floor_id) if area and area.floor_id else None
        anchors.append(
            {
                "source": source,
                "label": _scanner_label(scanner, device_reg),
                "area": area.name if area else None,
                "floor": floor.name if floor else None,
                "level": float(floor.level) if floor and floor.level is not None else None,
            }
        )

    connection.send_result(
        msg["id"],
        {
            "anchors": anchors,
            "elapsed": round(recorder.elapsed),
            "observations": recorder.observations(sources),
            "direct_links": [
                {"listener": a, "advertiser": b, "rssi": v}
                for (a, b), v in recorder.direct_links(sources).items()
            ],
            "quality": recorder.beacon_quality(sources),
        },
    )
