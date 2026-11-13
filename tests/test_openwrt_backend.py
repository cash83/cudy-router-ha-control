"""The OpenWrt backend must hand the coordinator what the Cudy backend does."""

from __future__ import annotations

from typing import Any

from tests.module_loader import load_cudy_module

openwrt = load_cudy_module("openwrt")

MAIN = "https://192.168.50.1"
NODE = "https://192.168.50.2"


def _station(mac: str, signal: int, rx: int, tx: int, connected: int = 90061) -> dict[str, Any]:
    return {
        "mac": mac,
        "signal": signal,
        "inactive": 10,
        "connected_time": connected,
        "rx": {"bytes": rx, "rate": 864000},
        "tx": {"bytes": tx, "rate": 960700},
    }


def _radios(ap_bssid_2g: str, with_mesh: bool = True) -> dict[str, Any]:
    interfaces_5g = [
        {
            "section": "default_radio1",
            "ifname": "phy1-ap0",
            "config": {"mode": "ap", "ssid": "home_5G"},
            "iwinfo": {"channel": 36, "bssid": "82:AF:CA:00:00:01"},
        }
    ]
    if with_mesh:
        interfaces_5g.append(
            {
                "section": "mesh",
                "ifname": "phy1-mesh0",
                "config": {"mode": "mesh"},
                "iwinfo": {"bssid": "86:AF:CA:00:00:01"},
            }
        )
    return {
        "radio0": {
            "disabled": False,
            "config": {"band": "2g", "channel": "11", "htmode": "HT20"},
            "iwinfo": {"channel": 11, "htmodes": ["HT20", "HT40", "HE20", "HE40"]},
            "interfaces": [
                {
                    "section": "default_radio0",
                    "ifname": "phy0-ap0",
                    "config": {"mode": "ap", "ssid": "home", "hidden": "1"},
                    "iwinfo": {"channel": 11, "bssid": ap_bssid_2g},
                }
            ],
        },
        "radio1": {
            "disabled": False,
            "config": {"band": "5g", "channel": "auto", "htmode": "HE80", "txpower": "20"},
            "iwinfo": {"channel": 36, "htmodes": ["HT20", "VHT80", "HE80"]},
            "interfaces": interfaces_5g,
        },
    }


class _Router(openwrt.OpenWrtRouter):
    """An OpenWrtRouter whose ubus calls are answered from a table."""

    def __init__(self) -> None:
        super().__init__(None, MAIN, "root", "secret", "M3000 V1.0")
        self.calls: list[tuple[str, str, str, dict]] = []
        self.stations = {
            (MAIN, "phy0-ap0"): [_station("AA:BB:CC:00:00:01", -50, 1_000_000, 2_000_000)],
            (MAIN, "phy1-ap0"): [],
            (NODE, "phy0-ap0"): [_station("AA:BB:CC:00:00:02", -60, 0, 0)],
            (NODE, "phy1-ap0"): [],
            (NODE, "phy1-mesh0"): [_station("86:AF:CA:00:00:01", -64, 5, 5)],
        }

    def _call(self, obj, method, args=None, *, base=None, default=None, strict=False):  # noqa: ANN001
        base = base or self.base_url
        args = args or {}
        self.calls.append((base, obj, method, args))
        key = f"{obj}.{method}"
        if key == "system.board":
            name = "Salotto" if base == MAIN else "Ufficio"
            return {
                "hostname": name,
                "model": "Cudy M3000 v1/v2",
                "board_name": "cudy,m3000-v1",
                "release": {"distribution": "OpenWrt", "version": "25.12.6"},
            }
        if key == "system.info":
            return {
                "uptime": 3600,
                "localtime": 1794565502,
                "memory": {"total": 200, "available": 50},
            }
        if key == "network.interface.dump":
            return {
                "interface": [
                    {
                        "interface": "wan",
                        "up": True,
                        "uptime": 120,
                        "proto": "dhcp",
                        "l3_device": "eth0",
                        "ipv4-address": [{"address": "8.8.4.4", "mask": 24}],
                        "route": [{"target": "0.0.0.0", "nexthop": "8.8.4.1"}],
                        "dns-server": ["192.168.50.165", "1.1.1.1"],
                    },
                    {
                        "interface": "lan",
                        "l3_device": "br-lan",
                        "ipv4-address": [{"address": "192.168.50.1", "mask": 24}],
                    },
                ]
            }
        if key == "network.device.status":
            if args.get("name") == "eth0":
                return {
                    "macaddr": "80:af:ca:00:00:02",
                    "statistics": {"rx_bytes": 2 * 1024 * 1024, "tx_bytes": 1024 * 1024},
                }
            return {"macaddr": "80:af:ca:00:00:03" if base == MAIN else "80:af:ca:00:00:11"}
        if key == "uci.get" and args.get("config") == "dhcp":
            return {"values": {"lan": {"start": "10", "limit": "241", "leasetime": "2h"}}}
        if key == "uci.get" and args.get("config") == "firewall":
            return {"values": {"cudyblk_aabbcc000002": {"target": "REJECT"}}}
        if key == "luci-rpc.getDHCPLeases":
            return {
                "dhcp_leases": [
                    {"macaddr": "AA:BB:CC:00:00:01", "ipaddr": "192.168.50.20", "hostname": "phone"}
                ]
            }
        if key == "luci-rpc.getHostHints":
            return {"AA:BB:CC:00:00:09": {"ipaddrs": ["192.168.50.165"], "name": "ha.lan"}}
        if key == "file.exec":
            return {
                "stdout": (
                    "192.168.50.165 dev br-lan lladdr aa:bb:cc:00:00:09 ref 1 REACHABLE\n"
                    "192.168.50.20 dev br-lan lladdr aa:bb:cc:00:00:01 used 0/0/0 STALE\n"
                    "8.8.4.1 dev eth0 lladdr aa:bb:cc:00:00:77 used 0/0/0 REACHABLE\n"
                    "192.168.50.99 dev br-lan  used 0/0/0 probes 6 FAILED\n"
                )
            }
        if key == "file.read":
            return {"data": "cpu  100 0 100 800 0 0 0 0 0 0\n"}
        if key == "luci-rpc.getWirelessDevices":
            return _radios("80:AF:CA:00:00:01" if base == MAIN else "80:AF:CA:00:00:10")
        if key == "iwinfo.assoclist":
            return {"results": self.stations.get((base, args.get("device")), [])}
        if key == "iwinfo.freqlist":
            return {
                "results": [
                    {"channel": 36, "mhz": 5180, "restricted": False},
                    {"channel": 52, "mhz": 5260, "restricted": True},
                ]
            }
        if key == "iwinfo.txpowerlist":
            return {"results": [{"dbm": 0}, {"dbm": 23}]}
        if key == "luci.getLEDs":
            return {"white:status": {"active_trigger": "none", "brightness": 1}}
        if key == "usteer.remote_info":
            return {"192.168.50.2#hostapd.phy0-ap0": {}, "192.168.50.2#hostapd.phy1-ap0": {}}
        return default


def test_collect_matches_the_stock_layout(monkeypatch) -> None:
    router = _Router()
    monkeypatch.setattr(openwrt.time, "sleep", lambda _seconds: None)
    data = router.collect()

    clients = {c["mac"]: c for c in data["devices"]["device_list"]}
    assert set(clients) == {"AA:BB:CC:00:00:01", "AA:BB:CC:00:00:02", "AA:BB:CC:00:00:09"}
    phone = clients["AA:BB:CC:00:00:01"]
    assert phone["hostname"] == "phone"
    assert phone["ip"] == "192.168.50.20"
    assert phone["connection_type"] == "WiFi 2.4G"
    assert (phone["signal"], phone["signal_dbm"]) == ("-50 dBm", -50)
    assert phone["online_time"] == "1 Day 01:01:01"
    assert phone["internet"] is True
    assert clients["AA:BB:CC:00:00:02"]["internet"] is False
    assert clients["AA:BB:CC:00:00:09"]["connection_type"] == "Wired"
    assert clients["AA:BB:CC:00:00:09"]["hostname"] == "ha"
    # Speeds are always numbers, since the top-talker sensors compare them.
    assert all(isinstance(c["down_speed"], float) for c in clients.values())

    devices = data["devices"]
    assert devices["device_count"]["value"] == 3
    assert devices["wifi_2g_clients"]["value"] == 2
    assert devices["wired_clients"]["value"] == 1
    assert devices["arp_br_lan_count"]["value"] == 2

    assert data["system"]["ram_usage"]["value"] == 75.0
    assert data["system"]["firmware_version"]["value"] == "OpenWrt 25.12.6"
    assert data["lan"]["mac_address"]["value"] == "80:AF:CA:00:00:03"
    assert data["wan"]["public_ip"]["value"] == "8.8.4.4"
    assert data["wan"]["gateway"]["value"] == "8.8.4.1"
    assert data["wan"]["session_download"]["value"] == 2.0
    assert data["dhcp"]["dhcp_ip_start"]["value"] == "192.168.50.10"
    assert data["dhcp"]["dhcp_ip_end"]["value"] == "192.168.50.250"
    assert data["dhcp"]["dhcp_leasetime"]["value"] == "120 Minutes"
    assert data["wifi_2g"] == {"ssid": {"value": "home"}, "channel": {"value": 11}}

    settings = data["wireless_settings"]
    assert settings["wifi_2g_hidden"]["value"] is True
    assert settings["wifi_2g_mode"]["value"] == "11bgn"
    assert settings["wifi_5g_mode"]["value"] == "11anacax"
    assert settings["wifi_5g_channel_width"]["value"] == "ht80"
    assert settings["wifi_5g_channel"]["value"] == "0"
    assert settings["wifi_5g_channel"]["options"] == {"0": "Auto", "36": "36 (5180 MHz)"}
    assert settings["wifi_5g_tx_power"]["value"] == "50"
    for key in ("wifi_5g_mode", "wifi_5g_channel_width", "wifi_5g_channel", "wifi_5g_tx_power"):
        assert settings[key]["value"] in settings[key]["options"]

    # Entities that have no counterpart on OpenWrt still get a value, because
    # an entity without one is deleted from the registry at setup.
    assert data["wireless_settings"]["smart_connect"]["value"] is False
    assert data["vpn_settings"]["enabled"]["value"] is False
    assert data["wisp"]["status"]["value"] == "Disabled"
    assert "modem" not in data


def test_second_node_is_reported_as_a_mesh_satellite(monkeypatch) -> None:
    router = _Router()
    monkeypatch.setattr(openwrt.time, "sleep", lambda _seconds: None)
    mesh = router.collect()["mesh"]

    assert mesh["mesh_count"]["value"] == 1
    assert mesh["main_router_name"] == "Salotto"
    assert mesh["main_router_led_status"] == "on"
    node = mesh["mesh_devices"]["80:AF:CA:00:00:10"]
    assert node["name"] == "Ufficio"
    assert node["model"] == "M3000"
    assert node["hardware"] == "M3000 V1"
    assert node["status"] == "online"
    assert node["ip_address"] == "192.168.50.2"
    assert node["backhaul"] == "Wi-Fi 5 GHz"
    assert node["backhaul_signal"] == -64
    assert (node["backhaul_tx_rate"], node["backhaul_rx_rate"]) == (960, 864)
    assert node["connected_devices"] == 1
    assert node["client_devices"][0]["mac_address"] == "AA:BB:CC:00:00:02"
    assert not any(key.startswith("_") for key in node)


def test_a_node_that_stops_answering_is_kept_as_offline(monkeypatch) -> None:
    router = _Router()
    monkeypatch.setattr(openwrt.time, "sleep", lambda _seconds: None)
    router.collect()

    original = router._call

    def failing(obj, method, args=None, *, base=None, default=None, strict=False):  # noqa: ANN001
        if base == NODE:
            raise openwrt.OpenWrtError("unreachable")
        return original(obj, method, args, base=base, default=default, strict=strict)

    router._call = failing  # type: ignore[method-assign]
    node = router.collect()["mesh"]["mesh_devices"]["80:AF:CA:00:00:10"]
    assert node["status"] == "offline"
    assert node["connected_devices"] == 0
    assert node["name"] == "Ufficio"


def test_speed_comes_from_the_counters_of_two_polls() -> None:
    router = _Router()
    assert router._speed("aa", 0, 0, 100.0) == (0.0, 0.0)
    assert router._speed("aa", 1_250_000, 2_500_000, 110.0) == (1.0, 2.0)
    # A client that reconnected starts counting from zero again.
    assert router._speed("aa", 10, 10, 120.0) == (0.0, 0.0)


def test_wireless_change_restarts_the_radio_that_carries_the_mesh(monkeypatch) -> None:
    router = _Router()
    monkeypatch.setattr(openwrt.time, "sleep", lambda _seconds: None)
    router.collect()
    router.calls.clear()

    assert router.set_wireless_setting("wifi_5g_channel_width", "ht40") == (200, "OK")
    written = [c for c in router.calls if c[1:3] == ("uci", "set")]
    assert [c[0] for c in written] == [NODE, MAIN]
    assert all(c[3]["values"] == {"htmode": "HE40"} for c in written)
    assert [c[2] for c in router.calls if c[1] == "network.wireless"] == ["down", "up"] * 2

    router.calls.clear()
    assert router.set_wireless_setting("wifi_2g_channel", "0") == (200, "OK")
    written = [c for c in router.calls if c[1:3] == ("uci", "set")]
    assert [(c[0], c[3]["values"]) for c in written] == [(MAIN, {"channel": "auto"})]
    assert not [c for c in router.calls if c[1] == "network.wireless"]

    router.calls.clear()
    assert router.set_wireless_setting("wifi_2g_enabled", False) == (200, "OK")
    assert all(
        c[3]["values"] == {"disabled": "1"} for c in router.calls if c[1:3] == ("uci", "set")
    )


def test_blocking_a_client_writes_one_named_firewall_rule() -> None:
    router = _Router()
    device = {"mac": "AA-BB-CC-00-00-01", "hostname": "phone"}
    assert router.set_device_access(device, "internet", False) == (200, "OK")
    added = [c for c in router.calls if c[1:3] == ("uci", "add")]
    assert added[0][3]["name"] == "cudyblk_aabbcc000001"
    assert added[0][3]["values"]["src_mac"] == "AA:BB:CC:00:00:01"

    router.calls.clear()
    assert router.set_device_access(device, "internet", True) == (200, "OK")
    assert not [c for c in router.calls if c[1:3] == ("uci", "add")]
    assert [c[3]["section"] for c in router.calls if c[1:3] == ("uci", "delete")] == [
        "cudyblk_aabbcc000001"
    ]
    assert router.set_device_access(device, "vpn", True)[0] == 501


def test_stock_firmware_is_not_mistaken_for_openwrt(monkeypatch) -> None:
    class _Reply:
        def __init__(self, text: str = "", payload: Any = None) -> None:
            self.text = text
            self._payload = payload

        def json(self) -> Any:
            if self._payload is None:
                raise ValueError("not json")
            return self._payload

    rpc = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32002}}
    monkeypatch.setattr(openwrt.requests, "post", lambda *a, **k: _Reply(payload=rpc))

    monkeypatch.setattr(
        openwrt.requests, "get", lambda *a, **k: _Reply("<span>HW: M3000 V1.0</span>")
    )
    assert openwrt.is_openwrt("192.168.10.1") is False

    monkeypatch.setattr(openwrt.requests, "get", lambda *a, **k: _Reply("<title>LuCI</title>"))
    assert openwrt.is_openwrt("192.168.10.1") is True

    monkeypatch.setattr(openwrt.requests, "post", lambda *a, **k: _Reply("<html>"))
    assert openwrt.is_openwrt("192.168.10.1") is False
