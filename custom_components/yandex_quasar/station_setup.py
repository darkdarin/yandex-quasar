"""Flow steps that point a rooted station at the adapter (or back to the Yandex cloud) over SSH or ADB.

The adapter does the work (``station.provision`` / ``station.restore``); these steps only collect the access data,
show progress and report the result. Used by the station discovery flow and by the adapter's options menu.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .client import AdapterClient, AdapterCommandError, AdapterConnectionError

if TYPE_CHECKING:
    from homeassistant.data_entry_flow import FlowHandler

    _Base = FlowHandler
else:
    _Base = object

CONF_METHOD = "method"
CONF_PRIVATE_KEY = "private_key"
CONF_MODE = "mode"
CONF_ALLOW_UNTESTED = "allow_untested"
# The adapter checks the quasar restart for up to 30 s (rebooting the station if it did not happen), waits up to
# 4 min for the station to come back and may then roll back with another restart check.
SETUP_TIMEOUT = 420
KNOWN_ERRORS = {"station_unreachable", "auth_failed", "unsupported", "timeout", "station_error", "busy", "no_backup"}


def _access_schema(defaults: dict[str, Any], with_mode: bool) -> vol.Schema:
    fields: dict[Any, Any] = {
        vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
        vol.Required(CONF_METHOD, default=defaults.get(CONF_METHOD, "ssh")): SelectSelector(
            SelectSelectorConfig(options=["ssh", "adb"], mode=SelectSelectorMode.LIST, translation_key=CONF_METHOD)
        ),
        vol.Optional(CONF_PORT, description={"suggested_value": defaults.get(CONF_PORT)}): NumberSelector(
            NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "root")): str,
        vol.Optional(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        vol.Optional(CONF_PRIVATE_KEY): TextSelector(TextSelectorConfig(multiline=True)),
    }
    if with_mode:
        fields[vol.Required(CONF_MODE, default=defaults.get(CONF_MODE, "full"))] = SelectSelector(
            SelectSelectorConfig(options=["full", "voice"], mode=SelectSelectorMode.LIST, translation_key=CONF_MODE)
        )
        fields[vol.Optional(CONF_ALLOW_UNTESTED, default=False)] = BooleanSelector()
    return vol.Schema(fields)


class StationSetupSteps(_Base):
    """Mixin for ConfigFlow/OptionsFlow; subclasses provide ``_setup_client()``."""

    _setup_defaults: dict[str, Any]
    _setup_input: dict[str, Any] | None = None
    _setup_action: str = "station.provision"
    _setup_task: asyncio.Task | None = None
    _setup_result: dict[str, Any] | None = None
    _setup_error: str | None = None

    def _setup_client(self) -> AdapterClient | None:
        raise NotImplementedError

    async def _setup_form(self, step_id: str, user_input: dict[str, Any] | None, action: str) -> FlowResult:
        defaults = getattr(self, "_setup_defaults", {})
        if user_input is not None:
            self._setup_input = {k: v for k, v in user_input.items() if v not in ("", None)}
            if CONF_PORT in self._setup_input:
                self._setup_input[CONF_PORT] = int(self._setup_input[CONF_PORT])
            self._setup_action = action
            self._setup_task = None
            return await self.async_step_setup_run()
        errors = {"base": self._setup_error} if self._setup_error else {}
        self._setup_error = None
        return self.async_show_form(
            step_id=step_id, data_schema=self.add_suggested_values_to_schema(
                _access_schema(defaults, with_mode=action == "station.provision"), self._setup_input or {}
            ),
            errors=errors,
        )

    async def _run_setup(self) -> dict[str, Any]:
        client = self._setup_client()
        if client is None:
            raise AdapterConnectionError("adapter is not connected")
        return await client.request(self._setup_action, None, self._setup_input, timeout=SETUP_TIMEOUT)

    async def async_step_setup_run(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if self._setup_task is None:
            self._setup_task = self.hass.async_create_task(self._run_setup())
        if not self._setup_task.done():
            action = "provisioning" if self._setup_action == "station.provision" else "restoring"
            return self.async_show_progress(
                step_id="setup_run", progress_action=action, progress_task=self._setup_task,
                description_placeholders={"host": (self._setup_input or {}).get(CONF_HOST, "")},
            )
        try:
            self._setup_result = self._setup_task.result()
        except AdapterCommandError as err:
            code = "station_unreachable" if err.code == "cannot_connect" else err.code
            self._setup_error = code if code in KNOWN_ERRORS else "unknown"
            return self.async_show_progress_done(next_step_id="setup_failed")
        except AdapterConnectionError:
            self._setup_error = "adapter_offline"
            return self.async_show_progress_done(next_step_id="setup_failed")
        return self.async_show_progress_done(next_step_id="setup_done")

    async def async_step_setup_failed(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        step = "station" if self._setup_action == "station.provision" else "restore"
        return await getattr(self, f"async_step_{step}")()

    async def async_step_setup_done(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        result = self._setup_result or {}
        reason = "station_provisioned" if self._setup_action == "station.provision" else "station_restored"
        return self.async_abort(reason=reason, description_placeholders={
            "host": (self._setup_input or {}).get(CONF_HOST, ""),
            "steps": "\n".join(f"- {step}" for step in result.get("steps", [])),
        })
