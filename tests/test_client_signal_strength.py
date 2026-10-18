"""Tests for the per-client Wi-Fi signal reading."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from tests.module_loader import load_cudy_module


const = load_cudy_module("const")
parser = load_cudy_module("parser")
sensor = load_cudy_module("sensor")
sensor_descriptions = load_cudy_module("sensor_descriptions")

ROOT = Path(__file__).resolve().parents[1]
MODERN_DEVICES_HTML = (ROOT / "tests" / "fixtures" / "devices" / "modern_devices.html").read_text(
    encoding="utf-8"
)
POWERSTATION_MAC = "90:03:71:D8:A2:77"
NORMALIZED_MAC = "900371d8a277"
SIGNAL_UNIQUE_ID = f"entry123-device-{NORMALIZED_MAC}-signal_dbm"


def test_signal_text_is_read_as_dbm() -> None:
    """The column text the router prints should become the number it states."""
    assert parser.parse_client_signal_dbm("-52 dBm") == -52
    assert parser.parse_client_signal_dbm("-71dBm") == -71
    assert parser.parse_client_signal_dbm("  -60 DBM  ") == -60
    # A bare negative number is dBm by definition.
    assert parser.parse_client_signal_dbm("-49") == -49
    assert parser.parse_client_signal_dbm(-49) == -49


def test_columns_without_a_measurement_report_nothing() -> None:
    """Anything that is not a dBm reading should stay unknown, not become one."""
    for raw_signal in (None, "", "   ", "-", "--", "n/a", "65", "65%", "100 Mbps", True):
        assert parser.parse_client_signal_dbm(raw_signal) is None

    # 0 dBm is what an unmeasured cell decodes to, and -100 is the floor.
    assert parser.parse_client_signal_dbm("0 dBm") is None
    assert parser.parse_client_signal_dbm("-100 dBm") is None
    assert parser.parse_client_signal_dbm("-99 dBm") == -99


def test_device_list_publishes_the_reading_next_to_the_raw_text() -> None:
    """Each parsed client should carry both the text and the dBm value."""
    devices = parser.parse_devices(MODERN_DEVICES_HTML, None)["device_list"]
    by_mac = {device["mac"]: device for device in devices}

    gaming_pc = by_mac["AA-BB-CC-DD-EE-30"]
    assert gaming_pc["signal"] == "-54 dBm"
    assert gaming_pc["signal_dbm"] == -54

    tablet = by_mac["AA:BB:CC:DD:EE:31"]
    assert tablet["signal"] == "-61 dBm"
    assert tablet["signal_dbm"] == -61


def test_legacy_table_without_a_signal_column_stays_silent() -> None:
    """A layout that has no signal column should not invent a reading."""
    legacy_html = (ROOT / "tests" / "fixtures" / "devices" / "legacy_devices.html").read_text(
        encoding="utf-8"
    )

    for device in parser.parse_devices(legacy_html, None)["device_list"]:
        assert device["signal_dbm"] is None


def test_signal_sensor_is_a_dbm_measurement() -> None:
    """The client signal sensor should describe itself as a signal strength."""
    description = sensor_descriptions.DEVICE_SIGNAL_DETAILS_SENSOR

    assert description.key == "signal_dbm"
    assert description.name_suffix == "Signal"
    assert description.device_class == "signal_strength"
    assert description.native_unit_of_measurement == "dBm"
    assert description.state_class == "measurement"
    assert description.entity_category == "diagnostic"


class _Registry:
    def __init__(self, entities: dict[str, SimpleNamespace] | None = None) -> None:
        self.entities = entities or {}
        self.removed: list[str] = []
        self.updated: list[tuple[str, str]] = []

    def async_get_entity_id(self, domain: str, platform: str, unique_id: str):
        for entity in self.entities.values():
            if entity.domain == domain and entity.platform == platform and entity.unique_id == unique_id:
                return entity.entity_id
        return None

    def async_remove(self, entity_id: str) -> None:
        self.removed.append(entity_id)
        self.entities.pop(entity_id, None)

    def async_update_entity(self, entity_id: str, new_unique_id: str) -> None:
        self.updated.append((entity_id, new_unique_id))
        self.entities[entity_id].unique_id = new_unique_id


def _powerstation_device() -> dict:
    return {
        "hostname": "wlan0",
        "ip": "192.168.50.67",
        "mac": POWERSTATION_MAC,
        "up_speed": 0.0,
        "down_speed": 0.0,
        "connection_type": "WiFi 2.4G",
        "signal": "-52 dBm",
        "signal_dbm": -52,
        "online_time": "1 Day 02:03:04",
        "internet": True,
        "dnsfilter": None,
        "vpn": None,
    }


def _setup_client_sensors(monkeypatch, registry: _Registry) -> list:
    """Run the sensor platform for a single hand-picked client."""
    config_entry = SimpleNamespace(
        entry_id="entry123",
        data={"model": "M3000"},
        options={
            const.OPTIONS_AUTO_ADD_CONNECTED_DEVICES: False,
            const.OPTIONS_DEVICELIST: [POWERSTATION_MAC],
        },
        title="Salotto",
        async_on_unload=lambda unload: None,
    )
    coordinator = SimpleNamespace(
        api=SimpleNamespace(),
        config_entry=config_entry,
        data={
            const.MODULE_DEVICES: {
                const.SECTION_DEVICE_LIST: [_powerstation_device()],
            },
        },
        hass=None,
        async_add_listener=lambda listener: (lambda: None),
    )
    hass = SimpleNamespace(data={const.DOMAIN: {"entry123": coordinator}})
    coordinator.hass = hass

    monkeypatch.setattr(sensor, "async_get_entity_registry", lambda hass: registry)
    monkeypatch.setattr(sensor, "known_client_devices", lambda hass, entry: {})
    monkeypatch.setattr(
        sensor,
        "async_cleanup_stale_client_entities",
        lambda hass, entry, platform, allowed: None,
    )
    monkeypatch.setattr(sensor, "build_client_device_info", lambda hass, entry, device: {})

    added_entities: list = []
    asyncio.run(
        sensor.async_setup_entry(
            hass,
            config_entry,
            lambda entities: added_entities.extend(entities),
        )
    )
    return added_entities


def test_hand_picked_client_reports_its_signal_in_dbm(monkeypatch) -> None:
    """The picked client should get a numeric signal sensor and keep the raw text."""
    added_entities = _setup_client_sensors(monkeypatch, _Registry())

    signal_entities = [
        entity
        for entity in added_entities
        if entity.unique_id == SIGNAL_UNIQUE_ID
    ]
    assert len(signal_entities) == 1

    signal_sensor = signal_entities[0]
    assert signal_sensor.native_value == -52
    assert signal_sensor.extra_state_attributes == {"signal_text": "-52 dBm"}
    assert signal_sensor.available is True


def test_existing_text_signal_sensor_is_migrated_not_duplicated(monkeypatch) -> None:
    """An entity created before the change should keep its ID and history."""
    registry = _Registry(
        {
            "sensor.wlan0_signal": SimpleNamespace(
                domain="sensor",
                platform=const.DOMAIN,
                entity_id="sensor.wlan0_signal",
                unique_id=SIGNAL_UNIQUE_ID.removesuffix("_dbm"),
            ),
        }
    )

    added_entities = _setup_client_sensors(monkeypatch, registry)

    assert registry.updated == [("sensor.wlan0_signal", SIGNAL_UNIQUE_ID)]
    assert registry.removed == []
    assert sum(1 for entity in added_entities if entity.unique_id.endswith("-signal_dbm")) == 1


def test_leftover_text_sensor_is_removed_when_the_dbm_one_exists(monkeypatch) -> None:
    """Two registry rows for one reading should collapse onto the dBm entity."""
    migrated_unique_id = SIGNAL_UNIQUE_ID
    registry = _Registry(
        {
            "sensor.wlan0_signal": SimpleNamespace(
                domain="sensor",
                platform=const.DOMAIN,
                entity_id="sensor.wlan0_signal",
                unique_id=migrated_unique_id.removesuffix("_dbm"),
            ),
            "sensor.wlan0_signal_2": SimpleNamespace(
                domain="sensor",
                platform=const.DOMAIN,
                entity_id="sensor.wlan0_signal_2",
                unique_id=migrated_unique_id,
            ),
        }
    )

    _setup_client_sensors(monkeypatch, registry)

    assert registry.removed == ["sensor.wlan0_signal"]
    assert registry.updated == []
