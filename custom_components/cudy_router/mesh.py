"""Helpers to read mesh node telemetry out of the router's mesh clients payload.

The ``admin/network/mesh/clients?clients=all`` endpoint returns one JSON object
per mesh node. Each node carries a ``sysreport`` block describing how it is
attached to the mesh, including a ``sta`` list with one entry per station
interface. Only one of those entries belongs to the active backhaul; the rest
are other radios the node happens to have associated.
"""

from __future__ import annotations

import logging
import re
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Station interfaces are named after the radio they belong to: wlan11 and
# apclix1 both live on radio1. The trailing digits identify the virtual
# interface and are not useful here.
_RADIO_IFACE_RE = re.compile(r"^(?:wlan|ra|apclix|apcli)(\d)", re.IGNORECASE)
_WIRED_IFACE_RE = re.compile(r"^(?:br-|eth|lan|wan)", re.IGNORECASE)

_BAND_LABELS = {
    "2.4g": "2.4 GHz",
    "2g": "2.4 GHz",
    "5g": "5 GHz",
    "6g": "6 GHz",
}

_WIFI_GENERATIONS = {
    "11ax": "Wi-Fi 6",
    "11ac": "Wi-Fi 5",
    "11n": "Wi-Fi 4",
    "11g": "Wi-Fi 3",
}

WIRED_BACKHAUL_LABEL = "Ethernet"

# Shift some firmware applies to keep its reported RSSI positive.
_RSSI_POSITIVE_OFFSET = 100
# A Wi-Fi link runs from roughly -30 dBm down to -95; -100 is the floor a
# reading of zero would decode to, which means "nothing measured" rather than
# an extraordinarily weak link.
_WEAKEST_PLAUSIBLE_DBM = -100


def radio_index(iface: str | None) -> int | None:
    """Return the radio index an interface name belongs to, if it names one."""
    if not iface:
        return None
    match = _RADIO_IFACE_RE.match(iface.strip())
    if not match:
        return None
    return int(match.group(1))


def is_wired_iface(iface: str | None) -> bool:
    """Return True when the interface name is a wired bridge or port."""
    if not iface:
        return False
    return bool(_WIRED_IFACE_RE.match(iface.strip()))


def _declared_bands(sysreport: dict[str, Any] | None) -> list[str]:
    """Return the band labels a node lists in ``bands``, in the order given."""
    bands = (sysreport or {}).get("bands")
    if not isinstance(bands, str) or not bands:
        return []
    return [
        label
        for part in bands.split("|")
        if (label := _BAND_LABELS.get(part.strip().lower()))
    ]


def is_wide_channel(bandwidth: Any) -> bool:
    """Return True for a channel wider than 2.4 GHz can offer.

    2.4 GHz tops out at 40 MHz, so an 80 or 160 MHz link cannot be on it no
    matter how the radios are numbered.
    """
    match = re.search(r"(\d+)", str(bandwidth or ""))
    return bool(match) and int(match.group(1)) > 40


def band_label(
    index: int | None,
    sysreport: dict[str, Any] | None,
    *,
    bandwidth: Any = None,
) -> str | None:
    """Return a human band label ("5 GHz") for a radio index.

    The radio's own channel decides, because a channel number is a measured
    fact. The order of the ``bands`` list is only a convention, and at least one
    firmware lists the bands the other way round from the radio numbering, so it
    is the last resort rather than the first.
    """
    if index is None:
        return None

    label = _band_from_channel(_radio_channel(index, sysreport))

    if label is None:
        declared = _declared_bands(sysreport)
        if index < len(declared):
            label = declared[index]

    if label == "2.4 GHz" and is_wide_channel(bandwidth):
        # The radios are numbered or ordered differently than assumed. Only the
        # node's own band list can say which one this actually is, and it only
        # helps when that leaves a single candidate.
        alternatives = {band for band in _declared_bands(sysreport) if band != "2.4 GHz"}
        return alternatives.pop() if len(alternatives) == 1 else None

    return label


def _band_from_channel(channel: int | None) -> str | None:
    """Return the band a Wi-Fi channel number belongs to."""
    if channel is None:
        return None
    return "2.4 GHz" if channel <= 14 else "5 GHz"


def _radio_channel(index: int, sysreport: dict[str, Any] | None) -> int | None:
    """Return the channel a radio is parked on, when the node reports it."""
    stats = (sysreport or {}).get("sysstatinfo")
    if not isinstance(stats, dict):
        return None
    radio = stats.get(f"radio{index}")
    if not isinstance(radio, dict):
        return None
    try:
        return int(radio.get("channel"))
    except (TypeError, ValueError):
        return None


def select_backhaul_station(sysreport: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the ``sta`` entry that carries the active backhaul.

    Nodes list every associated station interface, so the entry is picked by
    matching ``iface`` against the ``backhaul`` field. When the node does not
    name its backhaul interface there is nothing to match on, and picking an
    arbitrary radio would report the wrong link, so nothing is returned.
    """
    if not isinstance(sysreport, dict):
        return None

    stations = sysreport.get("sta")
    if not isinstance(stations, list):
        return None
    stations = [station for station in stations if isinstance(station, dict)]
    if not stations:
        return None

    backhaul_iface = sysreport.get("backhaul")
    if not backhaul_iface:
        return None

    wanted = str(backhaul_iface).strip().lower()
    for station in stations:
        if str(station.get("iface", "")).strip().lower() == wanted:
            return station

    # Fall back to the radio the backhaul interface belongs to, which covers
    # firmware that names the backhaul after the radio rather than the exact
    # virtual interface.
    index = radio_index(backhaul_iface)
    if index is None:
        return None
    for station in stations:
        if radio_index(station.get("iface")) == index:
            return station
    return None


def backhaul_label(sysreport: dict[str, Any] | None) -> str | None:
    """Return a readable description of how a node is attached to the mesh."""
    if not isinstance(sysreport, dict):
        return None

    if str(sysreport.get("meshtype", "")).strip().lower() == "wired":
        return WIRED_BACKHAUL_LABEL

    iface = sysreport.get("backhaul") or (sysreport.get("parent") or {}).get("iface")
    if not iface:
        return None
    if is_wired_iface(iface):
        return WIRED_BACKHAUL_LABEL

    # The width of the active link is itself evidence of the band, so the
    # station is looked up before labelling.
    station = select_backhaul_station(sysreport) or {}
    band = band_label(radio_index(iface), sysreport, bandwidth=station.get("bw"))
    if band:
        return f"Wi-Fi {band}"
    return str(iface)


def bandwidth_label(value: Any) -> str | None:
    """Turn a reported channel width ("ht160") into a readable label."""
    if value in (None, ""):
        return None
    match = re.search(r"(\d+)", str(value))
    if not match:
        return str(value)
    return f"{match.group(1)} MHz"


def wifi_generation(mode: Any) -> str | None:
    """Turn a reported PHY mode ("11ax") into a marketing name."""
    if mode in (None, ""):
        return None
    return _WIFI_GENERATIONS.get(str(mode).strip().lower(), str(mode))


def _as_int(value: Any) -> int | None:
    """Coerce a reported number to int, tolerating strings and junk."""
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def signal_from_plain_rssi(value: Any) -> int | None:
    """Return a dBm reading from a bare ``rssi`` field, or None.

    Firmware that omits ``rssireal`` reports the same measurement as ``rssi``,
    shifted to keep it positive. On an M3000, where both fields are present,
    ``rssi`` is exactly dBm + 100 (43 against -57, 42 against -58), and a
    WR3600E that reports only ``rssi`` lands on the values its own web
    interface shows.

    A negative value is already dBm. A positive one is converted only when the
    result falls inside the range a Wi-Fi link can occupy, so a field that
    turns out to mean something else is ignored rather than published as a
    wrong signal.
    """
    reported = _as_int(value)
    if reported is None:
        return None

    dbm = reported if reported < 0 else reported - _RSSI_POSITIVE_OFFSET
    return dbm if _WEAKEST_PLAUSIBLE_DBM < dbm < 0 else None


def backhaul_details(sysreport: dict[str, Any] | None) -> dict[str, Any]:
    """Return the backhaul link quality fields a node reports.

    Keys are left out entirely when the node does not report them, so callers
    can merge this over data parsed from HTML without blanking it.
    """
    details: dict[str, Any] = {}
    if not isinstance(sysreport, dict):
        return details

    label = backhaul_label(sysreport)
    if label:
        details["backhaul"] = label
    if sysreport.get("backhaul"):
        details["backhaul_interface"] = str(sysreport["backhaul"])
    if sysreport.get("meshtype"):
        details["mesh_type"] = str(sysreport["meshtype"])

    parent = sysreport.get("parent")
    if isinstance(parent, dict):
        hop = _as_int(parent.get("hop"))
        if hop is not None:
            details["hop"] = hop
        if parent.get("macaddr"):
            details["parent_mac"] = str(parent["macaddr"]).upper()

    station = select_backhaul_station(sysreport)
    if station is None:
        return details

    # rssireal is the calibrated value and rssi0/rssi1 are the per-chain
    # readings. Plain `rssi` is the same number offset by 100 on the firmware
    # this was written against, so it is only trusted when it is negative:
    # a negative RSSI is dBm by definition, while a positive one is the offset
    # form and would be reported as an absurd signal.
    signal = _as_int(station.get("rssireal"))
    if signal is None:
        chains = [_as_int(station.get(key)) for key in ("rssi0", "rssi1", "rssi2", "rssi3")]
        chains = [chain for chain in chains if chain is not None]
        if chains:
            signal = max(chains)
    if signal is None:
        signal = signal_from_plain_rssi(station.get("rssi"))
    if signal is not None:
        details["backhaul_signal"] = signal

    for chain_key in ("rssi0", "rssi1", "rssi2", "rssi3"):
        chain = _as_int(station.get(chain_key))
        if chain is not None:
            details[f"backhaul_signal_chain{chain_key[-1]}"] = chain

    tx_rate = _as_int(station.get("txrate"))
    if tx_rate is not None:
        details["backhaul_tx_rate"] = tx_rate
    rx_rate = _as_int(station.get("rxrate"))
    if rx_rate is not None:
        details["backhaul_rx_rate"] = rx_rate

    bandwidth = bandwidth_label(station.get("bw"))
    if bandwidth:
        details["backhaul_bandwidth"] = bandwidth
    generation = wifi_generation(station.get("mode"))
    if generation:
        details["backhaul_standard"] = generation
    if station.get("bssid"):
        details["backhaul_bssid"] = str(station["bssid"]).upper()

    return details


def node_load(client_json: dict[str, Any] | None) -> dict[str, Any]:
    """Return the CPU and memory load a node reports, when it reports them."""
    load: dict[str, Any] = {}
    stats = (client_json or {}).get("sysstatinfo")
    if not isinstance(stats, dict):
        return load

    cpu = _as_int(stats.get("cpuload"))
    if cpu is not None:
        load["cpu_load"] = cpu
    memory = _as_int(stats.get("memload"))
    if memory is not None:
        load["memory_load"] = memory
    return load


def node_telemetry(client_json: dict[str, Any] | None) -> dict[str, Any]:
    """Return every extra field a mesh node reports about itself.

    This is the single place that decides what the mesh clients payload is worth
    surfacing, so ``router_data`` only has to merge the result.
    """
    if not isinstance(client_json, dict):
        return {}

    sysreport = client_json.get("sysreport")
    sysreport = sysreport if isinstance(sysreport, dict) else {}
    # band_label reads the radio channels, which live next to sysreport rather
    # than inside it.
    if "sysstatinfo" not in sysreport and isinstance(client_json.get("sysstatinfo"), dict):
        sysreport = {**sysreport, "sysstatinfo": client_json["sysstatinfo"]}

    telemetry: dict[str, Any] = {}
    telemetry.update(backhaul_details(sysreport))
    telemetry.update(node_load(client_json))

    device_count = _as_int(client_json.get("devcnt"))
    if device_count is not None:
        telemetry["connected_devices"] = device_count

    uptime = _as_int(sysreport.get("run_time"))
    if uptime is not None:
        telemetry["uptime_seconds"] = uptime

    _LOGGER.debug(
        "Mesh node %s telemetry: %s",
        client_json.get("name") or client_json.get("id"),
        telemetry,
    )
    return telemetry
