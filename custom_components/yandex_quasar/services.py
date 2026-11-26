"""Services: native station alarms."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.helpers import config_validation as cv

from . import alarms
from .const import DOMAIN

SERVICE_SET_ALARM = "set_alarm"
SERVICE_CANCEL_ALARM = "cancel_alarm"
SERVICE_STOP_ALARM = "stop_alarm"

SET_ALARM_SCHEMA = vol.Schema({
    vol.Required(ATTR_DEVICE_ID): cv.string,
    vol.Required("time"): cv.string,
    vol.Optional("weekdays"): vol.All(cv.ensure_list, [vol.In(alarms.WEEKDAYS)]),
    vol.Optional("date"): cv.string,
    vol.Optional("title"): cv.string,
    vol.Optional("alarm_id"): cv.string,
})
CANCEL_ALARM_SCHEMA = vol.Schema({
    vol.Required(ATTR_DEVICE_ID): cv.string,
    vol.Optional("alarm_id"): cv.string,
})
STOP_ALARM_SCHEMA = vol.Schema({vol.Required(ATTR_DEVICE_ID): cv.string})


def async_setup_services(hass: HomeAssistant) -> None:
    async def set_alarm(call: ServiceCall) -> ServiceResponse:
        client, station = alarms.resolve_station(hass, call.data[ATTR_DEVICE_ID])
        data = alarms.alarm_request(
            hass, call.data["time"], call.data.get("weekdays"), call.data.get("date"), call.data.get("title"),
            call.data.get("alarm_id"),
        )
        result = await alarms.call(client, station, "alarm.set", data)
        return {"alarm": alarms.describe(result["alarm"])}

    async def cancel_alarm(call: ServiceCall) -> None:
        client, station = alarms.resolve_station(hass, call.data[ATTR_DEVICE_ID])
        data = {"alarm_id": call.data["alarm_id"]} if call.data.get("alarm_id") else {}
        await alarms.call(client, station, "alarm.cancel", data)

    async def stop_alarm(call: ServiceCall) -> None:
        client, station = alarms.resolve_station(hass, call.data[ATTR_DEVICE_ID])
        await alarms.call(client, station, "alarm.stop")

    hass.services.async_register(
        DOMAIN, SERVICE_SET_ALARM, set_alarm, SET_ALARM_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(DOMAIN, SERVICE_CANCEL_ALARM, cancel_alarm, CANCEL_ALARM_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_STOP_ALARM, stop_alarm, STOP_ALARM_SCHEMA)
