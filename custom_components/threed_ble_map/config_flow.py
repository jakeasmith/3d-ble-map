"""Config flow for 3D BLE Map."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN, PANEL_TITLE


class ThreeDBleMapConfigFlow(ConfigFlow, domain=DOMAIN):
    """Single-instance, no-input config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding the integration."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is None:
            return self.async_show_form(step_id="user")

        return self.async_create_entry(title=PANEL_TITLE, data={})
