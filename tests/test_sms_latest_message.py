"""The inbox sensor publishes the newest received message for automations."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tests.module_loader import load_cudy_module


const = load_cudy_module("const")
load_cudy_module("model_names")
sms = load_cudy_module("sms")
coordinator_module = load_cudy_module("coordinator")


class _Coordinator(coordinator_module.CudyRouterDataUpdateCoordinator):
    """A coordinator with the network and Home Assistant parts stubbed out."""

    def __init__(self, previous: dict | None, fetched=None) -> None:
        self.hass = object()
        self.api = object()
        self.data = previous
        self._fetched = fetched
        self.fetches = 0

    async def _fetch(self, hass, router):
        self.fetches += 1
        if isinstance(self._fetched, Exception):
            raise self._fetched
        return self._fetched


def _attach(coordinator, data) -> None:
    coordinator_module.async_fetch_latest_inbox_message = coordinator._fetch
    asyncio.run(coordinator._async_attach_latest_sms(data))


def _sms(count: int, attributes: dict | None = None) -> dict:
    inbox: dict = {"value": count}
    if attributes is not None:
        inbox["attributes"] = attributes
    return {const.MODULE_SMS: {"inbox_count": inbox}}


MESSAGE = {"phone": "+390000000000", "text": "Ciao", "timestamp": "09/21/26, 05:16:50"}
ATTRIBUTES = {
    "last_sender": "+390000000000",
    "last_message": "Ciao",
    "last_received": "09/21/26, 05:16:50",
}


def test_a_new_message_is_fetched_and_published() -> None:
    """A higher inbox count is exactly when a message has arrived."""
    coordinator = _Coordinator(previous=_sms(1, ATTRIBUTES), fetched=MESSAGE)
    data = _sms(2)

    _attach(coordinator, data)

    assert coordinator.fetches == 1
    assert data[const.MODULE_SMS]["inbox_count"]["attributes"] == ATTRIBUTES


def test_an_unchanged_count_costs_no_requests() -> None:
    """Listing the mailbox costs one request per message, so it is not routine."""
    coordinator = _Coordinator(previous=_sms(2, ATTRIBUTES), fetched=MESSAGE)
    data = _sms(2)

    _attach(coordinator, data)

    assert coordinator.fetches == 0
    # The message is carried over, so the attributes do not blink away.
    assert data[const.MODULE_SMS]["inbox_count"]["attributes"] == ATTRIBUTES


def test_a_deleted_message_does_not_trigger_a_fetch() -> None:
    """A falling count means messages were removed, not received."""
    coordinator = _Coordinator(previous=_sms(5, ATTRIBUTES), fetched=MESSAGE)
    data = _sms(3)

    _attach(coordinator, data)

    assert coordinator.fetches == 0


def test_the_first_refresh_fills_the_attributes() -> None:
    """Otherwise the sensor stays bare until the next message arrives."""
    coordinator = _Coordinator(previous=None, fetched=MESSAGE)
    data = _sms(4)

    _attach(coordinator, data)

    assert coordinator.fetches == 1
    assert data[const.MODULE_SMS]["inbox_count"]["attributes"] == ATTRIBUTES


def test_an_empty_inbox_is_not_listed() -> None:
    """There is nothing to read, and no attributes to invent."""
    coordinator = _Coordinator(previous=None, fetched=MESSAGE)
    data = _sms(0)

    _attach(coordinator, data)

    assert coordinator.fetches == 0
    assert "attributes" not in data[const.MODULE_SMS]["inbox_count"]


def test_a_failed_read_keeps_the_previous_message_and_the_refresh() -> None:
    """Losing the mailbox page must not fail the whole refresh."""
    coordinator = _Coordinator(previous=_sms(1, ATTRIBUTES), fetched=RuntimeError("boom"))
    data = _sms(2)

    _attach(coordinator, data)

    assert data[const.MODULE_SMS]["inbox_count"]["attributes"] == ATTRIBUTES


def test_routers_without_sms_are_left_alone() -> None:
    """Most models report no SMS module at all."""
    coordinator = _Coordinator(previous=None, fetched=MESSAGE)
    data = {"wan": {}}

    _attach(coordinator, data)

    assert coordinator.fetches == 0
    assert data == {"wan": {}}
