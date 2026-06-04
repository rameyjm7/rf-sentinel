import os
import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import requests
import websocket
from flask import Flask, jsonify, request, send_from_directory
from websocket import WebSocketConnectionClosedException


BLE_ADV_CHANNELS = {
    37: 2_402_000_000,
    38: 2_426_000_000,
    39: 2_480_000_000,
}

BT_CLASSIC_CHANNELS = {idx: 2_402_000_000 + (idx * 1_000_000) for idx in range(79)}
BT_CLASSIC_BANK_SIZE = 60
# Classic BR/EDR channels are 1 MHz spaced; we decode lanes at 2 Msps for a little timing margin.
BT_CLASSIC_CHANNEL_BW_HZ = 1_000_000
BT_CLASSIC_LANE_RATE_SPS = 2_000_000
BT_CLASSIC_LANE_SPACING_HZ = 1_000_000
BLE_ADV_CHANNEL_BW_HZ = 2_000_000
BLE_ADV_SAMPLE_RATE_SPS = 2_000_000
BLE_ADV_ACCESS_BYTES = bytes.fromhex("d6be898e")
DATA_DIR = Path(__file__).resolve().parent / "data"
BLE_IDENTITY_CACHE_PATH = DATA_DIR / "ble_identities.json"
COMPANY_IDENTIFIERS_PATH = DATA_DIR / "company_identifiers.json"
UUID16_IDENTIFIERS_PATH = DATA_DIR / "uuid16_identifiers.json"
INVALID_CLK_INDEX = -1
DELTA_TS_SAME_THRESHOLD_US = 40
DELTA_TS_SLOT_THRESHOLD_US = 620
SLOT_DURATION_US = 625.0
SLOT_ERROR_THRESHOLD = 0.03


def _gateway_base() -> str:
    return os.getenv("SDR_GATEWAY_BASE_URL", "http://127.0.0.1:8080").rstrip("/")


def _gateway_token() -> str:
    token = (os.getenv("SDR_GATEWAY_API_TOKEN", "") or "").strip()
    if token:
        return token
    return "Vaed36MgaPWugC0Ie5KLYGsiR9wRWKDN/yMNImjGyyENH9lsmZMHUfcRiKShAr4Y"


def _gateway_headers() -> dict[str, str]:
    token = _gateway_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _ws_url_for_stream(stream_id: str) -> str:
    base = _gateway_base()
    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://") :]
    else:
        ws_base = "ws://" + base[len("http://") :]
    return f"{ws_base}/ws/iq/{stream_id}?keep=1"


@dataclass
class ExplorerState:
    running: bool = False
    mode: str = "ble"
    stream_id: str | None = None
    stream_ids: dict[str, str] = field(default_factory=dict)
    device_id: str | None = None
    device_ids: dict[str, str] = field(default_factory=dict)
    center_freq_hz: int = BLE_ADV_CHANNELS[37]
    sample_rate_sps: int = BLE_ADV_SAMPLE_RATE_SPS
    lna_gain_db: int = 24
    vga_gain_db: int = 28
    channel: int = 37
    channels_by_mode: dict[str, int] = field(default_factory=dict)
    worker_alive: bool = False
    worker_alive_by_mode: dict[str, bool] = field(default_factory=dict)
    worker_error: str = ""
    worker_errors: dict[str, str] = field(default_factory=dict)
    gateway_start_response: dict[str, Any] | None = None
    chunks_seen: int = 0
    bytes_seen: int = 0
    last_rssi_dbfs: float = -120.0
    rssi_by_mode: dict[str, float] = field(default_factory=dict)
    chunks_by_mode: dict[str, int] = field(default_factory=dict)
    bytes_by_mode: dict[str, int] = field(default_factory=dict)
    noise_floor_dbfs: float = -120.0
    bursts_seen: int = 0
    ble_packets_seen: int = 0
    classic_bursts_seen: int = 0
    detections: list[dict[str, Any]] = field(default_factory=list)
    classic_candidates: list[dict[str, Any]] = field(default_factory=list)
    classic_addresses: list[dict[str, Any]] = field(default_factory=list)
    discovery_table: list[dict[str, Any]] = field(default_factory=list)
    channel_activity: dict[int, dict[str, Any]] = field(default_factory=dict)
    decoder_stats: dict[str, Any] = field(default_factory=dict)
    test_target: dict[str, Any] | None = None
    test_target_error: str = ""


@dataclass
class LapState:
    lap: int
    status: str = "new"
    ts_us: int = 0
    candidates: list[dict[str, Any]] = field(default_factory=list)
    processed_packets: int = 0
    cannot_init: int = 0
    broken_packets: int = 0


class BluetoothDetector:
    def __init__(self, sample_rate_sps: int, mode: str, center_freq_hz: int, channel: int) -> None:
        self.sample_rate_sps = int(sample_rate_sps)
        self.mode = mode
        self.center_freq_hz = int(center_freq_hz)
        self.channel = int(channel)
        self._prev = np.complex64(1.0 + 0j)
        self._bit_tail: list[int] = []
        self._classic_bit_tails: dict[int, list[int]] = {}
        self._classic_bits_processed = 0
        self._lap_map: dict[int, LapState] = {}
        self._seen_packet_keys: dict[str, float] = {}
        self._burst_holdoff = 0
        self.stats = {
            "preamble_hits": 0,
            "barker_hits": 0,
            "access_code_hits": 0,
            "lap_hits": 0,
            "header_failures": 0,
            "uap_candidate_hits": 0,
        }

    def process_iq_i8(self, raw: bytes) -> tuple[float, list[dict[str, Any]], list[dict[str, Any]]]:
        z = self._iq_bytes_to_complex(raw)
        if z.size < 64:
            return -120.0, [], []
        return self.process_complex(z)

    def process_complex(self, z: np.ndarray) -> tuple[float, list[dict[str, Any]], list[dict[str, Any]]]:
        power = np.abs(z) ** 2
        rssi = float(10.0 * np.log10(float(np.mean(power)) + 1e-12))
        threshold = max(float(np.median(power) * 6.5), float(np.mean(power) * 2.2))
        burst_spans = self._find_bursts(power, threshold)

        if self.mode == "classic":
            return self._classic_events(z, rssi, burst_spans)
        return rssi, self._ble_events(z, rssi, burst_spans), []

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

    def _ble_events(
        self,
        z: np.ndarray,
        rssi_dbfs: float,
        burst_spans: list[tuple[int, int, float]],
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        bits = self._gfsk_bits(z)
        if not bits:
            return self._burst_only_events("ble_burst", rssi_dbfs, burst_spans)

        search_bits = (self._bit_tail + bits)[-8192:]
        self._bit_tail = search_bits[-96:]
        for polarity in (0, 1):
            normalized = [bit ^ polarity for bit in search_bits]
            packet_events = self._extract_ble_adv_packets(normalized, rssi_dbfs)
            events.extend(packet_events)
        if not events:
            events.extend(self._burst_only_events("ble_burst", rssi_dbfs, burst_spans))
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
        # Same sign discriminator idea as the research code, but vectorized.
        cross = (prev.real * z.imag) - (prev.imag * z.real)
        freq = cross.astype(np.float32)
        freq -= float(np.median(freq))
        return freq

    def _classic_bit_phase_streams(self, z: np.ndarray) -> list[tuple[int, list[int]]]:
        freq = self._gfsk_discriminator(z)
        sps = max(1, int(round(self.sample_rate_sps / 1_000_000.0)))
        streams: list[tuple[int, list[int]]] = []
        for phase in range(sps):
            symbols = freq[phase::sps]
            if symbols.size:
                streams.append((phase, [1 if value > 0 else 0 for value in symbols.tolist()]))
        return streams

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
            local_name = self._ble_local_name_from_fields(ad_fields)
            uuid16 = self._ble_uuid16s_from_fields(ad_fields)
            manufacturer = self._ble_manufacturer_from_fields(ad_fields)
            key = f"own-crc:{self.channel}:{pdu_type}:{advertiser}:{packet.hex()}"
            now = time.time()
            if now - self._seen_packet_keys.get(key, 0.0) < 1.0:
                continue
            self._seen_packet_keys[key] = now
            out.append(
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
                    "name": local_name,
                    "uuid16": uuid16,
                    "manufacturer": manufacturer,
                    "payload_len": length,
                    "confidence": 0.94,
                    "decoder": "own-crc",
                }
            )
        return out

    @staticmethod
    def _ble_dewhiten_bits(bits: list[int], channel: int) -> list[int]:
        lfsr = [
            1,
            (channel >> 5) & 1,
            (channel >> 4) & 1,
            (channel >> 3) & 1,
            (channel >> 2) & 1,
            (channel >> 1) & 1,
            channel & 1,
        ]
        out: list[int] = []
        for raw_bit in bits:
            out.append((raw_bit & 1) ^ lfsr[6])
            lfsr = [
                lfsr[6],
                lfsr[0],
                lfsr[1],
                lfsr[2],
                lfsr[3] ^ lfsr[6],
                lfsr[4],
                lfsr[5],
            ]
        return out

    @staticmethod
    def _ble_crc24_bits(bits: list[int]) -> list[int]:
        state = [1, 0] * 12
        for bit in bits:
            new_bit = state[23] ^ (bit & 1)
            state = [
                new_bit,
                state[0] ^ new_bit,
                state[1],
                state[2] ^ new_bit,
                state[3] ^ new_bit,
                state[4],
                state[5] ^ new_bit,
                state[6],
                state[7],
                state[8] ^ new_bit,
                state[9] ^ new_bit,
                state[10],
                state[11],
                state[12],
                state[13],
                state[14],
                state[15],
                state[16],
                state[17],
                state[18],
                state[19],
                state[20],
                state[21],
                state[22],
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
            ad_type = ad_data[idx + 1]
            value = ad_data[idx + 2 : field_end]
            fields.append((ad_type, value))
            idx = field_end
        return fields

    @classmethod
    def _ble_local_name(cls, ad_data: bytes) -> str:
        return cls._ble_local_name_from_fields(cls._ble_ad_fields(ad_data))

    @staticmethod
    def _ble_local_name_from_fields(fields: list[tuple[int, bytes]]) -> str:
        best = ""
        for ad_type, value in fields:
            if ad_type in {0x08, 0x09} and value:
                try:
                    name = value.decode("utf-8", errors="replace").strip("\x00\r\n\t ")
                except Exception:
                    name = ""
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
                continue
            if ad_type == 0x16 and len(value) >= 2:
                uuids.append(f"0x{int.from_bytes(value[:2], 'little'):04X}")
        return uuids

    @staticmethod
    def _ble_manufacturer_from_fields(fields: list[tuple[int, bytes]]) -> dict[str, Any] | None:
        for ad_type, value in fields:
            if ad_type != 0xFF or len(value) < 2:
                continue
            company_id = int.from_bytes(value[:2], "little")
            company_hex = f"0x{company_id:04X}"
            return {
                "company_id": company_hex,
                "company_name": _company_name(company_hex),
                "data": value[2:].hex().upper(),
            }
        return None

    def _classic_events(
        self,
        z: np.ndarray,
        rssi_dbfs: float,
        burst_spans: list[tuple[int, int, float]],
    ) -> tuple[float, list[dict[str, Any]], list[dict[str, Any]]]:
        events: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        phase_streams = self._classic_bit_phase_streams(z)
        for phase, bits in phase_streams:
            tail = self._classic_bit_tails.get(phase, [])
            base_bit_index = self._classic_bits_processed - len(tail)
            search_bits = tail + bits
            for polarity in (0, 1):
                normalized = [bit ^ polarity for bit in search_bits]
                for observation in self._extract_classic_observations(normalized, base_bit_index, rssi_dbfs):
                    event, lap_candidates = self._update_lap_state(observation)
                    event["phase"] = phase
                    events.append(event)
                    candidates.extend(lap_candidates)
            self._classic_bit_tails[phase] = search_bits[-192:]

        self._classic_bits_processed += max((len(bits) for _, bits in phase_streams), default=0)
        if events:
            return rssi_dbfs, events[:16], candidates[:24]

        for start, stop, peak in burst_spans:
            duration_us = (stop - start + 1) * 1_000_000.0 / float(self.sample_rate_sps)
            events.append(
                {
                    "kind": "classic_burst",
                    "seen_at": time.time(),
                    "channel": self.channel,
                    "center_freq_hz": self.center_freq_hz,
                    "rssi_dbfs": round(rssi_dbfs, 1),
                    "peak_dbfs": round(peak, 1),
                    "duration_us": round(duration_us, 1),
                    "uap": None,
                    "confidence": 0.35,
                }
            )
        return rssi_dbfs, events, []

    def _extract_classic_observations(
        self,
        bits: list[int],
        base_bit_index: int,
        rssi_dbfs: float,
    ) -> list[dict[str, Any]]:
        observations: list[dict[str, Any]] = []
        min_packet_bits = 72 + 54
        for pos in range(0, max(0, len(bits) - min_packet_bits)):
            if not self._classic_preamble_ok(bits, pos):
                continue
            self.stats["preamble_hits"] += 1
            if self._classic_barker(bits, pos) is not None:
                self.stats["barker_hits"] += 1
            access = self._classic_access_code(bits, pos)
            if access is None:
                continue
            self.stats["access_code_hits"] += 1
            header_result = self._classic_bruteforce_all_uaps(bits[pos + 72 : pos + 72 + 54])
            if header_result["valid_uaps"] == 0:
                self.stats["header_failures"] += 1
                continue
            self.stats["lap_hits"] += 1
            self.stats["uap_candidate_hits"] += int(header_result["valid_uaps"])
            observations.append(
                {
                    "lap": access["lap"],
                    "access_word": access["access_word"],
                    "header": header_result["header"],
                    "uap_results": header_result["uap_results"],
                    "valid_uaps": header_result["valid_uaps"],
                    "ts_us": int(base_bit_index + pos),
                    "rssi_dbfs": round(rssi_dbfs, 1),
                }
            )
            if len(observations) >= 8:
                break
        return observations

    def _classic_preamble_ok(self, bits: list[int], pos: int) -> bool:
        if pos + 68 >= len(bits):
            return False
        even = bits[pos] + bits[pos + 2]
        odd = bits[pos + 1] + bits[pos + 3]
        return (even == 2 and odd == 0) or (even == 0 and odd == 2)

    def _classic_barker(self, bits: list[int], pos: int) -> int | None:
        barker = self._swap_bits(self._extract_msb_byte(bits, pos + 62)) & 0x3F
        return barker if barker in {0x13, 0x2C} else None

    def _classic_access_code(self, bits: list[int], pos: int) -> dict[str, int] | None:
        barker = self._classic_barker(bits, pos)
        if barker is None:
            return None
        lap = (
            (self._swap_bits(self._extract_msb_byte(bits, pos + 54)) << 16)
            | (self._swap_bits(self._extract_msb_byte(bits, pos + 46)) << 8)
            | self._swap_bits(self._extract_msb_byte(bits, pos + 38))
        )
        code = (
            (self._swap_bits(self._extract_msb_byte(bits, pos + 4)) << 0)
            | (self._swap_bits(self._extract_msb_byte(bits, pos + 12)) << 8)
            | (self._swap_bits(self._extract_msb_byte(bits, pos + 20)) << 16)
            | (self._swap_bits(self._extract_msb_byte(bits, pos + 28)) << 24)
            | (self._swap_bits(self._extract_msb_byte(bits, pos + 36)) << 32)
        ) & 0x3FFFFFFFF
        access_word = (barker << 58) | (lap << 34) | code

        barker_true = 0x13 if (lap & 0x800000) else 0x2C
        x = (barker_true << 24) | lap
        p = 0x83848D96BBCC54FC
        xtilde = (p >> 34) ^ x
        gp = int("157464165547", 8)
        g = (gp << 1) ^ gp
        ctilde = self._compute_remainder(xtilde, g)
        expected = (ctilde | (xtilde << 34)) ^ p
        if access_word != expected:
            return None
        return {"lap": lap, "access_word": access_word}

    def _classic_bruteforce_all_uaps(self, header_bits: list[int]) -> dict[str, Any]:
        header = 0
        perfect_rx = 0
        for idx in range(0, 54, 3):
            triple = header_bits[idx : idx + 3]
            s1 = sum(triple)
            s0 = 3 - s1
            header >>= 1
            if s1 == 0 or s0 == 0:
                perfect_rx += 1
            if s1 > s0:
                header |= 0x20000
        if perfect_rx != 18:
            return {"header": header, "valid_uaps": 0, "uap_results": []}

        results: list[dict[str, Any]] = []
        for uap in range(256):
            clks = self._classic_header_clks_for_uap(header, uap)
            if clks:
                results.append({"uap": uap, "clks": clks})
        return {"header": header, "valid_uaps": len(results), "uap_results": results}

    def _classic_header_clks_for_uap(self, header: int, uap: int) -> list[int]:
        found: list[int] = []
        for clk in range(64):
            header_dewhiten = header
            whitener = (clk & 0x3F) | 0x40
            for bit_idx in range(18):
                whitener_out = (whitener >> 6) & 0x1
                whitener_shifted = (whitener << 1) & 0x7F
                whitener = whitener_shifted ^ (whitener_out | (whitener_out << 4))
                header_dewhiten ^= whitener_out << bit_idx

            lfsr = uap
            for bit_idx in range(10):
                lfsr_out = (lfsr >> 7) & 0x1
                data_in = (header_dewhiten >> bit_idx) & 0x1
                lfsr_in = lfsr_out ^ data_in
                lfsr_adder = (
                    (lfsr_in << 7)
                    | (lfsr_in << 5)
                    | (lfsr_in << 2)
                    | (lfsr_in << 1)
                    | (lfsr_in << 0)
                )
                lfsr = ((lfsr << 1) & 0xFF) ^ lfsr_adder

            for bit_idx in range(8):
                bit_rx = (header_dewhiten >> (10 + bit_idx)) & 0x1
                bit_tx = (lfsr >> (7 - bit_idx)) & 0x1
                if bit_rx != bit_tx:
                    break
            else:
                found.append(clk)
        return found

    def _update_lap_state(self, observation: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        lap = int(observation["lap"])
        node = self._lap_map.get(lap)
        if node is None:
            node = LapState(lap=lap)
            self._lap_map[lap] = node
        node.processed_packets += 1
        event_status = node.status

        if node.status == "new":
            init_candidates = [
                {"uap": item["uap"], "clks": item["clks"], "clk_index": INVALID_CLK_INDEX, "valid": True}
                for item in observation["uap_results"]
                if len(item["clks"]) == 2
            ]
            node.ts_us = int(observation["ts_us"])
            if len(init_candidates) == 32:
                node.candidates = init_candidates
                node.status = "brute_forcing"
                event_status = "initialized"
            else:
                node.cannot_init += 1
                event_status = f"init_found_{len(init_candidates)}"
        elif node.status == "brute_forcing":
            self._prune_lap_candidates(node, observation)
            event_status = node.status

        candidates = [self._candidate_payload(node, item, observation) for item in node.candidates if item.get("valid")]
        event = {
            "kind": "classic_lap",
            "seen_at": time.time(),
            "channel": self.channel,
            "center_freq_hz": self.center_freq_hz,
            "rssi_dbfs": observation["rssi_dbfs"],
            "lap": f"{lap:06X}",
            "access_word": f"{int(observation['access_word']):018X}",
            "candidate_count": len(candidates),
            "uap": f"{candidates[0]['uap']:02X}" if len(candidates) == 1 else None,
            "status": event_status,
            "confidence": 0.92 if len(candidates) == 1 else 0.68 if candidates else 0.42,
        }
        return event, candidates

    def _prune_lap_candidates(self, node: LapState, observation: dict[str, Any]) -> None:
        delta_us = int(observation["ts_us"]) - int(node.ts_us)
        if delta_us < 0 or abs(delta_us) < DELTA_TS_SAME_THRESHOLD_US:
            return
        if abs(delta_us) < DELTA_TS_SLOT_THRESHOLD_US:
            self._lap_map.pop(node.lap, None)
            return
        periods = float(delta_us) / SLOT_DURATION_US
        periods_round = round(periods)
        if abs(periods - periods_round) > SLOT_ERROR_THRESHOLD:
            self._lap_map.pop(node.lap, None)
            return

        result_by_uap = {item["uap"]: item["clks"] for item in observation["uap_results"] if len(item["clks"]) == 2}
        valid_count = 0
        broken_count = 0
        slot = int(periods_round) % 64
        for candidate in node.candidates:
            if not candidate.get("valid"):
                continue
            new_clks = result_by_uap.get(candidate["uap"])
            if not new_clks:
                candidate["valid"] = False
                broken_count += 1
                continue
            old_clks = candidate["clks"]
            if new_clks == old_clks:
                valid_count += 1
                continue
            old_to_check = [old_clks[candidate["clk_index"]]] if candidate["clk_index"] != INVALID_CLK_INDEX else old_clks
            matches: list[int] = []
            for old_clk in old_to_check:
                for new_idx, new_clk in enumerate(new_clks):
                    if ((new_clk - old_clk) % 64) == slot:
                        matches.append(new_idx)
            if len(matches) > 1:
                candidate["clks"] = new_clks
                candidate["clk_index"] = INVALID_CLK_INDEX
                valid_count += 1
            elif len(matches) == 1:
                candidate["clks"] = new_clks
                candidate["clk_index"] = matches[0]
                valid_count += 1
            else:
                candidate["valid"] = False

        if valid_count == 0 and broken_count > 0:
            node.broken_packets += 1
            return
        if valid_count <= 2:
            node.status = "resolved"
        node.ts_us = int(observation["ts_us"])

    def _candidate_payload(self, node: LapState, candidate: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
        valid = [item for item in node.candidates if item.get("valid")]
        score = 1.0 / max(1, len(valid))
        return {
            "lap": f"{node.lap:06X}",
            "uap": int(candidate["uap"]),
            "uap_hex": f"{int(candidate['uap']):02X}",
            "score": round(score, 3),
            "channel": self.channel,
            "center_freq_hz": self.center_freq_hz,
            "rssi_dbfs": observation["rssi_dbfs"],
            "status": node.status,
            "candidate_count": len(valid),
            "processed_packets": node.processed_packets,
            "clks": candidate["clks"],
            "notes": [
                "LAP extracted from Classic access code.",
                "UAP candidate validated by dewhitening header and matching HEC.",
            ],
        }

    @staticmethod
    def _extract_msb_byte(bits: list[int], start: int) -> int:
        value = 0
        for idx in range(8):
            value |= (bits[start + idx] & 1) << (7 - idx)
        return value

    @staticmethod
    def _swap_bits(value: int) -> int:
        out = 0
        for idx in range(8):
            out = (out << 1) | ((value >> idx) & 1)
        return out

    @staticmethod
    def _bit_length(value: int) -> int:
        return int(value).bit_length()

    @classmethod
    def _compute_remainder(cls, input_value: int, divisor: int) -> int:
        divisor_length = cls._bit_length(divisor)
        input_length = cls._bit_length(input_value)
        if divisor_length + input_length > 63:
            return input_value
        input_value <<= divisor_length
        while cls._bit_length(input_value) >= divisor_length:
            input_value ^= divisor << (cls._bit_length(input_value) - divisor_length)
        return input_value

    def _burst_only_events(
        self,
        kind: str,
        rssi_dbfs: float,
        burst_spans: list[tuple[int, int, float]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "kind": kind,
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
    def _bytes_to_lsb_bits(data: bytes) -> list[int]:
        return [(byte >> bit) & 1 for byte in data for bit in range(8)]

    @staticmethod
    def _bits_to_bytes(bits: list[int]) -> bytes:
        if len(bits) < 8:
            return b""
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
        names = {
            0: "ADV_IND",
            1: "ADV_DIRECT_IND",
            2: "ADV_NONCONN_IND",
            3: "SCAN_REQ",
            4: "SCAN_RSP",
            5: "CONNECT_IND",
            6: "ADV_SCAN_IND",
        }
        return names.get(pdu_type, f"PDU_{pdu_type}")


class WideClassicDetector:
    def __init__(self, sample_rate_sps: int, center_freq_hz: int, bank_start_channel: int) -> None:
        self.sample_rate_sps = int(sample_rate_sps)
        self.center_freq_hz = int(center_freq_hz)
        self.bank_start_channel = int(bank_start_channel)
        self.lane_rate_sps = BT_CLASSIC_LANE_RATE_SPS
        self.decim = max(1, int(round(self.sample_rate_sps / self.lane_rate_sps)))
        self.lanes: list[dict[str, Any]] = []
        self.stats = {
            "preamble_hits": 0,
            "barker_hits": 0,
            "access_code_hits": 0,
            "lap_hits": 0,
            "header_failures": 0,
            "uap_candidate_hits": 0,
        }
        for idx in range(BT_CLASSIC_BANK_SIZE):
            channel = self.bank_start_channel + idx
            if channel not in BT_CLASSIC_CHANNELS:
                continue
            freq_hz = BT_CLASSIC_CHANNELS[channel]
            self.lanes.append(
                {
                    "channel": channel,
                    "freq_hz": freq_hz,
                    "offset_hz": float(freq_hz - self.center_freq_hz),
                    "detector": BluetoothDetector(self.lane_rate_sps, "classic", freq_hz, channel),
                }
            )

    def process_iq_i8(self, raw: bytes) -> tuple[float, list[dict[str, Any]], list[dict[str, Any]]]:
        z = BluetoothDetector._iq_bytes_to_complex(self, raw)
        if z.size < 64:
            return -120.0, [], []

        all_events: list[dict[str, Any]] = []
        all_candidates: list[dict[str, Any]] = []
        rssis: list[float] = []
        sample_idx = np.arange(z.size, dtype=np.float32)
        for lane in self.lanes:
            offset_hz = float(lane["offset_hz"])
            rot = np.exp((-2j * np.pi * offset_hz / float(self.sample_rate_sps)) * sample_idx).astype(np.complex64)
            mixed = z * rot
            lane_samples = self._decimate_lane(mixed)
            if lane_samples.size < 64:
                continue
            rssi, events, candidates = lane["detector"].process_complex(lane_samples)
            for key, value in lane["detector"].stats.items():
                self.stats[key] = int(self.stats.get(key, 0)) + int(value)
                lane["detector"].stats[key] = 0
            rssis.append(rssi)
            all_events.extend(events)
            all_candidates.extend(candidates)

        bank_rssi = max(rssis) if rssis else -120.0
        return bank_rssi, all_events[:80], all_candidates[:80]

    def _decimate_lane(self, z: np.ndarray) -> np.ndarray:
        if self.decim <= 1:
            return z
        usable = (z.size // self.decim) * self.decim
        if usable <= 0:
            return np.empty(0, dtype=np.complex64)
        # A boxcar averager is a cheap low-pass for ten 2 MHz lanes from a 20 MHz stream.
        return z[:usable].reshape(-1, self.decim).mean(axis=1).astype(np.complex64)


class CombinedBluetoothDetector:
    def __init__(self, sample_rate_sps: int, center_freq_hz: int, bank_start_channel: int) -> None:
        self.sample_rate_sps = int(sample_rate_sps)
        self.center_freq_hz = int(center_freq_hz)
        self.classic = WideClassicDetector(sample_rate_sps, center_freq_hz, bank_start_channel)
        self.ble_lanes: list[dict[str, Any]] = []
        self.stats = self.classic.stats
        for channel, freq_hz in BLE_ADV_CHANNELS.items():
            offset_hz = float(freq_hz - self.center_freq_hz)
            if abs(offset_hz) > (self.sample_rate_sps / 2.0) - 1_200_000:
                continue
            self.ble_lanes.append(
                {
                    "channel": channel,
                    "freq_hz": freq_hz,
                    "offset_hz": offset_hz,
                    "detector": BluetoothDetector(BT_CLASSIC_LANE_RATE_SPS, "ble", freq_hz, channel),
                }
            )

    def process_iq_i8(self, raw: bytes) -> tuple[float, list[dict[str, Any]], list[dict[str, Any]]]:
        z = BluetoothDetector._iq_bytes_to_complex(self, raw)
        if z.size < 64:
            return -120.0, [], []

        classic_rssi, events, candidates = self.classic.process_iq_i8(raw)
        self.stats = self.classic.stats
        rssis = [classic_rssi]
        sample_idx = np.arange(z.size, dtype=np.float32)
        decim = max(1, int(round(self.sample_rate_sps / BT_CLASSIC_LANE_RATE_SPS)))
        for lane in self.ble_lanes:
            rot = np.exp((-2j * np.pi * float(lane["offset_hz"]) / float(self.sample_rate_sps)) * sample_idx).astype(np.complex64)
            mixed = z * rot
            lane_samples = self._decimate(mixed, decim)
            if lane_samples.size < 64:
                continue
            rssi, ble_events, _ = lane["detector"].process_complex(lane_samples)
            rssis.append(rssi)
            events.extend(ble_events)
        return max(rssis) if rssis else -120.0, events[:96], candidates[:80]

    @staticmethod
    def _decimate(z: np.ndarray, decim: int) -> np.ndarray:
        if decim <= 1:
            return z
        usable = (z.size // decim) * decim
        if usable <= 0:
            return np.empty(0, dtype=np.complex64)
        return z[:usable].reshape(-1, decim).mean(axis=1).astype(np.complex64)


app = Flask(__name__, static_folder="../frontend", static_url_path="")
state = ExplorerState()
state_lock = threading.Lock()
identity_cache_lock = threading.Lock()
worker_stop = threading.Event()
worker_thread: threading.Thread | None = None
worker_stops: dict[str, threading.Event] = {}
worker_threads: dict[str, threading.Thread] = {}
ble_identity_cache: dict[str, dict[str, Any]] = {}
company_identifier_lut: dict[str, str] = {}
uuid16_identifier_lut: dict[str, str] = {}


def _normalize_mac(mac: str) -> str:
    clean = re.sub(r"[^0-9A-Fa-f]", "", mac or "").upper()
    if len(clean) != 12:
        return str(mac or "").upper()
    return ":".join(clean[idx : idx + 2] for idx in range(0, 12, 2))


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


def _company_name(company_id: str) -> str:
    return company_identifier_lut.get(str(company_id or "").upper().replace("X", "x"), "")


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


def _uuid16_name(uuid16: str) -> str:
    return uuid16_identifier_lut.get(str(uuid16 or "").upper().replace("X", "x"), "")


def _uuid16_names(uuid16_values: list[str]) -> list[str]:
    return list(dict.fromkeys(name for uuid in uuid16_values for name in [_uuid16_name(uuid)] if name))


def _ble_identity_source(name: str, uuid16_names: list[str], manufacturer: dict[str, Any] | None) -> str:
    manufacturer_name = str((manufacturer or {}).get("company_name") or "")
    if name:
        return "Local name"
    if uuid16_names:
        label = uuid16_names[0]
        return f"{label} UUID16 service"
    if manufacturer_name:
        return f"{manufacturer_name} manufacturer data"
    return "MAC only"


def _load_ble_identity_cache() -> dict[str, dict[str, Any]]:
    if not BLE_IDENTITY_CACHE_PATH.exists():
        return {}
    try:
        with BLE_IDENTITY_CACHE_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    rows = data.get("devices", data) if isinstance(data, dict) else {}
    if not isinstance(rows, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in rows.items():
        if not isinstance(value, dict):
            continue
        mac = _normalize_mac(str(value.get("mac") or key))
        if not mac:
            continue
        manufacturer = value.get("manufacturer") if isinstance(value.get("manufacturer"), dict) else None
        if manufacturer and manufacturer.get("company_id") and not manufacturer.get("company_name"):
            manufacturer = dict(manufacturer)
            manufacturer["company_name"] = _company_name(str(manufacturer.get("company_id")))
        out[mac] = {
            "mac": mac,
            "name": str(value.get("name") or "").strip(),
            "address_type": str(value.get("address_type") or "").strip(),
            "uuid16": value.get("uuid16") if isinstance(value.get("uuid16"), list) else [],
            "uuid16_names": _uuid16_names(value.get("uuid16") if isinstance(value.get("uuid16"), list) else []),
            "manufacturer": manufacturer,
            "identity_source": str(value.get("identity_source") or ""),
            "first_seen_at": float(value.get("first_seen_at") or value.get("last_seen_at") or time.time()),
            "last_seen_at": float(value.get("last_seen_at") or time.time()),
            "seen_count": int(value.get("seen_count") or 0),
        }
    return out


def _save_ble_identity_cache() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": time.time(),
        "devices": dict(sorted(ble_identity_cache.items())),
    }
    tmp_path = BLE_IDENTITY_CACHE_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    tmp_path.replace(BLE_IDENTITY_CACHE_PATH)


def _remember_ble_identity(
    mac: str,
    name: str,
    address_type: str,
    seen_at: float,
    uuid16: list[str] | None = None,
    manufacturer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = _normalize_mac(mac)
    with identity_cache_lock:
        row = dict(ble_identity_cache.get(normalized) or {})
        row["mac"] = normalized
        if name:
            row["name"] = name
        else:
            row.setdefault("name", "")
        if address_type:
            row["address_type"] = address_type
        else:
            row.setdefault("address_type", "")
        merged_uuid16 = list(dict.fromkeys([*(row.get("uuid16") or []), *(uuid16 or [])]))
        row["uuid16"] = merged_uuid16
        row["uuid16_names"] = _uuid16_names(merged_uuid16)
        if manufacturer:
            row["manufacturer"] = manufacturer
        else:
            row.setdefault("manufacturer", None)
        row["identity_source"] = _ble_identity_source(str(row.get("name") or ""), row["uuid16_names"], row.get("manufacturer"))
        row["first_seen_at"] = float(row.get("first_seen_at") or seen_at)
        row["last_seen_at"] = seen_at
        row["seen_count"] = int(row.get("seen_count") or 0) + 1
        ble_identity_cache[normalized] = row
        _save_ble_identity_cache()
        return dict(row)


company_identifier_lut.update(_load_company_identifier_lut())
uuid16_identifier_lut.update(_load_uuid16_identifier_lut())
ble_identity_cache.update(_load_ble_identity_cache())


def _gateway_streams() -> list[dict[str, Any]]:
    try:
        resp = requests.get(f"{_gateway_base()}/streams", headers=_gateway_headers(), timeout=2)
        if resp.status_code >= 400:
            return []
        body = resp.json()
        return body if isinstance(body, list) else []
    except requests.RequestException:
        return []


def _stop_gateway_stream(stream_id: str | None) -> None:
    if not stream_id:
        return
    try:
        requests.post(f"{_gateway_base()}/streams/{stream_id}/stop", headers=_gateway_headers(), timeout=3)
    except requests.RequestException:
        pass


def _stop_duplicate_gateway_streams(device_id: str | None, keep_stream_id: str | None = None) -> None:
    if not device_id:
        return
    for stream in _gateway_streams():
        stream_id = str(stream.get("stream_id", "")).strip()
        cfg = stream.get("config", {}) or {}
        if stream_id and stream_id != keep_stream_id and str(cfg.get("device_id", "")).strip() == device_id:
            _stop_gateway_stream(stream_id)


def _append_detections(events: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> None:
    if not events and not candidates:
        return
    with state_lock:
        for event in events:
            state.bursts_seen += 1 if event["kind"].endswith("burst") else 0
            state.ble_packets_seen += 1 if event["kind"] == "ble_adv" else 0
            state.classic_bursts_seen += 1 if event["kind"] in {"classic_burst", "classic_lap"} else 0
            if event["kind"] in {"ble_adv", "classic_lap"}:
                _upsert_discovery_row(event)
            if event["kind"] == "classic_lap":
                _upsert_classic_address(event)
            if event["kind"] in {"classic_burst", "classic_lap"}:
                _upsert_channel_activity(event)
        state.detections = (events + state.detections)[:240]
        if candidates:
            state.classic_candidates = (candidates + state.classic_candidates)[:64]


def _upsert_discovery_row(event: dict[str, Any]) -> None:
    now = float(event.get("seen_at", time.time()))
    if event.get("kind") == "ble_adv":
        mac = str(event.get("address") or "unknown").strip()
        if not mac or mac == "unknown":
            return
        name = str(event.get("name") or "").strip()
        address_type = str(event.get("address_type") or "")
        uuid16 = event.get("uuid16") if isinstance(event.get("uuid16"), list) else []
        manufacturer = event.get("manufacturer") if isinstance(event.get("manufacturer"), dict) else None
        cached = _remember_ble_identity(mac, name, address_type, now, uuid16, manufacturer)
        name = name or str(cached.get("name") or "").strip()
        uuid16 = list(cached.get("uuid16") or uuid16)
        uuid16_names = list(cached.get("uuid16_names") or _uuid16_names(uuid16))
        manufacturer = cached.get("manufacturer") or manufacturer
        manufacturer_name = str((manufacturer or {}).get("company_name") or "")
        identity = name or (uuid16_names[0] if uuid16_names else "") or manufacturer_name or mac
        identity_source = _ble_identity_source(name, uuid16_names, manufacturer)
        row = {
            "key": f"ble:{mac}",
            "protocol": "BTLE",
            "identity": identity,
            "mac": mac,
            "name": name,
            "uuid16": uuid16,
            "uuid16_names": uuid16_names,
            "manufacturer": manufacturer,
            "identity_source": identity_source,
            "detail": address_type,
            "detections": 1,
            "last_seen_at": now,
            "last_rssi_dbfs": event.get("rssi_dbfs"),
            "channel": event.get("channel"),
            "center_freq_hz": event.get("center_freq_hz"),
        }
    elif event.get("kind") == "classic_lap":
        lap = str(event.get("lap") or "").strip()
        if not lap:
            return
        uap = str(event.get("uap") or "XXX")
        target = _classic_test_match(lap, uap)
        identity = f"LAP {lap} / UAP {uap}"
        detail = str(event.get("status") or "")
        if target:
            identity = f"TEST DONGLE {identity}"
            detail = "target-match" if not detail else f"target-match · {detail}"
        row = {
            "key": f"btc:{lap}:{uap if uap != 'XXX' else 'missing'}",
            "protocol": "BTC",
            "identity": identity,
            "mac": "",
            "detail": detail,
            "target": bool(target),
            "detections": 1,
            "last_seen_at": now,
            "last_rssi_dbfs": event.get("rssi_dbfs"),
            "channel": event.get("channel"),
            "center_freq_hz": event.get("center_freq_hz"),
        }
    else:
        return

    for idx, existing in enumerate(state.discovery_table):
        if existing.get("key") != row["key"]:
            continue
        row["detections"] = int(existing.get("detections") or 0) + 1
        if not row.get("name") and existing.get("name"):
            row["name"] = existing["name"]
        if row.get("protocol") == "BTLE":
            row["uuid16"] = list(dict.fromkeys([*(existing.get("uuid16") or []), *(row.get("uuid16") or [])]))
            row["uuid16_names"] = _uuid16_names(row["uuid16"])
            if not row.get("manufacturer") and existing.get("manufacturer"):
                row["manufacturer"] = existing["manufacturer"]
            row["identity_source"] = _ble_identity_source(
                str(row.get("name") or ""),
                row.get("uuid16_names") or [],
                row.get("manufacturer") if isinstance(row.get("manufacturer"), dict) else None,
            )
        if row.get("protocol") == "BTLE" and row.get("name"):
            row["identity"] = row["name"]
        elif row.get("protocol") == "BTLE" and row.get("uuid16_names"):
            row["identity"] = row["uuid16_names"][0]
        state.discovery_table[idx] = row
        break
    else:
        state.discovery_table.insert(0, row)
    state.discovery_table.sort(key=lambda item: float(item.get("last_seen_at") or 0), reverse=True)
    state.discovery_table = state.discovery_table[:160]


def _upsert_classic_address(event: dict[str, Any]) -> None:
    lap = str(event.get("lap", "")).strip()
    if not lap:
        return
    now = float(event.get("seen_at", time.time()))
    target = _classic_test_match(lap, str(event.get("uap") or "XXX"))
    row = {
        "lap": lap,
        "uap": str(event.get("uap") or "XXX"),
        "status": "target-match" if target else str(event.get("status") or "observed"),
        "target": bool(target),
        "candidate_count": int(event.get("candidate_count") or 0),
        "channel": event.get("channel"),
        "center_freq_hz": event.get("center_freq_hz"),
        "rssi_dbfs": event.get("rssi_dbfs"),
        "first_seen_at": now,
        "last_seen_at": now,
        "seen_count": 1,
    }
    for idx, existing in enumerate(state.classic_addresses):
        if existing.get("lap") != lap:
            continue
        row["first_seen_at"] = existing.get("first_seen_at", now)
        row["seen_count"] = int(existing.get("seen_count") or 0) + 1
        if existing.get("uap") not in {"", None, "XXX"} and row["uap"] == "XXX":
            row["uap"] = existing["uap"]
        state.classic_addresses[idx] = row
        break
    else:
        state.classic_addresses.insert(0, row)
    state.classic_addresses.sort(key=lambda item: float(item.get("last_seen_at") or 0), reverse=True)
    state.classic_addresses = state.classic_addresses[:96]


def _upsert_channel_activity(event: dict[str, Any]) -> None:
    try:
        channel = int(event.get("channel"))
    except (TypeError, ValueError):
        return
    if channel not in BT_CLASSIC_CHANNELS:
        return
    row = state.channel_activity.get(channel, {"channel": channel, "hits": 0, "rssi_dbfs": -120.0})
    row["hits"] = int(row.get("hits") or 0) + 1
    row["rssi_dbfs"] = event.get("rssi_dbfs", row.get("rssi_dbfs", -120.0))
    row["last_seen_at"] = event.get("seen_at", time.time())
    state.channel_activity[channel] = row


def _classic_test_match(lap: str, uap: str = "XXX") -> dict[str, Any] | None:
    target = state.test_target or {}
    if target.get("protocol") != "BTC":
        return None
    if str(target.get("lap") or "").upper() != str(lap or "").upper():
        return None
    target_uap = str(target.get("uap") or "").upper()
    observed_uap = str(uap or "XXX").upper()
    if observed_uap not in {"", "XXX"} and target_uap and observed_uap != target_uap:
        return None
    return target


def _classic_target_from_mac(mac: str) -> dict[str, Any]:
    clean = re.sub(r"[^0-9A-Fa-f]", "", mac).upper()
    if len(clean) != 12:
        raise ValueError(f"Invalid Bluetooth MAC: {mac}")
    return {
        "protocol": "BTC",
        "mac": ":".join(clean[idx : idx + 2] for idx in range(0, 12, 2)),
        "nap": clean[0:4],
        "uap": clean[4:6],
        "lap": clean[6:12],
        "enabled_at": time.time(),
    }


def _enable_discoverable_controller() -> tuple[dict[str, Any], str]:
    commands = "\n".join(
        [
            "power on",
            "agent on",
            "default-agent",
            "pairable on",
            "discoverable-timeout 0",
            "discoverable on",
            "show",
            "exit",
        ]
    )
    proc = subprocess.run(
        ["bluetoothctl"],
        input=commands + "\n",
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    match = re.search(r"Controller\s+([0-9A-Fa-f:]{17})", output)
    if not match:
        raise RuntimeError(output.strip() or "bluetoothctl did not report a controller address")
    target = _classic_target_from_mac(match.group(1))
    target["discoverable"] = "Discoverable: yes" in output or "Changing discoverable on succeeded" in output
    target["bluetoothctl_returncode"] = proc.returncode
    return target, output


def _stream_active(stream_id: str) -> bool:
    return state.stream_id == stream_id or stream_id in set(state.stream_ids.values())


def _worker_loop(
    stream_id: str,
    sample_rate_sps: int,
    mode: str,
    center_freq_hz: int,
    channel: int,
    stop_event: threading.Event | None = None,
) -> None:
    stop = stop_event or worker_stop
    if mode == "both" and sample_rate_sps >= 10_000_000:
        detector = CombinedBluetoothDetector(sample_rate_sps, center_freq_hz, channel)
    elif mode == "classic" and sample_rate_sps >= 10_000_000:
        detector = WideClassicDetector(sample_rate_sps, center_freq_hz, channel)
    else:
        detector = BluetoothDetector(sample_rate_sps, mode, center_freq_hz, channel)
    headers = []
    token = _gateway_token()
    if token:
        headers.append(f"Authorization: Bearer {token}")
    with state_lock:
        state.worker_alive = True
        state.worker_alive_by_mode[mode] = True
        state.worker_error = "Worker starting"
        state.worker_errors[mode] = "Worker starting"
    try:
        while not stop.is_set():
            ws = websocket.WebSocket()
            try:
                ws.connect(_ws_url_for_stream(stream_id), timeout=8, header=headers)
                ws.settimeout(1.0)
                with state_lock:
                    state.worker_error = ""
                    state.worker_errors[mode] = ""
                while not stop.is_set() and _stream_active(stream_id):
                    try:
                        chunk = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    except WebSocketConnectionClosedException:
                        with state_lock:
                            state.worker_error = "Gateway websocket closed; reconnecting"
                            state.worker_errors[mode] = "Gateway websocket closed; reconnecting"
                        break
                    except Exception as exc:
                        with state_lock:
                            state.worker_error = f"Worker recv error: {exc}; reconnecting"
                            state.worker_errors[mode] = f"Worker recv error: {exc}; reconnecting"
                        break
                    if not isinstance(chunk, (bytes, bytearray)):
                        continue
                    try:
                        rssi, events, candidates = detector.process_iq_i8(bytes(chunk))
                    except Exception as exc:
                        with state_lock:
                            state.worker_error = f"Detector error: {exc}"
                            state.worker_errors[mode] = f"Detector error: {exc}"
                        break
                    with state_lock:
                        state.chunks_seen += 1
                        state.bytes_seen += len(chunk)
                        state.chunks_by_mode[mode] = int(state.chunks_by_mode.get(mode, 0)) + 1
                        state.bytes_by_mode[mode] = int(state.bytes_by_mode.get(mode, 0)) + len(chunk)
                        state.rssi_by_mode[mode] = round(rssi, 1)
                        state.last_rssi_dbfs = round(rssi, 1)
                        state.noise_floor_dbfs = round((state.noise_floor_dbfs * 0.92) + (rssi * 0.08), 1)
                        if hasattr(detector, "stats"):
                            for key, value in detector.stats.items():
                                state.decoder_stats[key] = int(state.decoder_stats.get(key, 0)) + int(value)
                                detector.stats[key] = 0
                    _append_detections(events, candidates)
            finally:
                try:
                    ws.close()
                except Exception:
                    pass
            if not stop.is_set() and _stream_active(stream_id):
                stop.wait(0.75)
    finally:
        with state_lock:
            state.worker_alive_by_mode[mode] = False
            state.worker_alive = any(state.worker_alive_by_mode.values())
            if _stream_active(stream_id) and not stop.is_set() and not state.worker_errors.get(mode):
                state.worker_errors[mode] = "Worker exited unexpectedly"
                state.worker_error = "Worker exited unexpectedly"


def _reset_stats() -> None:
    state.chunks_seen = 0
    state.bytes_seen = 0
    state.last_rssi_dbfs = -120.0
    state.rssi_by_mode = {}
    state.chunks_by_mode = {}
    state.bytes_by_mode = {}
    state.noise_floor_dbfs = -120.0
    state.bursts_seen = 0
    state.ble_packets_seen = 0
    state.classic_bursts_seen = 0
    state.detections = []
    state.classic_candidates = []
    state.classic_addresses = []
    state.discovery_table = []
    state.channel_activity = {}
    state.decoder_stats = {}


def _reset_live_stats_keep_discoveries() -> None:
    state.chunks_seen = 0
    state.bytes_seen = 0
    state.last_rssi_dbfs = -120.0
    state.rssi_by_mode = {}
    state.chunks_by_mode = {}
    state.bytes_by_mode = {}
    state.noise_floor_dbfs = -120.0


def _pick_device(devices: list[dict[str, Any]], preferred: str, fallback: str = "") -> str:
    preferred_l = preferred.lower()
    fallback_l = fallback.lower()
    for dev in devices:
        dev_id = str(dev.get("id", ""))
        label = str(dev.get("label", ""))
        haystack = f"{dev_id} {label}".lower()
        if preferred_l and preferred_l in haystack:
            return dev_id
    for dev in devices:
        dev_id = str(dev.get("id", ""))
        label = str(dev.get("label", ""))
        haystack = f"{dev_id} {label}".lower()
        if fallback_l and fallback_l in haystack:
            return dev_id
    return str(devices[0].get("id", "")) if devices else ""


def _available_devices() -> list[dict[str, Any]]:
    try:
        resp = requests.get(f"{_gateway_base()}/devices", headers=_gateway_headers(), timeout=3)
        if resp.status_code >= 400:
            return []
        body = resp.json()
        return body if isinstance(body, list) else []
    except requests.RequestException:
        return []


def _stop_scan(stop_gateway: bool = True) -> None:
    global worker_thread, worker_threads, worker_stops
    worker_stop.set()
    if worker_thread and worker_thread.is_alive():
        worker_thread.join(timeout=2.0)
    worker_thread = None
    for stop in worker_stops.values():
        stop.set()
    for thread in worker_threads.values():
        if thread.is_alive():
            thread.join(timeout=2.0)
    worker_threads = {}
    worker_stops = {}
    stream_ids = list(state.stream_ids.values())
    if state.stream_id:
        stream_ids.append(state.stream_id)
    if stop_gateway:
        for stream_id in set(stream_ids):
            _stop_gateway_stream(stream_id)
    with state_lock:
        state.running = False
        state.stream_id = None
        state.stream_ids = {}
        state.worker_alive = False
        state.worker_alive_by_mode = {}
        state.worker_error = ""
        state.worker_errors = {}
        state.gateway_start_response = None


def _channel_freq(mode: str, channel: int) -> int:
    if mode in {"classic", "both"}:
        start_hz = BT_CLASSIC_CHANNELS.get(channel, BT_CLASSIC_CHANNELS[0])
        return int(start_hz + ((BT_CLASSIC_BANK_SIZE - 1) * BT_CLASSIC_LANE_SPACING_HZ / 2.0))
    return BLE_ADV_CHANNELS.get(channel, BLE_ADV_CHANNELS[37])


def _start_gateway_stream(
    device_id: str,
    center_freq_hz: int,
    sample_rate_sps: int,
    lna_gain_db: int,
    vga_gain_db: int,
) -> tuple[dict[str, Any], int, int, int]:
    resp = requests.post(
        f"{_gateway_base()}/streams/start",
        headers=_gateway_headers(),
        json={
            "device_id": device_id,
            "center_freq_hz": center_freq_hz,
            "sample_rate_sps": sample_rate_sps,
            "lna_gain_db": lna_gain_db,
            "vga_gain_db": vga_gain_db,
            "amp_enable": False,
            "baseband_filter_hz": sample_rate_sps,
            "duration_seconds": None,
            "num_samples": None,
        },
        timeout=12,
    )
    if resp.status_code >= 400:
        raise RuntimeError(resp.text)
    body = resp.json()
    accepted_config = body.get("config", {}) or {}
    actual_rate = int(accepted_config.get("sample_rate_sps", sample_rate_sps))
    actual_lna = int(accepted_config.get("lna_gain_db", lna_gain_db))
    actual_vga = int(accepted_config.get("vga_gain_db", vga_gain_db))
    return body, actual_rate, actual_lna, actual_vga


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/devices")
def devices():
    try:
        resp = requests.get(f"{_gateway_base()}/devices", headers=_gateway_headers(), timeout=3)
        if resp.status_code >= 400:
            return jsonify(resp.json()), resp.status_code
        return jsonify(resp.json())
    except requests.RequestException as exc:
        return jsonify({"error": "sdr-gateway is unavailable", "detail": str(exc), "gateway_base": _gateway_base()}), 503


@app.post("/api/scan/start")
def start_scan():
    global worker_thread, worker_threads, worker_stops
    payload = request.get_json(force=True) or {}
    device_id = str(payload.get("device_id", "")).strip()
    btc_device_id = str(payload.get("btc_device_id", "")).strip()
    btle_device_id = str(payload.get("btle_device_id", "")).strip()
    mode = str(payload.get("mode", "classic")).strip().lower()
    channel = int(payload.get("channel", 37 if mode != "classic" else 0))
    sample_rate_sps = int(payload.get("sample_rate_sps", 60_000_000 if mode in {"classic", "both"} else BLE_ADV_SAMPLE_RATE_SPS))
    lna_gain_db = int(payload.get("lna_gain_db", 24))
    vga_gain_db = int(payload.get("vga_gain_db", 28))
    preserve_detections = bool(payload.get("preserve_detections", False))

    if mode not in {"ble", "classic", "both"}:
        return jsonify({"error": "mode must be ble, classic, or both"}), 400
    if mode == "ble" and channel not in BLE_ADV_CHANNELS:
        return jsonify({"error": "BLE channel must be 37, 38, or 39"}), 400
    max_classic_bank_start = 78 - (BT_CLASSIC_BANK_SIZE - 1)
    if mode in {"classic", "both"} and (channel < 0 or channel > max_classic_bank_start):
        return jsonify({"error": f"Classic bank start must be 0 through {max_classic_bank_start}"}), 400
    if mode in {"classic", "both"}:
        sample_rate_sps = max(sample_rate_sps, 60_000_000)

    devices_available = _available_devices()
    if mode in {"classic", "both"} and not btc_device_id:
        btc_device_id = _pick_device(devices_available, "bladerf", device_id or "sidekiq")
    if mode in {"ble", "both"} and not btle_device_id:
        btle_device_id = _pick_device(devices_available, "hackrf", device_id or "sidekiq")
    if mode == "classic" and not btc_device_id:
        return jsonify({"error": "btc_device_id is required"}), 400
    if mode == "ble" and not btle_device_id:
        return jsonify({"error": "btle_device_id is required"}), 400
    if mode == "both" and (not btc_device_id or not btle_device_id):
        return jsonify({"error": "both btc_device_id and btle_device_id are required"}), 400

    center_freq_hz = _channel_freq(mode, channel)
    if state.running:
        _stop_scan()
    if btc_device_id:
        _stop_duplicate_gateway_streams(btc_device_id)
    if btle_device_id and btle_device_id != btc_device_id:
        _stop_duplicate_gateway_streams(btle_device_id)

    btc_test_target: dict[str, Any] | None = None
    btc_test_error = ""
    if mode in {"classic", "both"}:
        try:
            btc_test_target, _ = _enable_discoverable_controller()
        except FileNotFoundError:
            btc_test_error = "bluetoothctl is not installed or not on PATH"
        except subprocess.TimeoutExpired:
            btc_test_error = "bluetoothctl timed out while enabling discoverable mode"
        except (RuntimeError, ValueError) as exc:
            btc_test_error = str(exc)

    try:
        started: dict[str, dict[str, Any]] = {}
        if mode in {"classic", "both"}:
            body, actual_rate, actual_lna, actual_vga = _start_gateway_stream(
                btc_device_id,
                center_freq_hz,
                sample_rate_sps,
                lna_gain_db,
                vga_gain_db,
            )
            started["classic"] = {
                "body": body,
                "stream_id": body["stream_id"],
                "device_id": btc_device_id,
                "center_freq_hz": center_freq_hz,
                "sample_rate_sps": actual_rate,
                "lna_gain_db": actual_lna,
                "vga_gain_db": actual_vga,
                "channel": channel,
            }
        if mode in {"ble", "both"}:
            ble_channel = int(payload.get("ble_channel", 37))
            ble_center = BLE_ADV_CHANNELS.get(ble_channel, BLE_ADV_CHANNELS[37])
            body, actual_rate, actual_lna, actual_vga = _start_gateway_stream(
                btle_device_id,
                ble_center,
                BLE_ADV_SAMPLE_RATE_SPS,
                lna_gain_db,
                vga_gain_db,
            )
            started["ble"] = {
                "body": body,
                "stream_id": body["stream_id"],
                "device_id": btle_device_id,
                "center_freq_hz": ble_center,
                "sample_rate_sps": actual_rate,
                "lna_gain_db": actual_lna,
                "vga_gain_db": actual_vga,
                "channel": ble_channel,
            }
    except requests.RequestException as exc:
        return jsonify({"error": "sdr-gateway is unavailable", "detail": str(exc), "gateway_base": _gateway_base()}), 503
    except RuntimeError as exc:
        return jsonify({"error": "sdr-gateway rejected stream", "detail": str(exc)}), 400

    worker_stop.clear()
    with state_lock:
        if preserve_detections:
            _reset_live_stats_keep_discoveries()
        else:
            _reset_stats()
        state.running = True
        state.mode = mode
        primary = started.get("classic") or started.get("ble")
        state.stream_id = primary["stream_id"] if primary else None
        state.stream_ids = {key: value["stream_id"] for key, value in started.items()}
        state.device_id = primary["device_id"] if primary else None
        state.device_ids = {key: value["device_id"] for key, value in started.items()}
        state.center_freq_hz = int(primary["center_freq_hz"]) if primary else center_freq_hz
        state.sample_rate_sps = int(primary["sample_rate_sps"]) if primary else sample_rate_sps
        state.lna_gain_db = int(primary["lna_gain_db"]) if primary else lna_gain_db
        state.vga_gain_db = int(primary["vga_gain_db"]) if primary else vga_gain_db
        state.channel = channel
        state.channels_by_mode = {key: int(value["channel"]) for key, value in started.items()}
        state.gateway_start_response = {key: value["body"] for key, value in started.items()}
        state.worker_error = ""
        if mode in {"classic", "both"}:
            state.test_target = btc_test_target
            state.test_target_error = btc_test_error
        else:
            state.test_target = None
            state.test_target_error = ""

    worker_threads = {}
    worker_stops = {}
    for protocol, cfg in started.items():
        stop = threading.Event()
        worker_stops[protocol] = stop
        worker_mode = "classic" if protocol == "classic" else "ble"
        thread = threading.Thread(
            target=_worker_loop,
            args=(
                cfg["stream_id"],
                cfg["sample_rate_sps"],
                worker_mode,
                cfg["center_freq_hz"],
                cfg["channel"],
                stop,
            ),
            daemon=True,
        )
        worker_threads[protocol] = thread
        thread.start()
    worker_thread = next(iter(worker_threads.values()), None)
    return jsonify(
        {
            "ok": True,
            "mode": mode,
            "streams": {
                key: {
                    "stream_id": value["stream_id"],
                    "device_id": value["device_id"],
                    "center_freq_hz": value["center_freq_hz"],
                    "sample_rate_sps": value["sample_rate_sps"],
                }
                for key, value in started.items()
            },
            "test_target": btc_test_target,
            "test_target_error": btc_test_error,
        }
    )


@app.post("/api/scan/stop")
def stop_scan():
    _stop_scan()
    return jsonify({"ok": True})


@app.get("/api/status")
def status():
    with state_lock:
        return jsonify(
            {
                "running": state.running,
                "mode": state.mode,
                "stream_id": state.stream_id,
                "stream_ids": state.stream_ids,
                "device_id": state.device_id,
                "device_ids": state.device_ids,
                "center_freq_hz": state.center_freq_hz,
                "sample_rate_sps": state.sample_rate_sps,
                "lna_gain_db": state.lna_gain_db,
                "vga_gain_db": state.vga_gain_db,
                "channel": state.channel,
                "channels_by_mode": state.channels_by_mode,
                "worker_alive": state.worker_alive,
                "worker_alive_by_mode": state.worker_alive_by_mode,
                "worker_error": state.worker_error,
                "worker_errors": state.worker_errors,
                "chunks_seen": state.chunks_seen,
                "bytes_seen": state.bytes_seen,
                "last_rssi_dbfs": state.last_rssi_dbfs,
                "rssi_by_mode": state.rssi_by_mode,
                "chunks_by_mode": state.chunks_by_mode,
                "bytes_by_mode": state.bytes_by_mode,
                "noise_floor_dbfs": state.noise_floor_dbfs,
                "bursts_seen": state.bursts_seen,
                "ble_packets_seen": state.ble_packets_seen,
                "classic_bursts_seen": state.classic_bursts_seen,
                "detections": state.detections[:120],
                "discovery_table": state.discovery_table[:120],
                "classic_candidates": state.classic_candidates[:32],
                "classic_addresses": state.classic_addresses[:64],
                "decoder_stats": state.decoder_stats,
                "test_target": state.test_target,
                "test_target_error": state.test_target_error,
                "channel_activity": [
                    state.channel_activity.get(idx, {"channel": idx, "hits": 0, "rssi_dbfs": -120.0})
                    for idx in range(79)
                ],
                "gateway_start_response": state.gateway_start_response,
            }
        )


@app.post("/api/test/discoverable-dongle")
def enable_discoverable_dongle():
    try:
        target, output = _enable_discoverable_controller()
    except FileNotFoundError:
        return jsonify({"error": "bluetoothctl is not installed or not on PATH"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "bluetoothctl timed out while enabling discoverable mode"}), 504
    except (RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 500

    with state_lock:
        state.test_target = target
    return jsonify(
        {
            "ok": True,
            "target": target,
            "message": f"Discoverable BTC test target armed: LAP {target['lap']} / UAP {target['uap']}",
            "bluetoothctl_output": output,
        }
    )


@app.post("/api/clear")
def clear():
    with state_lock:
        _reset_stats()
    return jsonify({"ok": True})


if __name__ == "__main__":
    host = os.getenv("BT_EXPLORER_HOST", "0.0.0.0")
    port = int(os.getenv("BT_EXPLORER_PORT", "5050"))
    app.run(host=host, port=port, threaded=True)
