from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np


BLE_ADV_CHANNELS = {
    37: 2_402_000_000,
    38: 2_426_000_000,
    39: 2_480_000_000,
}
BLE_ADV_ACCESS_BYTES = bytes.fromhex("d6be898e")
BLE_ADV_SAMPLE_RATE_SPS = 2_000_000

PROJECT_ROOT = Path(__file__).resolve().parents[5]
UI_DATA_DIR = PROJECT_ROOT / "ui" / "backend" / "data"
COMPANY_IDENTIFIERS_PATH = UI_DATA_DIR / "company_identifiers.json"
UUID16_IDENTIFIERS_PATH = UI_DATA_DIR / "uuid16_identifiers.json"

UUID16_VENDOR_OVERRIDES = {
    "0xFCB2": "Apple, Inc.",
    "0xFEED": "Tile, Inc.",
}

BLE_APPEARANCE_LABELS = {
    0x0000: "Unknown",
    0x0040: "Phone",
    0x0080: "Computer",
    0x00C0: "Watch",
    0x00C1: "Sports Watch",
    0x0100: "Clock",
    0x0140: "Display",
    0x0180: "Remote",
    0x01C0: "Eye-glasses",
    0x0200: "Tag",
    0x0240: "Keyring",
    0x0280: "Media Player",
    0x02C0: "Barcode Scanner",
    0x0300: "Thermometer",
    0x0340: "Heart Rate Sensor",
    0x0380: "Blood Pressure",
    0x03C0: "HID",
    0x03C1: "Keyboard",
    0x03C2: "Mouse",
    0x03C3: "Joystick",
    0x03C4: "Gamepad",
    0x03C5: "Digitizer Tablet",
    0x03C6: "Card Reader",
    0x03C7: "Digital Pen",
    0x03C8: "Barcode Scanner",
    0x0400: "Glucose Meter",
    0x0440: "Running Sensor",
    0x0441: "Running Sensor Pod",
    0x0442: "Running Sensor Shoe",
    0x0480: "Cycling",
    0x0481: "Cycling Computer",
    0x0482: "Cycling Speed Sensor",
    0x0483: "Cycling Cadence Sensor",
    0x0484: "Cycling Power Sensor",
    0x0485: "Cycling Speed/Cadence Sensor",
    0x04C0: "Pulse Oximeter",
    0x0500: "Weight Scale",
    0x0540: "Personal Mobility",
    0x0580: "Continuous Glucose Monitor",
    0x05C0: "Insulin Pump",
    0x0600: "Medication Delivery",
    0x0640: "Outdoor Sports",
    0x0641: "Location Display Device",
    0x0642: "Location Navigation Device",
    0x0643: "Location Pod",
    0x0644: "Location Beacon",
}


def _load_company_identifier_lut() -> dict[str, str]:
    if not COMPANY_IDENTIFIERS_PATH.exists():
        return {}
    try:
        with COMPANY_IDENTIFIERS_PATH.open("r", encoding="ascii") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    companies = data.get("companies") if isinstance(data, dict) else {}
    if not isinstance(companies, dict):
        return {}
    return {str(key).upper().replace("X", "x"): str(value) for key, value in companies.items()}


COMPANY_IDENTIFIER_LUT = _load_company_identifier_lut()


def company_name(company_id: str) -> str:
    return COMPANY_IDENTIFIER_LUT.get(str(company_id or "").upper().replace("X", "x"), "")


def _load_uuid16_identifier_lut() -> dict[str, str]:
    if not UUID16_IDENTIFIERS_PATH.exists():
        return {}
    try:
        with UUID16_IDENTIFIERS_PATH.open("r", encoding="ascii") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    uuids = data.get("uuids") if isinstance(data, dict) else {}
    if not isinstance(uuids, dict):
        return {}
    return {str(key).upper().replace("X", "x"): str(value) for key, value in uuids.items()}


UUID16_IDENTIFIER_LUT = _load_uuid16_identifier_lut()


def uuid16_name(uuid16: str) -> str:
    key = str(uuid16 or "").upper().replace("X", "x")
    return UUID16_VENDOR_OVERRIDES.get(key) or UUID16_IDENTIFIER_LUT.get(key, "")


def uuid16_names(uuid16_values: list[str]) -> list[str]:
    return list(dict.fromkeys(name for uuid in uuid16_values for name in [uuid16_name(uuid)] if name))


def canonical_ble_vendor(name: str) -> str:
    value = str(name or "").strip()
    lowered = value.lower()
    if "apple" in lowered:
        return "Apple, Inc."
    if "microsoft" in lowered:
        return "Microsoft"
    if "tile" in lowered:
        return "Tile, Inc."
    return value


def manufacturer_from_uuid16(uuid16_values: list[str]) -> dict[str, Any] | None:
    for uuid in uuid16_values:
        name = canonical_ble_vendor(uuid16_name(uuid))
        if not name:
            continue
        return {"company_id": "", "company_name": name, "data": "", "source": "uuid16", "uuid16": str(uuid).upper().replace("X", "x")}
    return None


def ble_identity_label(name: str, uuid16_name_values: list[str], manufacturer: dict[str, Any] | None, mac: str) -> str:
    local_name = str(name or "").strip()
    if local_name:
        return local_name
    manufacturer_name = canonical_ble_vendor(str((manufacturer or {}).get("company_name") or ""))
    manufacturer_source = str((manufacturer or {}).get("source") or "")
    if uuid16_name_values:
        first_uuid_name = str(uuid16_name_values[0] or "").strip()
        if manufacturer_name == "Apple, Inc." and manufacturer_source == "uuid16":
            return "AirTag"
        return first_uuid_name
    if manufacturer_name:
        if manufacturer_name == "Apple, Inc." and manufacturer_source == "uuid16":
            return "AirTag"
        return manufacturer_name
    return mac


def ble_device_type_label(name: str, uuid16_name_values: list[str], manufacturer: dict[str, Any] | None, appearance: dict[str, Any] | None) -> str:
    local_name = str(name or "").strip()
    if local_name:
        return ""
    manufacturer_name = canonical_ble_vendor(str((manufacturer or {}).get("company_name") or ""))
    manufacturer_source = str((manufacturer or {}).get("source") or "")
    if manufacturer_name == "Apple, Inc." and manufacturer_source == "uuid16":
        return "AirTag"
    if appearance and str(appearance.get("label") or "").strip():
        return str(appearance.get("label") or "").strip()
    if manufacturer_name == "Tile, Inc.":
        return "Tracker"
    return ""


def ble_device_type_detail(uuid16_name_values: list[str], manufacturer: dict[str, Any] | None, appearance: dict[str, Any] | None) -> str:
    manufacturer_name = canonical_ble_vendor(str((manufacturer or {}).get("company_name") or ""))
    manufacturer_source = str((manufacturer or {}).get("source") or "")
    manufacturer_data = str((manufacturer or {}).get("data") or "").upper()
    if manufacturer_name == "Apple, Inc." and manufacturer_source == "uuid16":
        return "Find My UUID16"
    if manufacturer_name == "Apple, Inc." and manufacturer_data:
        prefix = manufacturer_data[:4]
        if prefix == "1202":
            return "Find My manufacturer frame"
        if prefix in {"1005", "1003", "1001"}:
            return "Continuity frame"
        return f"Apple manufacturer frame {prefix}" if prefix else "Apple manufacturer frame"
    if manufacturer_name == "Microsoft" and manufacturer_data:
        prefix = manufacturer_data[:2]
        if prefix == "03":
            return "Swift Pair frame"
        return f"Microsoft manufacturer frame {manufacturer_data[:4]}" if manufacturer_data else "Microsoft manufacturer frame"
    if manufacturer_name == "Tile, Inc.":
        if uuid16_name_values:
            return "Tile UUID16 service"
        if manufacturer_data:
            return "Tile manufacturer frame"
        return "Tile tracker"
    if appearance and str(appearance.get("code") or "").strip():
        return str(appearance.get("code") or "").strip()
    return ""


def ble_identity_source(name: str, uuid16_name_values: list[str], manufacturer: dict[str, Any] | None) -> str:
    manufacturer_name = str((manufacturer or {}).get("company_name") or "")
    if name:
        return "Local name"
    if uuid16_name_values:
        label = uuid16_name_values[0]
        if canonical_ble_vendor(label) == "Apple, Inc.":
            return "AirTag inferred from UUID16 service"
        return f"{label} UUID16 service"
    if manufacturer_name:
        if (manufacturer or {}).get("source") == "uuid16":
            if canonical_ble_vendor(manufacturer_name) == "Apple, Inc.":
                return "AirTag inferred from UUID16 service"
            return f"{manufacturer_name} inferred from UUID16 service"
        return f"{manufacturer_name} manufacturer data"
    return "MAC only"


def enrich_ble_event(event: dict[str, Any]) -> dict[str, Any]:
    out = dict(event)
    uuid16_values = [str(item) for item in out.get("uuid16") or []]
    uuid16_name_values = uuid16_names(uuid16_values)
    manufacturer = out.get("manufacturer") if isinstance(out.get("manufacturer"), dict) else None
    if not manufacturer:
        manufacturer = manufacturer_from_uuid16(uuid16_values)
    out["manufacturer"] = manufacturer
    out["uuid16_names"] = uuid16_name_values
    out["identity"] = ble_identity_label(str(out.get("name") or ""), uuid16_name_values, manufacturer, str(out.get("address") or ""))
    out["identity_source"] = ble_identity_source(str(out.get("name") or ""), uuid16_name_values, manufacturer)
    out["device_type"] = ble_device_type_label(
        str(out.get("name") or ""),
        uuid16_name_values,
        manufacturer,
        out.get("appearance") if isinstance(out.get("appearance"), dict) else None,
    )
    out["device_type_detail"] = ble_device_type_detail(
        uuid16_name_values,
        manufacturer,
        out.get("appearance") if isinstance(out.get("appearance"), dict) else None,
    )
    return out


class BLEAdvertisingDetector:
    def __init__(self, sample_rate_sps: int = BLE_ADV_SAMPLE_RATE_SPS, center_freq_hz: int = BLE_ADV_CHANNELS[37], channel: int = 37) -> None:
        self.sample_rate_sps = int(sample_rate_sps)
        self.center_freq_hz = int(center_freq_hz)
        self.channel = int(channel)
        self._prev = np.complex64(1.0 + 0j)
        self._bit_tail: list[int] = []
        self._seen_packet_keys: dict[str, float] = {}
        self._burst_holdoff = 0

    def process_iq_i8(self, raw: bytes) -> tuple[float, list[dict[str, Any]]]:
        z = self._iq_bytes_to_complex(raw)
        if z.size < 64:
            return -120.0, []
        return self.process_complex(z)

    def process_complex(self, z: np.ndarray) -> tuple[float, list[dict[str, Any]]]:
        power = np.abs(z) ** 2
        rssi = float(10.0 * np.log10(float(np.mean(power)) + 1e-12))
        threshold = max(float(np.median(power) * 6.5), float(np.mean(power) * 2.2))
        burst_spans = self._find_bursts(power, threshold)
        return rssi, self._ble_events(z, rssi, burst_spans)

    def _iq_bytes_to_complex(self, raw: bytes) -> np.ndarray:
        if len(raw) < 4:
            return np.empty(0, dtype=np.complex64)
        if len(raw) % 2:
            raw = raw[:-1]
        iq = np.frombuffer(raw, dtype=np.int8).astype(np.float32) / 128.0
        return (iq[0::2] + 1j * iq[1::2]).astype(np.complex64)

    def _find_bursts(self, power: np.ndarray, threshold: float) -> list[tuple[int, int, float]]:
        active = power > threshold
        if not np.any(active):
            self._burst_holdoff = max(0, self._burst_holdoff - power.size)
            return []
        idx = np.flatnonzero(active)
        splits = np.where(np.diff(idx) > max(8, int(self.sample_rate_sps * 0.000012)))[0] + 1
        groups = np.split(idx, splits)
        spans: list[tuple[int, int, float]] = []
        min_len = max(20, int(self.sample_rate_sps * 0.000018))
        for group in groups:
            if group.size < min_len:
                continue
            start = int(group[0])
            stop = int(group[-1])
            if self._burst_holdoff > start:
                continue
            peak = float(10.0 * np.log10(float(np.max(power[start : stop + 1])) + 1e-12))
            spans.append((start, stop, peak))
            self._burst_holdoff = stop + int(self.sample_rate_sps * 0.000050)
        self._burst_holdoff = max(0, self._burst_holdoff - power.size)
        return spans[:12]

    def _ble_events(self, z: np.ndarray, rssi_dbfs: float, burst_spans: list[tuple[int, int, float]]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        bits = self._gfsk_bits(z)
        if not bits:
            return self._burst_only_events(rssi_dbfs, burst_spans)
        search_bits = (self._bit_tail + bits)[-8192:]
        self._bit_tail = search_bits[-96:]
        for polarity in (0, 1):
            normalized = [bit ^ polarity for bit in search_bits]
            events.extend(self._extract_ble_adv_packets(normalized, rssi_dbfs))
        if not events:
            events.extend(self._burst_only_events(rssi_dbfs, burst_spans))
        return events[:16]

    def _gfsk_bits(self, z: np.ndarray) -> list[int]:
        freq = self._gfsk_discriminator(z)
        sps = max(1, int(round(self.sample_rate_sps / 1_000_000.0)))
        usable = (freq.size // sps) * sps
        if usable <= 0:
            return []
        symbols = freq[:usable].reshape(-1, sps).mean(axis=1)
        return [1 if value > 0 else 0 for value in symbols.tolist()]

    def _gfsk_discriminator(self, z: np.ndarray) -> np.ndarray:
        prev = np.empty_like(z)
        prev[0] = self._prev
        prev[1:] = z[:-1]
        self._prev = z[-1]
        cross = (prev.real * z.imag) - (prev.imag * z.real)
        freq = cross.astype(np.float32)
        freq -= float(np.median(freq))
        return freq

    def _extract_ble_adv_packets(self, bits: list[int], rssi_dbfs: float) -> list[dict[str, Any]]:
        access_bits = self._bytes_to_lsb_bits(BLE_ADV_ACCESS_BYTES)
        out: list[dict[str, Any]] = []
        for pos in self._find_bit_pattern(bits, access_bits, max_errors=1):
            start = pos + len(access_bits)
            dewhitened = self._ble_dewhiten_bits(bits[start : start + (2 + 37 + 3) * 8], self.channel)
            if len(dewhitened) < 16:
                continue
            header = self._bits_to_bytes(dewhitened[:16])
            if len(header) != 2:
                continue
            pdu_type = header[0] & 0x0F
            if pdu_type > 6:
                continue
            tx_add = bool(header[0] & 0x40)
            length = header[1] & 0x3F
            if length < 6 or length > 37:
                continue
            packet_bit_len = (2 + length + 3) * 8
            dewhitened = self._ble_dewhiten_bits(bits[start : start + packet_bit_len], self.channel)
            if len(dewhitened) < packet_bit_len:
                continue
            pdu_bits = dewhitened[: (2 + length) * 8]
            crc_bits_rx = dewhitened[(2 + length) * 8 : packet_bit_len]
            if self._ble_crc24_bits(pdu_bits) != crc_bits_rx:
                continue
            packet = self._bits_to_bytes(pdu_bits)
            if len(packet) < 2 + length:
                continue
            body = packet[2 : 2 + length]
            advertiser = self._format_ble_addr(body[:6])
            ad_data = body[6:] if len(body) > 6 else b""
            ad_fields = self._ble_ad_fields(ad_data)
            key = f"own-crc:{self.channel}:{pdu_type}:{advertiser}:{packet.hex()}"
            now = time.time()
            if now - self._seen_packet_keys.get(key, 0.0) < 1.0:
                continue
            self._seen_packet_keys[key] = now
            out.append(
                enrich_ble_event(
                    {
                    "kind": "ble_adv",
                    "seen_at": now,
                    "channel": self.channel,
                    "center_freq_hz": self.center_freq_hz,
                    "rssi_dbfs": round(rssi_dbfs, 1),
                    "pdu_type": self._ble_pdu_name(pdu_type),
                    "pdu_type_id": pdu_type,
                    "address": advertiser or "unknown",
                    "address_type": "random" if tx_add else "public",
                    "name": self._ble_local_name_from_fields(ad_fields),
                    "uuid16": self._ble_uuid16s_from_fields(ad_fields),
                    "manufacturer": self._ble_manufacturer_from_fields(ad_fields),
                    "appearance": self._ble_appearance_from_fields(ad_fields),
                    "payload_len": length,
                    "confidence": 0.94,
                    "decoder": "own-crc",
                    "packet": packet.hex(),
                    }
                )
            )
        return out

    def _burst_only_events(self, rssi_dbfs: float, burst_spans: list[tuple[int, int, float]]) -> list[dict[str, Any]]:
        return [
            {
                "kind": "ble_burst",
                "seen_at": time.time(),
                "channel": self.channel,
                "center_freq_hz": self.center_freq_hz,
                "rssi_dbfs": round(rssi_dbfs, 1),
                "peak_dbfs": round(peak, 1),
                "confidence": 0.28,
            }
            for _, _, peak in burst_spans[:8]
        ]

    @staticmethod
    def _ble_dewhiten_bits(bits: list[int], channel: int) -> list[int]:
        lfsr = [1, (channel >> 5) & 1, (channel >> 4) & 1, (channel >> 3) & 1, (channel >> 2) & 1, (channel >> 1) & 1, channel & 1]
        out: list[int] = []
        for raw_bit in bits:
            out.append((raw_bit & 1) ^ lfsr[6])
            lfsr = [lfsr[6], lfsr[0], lfsr[1], lfsr[2], lfsr[3] ^ lfsr[6], lfsr[4], lfsr[5]]
        return out

    @staticmethod
    def _ble_crc24_bits(bits: list[int]) -> list[int]:
        state = [1, 0] * 12
        for bit in bits:
            new_bit = state[23] ^ (bit & 1)
            state = [
                new_bit, state[0] ^ new_bit, state[1], state[2] ^ new_bit,
                state[3] ^ new_bit, state[4], state[5] ^ new_bit, state[6],
                state[7], state[8] ^ new_bit, state[9] ^ new_bit, state[10],
                state[11], state[12], state[13], state[14], state[15], state[16],
                state[17], state[18], state[19], state[20], state[21], state[22],
            ]
        return list(reversed(state))

    @staticmethod
    def _ble_ad_fields(ad_data: bytes) -> list[tuple[int, bytes]]:
        idx = 0
        fields: list[tuple[int, bytes]] = []
        while idx < len(ad_data):
            field_len = ad_data[idx]
            if field_len == 0:
                break
            field_end = idx + 1 + field_len
            if field_end > len(ad_data):
                break
            fields.append((ad_data[idx + 1], ad_data[idx + 2 : field_end]))
            idx = field_end
        return fields

    @staticmethod
    def _ble_local_name_from_fields(fields: list[tuple[int, bytes]]) -> str:
        best = ""
        for ad_type, value in fields:
            if ad_type in {0x08, 0x09} and value:
                name = value.decode("utf-8", errors="replace").strip("\x00\r\n\t ")
                if name:
                    best = name
                    if ad_type == 0x09:
                        return best
        return best

    @staticmethod
    def _ble_uuid16s_from_fields(fields: list[tuple[int, bytes]]) -> list[str]:
        uuids: list[str] = []
        for ad_type, value in fields:
            if ad_type in {0x02, 0x03, 0x14}:
                for idx in range(0, len(value) - 1, 2):
                    uuids.append(f"0x{int.from_bytes(value[idx : idx + 2], 'little'):04X}")
            elif ad_type == 0x16 and len(value) >= 2:
                uuids.append(f"0x{int.from_bytes(value[:2], 'little'):04X}")
        return uuids

    @staticmethod
    def _ble_manufacturer_from_fields(fields: list[tuple[int, bytes]]) -> dict[str, Any] | None:
        for ad_type, value in fields:
            if ad_type != 0xFF or len(value) < 2:
                continue
            company_id = int.from_bytes(value[:2], "little")
            company_hex = f"0x{company_id:04X}"
            return {"company_id": company_hex, "company_name": company_name(company_hex), "data": value[2:].hex().upper()}
        return None

    @staticmethod
    def _ble_appearance_from_fields(fields: list[tuple[int, bytes]]) -> dict[str, Any] | None:
        for ad_type, value in fields:
            if ad_type != 0x19 or len(value) < 2:
                continue
            code = int.from_bytes(value[:2], "little")
            return {"code": f"0x{code:04X}", "label": BLE_APPEARANCE_LABELS.get(code, f"Appearance {code:#06x}")}
        return None

    @staticmethod
    def _bytes_to_lsb_bits(data: bytes) -> list[int]:
        return [(byte >> bit) & 1 for byte in data for bit in range(8)]

    @staticmethod
    def _bits_to_bytes(bits: list[int]) -> bytes:
        usable = (len(bits) // 8) * 8
        out = bytearray()
        for idx in range(0, usable, 8):
            value = 0
            for bit in range(8):
                value |= (bits[idx + bit] & 1) << bit
            out.append(value)
        return bytes(out)

    @staticmethod
    def _find_bit_pattern(bits: list[int], pattern: list[int], max_errors: int) -> list[int]:
        if len(bits) < len(pattern):
            return []
        hits: list[int] = []
        plen = len(pattern)
        for pos in range(0, len(bits) - plen + 1):
            errors = 0
            for offset, expected in enumerate(pattern):
                if bits[pos + offset] != expected:
                    errors += 1
                    if errors > max_errors:
                        break
            if errors <= max_errors:
                hits.append(pos)
        return hits[:8]

    @staticmethod
    def _format_ble_addr(raw: bytes) -> str:
        return ":".join(f"{byte:02X}" for byte in raw[::-1])

    @staticmethod
    def _ble_pdu_name(pdu_type: int) -> str:
        names = {0: "ADV_IND", 1: "ADV_DIRECT_IND", 2: "ADV_NONCONN_IND", 3: "SCAN_REQ", 4: "SCAN_RSP", 5: "CONNECT_IND", 6: "ADV_SCAN_IND"}
        return names.get(pdu_type, f"PDU_{pdu_type}")


class WideBLEAdvertisingDetector:
    """Channelize a wide 2.4 GHz capture into BLE advertising lanes."""

    def __init__(
        self,
        sample_rate_sps: int,
        center_freq_hz: int,
        channels: list[int] | None = None,
        channel_rate_sps: int = BLE_ADV_SAMPLE_RATE_SPS,
        guard_hz: int = 1_200_000,
    ) -> None:
        self.sample_rate_sps = int(sample_rate_sps)
        self.center_freq_hz = int(center_freq_hz)
        self.channel_rate_sps = int(channel_rate_sps)
        self.decim = max(1, int(round(self.sample_rate_sps / float(self.channel_rate_sps))))
        self.lanes: list[dict[str, Any]] = []
        requested = channels or sorted(BLE_ADV_CHANNELS)
        for channel in requested:
            freq_hz = BLE_ADV_CHANNELS[int(channel)]
            offset_hz = float(freq_hz - self.center_freq_hz)
            if abs(offset_hz) > (self.sample_rate_sps / 2.0) - float(guard_hz):
                continue
            self.lanes.append(
                {
                    "channel": int(channel),
                    "freq_hz": int(freq_hz),
                    "offset_hz": offset_hz,
                    "mix_phase_rad": 0.0,
                    "detector": BLEAdvertisingDetector(self.channel_rate_sps, freq_hz, int(channel)),
                }
            )

    def process_iq_i8(self, raw: bytes) -> tuple[float, list[dict[str, Any]]]:
        z = BLEAdvertisingDetector._iq_bytes_to_complex(self, raw)
        if z.size < 64:
            return -120.0, []
        return self.process_complex(z)

    def process_complex(self, z: np.ndarray) -> tuple[float, list[dict[str, Any]]]:
        rssis: list[float] = []
        events: list[dict[str, Any]] = []
        sample_idx = np.arange(z.size, dtype=np.float32)
        for lane in self.lanes:
            phase_step = float((-2.0 * np.pi * float(lane["offset_hz"])) / float(self.sample_rate_sps))
            phase0 = float(lane.get("mix_phase_rad", 0.0))
            rot = np.exp(1j * (phase0 + (phase_step * sample_idx))).astype(np.complex64)
            lane["mix_phase_rad"] = float((phase0 + (phase_step * float(z.size))) % (2.0 * np.pi))
            mixed = z * rot
            lane_samples = self._decimate(mixed, self.decim)
            if lane_samples.size < 64:
                continue
            rssi, lane_events = lane["detector"].process_complex(lane_samples)
            rssis.append(rssi)
            events.extend(lane_events)
        events.sort(key=lambda item: float(item.get("seen_at") or 0.0), reverse=True)
        return max(rssis) if rssis else -120.0, events[:48]

    @staticmethod
    def _decimate(z: np.ndarray, decim: int) -> np.ndarray:
        if decim <= 1:
            return z.astype(np.complex64, copy=False)
        usable = (z.size // decim) * decim
        if usable <= 0:
            return np.empty(0, dtype=np.complex64)
        return z[:usable].reshape(-1, decim).mean(axis=1).astype(np.complex64)
