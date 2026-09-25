"""Mesh node backhaul telemetry and per-node client list.

Fixtures are captured from a three-node M3000 mesh: a main router on a wired
backhaul, a satellite on a 5 GHz wireless backhaul, and a node that is powered
but has never joined (so it reports no sysreport at all).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.module_loader import load_cudy_module


mesh = load_cudy_module("mesh")
load_cudy_module("const")
load_cudy_module("model_names")
parser = load_cudy_module("parser")


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "mesh"


def _clients() -> list[dict]:
    return json.loads((FIXTURES / "mesh_clients.json").read_text(encoding="utf-8"))


@pytest.fixture(name="main_router")
def main_router_fixture() -> dict:
    return _clients()[0]


@pytest.fixture(name="satellite")
def satellite_fixture() -> dict:
    return _clients()[1]


@pytest.fixture(name="unjoined_node")
def unjoined_node_fixture() -> dict:
    return _clients()[2]


@pytest.fixture(name="devlist_html")
def devlist_html_fixture() -> str:
    return (FIXTURES / "mesh_client_devlist.html").read_text(encoding="utf-8")


def test_backhaul_station_is_picked_by_interface(satellite: dict) -> None:
    """The node associates on both radios; only the backhaul one counts."""
    station = mesh.select_backhaul_station(satellite["sysreport"])

    assert station is not None
    assert station["iface"] == "wlan11"
    # wlan01 is the other associated radio and must not be chosen.
    assert station["rssireal"] == -58


def test_backhaul_station_needs_a_named_interface() -> None:
    """Without a backhaul field there is no safe way to pick a radio."""
    sysreport = {"sta": [{"iface": "wlan01", "rssireal": -40}]}

    assert mesh.select_backhaul_station(sysreport) is None


def test_wireless_backhaul_reports_band_and_quality(satellite: dict) -> None:
    """A satellite on Wi-Fi reports its band, signal and negotiated rates."""
    telemetry = mesh.node_telemetry(satellite)

    assert telemetry["backhaul"] == "Wi-Fi 5 GHz"
    assert telemetry["backhaul_interface"] == "wlan11"
    assert telemetry["backhaul_signal"] == -58
    assert telemetry["backhaul_signal_chain0"] == -57
    assert telemetry["backhaul_signal_chain1"] == -58
    assert telemetry["backhaul_tx_rate"] == 2161
    assert telemetry["backhaul_rx_rate"] == 1729
    assert telemetry["backhaul_bandwidth"] == "160 MHz"
    assert telemetry["backhaul_standard"] == "Wi-Fi 6"
    assert telemetry["mesh_type"] == "auto"
    assert telemetry["hop"] == 11


def test_wired_backhaul_reports_no_link_quality(main_router: dict) -> None:
    """A wired node has no radio link, so quality fields stay absent."""
    telemetry = mesh.node_telemetry(main_router)

    assert telemetry["backhaul"] == mesh.WIRED_BACKHAUL_LABEL
    assert "backhaul_signal" not in telemetry
    assert "backhaul_tx_rate" not in telemetry
    assert "backhaul_rx_rate" not in telemetry


def test_node_reports_its_own_client_count(main_router: dict, satellite: dict) -> None:
    """devcnt is reported per node, so the count needs no HTML scraping."""
    assert mesh.node_telemetry(main_router)["connected_devices"] == 36
    assert mesh.node_telemetry(satellite)["connected_devices"] == 30


def test_node_load_is_reported(satellite: dict) -> None:
    """CPU and memory load come from sysstatinfo next to the radios."""
    telemetry = mesh.node_telemetry(satellite)

    assert telemetry["cpu_load"] == 5
    assert telemetry["memory_load"] == 73


def test_unjoined_node_yields_only_what_it_reports(unjoined_node: dict) -> None:
    """A node that never joined has no sysreport and must not raise."""
    telemetry = mesh.node_telemetry(unjoined_node)

    assert telemetry == {"connected_devices": 0}


def test_telemetry_tolerates_junk() -> None:
    """Unexpected payload shapes must not break a coordinator refresh."""
    assert mesh.node_telemetry(None) == {}
    assert mesh.node_telemetry({"sysreport": "nonsense"}) == {}
    assert mesh.node_telemetry({"sysreport": {"sta": "nonsense", "backhaul": "wlan11"}}) == {
        "backhaul": "wlan11",
        "backhaul_interface": "wlan11",
    }


def test_band_label_falls_back_to_channel() -> None:
    """Firmware that omits the bands list still resolves via radio channel."""
    sysreport = {"sysstatinfo": {"radio1": {"channel": 44}, "radio0": {"channel": "6"}}}

    assert mesh.band_label(0, sysreport) == "2.4 GHz"
    assert mesh.band_label(1, sysreport) == "5 GHz"
    assert mesh.band_label(None, sysreport) is None


def test_unknown_interface_keeps_the_raw_value() -> None:
    """An interface we cannot classify is reported verbatim, not dropped."""
    assert mesh.backhaul_label({"backhaul": "mesh0"}) == "mesh0"


def test_client_devices_are_parsed_from_devlist(devlist_html: str) -> None:
    """Every end device of the node is returned, not just counted."""
    devices = parser.parse_mesh_client_devices(devlist_html)

    assert len(devices) == 3
    assert devices[0] == {
        "hostname": "tablet-hall",
        "connection": "WiFi 5G",
        "ip_address": "192.168.1.35",
        "mac_address": "AA:BB:CC:00:11:22",
        "tx_rate_kbps": 12.5,
        "rx_rate_kbps": 3.25,
        "connected_time": "03:52:25",
    }
    assert [device["connection"] for device in devices] == ["WiFi 5G", "WiFi 2.4G", "Wired"]
    assert devices[2]["mac_address"] == "00:11:22:33:44:55"


def test_client_device_parsing_is_safe_without_html() -> None:
    """A failed devlist fetch returns an empty list rather than raising."""
    assert parser.parse_mesh_client_devices(None) == []
    assert parser.parse_mesh_client_devices("") == []
    assert parser.parse_mesh_client_devices("<html><body>nope</body></html>") == []


def test_client_status_exposes_list_and_matching_count(devlist_html: str) -> None:
    """The count the sensor shows must match the list it carries."""
    status = parser.parse_mesh_client_status(
        "<table><tr>"
        '<td><div id="cbi-table-1-content">Backhaul</div></td>'
        '<td><div id="cbi-table-1-data">5GHz</div></td>'
        "</tr></table>",
        devlist_html,
    )

    assert status is not None
    assert status["connected_devices"] == 3
    assert len(status["client_devices"]) == 3
    assert status["backhaul"] == "5GHz"


def _mesh_sensor(description_key: str, node: dict):
    """Build one mesh sensor of a node, the way the platform does."""
    from types import SimpleNamespace

    sensor = load_cudy_module("sensor")
    sensor_descriptions = load_cudy_module("sensor_descriptions")
    const = load_cudy_module("const")

    description = next(
        value
        for name, value in vars(sensor_descriptions).items()
        if name.startswith("MESH_DEVICE_")
        and getattr(value, "key", None) == description_key
    )
    coordinator = SimpleNamespace(
        hass=object(),
        config_entry=SimpleNamespace(entry_id="entry123", data={"model": "M3000"}),
        data={const.MODULE_MESH: {"mesh_devices": {"AA:BB:CC:11:22:44": node}}},
    )
    return sensor.CudyRouterMeshDeviceSensor(
        coordinator, "AA:BB:CC:11:22:44", node, description
    )


def _node_with_clients() -> dict:
    return {
        "name": "Office",
        "mac_address": "AA:BB:CC:11:22:44",
        "connected_devices": 2,
        "backhaul": "Wi-Fi 5 GHz",
        "backhaul_signal": -58,
        "backhaul_signal_chain0": -57,
        "cpu_load": 5,
        "client_devices": [
            {
                "hostname": "tablet-hall",
                "connection": "WiFi 5G",
                "ip_address": "192.168.1.35",
                "mac_address": "AA:BB:CC:00:11:22",
                "tx_rate_kbps": 12.5,
                "rx_rate_kbps": 3.25,
                "connected_time": "03:52:25",
            },
            {
                "hostname": "nas-box",
                "connection": "Wired",
                "ip_address": "192.168.1.60",
                "mac_address": "00:11:22:33:44:55",
                "tx_rate_kbps": 0.0,
                "rx_rate_kbps": 0.0,
                "connected_time": "120:01:02",
            },
        ],
    }


def test_client_list_is_published_on_the_count_sensor_only() -> None:
    """Repeating the list on all sensors of a node would multiply its cost."""
    node = _node_with_clients()

    counter = _mesh_sensor("connected_devices", node)
    other = _mesh_sensor("backhaul", node)

    assert "devices" in counter.extra_state_attributes
    assert "devices" not in other.extra_state_attributes


def test_published_client_fields_hold_still_between_refreshes() -> None:
    """Throughput and connection age would rewrite the attribute every poll."""
    attributes = _mesh_sensor("connected_devices", _node_with_clients()).extra_state_attributes

    assert attributes["devices"] == [
        {
            "hostname": "tablet-hall",
            "connection": "WiFi 5G",
            "ip_address": "192.168.1.35",
            "mac_address": "AA:BB:CC:00:11:22",
        },
        {
            "hostname": "nas-box",
            "connection": "Wired",
            "ip_address": "192.168.1.60",
            "mac_address": "00:11:22:33:44:55",
        },
    ]


def test_client_list_is_kept_out_of_the_recorder() -> None:
    """The list is live state, not history worth kilobytes per refresh."""
    sensor = load_cudy_module("sensor")

    assert "devices" in sensor.CudyRouterMeshDeviceSensor._unrecorded_attributes


def test_volatile_node_values_stay_off_the_shared_attributes() -> None:
    """Shared attributes are on every sensor, so they must not churn."""
    attributes = _mesh_sensor("backhaul", _node_with_clients()).extra_state_attributes

    assert "cpu_load" not in attributes
    assert "backhaul_signal_chain0" not in attributes


def test_signal_chains_ride_along_with_the_signal_sensor() -> None:
    """Per-chain readings belong to the sensor that already changes each poll."""
    attributes = _mesh_sensor("backhaul_signal", _node_with_clients()).extra_state_attributes

    assert attributes["backhaul_signal_chain0"] == -57


def _panel(*lines: str) -> str:
    """A Cudy panel, with every line rendered twice as the UI does."""
    body = "".join(
        f'<p class="form-control-static hidden-xs">{line}</p><p class="visible-xs">{line}</p>'
        for line in lines
    )
    return f'<div class="panel">{body}</div>'


def test_section_headings_are_not_turned_into_mesh_nodes() -> None:
    """"Mesh Units" labels the page's own section; it is not a node."""
    assert parser.parse_mesh_devices(_panel("Mesh Units"))["mesh_devices"] == {}
    assert parser.parse_mesh_devices(_panel("Device Name"))["mesh_devices"] == {}


def test_node_name_does_not_absorb_the_narrow_layout_copy() -> None:
    """The UI writes each line twice; the name must not contain both."""
    devices = parser.parse_mesh_devices(_panel("Satellite 1"))["mesh_devices"]

    assert [device["name"] for device in devices.values()] == ["Satellite 1"]


def test_name_derived_mac_is_marked_as_generated() -> None:
    """A node with no address of its own is keyed by a hash of its name."""
    devices = parser.parse_mesh_devices(_panel("Satellite 1"))["mesh_devices"]
    (mac, device), = devices.items()

    assert device["generated_mac"] == mac
    assert mac == parser._generate_pseudo_mac("Satellite 1")


def test_generated_macs_are_not_published_as_hardware_addresses() -> None:
    """A hashed MAC in connections would let HA match an unrelated device."""
    from types import SimpleNamespace

    device_info = load_cudy_module("device_info")
    coordinator = SimpleNamespace(
        config_entry=SimpleNamespace(entry_id="entry-1", data={}), data={}
    )

    generated = parser._generate_pseudo_mac("Satellite 1")
    invented = device_info.build_mesh_device_info(
        object(),
        coordinator,
        generated,
        {"name": "Satellite 1", "mac_address": generated, "generated_mac": generated},
    )
    reported = device_info.build_mesh_device_info(
        object(),
        coordinator,
        "AA:BB:CC:11:22:44",
        {"name": "Office", "mac_address": "AA:BB:CC:11:22:44"},
    )

    assert "connections" not in invented
    assert reported["connections"] == {("mac", "aa:bb:cc:11:22:44")}


def test_stale_devices_are_distinguished_from_the_ones_still_reported() -> None:
    """Home Assistant refuses every deletion unless the hook says the device is gone."""
    device_info = load_cudy_module("device_info")
    const = load_cudy_module("const")

    reported = device_info.reported_device_identifiers(
        "entry-1",
        {
            const.MODULE_MESH: {"mesh_devices": {"AA:BB:CC:11:22:44": {}}},
            const.MODULE_DEVICES: {
                const.SECTION_DEVICE_LIST: [{"mac": "AA:BB:CC:00:11:22"}]
            },
        },
    )

    # The router itself and everything it still lists must stay.
    assert "entry-1" in reported
    assert "entry-1-mesh-AA:BB:CC:11:22:44" in reported
    assert "entry-1-device-aabbcc001122" in reported
    # A node the router has stopped listing is a leftover the user can clear.
    assert "entry-1-mesh-4A:C8:1B:AE:8B:6B" not in reported


def test_reported_identifiers_survive_an_empty_refresh() -> None:
    """A failed refresh must not make every device look deletable."""
    device_info = load_cudy_module("device_info")

    assert device_info.reported_device_identifiers("entry-1", None) == {"entry-1"}


def _wr3600e_node(*, bands: str, radio1_channel=None) -> dict:
    """A node shaped like the WR3600E on 2.5.30b from issue #1.

    Its backhaul is the 160 MHz link on wlan11, which the router's own web
    interface reports as "5G WiFi".
    """
    sysstatinfo = {"radio0": {"channel": "6"}}
    if radio1_channel is not None:
        sysstatinfo["radio1"] = {"channel": radio1_channel}
    return {
        "devcnt": 7,
        "name": "EG",
        "state": "connected",
        "sysstatinfo": sysstatinfo,
        "sysreport": {
            "bands": bands,
            "backhaul": "wlan11",
            "meshtype": "auto",
            "sta": [
                {"iface": "wlan01", "bw": "ht20", "txrate": 144, "rxrate": 144},
                {"iface": "wlan11", "bw": "ht160", "txrate": 598, "rxrate": 938, "mode": "11ax"},
            ],
        },
    }


def test_band_comes_from_the_radio_channel_not_the_bands_order() -> None:
    """A channel is a measured fact; the order of `bands` is only a convention."""
    # This firmware lists its bands the other way round from the radio numbering.
    node = _wr3600e_node(bands="5G|2.4G", radio1_channel=100)

    assert mesh.node_telemetry(node)["backhaul"] == "Wi-Fi 5 GHz"


def test_a_wide_link_is_never_reported_as_2_4_ghz() -> None:
    """2.4 GHz tops out at 40 MHz, so a 160 MHz link cannot be on it."""
    # No channel to go by, and the bands order would wrongly say 2.4 GHz.
    node = _wr3600e_node(bands="5G|2.4G")

    assert mesh.node_telemetry(node)["backhaul"] == "Wi-Fi 5 GHz"


def test_the_bands_order_still_serves_when_nothing_contradicts_it() -> None:
    """It remains the best guess for a narrow link with no channel reported."""
    node = _wr3600e_node(bands="2.4G|5G")
    node["sysreport"]["backhaul"] = "wlan01"

    assert mesh.node_telemetry(node)["backhaul"] == "Wi-Fi 2.4 GHz"


def test_an_ambiguous_wide_link_is_left_unlabelled() -> None:
    """With 5 and 6 GHz both on offer, guessing would just be a coin flip."""
    node = _wr3600e_node(bands="5G|2.4G|6G")

    # Falls back to the raw interface rather than inventing a band.
    assert mesh.node_telemetry(node)["backhaul"] == "wlan11"


def test_a_negative_plain_rssi_is_read_as_dbm() -> None:
    """Some firmware reports only `rssi`; negative means it is already dBm."""
    node = _wr3600e_node(bands="5G|2.4G", radio1_channel=100)
    node["sysreport"]["sta"][1]["rssi"] = -60

    assert mesh.node_telemetry(node)["backhaul_signal"] == -60


def test_a_positive_plain_rssi_is_the_reading_shifted_by_100() -> None:
    """The WR3600E in #1 reports only `rssi`, as 41 and 44 for -59 and -56 dBm."""
    node = _wr3600e_node(bands="5G|2.4G", radio1_channel=100)
    node["sysreport"]["sta"][1]["rssi"] = 41

    assert mesh.node_telemetry(node)["backhaul_signal"] == -59


def test_the_calibrated_field_still_wins_where_both_exist(satellite: dict) -> None:
    """On the M3000 rssi is the same reading shifted, so it must not be preferred."""
    station = mesh.select_backhaul_station(satellite["sysreport"])

    assert station["rssi"] == 42 and station["rssireal"] == -58
    assert mesh.node_telemetry(satellite)["backhaul_signal"] == -58


def test_an_implausible_rssi_is_ignored_rather_than_reported() -> None:
    """A field that means something else must not surface as a wrong signal."""
    # 0 decodes to the -100 dBm floor: nothing was measured.
    assert mesh.signal_from_plain_rssi(0) is None
    # Neither a percentage-looking 150 nor junk should produce a reading.
    assert mesh.signal_from_plain_rssi(150) is None
    assert mesh.signal_from_plain_rssi("nonsense") is None
    assert mesh.signal_from_plain_rssi(None) is None
