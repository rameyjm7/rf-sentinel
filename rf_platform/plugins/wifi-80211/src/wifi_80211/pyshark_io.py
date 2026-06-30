from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable


def normalize_frame_event(record: dict[str, Any], *, source: str = "wifi_decoder") -> dict[str, Any]:
    timestamp = _float(record.get("timestamp") or record.get("seen_at") or time.time(), time.time())
    event = {
        "protocol": "wifi",
        "kind": "wifi_frame",
        "source": source,
        "seen_at": timestamp,
        "timestamp": timestamp,
        "frame_type": _clean(record.get("frame_type")),
        "subtype": _clean(record.get("subtype")),
        "receiver": _mac(record.get("receiver") or record.get("ra")),
        "transmitter": _mac(record.get("transmitter") or record.get("ta")),
        "destination": _mac(record.get("destination") or record.get("da")),
        "source_mac": _mac(record.get("source") or record.get("source_mac") or record.get("sa")),
        "bssid": _mac(record.get("bssid")),
        "ssid": _clean(record.get("ssid")),
        "channel": _int(record.get("channel") or record.get("wifi_channel")),
        "frequency_mhz": _frequency_mhz(record),
        "rssi_dbm": _number(record.get("rssi_dbm") or record.get("dbm_antsignal") or record.get("signal_dbm")),
        "rssi_dbfs": _number(record.get("rssi_dbfs") or record.get("power_dbfs")),
        "sequence": _int(record.get("sequence")),
        "length": _int(record.get("length")),
    }
    if "channel" not in event or event["channel"] is None:
        event["channel"] = _channel_from_frequency_mhz(event.get("frequency_mhz"))
    return {key: value for key, value in event.items() if value not in {None, ""}}


def read_jsonl_events(path: Path, *, start_offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    events: list[dict[str, Any]] = []
    if not path.exists():
        return events, start_offset
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(max(0, int(start_offset)))
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(normalize_frame_event(record, source="wifi_jsonl"))
        return events, fh.tell()


def read_pcap_events(path: Path, *, skip: int = 0, limit: int = 50) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], skip
    try:
        events = _read_pcap_events_pyshark(path, skip=skip, limit=limit)
    except Exception:
        events = _read_pcap_events_tshark(path, skip=skip, limit=limit)
    return events, skip + len(events)


def _read_pcap_events_pyshark(path: Path, *, skip: int, limit: int) -> list[dict[str, Any]]:
    import pyshark

    out: list[dict[str, Any]] = []
    capture = pyshark.FileCapture(str(path), keep_packets=False, use_json=True)
    try:
        for idx, packet in enumerate(capture):
            if idx < skip:
                continue
            if len(out) >= limit:
                break
            wlan = getattr(packet, "wlan", None)
            if wlan is None:
                continue
            record = {
                "timestamp": float(getattr(packet, "sniff_timestamp", time.time())),
                "frame_type": _pyshark_attr(wlan, "fc_type"),
                "subtype": _pyshark_attr(wlan, "fc_type_subtype"),
                "receiver": _pyshark_attr(wlan, "ra"),
                "transmitter": _pyshark_attr(wlan, "ta"),
                "destination": _pyshark_attr(wlan, "da"),
                "source": _pyshark_attr(wlan, "sa"),
                "bssid": _pyshark_attr(wlan, "bssid"),
                "sequence": _pyshark_attr(wlan, "seq"),
                "length": getattr(packet, "length", None),
            }
            radio = getattr(packet, "wlan_radio", None)
            radiotap = getattr(packet, "radiotap", None)
            if radio is not None:
                record["channel"] = _pyshark_attr(radio, "channel")
                record["frequency_mhz"] = _pyshark_attr(radio, "frequency")
                record["rssi_dbm"] = _pyshark_attr(radio, "signal_dbm")
            if radiotap is not None:
                record.setdefault("frequency_mhz", _pyshark_attr(radiotap, "channel_freq"))
                record.setdefault("rssi_dbm", _pyshark_attr(radiotap, "dbm_antsignal"))
            mgt = getattr(packet, "wlan_mgt", None)
            if mgt is not None:
                record["ssid"] = _pyshark_attr(mgt, "ssid")
            out.append(normalize_frame_event(record, source="pyshark"))
    finally:
        capture.close()
    return out


def _read_pcap_events_tshark(path: Path, *, skip: int, limit: int) -> list[dict[str, Any]]:
    cmd = ["tshark", "-r", str(path), "-T", "json"]
    raw = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=10)
    packets = json.loads(raw.decode("utf-8", errors="replace"))
    out: list[dict[str, Any]] = []
    for idx, packet in enumerate(packets):
        if idx < skip:
            continue
        if len(out) >= limit:
            break
        layers = packet.get("_source", {}).get("layers", {})
        frame = layers.get("frame", {})
        radiotap = layers.get("radiotap", {})
        wlan_radio = layers.get("wlan_radio", {})
        wlan = layers.get("wlan", {})
        mgt = layers.get("wlan.mgt", {})
        record = {
            "timestamp": frame.get("frame.time_epoch"),
            "frame_type": _frame_type_name(wlan.get("wlan.fc.type")),
            "subtype": _subtype_name(wlan.get("wlan.fc.type_subtype")),
            "receiver": wlan.get("wlan.ra"),
            "transmitter": wlan.get("wlan.ta"),
            "destination": wlan.get("wlan.da"),
            "source": wlan.get("wlan.sa"),
            "bssid": wlan.get("wlan.bssid"),
            "sequence": wlan.get("wlan.seq"),
            "length": frame.get("frame.len"),
            "ssid": _ssid_from_tshark_layers(wlan, mgt),
            "channel": wlan_radio.get("wlan_radio.channel"),
            "frequency_mhz": wlan_radio.get("wlan_radio.frequency") or radiotap.get("radiotap.channel.freq"),
            "rssi_dbm": wlan_radio.get("wlan_radio.signal_dbm") or radiotap.get("radiotap.dbm_antsignal"),
        }
        if any(record.get(key) for key in ("receiver", "transmitter", "source", "bssid", "ssid")):
            out.append(normalize_frame_event(record, source="tshark"))
    return out


def _ssid_from_tshark_layers(*layers: dict[str, Any]) -> str | None:
    for layer in layers:
        for key, value in _walk_items(layer):
            if key.endswith("ssid") or key.endswith("ssid_tree"):
                if isinstance(value, str) and value and not value.startswith("0x"):
                    return value
            if key in {"wlan.ssid", "wlan_mgt.ssid", "wlan.tag.ssid"} and isinstance(value, str):
                return value
    return None


def _walk_items(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from _walk_items(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_items(item)


def _pyshark_attr(layer: Any, name: str) -> Any:
    try:
        return getattr(layer, name)
    except Exception:
        return None


def _frame_type_name(value: Any) -> str | None:
    mapping = {"0": "management", "1": "control", "2": "data", "3": "extension"}
    return mapping.get(str(value), None)


def _subtype_name(value: Any) -> str | None:
    mapping = {
        "0x0000": "association-request",
        "0x0004": "probe-request",
        "0x0005": "probe-response",
        "0x0008": "beacon",
        "0x000b": "authentication",
        "0x000c": "deauthentication",
        "0x001d": "ack",
    }
    return mapping.get(str(value), str(value) if value is not None else None)


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _mac(value: Any) -> str | None:
    text = _clean(value)
    if not text:
        return None
    text = text.lower()
    return text if text.count(":") == 5 else None


def _int(value: Any) -> int | None:
    try:
        return int(str(value), 0)
    except Exception:
        return None


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _frequency_mhz(record: dict[str, Any]) -> float | None:
    value = record.get("frequency_mhz") or record.get("freq_mhz")
    freq = _number(value)
    if freq is None:
        freq = _number(record.get("frequency_hz") or record.get("freq_hz"))
        if freq is not None:
            freq /= 1e6
    if freq is not None and freq > 1_000_000:
        freq /= 1e6
    return freq


def _channel_from_frequency_mhz(freq_mhz: Any) -> int | None:
    freq = _number(freq_mhz)
    if freq is None:
        return None
    if 2407 <= freq <= 2472:
        channel = round((freq - 2407) / 5)
        return int(channel) if 1 <= channel <= 13 else None
    if 2482 <= freq <= 2486:
        return 14
    return None
