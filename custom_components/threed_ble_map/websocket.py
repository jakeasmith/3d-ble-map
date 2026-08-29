"""Websocket API for 3D BLE Map."""

from __future__ import annotations

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

from . import geometry
from .const import (
    DEFAULT_SIGNAL_LIMIT,
    DOMAIN,
    MAX_SIGNAL_LIMIT,
    MIN_RECORDING_SECONDS,
    SOLVE_CACHE_SECONDS,
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
            hass, recorder, ordered, levels
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
) -> dict[str, Any]:
    """Solve the layout off the event loop, reusing a recent result.

    The solve takes hundreds of milliseconds, which would stall Home Assistant
    if run inline, and the geometry changes far more slowly than the panel polls.
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

    # Snapshot the recorder on the event loop, then do the arithmetic off it.
    direct = recorder.direct_links(ordered)
    observations = recorder.observations(ordered)
    rejected = len(recorder.unstable(ordered))
    tracked = len(recorder.stability(ordered))

    result = await hass.async_add_executor_job(
        partial(geometry.solve_layout, ordered, direct, observations, levels)
    )
    result["rejected_beacons"] = rejected
    result["tracked_beacons"] = tracked

    cache.update({"result": result, "key": key, "at": now})
    return result


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
            "all_observations": recorder.observations(sources, stable_only=False),
            "direct_links": [
                {"listener": a, "advertiser": b, "rssi": v}
                for (a, b), v in recorder.direct_links(sources).items()
            ],
            "stability": {
                address: {"spread": round(spread, 2), "samples": samples}
                for address, (spread, samples) in recorder.stability(sources).items()
            },
        },
    )
