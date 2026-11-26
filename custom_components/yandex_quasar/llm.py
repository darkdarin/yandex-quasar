"""LLM tools: native station alarms for Assist agents ("Alice, wake me up at 7 on weekdays")."""

from __future__ import annotations

from typing import Any, override

import probatio

from homeassistant.components.llm import LLMTools
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.llm import LLM_API_ASSIST, LLMContext, Tool, ToolAnnotations, ToolInput, ToolResult

from . import alarms
from .client import AdapterClient
from .const import DOMAIN

_TIME = probatio.All(str, probatio.Match(r"^([01]?\d|2[0-3]):[0-5]\d$"))
_DATE = probatio.All(str, probatio.Match(r"^\d{4}-\d{2}-\d{2}$"))
_WEEKDAYS = probatio.All([probatio.All(str, probatio.Lower, probatio.In(alarms.WEEKDAYS))], probatio.Length(min=1))


class _StationTool(Tool):
    """A tool acting on one station: the one the request came from, the only one, or the one named."""

    integration = DOMAIN

    def __init__(self, stations: dict[str, tuple[AdapterClient, str]], own: str | None) -> None:
        self._stations = stations  # name -> (client, station id)
        self._own = own
        schema = dict(self.fields())
        if own is None and len(stations) > 1:
            schema[probatio.Required("station", description="Which Yandex Station")] = probatio.In(list(stations))
        self.parameters = probatio.Schema(schema)

    def fields(self) -> dict:
        return {}

    def _target(self, args: dict[str, Any]) -> tuple[AdapterClient, str]:
        if self._own is not None:
            return self._stations[self._own]
        if len(self._stations) == 1:
            return next(iter(self._stations.values()))
        return self._stations[args["station"]]

    @override
    async def async_call(
        self, hass: HomeAssistant, tool_input: ToolInput, llm_context: LLMContext
    ) -> ToolResult:
        args = self.parameters(tool_input.tool_args)
        client, station = self._target(args)
        try:
            return ToolResult(data=await self.run(hass, client, station, args))
        except HomeAssistantError as err:
            return ToolResult(data={"error": str(err)}, error=True)

    async def run(self, hass: HomeAssistant, client: AdapterClient, station: str, args: dict) -> dict:
        raise NotImplementedError


class SetAlarmTool(_StationTool):
    name = f"{DOMAIN}__set_alarm"
    title = "Set a Yandex Station alarm"
    description = (
        "Set an alarm clock on the Yandex Station: the station itself rings at that local time. "
        "Without weekdays and date it rings once, at the next such time. "
        "Use for 'wake me up at 7', 'set an alarm for 6:30 on weekdays'. Not for timers."
    )
    annotations = ToolAnnotations(destructive=False, open_world=False)

    def fields(self) -> dict:
        return {
            probatio.Required("time", description="Local time, 24-hour HH:MM"): _TIME,
            probatio.Optional("weekdays", description="Repeat on these days: mon … sun"): _WEEKDAYS,
            probatio.Optional("date", description="Ring once on this date, YYYY-MM-DD"): _DATE,
            probatio.Optional("title", description="Short label, only if the user gave one"): str,
        }

    async def run(self, hass: HomeAssistant, client: AdapterClient, station: str, args: dict) -> dict:
        data = alarms.alarm_request(hass, args["time"], args.get("weekdays"), args.get("date"), args.get("title"))
        result = await alarms.call(client, station, "alarm.set", data)
        return {"success": True, "alarm": alarms.describe(result["alarm"])}


class ListAlarmsTool(_StationTool):
    name = f"{DOMAIN}__list_alarms"
    title = "List Yandex Station alarms"
    description = "List the alarms set on the Yandex Station, soonest first, with their alarm_id."
    annotations = ToolAnnotations(read_only=True, destructive=False, idempotent=True, open_world=False)

    async def run(self, hass: HomeAssistant, client: AdapterClient, station: str, args: dict) -> dict:
        state = client.stations.get(station, {}).get("state") or {}
        return {"alarms": alarms.upcoming(state.get("alarms") or [])}


class CancelAlarmTool(_StationTool):
    name = f"{DOMAIN}__cancel_alarm"
    title = "Cancel a Yandex Station alarm"
    description = (
        "Cancel one alarm on the Yandex Station by its alarm_id (see the list alarms tool), "
        "or every alarm with all=true when the user asks to cancel all of them."
    )
    annotations = ToolAnnotations(idempotent=True, open_world=False)

    def fields(self) -> dict:
        return {
            probatio.Optional("alarm_id", description="alarm_id from the list alarms tool"): str,
            probatio.Optional("all", description="Cancel every alarm"): bool,
        }

    async def run(self, hass: HomeAssistant, client: AdapterClient, station: str, args: dict) -> dict:
        if args.get("alarm_id"):
            data: dict[str, Any] = {"alarm_id": args["alarm_id"]}
        elif args.get("all"):
            data = {}
        else:
            return {"error": "Give alarm_id, or all=true to cancel every alarm"}
        result = await alarms.call(client, station, "alarm.cancel", data)
        return {"success": True, "alarms": alarms.upcoming(result.get("alarms") or [])}


@callback
def async_get_tools(hass: HomeAssistant, llm_context: LLMContext, api_id: str) -> LLMTools | None:
    """Alarm tools whenever the adapter knows a station."""
    if api_id != LLM_API_ASSIST:
        return None
    clients = alarms.station_clients(hass)
    if not clients:
        return None
    stations: dict[str, tuple[AdapterClient, str]] = {}
    own: str | None = None
    own_station = alarms.station_of_device(hass, llm_context.device_id) if llm_context.device_id else None
    for station_id, client in clients.items():
        name = client.stations[station_id].get("name") or station_id
        if name in stations:
            name = f"{name} ({station_id[-4:]})"
        stations[name] = (client, station_id)
        if station_id == own_station:
            own = name
    return LLMTools(tools=[SetAlarmTool(stations, own), ListAlarmsTool(stations, own), CancelAlarmTool(stations, own)])
