"""Coordinator for Cudy Router integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_MODEL, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, MODULE_SMS, normalize_scan_interval
from .router import CudyRouter
from .sms import async_fetch_latest_inbox_message, latest_message_attributes

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 90


class CudyRouterDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Get the latest data from the router."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: CudyRouter) -> None:
        """Initialize router data."""
        self.config_entry = entry
        self.host: str = entry.data[CONF_HOST]
        self.api = api
        scan_interval = normalize_scan_interval(
            entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} - {self.host}",
            update_interval=timedelta(seconds=scan_interval),
        )

    def _previous_inbox(self) -> dict[str, Any]:
        """Return the inbox entry from the previous refresh."""
        previous = (self.data or {}).get(MODULE_SMS)
        if not isinstance(previous, dict):
            return {}
        inbox = previous.get("inbox_count")
        return inbox if isinstance(inbox, dict) else {}

    async def _async_attach_latest_sms(self, data: dict[str, Any]) -> None:
        """Publish the newest received message as attributes of the inbox sensor.

        A refresh only reads the SMS counts, because listing the mailbox costs
        one request per stored message. The listing is therefore fetched only
        when the router reports more received messages than last time, which is
        exactly when a new one has arrived. Otherwise the previous message is
        carried over so the attributes do not blink away between refreshes.
        """
        sms_data = data.get(MODULE_SMS)
        if not isinstance(sms_data, dict):
            return
        inbox = sms_data.get("inbox_count")
        if not isinstance(inbox, dict):
            return

        previous = self._previous_inbox()
        previous_attributes = previous.get("attributes") or {}
        count = inbox.get("value")

        arrived = isinstance(count, int) and count > (previous.get("value") or 0)
        missing = bool(count) and not previous_attributes
        if not arrived and not missing:
            if previous_attributes:
                inbox["attributes"] = previous_attributes
            return

        try:
            message = await async_fetch_latest_inbox_message(self.hass, self.api)
        except Exception as err:  # noqa: BLE001 - never fail a refresh over this
            _LOGGER.debug("Could not read the latest received SMS: %s", err)
            if previous_attributes:
                inbox["attributes"] = previous_attributes
            return

        attributes = latest_message_attributes(message)
        if attributes:
            inbox["attributes"] = attributes
        elif previous_attributes:
            inbox["attributes"] = previous_attributes

    async def _async_update_data(self) -> dict[str, Any]:
        """Get the latest data from the router."""
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                data = await self.api.get_data(
                    self.hass,
                    self.config_entry.options,
                    self.config_entry.data.get(CONF_MODEL, "default"),
                )
                await self._async_attach_latest_sms(data)
                return data
        except TimeoutError as err:
            raise UpdateFailed(f"Timeout communicating with router: {err}") from err
        except Exception as err:
            _LOGGER.error(err)
            raise UpdateFailed(f"Error communicating with router: {err}") from err
