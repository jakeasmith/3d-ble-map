"""The 3D BLE Map integration."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components import panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import (
    DOMAIN,
    PANEL_ICON,
    PANEL_JS_FILENAME,
    PANEL_TITLE,
    PANEL_URL_PATH,
    STATIC_URL_BASE,
)
from .websocket import async_register_websocket_api

_LOGGER = logging.getLogger(__name__)

# The panel is a singleton keyed off the frontend URL path, so it is registered
# once for the whole integration rather than once per config entry.
_PANEL_REGISTERED = f"{DOMAIN}_panel_registered"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the websocket API, which is independent of any config entry."""
    async_register_websocket_api(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up 3D BLE Map from a config entry."""
    await _async_register_panel(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and tear the panel back down."""
    if hass.data.pop(_PANEL_REGISTERED, False):
        panel_custom.async_remove_panel(hass, PANEL_URL_PATH)
    return True


async def _async_register_panel(hass: HomeAssistant) -> None:
    """Serve the panel bundle and add it to the sidebar."""
    if hass.data.get(_PANEL_REGISTERED):
        return

    frontend_dir = Path(__file__).parent / "frontend"
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                STATIC_URL_BASE,
                str(frontend_dir),
                # The bundle is unversioned, so let the browser revalidate it
                # instead of pinning a stale copy after an update.
                cache_headers=False,
            )
        ]
    )

    # cache_headers=False stops the HTTP layer caching, but the browser still
    # holds the resolved ES module against its URL. Version the URL by the
    # bundle's mtime so an edit is actually picked up without a hard reload.
    panel_js = frontend_dir / PANEL_JS_FILENAME
    version = await hass.async_add_executor_job(_module_version, panel_js)

    await panel_custom.async_register_panel(
        hass,
        webcomponent_name="threed-ble-map-panel",
        frontend_url_path=PANEL_URL_PATH,
        module_url=f"{STATIC_URL_BASE}/{PANEL_JS_FILENAME}?v={version}",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        require_admin=True,
    )

    hass.data[_PANEL_REGISTERED] = True
    _LOGGER.debug("Registered %s panel at /%s", PANEL_TITLE, PANEL_URL_PATH)


def _module_version(path: Path) -> int:
    """Cache-busting token for the panel bundle: its modification time."""
    try:
        return int(path.stat().st_mtime)
    except OSError:  # pragma: no cover - the file ships with the integration
        return 0
