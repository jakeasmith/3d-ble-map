"""Websocket API for 3D BLE Map."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import bluetooth, websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, device_registry as dr

from .const import WS_LIST_ADAPTERS

_LOGGER = logging.getLogger(__name__)


@callback
def async_register_websocket_api(hass: HomeAssistant) -> None:
    """Register this integration's websocket commands."""
    websocket_api.async_register_command(hass, ws_list_adapters)


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
