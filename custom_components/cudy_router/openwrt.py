"""Backend for Cudy routers that have been flashed with OpenWrt.

Stock firmware is driven by scraping its web pages (router.py). OpenWrt has no
such pages, but exposes the same information as JSON over ubus, so this module
talks to `/ubus` and produces the very same data structure the Cudy backend
returns. Entities, unique ids and devices therefore stay exactly as they are:
only the source of the numbers changes.

A second OpenWrt node (an access point joined over 802.11s, WDS or cable) is
reported the way a Cudy mesh satellite is. Nodes are discovered through usteer,
which OpenWrt uses to let access points talk to each other, and are queried
with the credentials of the main router.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import requests
import urllib3

from .const import (
    MODULE_AUTO_UPDATE_SETTINGS,
    MODULE_DEVICES,
    MODULE_DHCP,
    MODULE_LAN,
    MODULE_MESH,
    MODULE_SYSTEM,
    MODULE_VPN,
    MODULE_VPN_SETTINGS,
    MODULE_WAN,
    MODULE_WIFI_2G,
    MODULE_WIFI_5G,
    MODULE_WIRELESS_SETTINGS,
    MODULE_WISP,
    SECTION_DEVICE_LIST,
    SECTION_MESH_DEVICES,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

BACKEND_CUDY = "cudy"
BACKEND_OPENWRT = "openwrt"
CONF_BACKEND = "backend"

_ANONYMOUS_SESSION = "0" * 32
_TIMEOUT = 10
_UBUS_PERMISSION_DENIED = 6
_RPC_ACCESS_DENIED = -32002

# Markers that only the vendor firmware prints on its login page. The vendor
# firmware is itself built on OpenWrt and may answer on /ubus too, so the
# absence of these is what tells a plain OpenWrt router apart.
_CUDY_PAGE_MARKERS = ("Shenzhen Cudy", "www.cudy.com", "HW: ")

_NOT_SUPPORTED = (501, "Not available on a router running OpenWrt")
_OK = (200, "OK")

_BLOCK_RULE_PREFIX = "cudyblk_"
_PUBLIC_IP_URL = "https://api.ipify.org"
_PUBLIC_IP_MAX_AGE = 900
_WIRED_GRACE = 180

_BANDS = {"2g": ("wifi_2g", MODULE_WIFI_2G, "WiFi 2.4G", "Wi-Fi 2.4 GHz"),
          "5g": ("wifi_5g", MODULE_WIFI_5G, "WiFi 5G", "Wi-Fi 5 GHz")}

_MODE_OPTIONS = {
    "2g": {"11bgn": "2.4GHz (802.11b+g+n)", "11bgnax": "2.4GHz (802.11b+g+n+ax)"},
    "5g": {
        "11an": "5GHz (802.11a+n)",
        "11anac": "5GHz (802.11a+n+ac)",
        "11anacax": "5GHz (802.11a+n+ac+ax)",
    },
}
_MODE_PREFIX = {"11bgn": "HT", "11an": "HT", "11anac": "VHT", "11bgnax": "HE", "11anacax": "HE"}
_TX_POWER_OPTIONS = {"100": "High", "50": "Middle", "20": "Low"}
_TX_POWER_BACKOFF = {"100": 0, "50": 3, "20": 7}


class OpenWrtError(Exception):
    """A ubus call failed."""


def _base_url(host: str) -> str:
    host = host.strip()
    if not host.startswith(("http://", "https://")):
        host = f"https://{host}"
    return host.rstrip("/")


def is_openwrt(host: str) -> bool:
    """Tell whether the router at `host` runs plain OpenWrt.

    Never raises: anything unexpected means "not OpenWrt", which leaves the
    stock-firmware code path in charge.
    """
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    base = _base_url(host)
    parsed = urllib.parse.urlsplit(base)
    candidates = [base]
    other = "http" if parsed.scheme == "https" else "https"
    candidates.append(urllib.parse.urlunsplit((other, parsed.netloc, "", "", "")))
    for candidate in candidates:
        try:
            reply = requests.post(
                f"{candidate}/ubus",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "call",
                    "params": [_ANONYMOUS_SESSION, "session", "access", {}],
                },
                timeout=_TIMEOUT,
                verify=False,
            )
            payload = reply.json()
        except Exception:  # noqa: BLE001 - any failure means "not OpenWrt here"
            continue
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            continue
        try:
            page = requests.get(
                f"{candidate}/cgi-bin/luci/", timeout=_TIMEOUT, verify=False
            ).text
        except Exception:  # noqa: BLE001
            page = ""
        if any(marker in page for marker in _CUDY_PAGE_MARKERS):
            return False
        return True
    return False


def _mac12(mac: str | None) -> str:
    return re.sub(r"[^0-9a-f]", "", (mac or "").lower())


def _mac_colon(mac: str | None) -> str:
    raw = _mac12(mac).upper()
    return ":".join(raw[i : i + 2] for i in range(0, 12, 2)) if len(raw) == 12 else ""


def _duration_text(seconds: Any) -> str | None:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return None
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    clock = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{days} Day {clock}" if days else clock


def _netmask(prefix: Any) -> str | None:
    try:
        return str(ipaddress.ip_network(f"0.0.0.0/{int(prefix)}").netmask)
    except (TypeError, ValueError):
        return None


def _lease_minutes(raw: str | None) -> str | None:
    match = re.fullmatch(r"\s*(\d+)\s*([smhdw]?)\s*", raw or "")
    if not match:
        return raw or None
    amount = int(match.group(1))
    factor = {"s": 1 / 60, "m": 1, "": 1, "h": 60, "d": 1440, "w": 10080}[match.group(2)]
    return f"{int(amount * factor)} Minutes"


def _short_model(board_model: str | None) -> str:
    """'Cudy M3000 v1/v2' -> 'M3000'."""
    text = re.sub(r"^\s*cudy\s+", "", board_model or "", flags=re.IGNORECASE)
    return text.split()[0] if text.split() else "OpenWrt"


def _hardware(board: dict[str, Any]) -> str:
    """'cudy,m3000-v1' -> 'M3000 V1'."""
    name = str(board.get("board_name") or "").split(",")[-1]
    match = re.fullmatch(r"(.+?)-v(\d+)", name)
    if match:
        return f"{match.group(1).upper()} V{match.group(2)}"
    return _short_model(board.get("model"))


def _value(value: Any, attributes: dict[str, Any] | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"value": value}
    if attributes:
        entry["attributes"] = attributes
    return entry


class OpenWrtRouter:
    """Drop-in replacement for CudyRouter, for a router running OpenWrt."""

    def __init__(
        self,
        hass: HomeAssistant | None,
        host: str,
        username: str,
        password: str,
        model: str = "default",
    ) -> None:
        self.hass = hass
        self.host = host
        self.username = username
        self.password = password
        self.model = model
        self.base_url = _base_url(host)
        self._http = requests.Session()
        self._http.verify = False
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._sessions: dict[str, str] = {}
        self._cpu_sample: tuple[int, int] | None = None
        self._byte_samples: dict[str, tuple[int, int, float]] = {}
        self._wired_seen: dict[str, tuple[float, str]] = {}
        self._nodes: dict[str, dict[str, Any]] = {}
        self._node_urls: dict[str, str] = {}
        self._led_snapshots: dict[str, dict[str, tuple[str, int]]] = {}
        self._public_ip: tuple[float, str] | None = None

    # ------------------------------------------------------------------ rpc

    def _post(self, base: str, session: str, obj: str, method: str, args: dict) -> Any:
        reply = self._http.post(
            f"{base}/ubus",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "call",
                "params": [session, obj, method, args],
            },
            timeout=_TIMEOUT,
        )
        reply.raise_for_status()
        return reply.json()

    def _login(self, base: str) -> bool:
        payload = self._post(
            base,
            _ANONYMOUS_SESSION,
            "session",
            "login",
            {"username": self.username, "password": self.password},
        )
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, list) or not result or result[0] != 0:
            self._sessions.pop(base, None)
            return False
        session = (result[1] or {}).get("ubus_rpc_session") if len(result) > 1 else None
        if not session:
            return False
        self._sessions[base] = session
        return True

    def _call(
        self,
        obj: str,
        method: str,
        args: dict | None = None,
        *,
        base: str | None = None,
        default: Any = None,
        strict: bool = False,
    ) -> Any:
        """Run one ubus call; log in again once when the session has expired."""
        base = base or self.base_url
        for attempt in (1, 2):
            session = self._sessions.get(base)
            if session is None:
                if not self._login(base):
                    raise OpenWrtError(f"login refused by {base}")
                session = self._sessions[base]
            payload = self._post(base, session, obj, method, args or {})
            error = payload.get("error") if isinstance(payload, dict) else None
            result = payload.get("result") if isinstance(payload, dict) else None
            denied = (
                isinstance(error, dict) and error.get("code") == _RPC_ACCESS_DENIED
            ) or (isinstance(result, list) and result[:1] == [_UBUS_PERMISSION_DENIED])
            if denied and attempt == 1:
                self._sessions.pop(base, None)
                continue
            if isinstance(result, list) and result and result[0] == 0:
                return result[1] if len(result) > 1 else {}
            if strict:
                raise OpenWrtError(f"{obj}.{method} on {base}: {error or result}")
            return default
        return default

    # -------------------------------------------------------------- identity

    def authenticate(self) -> bool:
        """Log in. False means the credentials were refused."""
        try:
            return self._login(self.base_url)
        except requests.exceptions.SSLError:
            parsed = urllib.parse.urlsplit(self.base_url)
            self.base_url = urllib.parse.urlunsplit(("http", parsed.netloc, "", "", ""))
            return self._login(self.base_url)

    def get_model(self) -> str:
        if self.model and self.model != "default":
            return self.model
        try:
            if self.authenticate():
                return _short_model((self._call("system", "board") or {}).get("model"))
        except Exception:  # noqa: BLE001 - the model is cosmetic
            _LOGGER.debug("Could not read the OpenWrt board model", exc_info=True)
        return "default"

    def get(self, url: str, silent: bool = False) -> str:  # noqa: ARG002
        """There are no pages to fetch on OpenWrt."""
        return ""

    def debug_get(self, path: str) -> dict[str, Any]:
        return {"path": path, "status": None, "body": "", "note": "OpenWrt backend"}

    # ------------------------------------------------------------------ data

    async def get_data(
        self, hass: HomeAssistant, options: Any, device_model: str = "default"
    ) -> dict[str, Any]:
        del options, device_model
        return await hass.async_add_executor_job(self.collect)

    def _wireless(self, base: str) -> dict[str, dict[str, Any]]:
        """Describe each band of one device: radio, access point, mesh link."""
        status = self._call("luci-rpc", "getWirelessDevices", base=base) or {}
        bands: dict[str, dict[str, Any]] = {}
        for radio_name, radio in status.items():
            config = radio.get("config") or {}
            band = config.get("band")
            if band not in _BANDS or band in bands:
                continue
            entry: dict[str, Any] = {
                "radio": radio_name,
                "radio_config": config,
                "radio_iwinfo": radio.get("iwinfo") or {},
                "radio_disabled": bool(radio.get("disabled")),
                "ap": None,
                "mesh": None,
            }
            for iface in radio.get("interfaces") or []:
                mode = (iface.get("config") or {}).get("mode")
                if mode == "ap" and entry["ap"] is None:
                    entry["ap"] = iface
                elif mode == "mesh" and entry["mesh"] is None:
                    entry["mesh"] = iface
                elif mode == "sta" and (iface.get("config") or {}).get("wds"):
                    entry["mesh"] = entry["mesh"] or iface
            bands[band] = entry
        return bands

    def _stations(self, base: str, ifname: str | None) -> list[dict[str, Any]]:
        if not ifname:
            return []
        reply = self._call("iwinfo", "assoclist", {"device": ifname}, base=base) or {}
        return [s for s in reply.get("results") or [] if isinstance(s, dict)]

    def _leds_on(self, base: str) -> str | None:
        leds = self._call("luci", "getLEDs", base=base)
        if not isinstance(leds, dict) or not leds:
            return None
        lit = any(
            (led.get("brightness") or 0) > 0 or led.get("active_trigger") not in (None, "none")
            for led in leds.values()
            if isinstance(led, dict)
        )
        return "on" if lit else "off"

    def _cpu_usage(self, fallback: float | None = None) -> float | None:
        """Share of processor time in use; `fallback` when /proc/stat is off limits."""
        def sample() -> tuple[int, int] | None:
            text = (self._call("file", "read", {"path": "/proc/stat"}) or {}).get("data", "")
            fields = text.split("\n", 1)[0].split()[1:]
            if len(fields) < 5:
                return None
            numbers = [int(x) for x in fields]
            return sum(numbers), numbers[3] + numbers[4]

        now = sample()
        if now is None:
            return fallback
        before = self._cpu_sample
        if before is None:
            time.sleep(0.5)
            before, now = now, sample() or now
        self._cpu_sample = now
        total = now[0] - before[0]
        idle = now[1] - before[1]
        if total <= 0:
            return 0.0
        return round(max(0.0, min(100.0, 100.0 * (total - idle) / total)), 1)

    def _external_ip(self, wan_ip: str | None) -> str | None:
        """The address the internet sees, which differs behind another router."""
        try:
            if wan_ip and ipaddress.ip_address(wan_ip).is_global:
                return wan_ip
        except ValueError:
            pass
        now = time.monotonic()
        if self._public_ip and now - self._public_ip[0] < _PUBLIC_IP_MAX_AGE:
            return self._public_ip[1]
        try:
            text = requests.get(_PUBLIC_IP_URL, timeout=5).text.strip()
            ipaddress.ip_address(text)
        except Exception:  # noqa: BLE001 - keep the last good answer
            return self._public_ip[1] if self._public_ip else wan_ip
        self._public_ip = (now, text)
        return text

    def _speed(self, mac: str, rx_bytes: int, tx_bytes: int, now: float) -> tuple[float, float]:
        """(upload, download) in Mbit/s from the counters of two polls."""
        previous = self._byte_samples.get(mac)
        self._byte_samples[mac] = (rx_bytes, tx_bytes, now)
        if previous is None:
            return 0.0, 0.0
        elapsed = now - previous[2]
        d_rx, d_tx = rx_bytes - previous[0], tx_bytes - previous[1]
        if elapsed <= 0 or d_rx < 0 or d_tx < 0:
            return 0.0, 0.0
        return round(d_rx * 8 / elapsed / 1e6, 3), round(d_tx * 8 / elapsed / 1e6, 3)

    def _discover_nodes(self) -> dict[str, str]:
        """Addresses of the other access points, as usteer sees them."""
        parsed = urllib.parse.urlsplit(self.base_url)
        remote = self._call("usteer", "remote_info") or {}
        for key in remote:
            address = str(key).split("#", 1)[0]
            try:
                ipaddress.ip_address(address)
            except ValueError:
                continue
            self._node_urls.setdefault(address, f"{parsed.scheme}://{address}")
        return dict(self._node_urls)

    def _blocked_macs(self) -> set[str]:
        firewall = (self._call("uci", "get", {"config": "firewall"}) or {}).get("values") or {}
        return {
            name[len(_BLOCK_RULE_PREFIX):]
            for name, section in firewall.items()
            if name.startswith(_BLOCK_RULE_PREFIX) and section.get("enabled") != "0"
        }

    def collect(self) -> dict[str, Any]:
        """Read everything and lay it out like the stock-firmware backend does."""
        now = time.monotonic()
        board = self._call("system", "board", strict=True)
        info = self._call("system", "info") or {}
        interfaces = {
            item.get("interface"): item
            for item in (self._call("network.interface", "dump") or {}).get("interface") or []
        }
        wan = interfaces.get("wan") or {}
        lan = interfaces.get("lan") or {}
        wan_device = (
            self._call("network.device", "status", {"name": wan.get("l3_device") or "eth0"})
            or {}
        )
        lan_device = (
            self._call("network.device", "status", {"name": lan.get("l3_device") or "br-lan"})
            or {}
        )
        dhcp_config = (self._call("uci", "get", {"config": "dhcp"}) or {}).get("values") or {}
        leases = (self._call("luci-rpc", "getDHCPLeases") or {}).get("dhcp_leases") or []
        hints = self._call("luci-rpc", "getHostHints") or {}
        neighbours = (
            self._call(
                "file",
                "exec",
                {"command": "/sbin/ip", "params": ["-4", "neigh", "show"]},
            )
            or {}
        ).get("stdout", "")
        blocked = self._blocked_macs()
        hostname = board.get("hostname") or "OpenWrt"

        ip_by_mac: dict[str, str] = {}
        name_by_mac: dict[str, str] = {}
        for mac, hint in hints.items():
            key = _mac12(mac)
            if hint.get("ipaddrs"):
                ip_by_mac[key] = hint["ipaddrs"][0]
            if hint.get("name"):
                name_by_mac[key] = str(hint["name"]).split(".")[0]
        for lease in leases:
            key = _mac12(lease.get("macaddr"))
            if lease.get("ipaddr"):
                ip_by_mac[key] = lease["ipaddr"]
            if lease.get("hostname"):
                name_by_mac[key] = lease["hostname"]

        # ---- access points: this router first, then every other node
        own_macs = {_mac12(lan_device.get("macaddr")), _mac12(wan_device.get("macaddr"))}
        stations: dict[str, dict[str, Any]] = {}
        main_bands = self._wireless(self.base_url)

        def add_stations(base: str, bands: dict[str, dict[str, Any]], node: str) -> list[str]:
            seen: list[str] = []
            for band, entry in bands.items():
                ifname = (entry["ap"] or {}).get("ifname")
                for station in self._stations(base, ifname):
                    key = _mac12(station.get("mac"))
                    if len(key) != 12:
                        continue
                    seen.append(key)
                    known = stations.get(key)
                    if known and known["inactive"] <= (station.get("inactive") or 0):
                        continue
                    stations[key] = {
                        "band": band,
                        "node": node,
                        "signal": station.get("signal"),
                        "inactive": station.get("inactive") or 0,
                        "connected_time": station.get("connected_time"),
                        "rx": (station.get("rx") or {}).get("bytes") or 0,
                        "tx": (station.get("tx") or {}).get("bytes") or 0,
                    }
            return seen

        add_stations(self.base_url, main_bands, hostname)

        mesh_devices: dict[str, dict[str, Any]] = {}
        main_mesh_mac = None
        for entry in main_bands.values():
            if entry["mesh"]:
                main_mesh_mac = ((entry["mesh"].get("iwinfo") or {}).get("bssid") or "").upper()
        for address, node_base in self._discover_nodes().items():
            try:
                node = self._collect_node(address, node_base, add_stations, main_mesh_mac)
            except Exception as err:  # noqa: BLE001 - a node may simply be off
                _LOGGER.debug("OpenWrt node %s did not answer: %s", address, err)
                self._sessions.pop(node_base, None)
                node = None
            if node is None:
                cached = next(
                    (n for n in self._nodes.values() if n.get("ip_address") == address), None
                )
                if cached is None:
                    continue
                node = {**cached, "status": "offline", "connected_devices": 0,
                        "client_devices": []}
            self._nodes[node["mac_address"]] = node
            own_macs.update(node.pop("_own_macs", set()))
        for mac, node in self._nodes.items():
            mesh_devices[mac] = {k: v for k, v in node.items() if not k.startswith("_")}

        # ---- client list
        clients: list[dict[str, Any]] = []
        for key, station in stations.items():
            label = _BANDS[station["band"]][2]
            up_speed, down_speed = self._speed(key, station["rx"], station["tx"], now)
            signal = station["signal"]
            clients.append(
                {
                    "hostname": name_by_mac.get(key) or "Unknown",
                    "ip": ip_by_mac.get(key),
                    "mac": _mac_colon(key),
                    "up_speed": up_speed,
                    "down_speed": down_speed,
                    "connection_type": label,
                    "signal": f"{signal} dBm" if signal is not None else None,
                    "signal_dbm": int(signal) if signal is not None else None,
                    "online_time": _duration_text(station["connected_time"]),
                    "internet": key not in blocked,
                    "dnsfilter": None,
                    "vpn": None,
                    "node": station["node"],
                }
            )

        arp_entries = 0
        lan_port = lan.get("l3_device") or "br-lan"
        for line in neighbours.splitlines():
            match = re.match(
                r"(\S+)\s+dev\s+(\S+)\s+lladdr\s+(\S+).*\s(\S+)$", line.strip()
            )
            if not match or match.group(2) != lan_port:
                continue
            arp_entries += 1
            address, _port, mac, state = match.groups()
            key = _mac12(mac)
            if key in stations or key in own_macs:
                continue
            if state in ("REACHABLE", "DELAY", "PROBE", "PERMANENT"):
                self._wired_seen[key] = (now, address)
        for key, (seen_at, address) in list(self._wired_seen.items()):
            if key in stations or key in own_macs or now - seen_at > _WIRED_GRACE:
                self._wired_seen.pop(key, None)
                continue
            clients.append(
                {
                    "hostname": name_by_mac.get(key) or "Unknown",
                    "ip": ip_by_mac.get(key) or address,
                    "mac": _mac_colon(key),
                    "up_speed": 0.0,
                    "down_speed": 0.0,
                    "connection_type": "Wired",
                    "signal": None,
                    "signal_dbm": None,
                    "online_time": None,
                    "internet": key not in blocked,
                    "dnsfilter": None,
                    "vpn": None,
                    "node": hostname,
                }
            )
        for stale in set(self._byte_samples) - set(stations):
            self._byte_samples.pop(stale, None)

        for node in mesh_devices.values():
            members = [c for c in clients if c.get("node") == node.get("name")]
            if node.get("status") == "online":
                node["connected_devices"] = len(members)
                node["client_devices"] = [
                    {
                        "hostname": c["hostname"],
                        "connection": c["connection_type"],
                        "ip_address": c["ip"],
                        "mac_address": c["mac"],
                    }
                    for c in members
                ]

        devices: dict[str, Any] = {
            SECTION_DEVICE_LIST: clients,
            "device_count": _value(len(clients)),
            "arp_br_lan_count": _value(arp_entries),
            "wifi_2g_clients": _value(sum(c["connection_type"] == "WiFi 2.4G" for c in clients)),
            "wifi_5g_clients": _value(sum(c["connection_type"] == "WiFi 5G" for c in clients)),
            "wired_clients": _value(sum(c["connection_type"] == "Wired" for c in clients)),
            "total_clients": _value(len(clients)),
            "total_down_speed": _value(round(sum(c["down_speed"] for c in clients), 3)),
            "total_up_speed": _value(round(sum(c["up_speed"] for c in clients), 3)),
        }
        if clients:
            for label, field in (("downloader", "down_speed"), ("uploader", "up_speed")):
                top = max(clients, key=lambda c: c[field])
                devices[f"top_{label}_hostname"] = _value(top["hostname"])
                devices[f"top_{label}_mac"] = _value(top["mac"])
                devices[f"top_{label}_speed"] = _value(top[field])

        # ---- system, lan, wan, dhcp
        memory = info.get("memory") or {}
        ram_usage = None
        if memory.get("total"):
            available = memory.get("available", memory.get("free", 0))
            ram_usage = round(100.0 * (memory["total"] - available) / memory["total"], 1)
        # Without the right to read /proc/stat, the one-minute load average
        # (a fixed-point number, 65536 per busy core) stands in for it.
        load_share = None
        if info.get("load"):
            cores = 2 if "ARMv8" in str(board.get("system")) else 1
            load_share = round(min(100.0, 100.0 * info["load"][0] / 65536 / cores), 1)
        release = board.get("release") or {}
        firmware = " ".join(
            str(x) for x in (release.get("distribution"), release.get("version")) if x
        )
        local_time = None
        if info.get("localtime"):
            local_time = datetime.fromtimestamp(info["localtime"], tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        lan_address = (lan.get("ipv4-address") or [{}])[0]
        wan_address = (wan.get("ipv4-address") or [{}])[0]
        gateway = next(
            (r.get("nexthop") for r in wan.get("route") or [] if r.get("target") == "0.0.0.0"),
            None,
        )
        statistics = wan_device.get("statistics") or {}
        megabyte = 1024 * 1024

        lan_section = dhcp_config.get("lan") or {}
        ip_start = ip_end = None
        try:
            network = ipaddress.ip_network(
                f"{lan_address['address']}/{lan_address['mask']}", strict=False
            )
            first = int(network.network_address) + int(lan_section.get("start", 100))
            ip_start = str(ipaddress.ip_address(first))
            ip_end = str(ipaddress.ip_address(first + int(lan_section.get("limit", 150)) - 1))
        except (KeyError, ValueError):
            pass

        data: dict[str, Any] = {
            MODULE_DEVICES: devices,
            MODULE_SYSTEM: {
                "uptime": _value(float(info["uptime"]) if "uptime" in info else None),
                "local_time": _value(local_time),
                "firmware_version": _value(firmware or None),
                "cpu_usage": _value(self._cpu_usage(load_share)),
                "ram_usage": _value(ram_usage),
            },
            MODULE_LAN: {
                "ip_address": _value(lan_address.get("address")),
                "mac_address": _value(_mac_colon(lan_device.get("macaddr")) or None),
                "subnet_mask": _value(_netmask(lan_address.get("mask"))),
            },
            MODULE_WAN: {
                "protocol": _value(str(wan.get("proto") or "").upper() or None),
                "connected_time": _value(float(wan["uptime"]) if wan.get("up") else 0.0),
                "public_ip": _value(self._external_ip(wan_address.get("address"))),
                "wan_ip": _value(wan_address.get("address")),
                "subnet_mask": _value(_netmask(wan_address.get("mask"))),
                "gateway": _value(gateway),
                "dns": _value(", ".join(wan.get("dns-server") or []) or None),
                "session_upload": _value(round(statistics.get("tx_bytes", 0) / megabyte, 2)),
                "session_download": _value(round(statistics.get("rx_bytes", 0) / megabyte, 2)),
            },
            MODULE_DHCP: {
                "dhcp_default_gateway": _value(lan_address.get("address")),
                "dhcp_prefered_dns": _value(lan_address.get("address")),
                "dhcp_ip_start": _value(ip_start),
                "dhcp_ip_end": _value(ip_end),
                "dhcp_leasetime": _value(_lease_minutes(lan_section.get("leasetime"))),
            },
            MODULE_VPN: {
                "protocol": _value("Disabled"),
                "vpn_clients": _value(0),
                "tunnel_ip": _value(None),
            },
            MODULE_VPN_SETTINGS: {
                "enabled": _value(False),
                "site_to_site": _value(False),
                "protocol": {"value": "none", "options": {"none": "Disabled"}},
                "default_rule": {
                    "value": "allow",
                    "options": {"allow": "Allow all devices", "deny": "Exclude all devices"},
                },
                "client_access": {
                    "value": "lanwan",
                    "options": {
                        "lanwan": "Internet and Local Network",
                        "wan": "Internet",
                        "lan": "Local Network",
                    },
                },
                "vpn_policy": {"value": "none", "options": {"none": "Disable"}},
            },
            MODULE_AUTO_UPDATE_SETTINGS: {
                "auto_update": _value(False),
                "update_time": {"value": "3", "options": {"3": "03:00 - 05:00"}},
            },
            MODULE_WISP: {
                "status": _value("Disabled", {"raw_status": "Disabled", "up": False}),
                "enabled": _value(False),
            },
            MODULE_MESH: {
                "mesh_count": _value(len(mesh_devices)),
                SECTION_MESH_DEVICES: mesh_devices,
                "main_router_name": hostname,
                "main_router_led_status": self._leds_on(self.base_url),
            },
            MODULE_WIRELESS_SETTINGS: {"smart_connect": _value(False)},
        }

        for band, entry in main_bands.items():
            prefix, module, _label, _backhaul = _BANDS[band]
            access_point = entry["ap"] or {}
            ap_config = access_point.get("config") or {}
            ap_iwinfo = access_point.get("iwinfo") or {}
            data[module] = {
                "ssid": _value(ap_config.get("ssid") or ap_iwinfo.get("ssid")),
                "channel": _value(ap_iwinfo.get("channel") or entry["radio_iwinfo"].get("channel")),
            }
            data[MODULE_WIRELESS_SETTINGS].update(
                self._wireless_settings(prefix, band, entry)
            )
        return data

    def _collect_node(self, address: str, base: str, add_stations, main_mesh_mac) -> dict | None:
        board = self._call("system", "board", base=base, strict=True)
        bands = self._wireless(base)
        name = board.get("hostname") or address
        add_stations(base, bands, name)
        lan_device = self._call("network.device", "status", {"name": "br-lan"}, base=base) or {}
        label_mac = ""
        for band in ("2g", "5g"):
            iwinfo = ((bands.get(band) or {}).get("ap") or {}).get("iwinfo") or {}
            if iwinfo.get("bssid"):
                label_mac = _mac_colon(iwinfo["bssid"])
                break
        label_mac = label_mac or _mac_colon(lan_device.get("macaddr"))
        if not label_mac:
            return None
        release = board.get("release") or {}
        node: dict[str, Any] = {
            "name": name,
            "model": _short_model(board.get("model")),
            "hardware": _hardware(board),
            "mac_address": label_mac,
            "firmware_version": " ".join(
                str(x) for x in (release.get("distribution"), release.get("version")) if x
            ),
            "status": "online",
            "ip_address": address,
            "connected_devices": 0,
            "client_devices": [],
            "backhaul": "Ethernet",
            "backhaul_signal": None,
            "backhaul_tx_rate": None,
            "backhaul_rx_rate": None,
            "led_status": self._leds_on(base),
            "hop": 1,
            "_own_macs": {_mac12(lan_device.get("macaddr")), _mac12(label_mac)},
        }
        for band, entry in bands.items():
            link = entry["mesh"]
            if not link:
                continue
            peers = self._stations(base, link.get("ifname"))
            if not peers:
                continue
            peer = max(peers, key=lambda s: s.get("signal") or -200)
            node.update(
                {
                    "backhaul": _BANDS[band][3],
                    "backhaul_signal": peer.get("signal"),
                    "backhaul_tx_rate": int(((peer.get("tx") or {}).get("rate") or 0) / 1000),
                    "backhaul_rx_rate": int(((peer.get("rx") or {}).get("rate") or 0) / 1000),
                    "backhaul_interface": link.get("ifname"),
                    "backhaul_bssid": _mac_colon(peer.get("mac")),
                    "mesh_type": (link.get("config") or {}).get("mode"),
                    "parent_mac": main_mesh_mac or _mac_colon(peer.get("mac")),
                }
            )
            break
        return node

    # ------------------------------------------------------ wireless settings

    def _wireless_settings(self, prefix: str, band: str, entry: dict[str, Any]) -> dict[str, Any]:
        radio = entry["radio_config"]
        access_point = (entry["ap"] or {}).get("config") or {}
        htmode = str(radio.get("htmode") or "HT20").upper()
        match = re.fullmatch(r"(HE|VHT|HT|EHT)(\d+)", htmode)
        family, width = (match.group(1), match.group(2)) if match else ("HT", "20")
        if band == "2g":
            mode = "11bgnax" if family in ("HE", "EHT") else "11bgn"
        else:
            mode = {"HT": "11an", "VHT": "11anac"}.get(family, "11anacax")

        supported = {str(m).upper() for m in entry["radio_iwinfo"].get("htmodes") or []}
        widths = ["20", "40"] if band == "2g" else ["20", "40", "80"]
        if any(m.endswith("160") for m in supported):
            widths.append("160")
        if width not in widths:
            widths.append(width)

        channels = {"0": "Auto"}
        ifname = (entry["ap"] or {}).get("ifname")
        if ifname:
            frequencies = self._call("iwinfo", "freqlist", {"device": ifname}) or {}
            for item in frequencies.get("results") or []:
                if item.get("restricted"):
                    continue
                channels[str(item["channel"])] = f"{item['channel']} ({item['mhz']} MHz)"
        channel = str(radio.get("channel") or "auto")
        channel = "0" if channel == "auto" else channel
        channels.setdefault(channel, channel)

        tx_power = "100"
        configured = radio.get("txpower")
        if configured not in (None, ""):
            ceiling = self._max_tx_power(ifname)
            backoff = (ceiling - int(configured)) if ceiling is not None else 0
            tx_power = "100" if backoff <= 1 else "50" if backoff <= 4 else "20"

        return {
            f"{prefix}_enabled": _value(
                access_point.get("disabled") != "1"
                and not entry["radio_disabled"]
                and bool(entry["ap"])
            ),
            f"{prefix}_hidden": _value(access_point.get("hidden") == "1"),
            f"{prefix}_isolate": _value(access_point.get("isolate") == "1"),
            f"{prefix}_mode": {"value": mode, "options": dict(_MODE_OPTIONS[band])},
            f"{prefix}_channel_width": {
                "value": f"ht{width}",
                "options": {f"ht{w}": f"{w} MHz" for w in widths},
            },
            f"{prefix}_channel": {"value": channel, "options": channels},
            f"{prefix}_tx_power": {"value": tx_power, "options": dict(_TX_POWER_OPTIONS)},
        }

    def _max_tx_power(self, ifname: str | None, base: str | None = None) -> int | None:
        if not ifname:
            return None
        levels = self._call("iwinfo", "txpowerlist", {"device": ifname}, base=base) or {}
        values = [item.get("dbm") for item in levels.get("results") or [] if "dbm" in item]
        return max(values) if values else None

    def set_wireless_setting(self, key: str, value: Any) -> tuple[int, str]:
        match = re.fullmatch(r"wifi_(2g|5g)_(\w+)", key)
        if not match:
            return (400, f"Unknown wireless setting {key}")
        band, setting = match.groups()
        targets = [self.base_url]
        # The 2.4 GHz channel is chosen per access point so that neighbours do
        # not overlap; everything else is kept the same on every node.
        if not (band == "2g" and setting == "channel"):
            targets = list(self._node_urls.values()) + targets
        failures = []
        for base in targets:
            try:
                self._apply_wireless(base, band, setting, value)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("OpenWrt wireless change failed on %s: %s", base, err)
                self._sessions.pop(base, None)
                failures.append(f"{base}: {err}")
        if failures and len(failures) == len(targets):
            return (500, "; ".join(failures))
        return _OK

    def _apply_wireless(self, base: str, band: str, setting: str, value: Any) -> None:
        entry = self._wireless(base).get(band)
        if not entry:
            raise OpenWrtError(f"no {band} radio")
        radio = entry["radio"]
        access_point = (entry["ap"] or {}).get("section")
        radio_config = entry["radio_config"]
        htmode = str(radio_config.get("htmode") or "HT20").upper()
        match = re.fullmatch(r"(HE|VHT|HT|EHT)(\d+)", htmode)
        family, width = (match.group(1), match.group(2)) if match else ("HT", "20")

        def uci_set(section: str, values: dict[str, str]) -> None:
            self._call(
                "uci", "set",
                {"config": "wireless", "section": section, "values": values},
                base=base, strict=True,
            )

        if setting in ("enabled", "hidden", "isolate"):
            if not access_point:
                raise OpenWrtError("no access point on this radio")
            option = {"enabled": "disabled", "hidden": "hidden", "isolate": "isolate"}[setting]
            flag = bool(value) if setting != "enabled" else not bool(value)
            uci_set(access_point, {option: "1" if flag else "0"})
        elif setting == "mode":
            if value not in _MODE_PREFIX:
                raise OpenWrtError(f"unknown mode {value}")
            if _MODE_PREFIX[value] == "HT" and width not in ("20", "40"):
                width = "40"
            uci_set(radio, {"htmode": f"{_MODE_PREFIX[value]}{width}"})
        elif setting == "channel_width":
            new_width = re.sub(r"\D", "", str(value))
            if not new_width:
                raise OpenWrtError(f"unknown width {value}")
            if family == "HT" and new_width not in ("20", "40"):
                family = "VHT"
            uci_set(radio, {"htmode": f"{family}{new_width}"})
        elif setting == "channel":
            uci_set(radio, {"channel": "auto" if str(value) == "0" else str(value)})
        elif setting == "tx_power":
            if str(value) not in _TX_POWER_BACKOFF:
                raise OpenWrtError(f"unknown power level {value}")
            if str(value) == "100":
                self._call(
                    "uci", "delete",
                    {"config": "wireless", "section": radio, "options": ["txpower"]},
                    base=base,
                )
            else:
                ceiling = self._max_tx_power((entry["ap"] or {}).get("ifname"), base)
                if ceiling is None:
                    raise OpenWrtError("the radio does not report its power levels")
                uci_set(radio, {"txpower": str(max(1, ceiling - _TX_POWER_BACKOFF[str(value)]))})
        else:
            raise OpenWrtError(f"unknown setting {setting}")

        self._call("uci", "commit", {"config": "wireless"}, base=base, strict=True)
        self._call("network", "reload", base=base)
        # A radio that also carries the link between nodes does not bring its
        # access point back after a reload; restarting the radio does.
        if entry["mesh"]:
            time.sleep(3)
            self._call("network.wireless", "down", {"radio": radio}, base=base)
            time.sleep(4)
            self._call("network.wireless", "up", {"radio": radio}, base=base)

    def set_smart_connect(self, value: bool) -> tuple[int, str]:  # noqa: ARG002
        return _NOT_SUPPORTED

    def set_vpn_setting(self, key: str, value: Any) -> tuple[int, str]:  # noqa: ARG002
        return _NOT_SUPPORTED

    def set_auto_update_setting(self, key: str, value: Any) -> tuple[int, str]:  # noqa: ARG002
        return _NOT_SUPPORTED

    def set_wisp_setting(self, key: str, value: Any) -> tuple[int, str]:  # noqa: ARG002
        return _NOT_SUPPORTED

    def set_cellular_setting(self, key: str, value: Any) -> tuple[int, str]:  # noqa: ARG002
        return _NOT_SUPPORTED

    def restart_5g_connection(self) -> tuple[int, str]:
        return _NOT_SUPPORTED

    def switch_5g_band(self, band: str) -> tuple[int, str]:  # noqa: ARG002
        return _NOT_SUPPORTED

    def set_cellular_bands(self, **kwargs: Any) -> tuple[int, str]:  # noqa: ARG002
        return _NOT_SUPPORTED

    def send_at_command(self, command: str) -> tuple[int, str]:  # noqa: ARG002
        return _NOT_SUPPORTED

    def send_sms(self, phone_number: str, message: str) -> tuple[int, str]:  # noqa: ARG002
        return _NOT_SUPPORTED

    # --------------------------------------------------------- other actions

    def set_device_access(self, device: dict[str, Any], feature: str, enabled: bool) -> tuple[int, str]:
        """Cut or restore a client's way out to the internet with a firewall rule."""
        if feature != "internet":
            return _NOT_SUPPORTED
        key = _mac12(device.get("mac"))
        if len(key) != 12:
            return (400, "The client has no MAC address")
        section = f"{_BLOCK_RULE_PREFIX}{key}"
        try:
            self._call("uci", "delete", {"config": "firewall", "section": section})
            if not enabled:
                self._call(
                    "uci", "add",
                    {
                        "config": "firewall",
                        "type": "rule",
                        "name": section,
                        "values": {
                            "name": f"Block internet {device.get('hostname') or key}",
                            "src": "lan",
                            "dest": "wan",
                            "src_mac": _mac_colon(key),
                            "proto": "all",
                            "target": "REJECT",
                        },
                    },
                    strict=True,
                )
            self._call("uci", "commit", {"config": "firewall"}, strict=True)
            self._call("rc", "init", {"name": "firewall", "action": "reload"})
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("OpenWrt firewall change failed: %s", err)
            return (500, str(err))
        return _OK

    def _node_base(self, mesh_mac: str) -> str | None:
        node = self._nodes.get(mesh_mac) or self._nodes.get(_mac_colon(mesh_mac))
        return self._node_urls.get(node["ip_address"]) if node else None

    def _reboot(self, base: str) -> tuple[int, str]:
        try:
            # LuCI's own reboot button runs this command, so the right to do
            # it is there on any router with the web interface installed.
            self._call("file", "exec", {"command": "/sbin/reboot"}, base=base)
        except requests.exceptions.RequestException:
            pass  # the router may drop the connection as it goes down
        self._sessions.pop(base, None)
        return _OK

    def reboot_router(self) -> tuple[int, str]:
        return self._reboot(self.base_url)

    def reboot_mesh_device(self, mesh_mac: str) -> tuple[int, str]:
        base = self._node_base(mesh_mac)
        return self._reboot(base) if base else (404, f"Unknown node {mesh_mac}")

    def _set_leds(self, base: str, enabled: bool) -> tuple[int, str]:
        try:
            leds = self._call("luci", "getLEDs", base=base, strict=True)

            def write(name: str, attribute: str, text: str) -> None:
                self._call(
                    "file", "write",
                    {"path": f"/sys/class/leds/{name}/{attribute}", "data": text},
                    base=base,
                )

            if not enabled:
                snapshot = {
                    name: (led.get("active_trigger") or "none", int(led.get("brightness") or 0))
                    for name, led in leds.items()
                }
                if any(t != "none" or b > 0 for t, b in snapshot.values()):
                    self._led_snapshots[base] = snapshot
                for name in leds:
                    write(name, "trigger", "none")
                    write(name, "brightness", "0")
                return _OK

            snapshot = self._led_snapshots.get(base)
            if snapshot:
                for name, (trigger, brightness) in snapshot.items():
                    write(name, "trigger", trigger)
                    if trigger == "none":
                        write(name, "brightness", str(brightness))
                return _OK
            # No memory of how they were: fall back to what a freshly booted
            # router shows, radio activity plus one steady status light.
            self._call("rc", "init", {"name": "led", "action": "restart"}, base=base)
            steady_done = False
            for name, led in leds.items():
                radio = re.search(r"phy(\d+)", name)
                if radio:
                    write(name, "trigger", f"phy{radio.group(1)}tpt")
                elif not steady_done and "red" not in name and re.search(
                    r"online|power|status|sys", name
                ):
                    write(name, "brightness", str(led.get("max_brightness") or 1))
                    steady_done = True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("OpenWrt LED change failed on %s: %s", base, err)
            return (500, str(err))
        return _OK

    def set_main_router_led(self, enabled: bool) -> tuple[int, str]:
        return self._set_leds(self.base_url, enabled)

    def set_mesh_led(self, mesh_mac: str, enabled: bool) -> tuple[int, str]:
        base = self._node_base(mesh_mac)
        return self._set_leds(base, enabled) if base else (404, f"Unknown node {mesh_mac}")
