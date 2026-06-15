import os
import calendar
import contextlib
import csv
import json
import logging
import platform
import queue
import re
import shutil
import signal
import shlex
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import requests
import websocket
from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.exceptions import BadRequest
from websocket._exceptions import WebSocketConnectionClosedException


BLE_ADV_CHANNELS = {
    37: 2_402_000_000,
    38: 2_426_000_000,
    39: 2_480_000_000,
}

BT_CLASSIC_CHANNELS = {idx: 2_402_000_000 + (idx * 1_000_000) for idx in range(79)}
BT_CLASSIC_BANK_SIZE = 60
# Match the reference path more closely: 1 MHz-spaced Classic lanes decoded at 1 Msps after channelization.
BT_CLASSIC_CHANNEL_BW_HZ = 1_000_000
BT_CLASSIC_LANE_RATE_SPS = 1_000_000
BT_CLASSIC_LANE_SPACING_HZ = 1_000_000
BLE_ADV_CHANNEL_BW_HZ = 2_000_000
BLE_ADV_SAMPLE_RATE_SPS = 2_000_000
BLE_ADV_ACCESS_BYTES = bytes.fromhex("d6be898e")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        try:
            parsed = shlex.split(value, comments=False, posix=True)
            value = parsed[0] if parsed else ""
        except ValueError:
            value = value.strip().strip("\"'")
        os.environ.setdefault(key, value)


_load_env_file(PROJECT_ROOT / "config" / "env.txt")

DATA_DIR = Path(__file__).resolve().parent / "data"
RF_SENTINEL_LOG_DIR = Path(os.getenv("RF_SENTINEL_LOG_DIR", "/var/log/rf_sentinel"))
RF_SENTINEL_CONTROL_PATH = RF_SENTINEL_LOG_DIR / "rf_sentinel_control.json"
RF_SENTINEL_UI_CONFIG_PATH = RF_SENTINEL_LOG_DIR / "rf_sentinel_ui_config.json"
RF_SENTINEL_RUNS_DIR = RF_SENTINEL_LOG_DIR / "runs"
RF_SENTINEL_ARCHIVE_DIR = RF_SENTINEL_LOG_DIR / "archives"
RF_SENTINEL_CSV_RETENTION_DAYS = max(1, int(os.getenv("RF_SENTINEL_CSV_RETENTION_DAYS", "7")))
RF_SENTINEL_CSV_ARCHIVE_MAX_MB = max(1, int(os.getenv("RF_SENTINEL_CSV_ARCHIVE_MAX_MB", "1000")))
RF_SENTINEL_NO_CHANGE = object()
RF_SENTINEL_PROTOCOLS = {"btc", "ble", "zigbee", "tpms", "wifi", "fm", "lfmf"}
RF_SENTINEL_KEEP_BAD_FCS = os.getenv("RF_SENTINEL_KEEP_BAD_FCS", "0").strip().lower() in {"1", "true", "yes", "on"}
BLE_IDENTITY_CACHE_PATH = DATA_DIR / "ble_identities.json"
COMPANY_IDENTIFIERS_PATH = DATA_DIR / "company_identifiers.json"
UUID16_IDENTIFIERS_PATH = DATA_DIR / "uuid16_identifiers.json"
BTC_SNIFFER_ROOT = Path(os.getenv("BTC_SNIFFER_ROOT", str(PROJECT_ROOT / "rf_platform" / "plugins" / "bluetooth-classic")))
BTC_SNIFFER_BINARY = Path(os.getenv("BTC_SNIFFER_BINARY", str(BTC_SNIFFER_ROOT / "build" / "btcexplorer-sniffer")))
BTC_SNIFFER_LOG_PATH = Path(os.getenv("BTC_SNIFFER_LOG", str(RF_SENTINEL_LOG_DIR / "btcexplorer-sniffer.log")))
BTC_SNIFFER_AUTO_BUILD = os.getenv("BTC_SNIFFER_AUTO_BUILD", "1").strip().lower() not in {"0", "false", "no"}
BTC_ENGINE_DEFAULT = os.getenv("BTC_ENGINE", "btcsniffer").strip().lower()
SDR_GATEWAY_DEVICES_TIMEOUT_SECONDS = float(os.getenv("SDR_GATEWAY_DEVICES_TIMEOUT_SECONDS", "10"))
INVALID_CLK_INDEX = -1
DELTA_TS_SAME_THRESHOLD_US = 40
DELTA_TS_SLOT_THRESHOLD_US = 620
SLOT_DURATION_US = 625.0
SLOT_ERROR_THRESHOLD = 0.05
BT_CLASSIC_ACCESS_REPAIR_MAX_DISTANCE = 0
BT_CLASSIC_HEADER_MIN_PERFECT_TRIPLETS = 18
BT_CLASSIC_USE_CPP_FFT = os.getenv("BT_CLASSIC_USE_CPP_FFT", "1").strip().lower() not in {"0", "false", "no"}
btcsniffer_build_lock = threading.Lock()


def _design_lowpass_taps(sample_rate_hz: int, cutoff_hz: float, num_taps: int) -> np.ndarray:
    taps = max(15, int(num_taps) | 1)
    nyquist = max(1.0, float(sample_rate_hz) / 2.0)
    normalized_cutoff = min(0.98, max(0.001, float(cutoff_hz) / nyquist))
    n = np.arange(taps, dtype=np.float64) - ((taps - 1) / 2.0)
    kernel = normalized_cutoff * np.sinc(normalized_cutoff * n)
    kernel *= np.hamming(taps)
    kernel_sum = float(np.sum(kernel))
    if abs(kernel_sum) < 1e-12:
        return np.array([1.0], dtype=np.float32)
    kernel /= kernel_sum
    return kernel.astype(np.float32)


class FmAudioDemod:
    def __init__(self, in_rate: int, out_rate: int = 48_000) -> None:
        self.in_rate = int(in_rate)
        self.out_rate = int(out_rate)
        self.decim = max(1, int(round(self.in_rate / 240_000.0)))
        self.demod_rate = self.in_rate / float(self.decim)
        self.prev = np.complex64(1.0 + 0j)
        self.channel_filter = self._design_lowpass(257, 125_000.0, float(self.in_rate))
        self._channel_tail = np.zeros(max(0, self.channel_filter.size - 1), dtype=np.complex64)
        self.mono_filter = self._design_lowpass(129, 15_000.0, float(self.demod_rate))
        self._mono_tail = np.zeros(max(0, self.mono_filter.size - 1), dtype=np.float32)
        self._audio_scale = 1.0
        self.resample_pos = 0.0
        self._leftover = b""

    def process_iq_i8(self, raw: bytes) -> bytes:
        if not raw:
            return b""
        if self._leftover:
            raw = self._leftover + raw
            self._leftover = b""
        if len(raw) % 2 != 0:
            self._leftover = raw[-1:]
            raw = raw[:-1]
        if len(raw) < 4:
            return b""
        iq = np.frombuffer(raw, dtype=np.int8).astype(np.float32)
        z = (iq[0::2] / 128.0 + 1j * (iq[1::2] / 128.0)).astype(np.complex64)
        if z.size < 8:
            return b""
        z = self._channel_filter_and_decimate(z)
        if z.size < 8:
            return b""
        prev = np.empty_like(z)
        prev[0] = self.prev
        prev[1:] = z[:-1]
        self.prev = z[-1]
        demod = np.angle(z * np.conj(prev)).astype(np.float32)
        if demod.size < 8:
            return b""
        demod -= float(np.mean(demod))
        # Match AetherCast's forgiving mono fallback: it is much harder to upset
        # on marginal RF than the stricter audio low-pass path.
        kernel = np.array([0.2, 0.2, 0.2, 0.2, 0.2], dtype=np.float32)
        mono = np.convolve(demod, kernel, mode="same").astype(np.float32)
        if mono.size < 4:
            return b""
        step = self.demod_rate / float(self.out_rate)
        positions = np.arange(self.resample_pos, mono.size - 1, step, dtype=np.float64)
        if positions.size == 0:
            self.resample_pos = float(self.resample_pos + mono.size)
            return b""
        next_pos = float(positions[-1] + step - (mono.size - 1))
        idx = np.floor(positions).astype(np.int32)
        valid = idx + 1 < mono.size
        idx = idx[valid]
        positions = positions[valid]
        if positions.size == 0:
            self.resample_pos = max(0.0, next_pos)
            return b""
        frac = positions - idx
        audio = mono[idx] * (1.0 - frac) + mono[idx + 1] * frac
        self.resample_pos = max(0.0, next_pos)
        peak = float(np.max(np.abs(audio))) if audio.size else 1.0
        target_scale = 0.85 / max(peak, 0.2)
        self._audio_scale = (self._audio_scale * 0.9) + (target_scale * 0.1)
        audio = np.clip(audio * self._audio_scale, -1.0, 1.0)
        mono_i16 = (audio * 32767.0).astype(np.int16)
        return mono_i16.tobytes()

    @staticmethod
    def _design_lowpass(num_taps: int, cutoff_hz: float, sample_rate_hz: float) -> np.ndarray:
        cutoff = min(float(cutoff_hz), (float(sample_rate_hz) / 2.0) * 0.92)
        n = np.arange(int(num_taps), dtype=np.float32) - ((int(num_taps) - 1) / 2.0)
        taps = 2.0 * cutoff / float(sample_rate_hz) * np.sinc(2.0 * cutoff / float(sample_rate_hz) * n)
        taps *= np.hamming(int(num_taps)).astype(np.float32)
        taps /= max(1e-12, float(np.sum(taps)))
        return taps.astype(np.float32)

    def _filter_float(self, x: np.ndarray, taps: np.ndarray, tail_name: str) -> np.ndarray:
        tail = getattr(self, tail_name)
        x = x.astype(np.float32, copy=False)
        x_ext = np.concatenate((tail, x))
        filtered = np.convolve(x_ext, taps, mode="valid").astype(np.float32)
        setattr(self, tail_name, x_ext[-tail.size :].astype(np.float32) if tail.size else tail)
        return filtered

    def _channel_filter_and_decimate(self, z: np.ndarray) -> np.ndarray:
        z_ext = np.concatenate((self._channel_tail, z))
        filtered = np.convolve(z_ext, self.channel_filter, mode="valid").astype(np.complex64)
        if self._channel_tail.size:
            self._channel_tail = z_ext[-self._channel_tail.size :].astype(np.complex64)
        decim = int(self.decim)
        if decim <= 1:
            return filtered
        usable = (filtered.size // decim) * decim
        if usable <= 0:
            return np.empty(0, dtype=np.complex64)
        return filtered[:usable].reshape(-1, decim).mean(axis=1).astype(np.complex64)


def _gateway_base() -> str:
    return os.getenv("SDR_GATEWAY_BASE_URL", "http://127.0.0.1:8080").rstrip("/")


def _gateway_token() -> str:
    token = (os.getenv("SDR_GATEWAY_API_TOKEN", "") or "").strip()
    if token:
        return token
    return ""


def _gateway_headers() -> dict[str, str]:
    token = _gateway_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _configured_btc_target(mac_override: str | None = None) -> dict[str, Any] | None:
    mac = str(mac_override or "").strip()
    if not mac:
        mac = (os.getenv("BTC_TARGET_MAC", "") or "").strip()
    if not mac:
        return None
    try:
        target = _classic_target_from_mac(mac)
    except ValueError:
        return None
    target["inquiry_status"] = "manual traffic generation"
    target["source"] = "scan form BTC target MAC" if mac_override else "env BTC_TARGET_MAC"
    return target


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
    btc_engine: str = ""
    btc_engine_command: list[str] = field(default_factory=list)
    btc_engine_log: str = ""
    scanner_log: list[str] = field(default_factory=list)
    scanner_assignments: dict[str, dict[str, Any]] = field(default_factory=dict)
    csv_run_id: str = ""
    csv_log_dir: str = ""


@dataclass
class FmPlaybackState:
    running: bool = False
    pending: bool = False
    pending_freq_mhz: float = 0.0
    pending_device_id: str = ""
    device_id: str = ""
    freq_mhz: float = 0.0
    sample_rate_sps: int = 2_000_000
    lna_gain_db: int = 32
    vga_gain_db: int = 32
    stream_id: str = ""
    worker_alive: bool = False
    worker_error: str = ""
    last_audio_rms: float = 0.0
    produced_chunks: int = 0
    served_chunks: int = 0
    empty_audio_polls: int = 0
    scanner_protocol_paused: bool = False


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
            "access_code_mismatch": 0,
            "access_code_hits": 0,
            "access_code_repair_hits": 0,
            "target_access_near_hits": 0,
            "target_access_best_distance": 68,
            "lap_hits": 0,
            "header_failures": 0,
            "header_relaxed_hits": 0,
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
            appearance = self._ble_appearance_from_fields(ad_fields)
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
                    "appearance": appearance,
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

    @staticmethod
    def _ble_appearance_from_fields(fields: list[tuple[int, bytes]]) -> dict[str, Any] | None:
        for ad_type, value in fields:
            if ad_type != 0x19 or len(value) < 2:
                continue
            code = int.from_bytes(value[:2], "little")
            return {
                "code": f"0x{code:04X}",
                "label": BLE_APPEARANCE_LABELS.get(code, f"Appearance {code:#06x}"),
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

    def process_classic_cpp_bits(self, bits: list[int], rssi_dbfs: float) -> tuple[float, list[dict[str, Any]], list[dict[str, Any]]]:
        events: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        tail = self._classic_bit_tails.get(0, [])
        base_bit_index = self._classic_bits_processed - len(tail)
        search_bits = tail + bits
        min_packet_bits = 72 + 54
        pos = 0
        stop = max(0, len(search_bits) - min_packet_bits)
        while pos < stop:
            if not self._classic_preamble_ok(search_bits, pos):
                pos += 1
                continue
            self.stats["preamble_hits"] += 1
            if self._classic_barker(search_bits, pos) is not None:
                self.stats["barker_hits"] += 1
            access = self._classic_access_code(search_bits, pos)
            if access is None:
                self._classic_target_access_diagnostic(search_bits, pos)
                pos += 1
                continue
            self.stats["access_code_hits"] += 1
            header_result = self._classic_bruteforce_all_uaps(search_bits[pos + 72 : pos + 72 + 54])
            if header_result["valid_uaps"] == 0:
                self.stats["header_failures"] += 1
                pos += 1
                continue
            self.stats["lap_hits"] += 1
            self.stats["uap_candidate_hits"] += int(header_result["valid_uaps"])
            observation = {
                "lap": access["lap"],
                "access_word": access["access_word"],
                "observed_access_word": access.get("observed_access_word", access["access_word"]),
                "repair_distance": int(access.get("repair_distance", 0)),
                "repaired": bool(access.get("repaired", False)),
                "header": header_result["header"],
                "header_perfect_triplets": int(header_result.get("perfect_triplets", 0)),
                "header_relaxed": bool(header_result.get("relaxed", False)),
                "uap_results": header_result["uap_results"],
                "valid_uaps": header_result["valid_uaps"],
                "ts_us": int(base_bit_index + pos),
                "rssi_dbfs": round(rssi_dbfs, 1),
            }
            event, lap_candidates = self._update_lap_state(observation)
            event["phase"] = 0
            event["demod"] = "cpp-cross"
            events.append(event)
            candidates.extend(lap_candidates)
            pos += 100
            if len(events) >= 8:
                break
        self._classic_bit_tails[0] = search_bits[-192:]
        self._classic_bits_processed += len(bits)
        return rssi_dbfs, events, candidates

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
                self._classic_target_access_diagnostic(bits, pos)
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
                    "observed_access_word": access.get("observed_access_word", access["access_word"]),
                    "repair_distance": int(access.get("repair_distance", 0)),
                    "repaired": bool(access.get("repaired", False)),
                    "header": header_result["header"],
                    "header_perfect_triplets": int(header_result.get("perfect_triplets", 0)),
                    "header_relaxed": bool(header_result.get("relaxed", False)),
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
        barker = self._extract_lsb_byte(bits, pos + 62) & 0x3F
        return barker if barker in {0x13, 0x2C} else None

    def _classic_access_code(self, bits: list[int], pos: int) -> dict[str, int] | None:
        barker = self._classic_barker(bits, pos)
        if barker is None:
            return None
        lap = (
            (self._extract_lsb_byte(bits, pos + 54) << 16)
            | (self._extract_lsb_byte(bits, pos + 46) << 8)
            | self._extract_lsb_byte(bits, pos + 38)
        )
        code = (
            (self._extract_lsb_byte(bits, pos + 4) << 0)
            | (self._extract_lsb_byte(bits, pos + 12) << 8)
            | (self._extract_lsb_byte(bits, pos + 20) << 16)
            | (self._extract_lsb_byte(bits, pos + 28) << 24)
            | (self._extract_lsb_byte(bits, pos + 36) << 32)
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
            self.stats["access_code_mismatch"] += 1
            distance = (access_word ^ expected).bit_count()
            if distance > BT_CLASSIC_ACCESS_REPAIR_MAX_DISTANCE:
                return None
            self.stats["access_code_repair_hits"] += 1
            return {
                "lap": lap,
                "access_word": expected,
                "observed_access_word": access_word,
                "repair_distance": distance,
                "repaired": True,
            }
        return {"lap": lap, "access_word": access_word, "repair_distance": 0, "repaired": False}

    def _classic_target_access_diagnostic(self, bits: list[int], pos: int) -> None:
        with state_lock:
            target = dict(state.test_target or {})
        if target.get("protocol") != "BTC":
            return
        try:
            lap = int(str(target.get("lap") or ""), 16)
        except ValueError:
            return
        expected = self._classic_expected_access_word(lap)
        observed = self._classic_observed_access_word(bits, pos)
        if observed is None:
            return
        distance = (observed ^ expected).bit_count()
        self.stats["target_access_best_distance"] = min(int(self.stats.get("target_access_best_distance", 68)), int(distance))
        if distance <= 8:
            self.stats["target_access_near_hits"] += 1

    def _classic_observed_access_word(self, bits: list[int], pos: int) -> int | None:
        if pos + 72 > len(bits):
            return None
        barker = self._classic_barker(bits, pos)
        if barker is None:
            return None
        lap = (
            (self._extract_lsb_byte(bits, pos + 54) << 16)
            | (self._extract_lsb_byte(bits, pos + 46) << 8)
            | self._extract_lsb_byte(bits, pos + 38)
        )
        code = (
            (self._extract_lsb_byte(bits, pos + 4) << 0)
            | (self._extract_lsb_byte(bits, pos + 12) << 8)
            | (self._extract_lsb_byte(bits, pos + 20) << 16)
            | (self._extract_lsb_byte(bits, pos + 28) << 24)
            | (self._extract_lsb_byte(bits, pos + 36) << 32)
        ) & 0x3FFFFFFFF
        return (barker << 58) | (lap << 34) | code

    @classmethod
    def _classic_expected_access_word(cls, lap: int) -> int:
        barker_true = 0x13 if (lap & 0x800000) else 0x2C
        x = (barker_true << 24) | lap
        p = 0x83848D96BBCC54FC
        xtilde = (p >> 34) ^ x
        gp = int("157464165547", 8)
        g = (gp << 1) ^ gp
        ctilde = cls._compute_remainder(xtilde, g)
        return (ctilde | (xtilde << 34)) ^ p

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
        if perfect_rx < BT_CLASSIC_HEADER_MIN_PERFECT_TRIPLETS:
            return {"header": header, "valid_uaps": 0, "uap_results": [], "perfect_triplets": perfect_rx, "relaxed": False}
        relaxed = perfect_rx != 18
        if relaxed:
            self.stats["header_relaxed_hits"] += 1

        results: list[dict[str, Any]] = []
        for uap in range(256):
            clks = self._classic_header_clks_for_uap(header, uap)
            if clks:
                results.append({"uap": uap, "clks": clks})
        return {
            "header": header,
            "valid_uaps": len(results),
            "uap_results": results,
            "perfect_triplets": perfect_rx,
            "relaxed": relaxed,
        }

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
            "observed_access_word": f"{int(observation.get('observed_access_word', observation['access_word'])):018X}",
            "repaired": bool(observation.get("repaired", False)),
            "repair_distance": int(observation.get("repair_distance", 0)),
            "header_perfect_triplets": int(observation.get("header_perfect_triplets", 0)),
            "header_relaxed": bool(observation.get("header_relaxed", False)),
            "ts_us": int(observation.get("ts_us", 0)),
            "candidate_count": len(candidates),
            "processed_packets": node.processed_packets,
            "broken_packets": node.broken_packets,
            "cannot_init": node.cannot_init,
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
            "broken_packets": node.broken_packets,
            "repaired": bool(observation.get("repaired", False)),
            "repair_distance": int(observation.get("repair_distance", 0)),
            "header_perfect_triplets": int(observation.get("header_perfect_triplets", 0)),
            "header_relaxed": bool(observation.get("header_relaxed", False)),
            "ts_us": int(observation.get("ts_us", 0)),
            "clks": candidate["clks"],
            "notes": [
                "LAP extracted from Classic access code.",
                "UAP candidate validated by dewhitening header and matching HEC.",
            ],
        }

    @staticmethod
    def _extract_lsb_byte(bits: list[int], start: int) -> int:
        value = 0
        for idx in range(8):
            value |= (bits[start + idx] & 1) << idx
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
        self.use_cpp_fft = bool(BT_CLASSIC_USE_CPP_FFT and self.decim == BT_CLASSIC_BANK_SIZE)
        self._fft_tail = np.empty(0, dtype=np.complex64)
        self.sample_phase_offsets = self._sample_phase_offsets(self.decim)
        self.freq_offset_adjustments_hz = self._freq_offset_adjustments_hz()
        filter_taps = max(31, min(193, (self.decim * 4) | 1))
        cutoff_hz = min(float(self.lane_rate_sps) * 0.40, 800_000.0)
        self._lane_filter_taps = _design_lowpass_taps(self.sample_rate_sps, cutoff_hz, filter_taps)
        self.lanes: list[dict[str, Any]] = []
        self.stats = {
            "preamble_hits": 0,
            "barker_hits": 0,
            "access_code_mismatch": 0,
            "access_code_hits": 0,
            "access_code_repair_hits": 0,
            "target_access_near_hits": 0,
            "target_access_best_distance": 68,
            "lap_hits": 0,
            "header_failures": 0,
            "header_relaxed_hits": 0,
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
                    "cpp_detector": BluetoothDetector(self.lane_rate_sps, "classic", freq_hz, channel),
                    "mix_paths": [
                        {
                            "freq_adjust_hz": int(freq_adjust_hz),
                            "mix_phase_rad": 0.0,
                            "filter_state": np.empty(0, dtype=np.complex64),
                            "phase_paths": [
                                {
                                    "sample_offset": sample_offset,
                                    "detector": BluetoothDetector(self.lane_rate_sps, "classic", freq_hz, channel),
                                }
                                for sample_offset in self.sample_phase_offsets
                            ],
                        }
                        for freq_adjust_hz in self.freq_offset_adjustments_hz
                    ],
                }
            )

    def process_iq_i8(self, raw: bytes) -> tuple[float, list[dict[str, Any]], list[dict[str, Any]]]:
        z = BluetoothDetector._iq_bytes_to_complex(self, raw)
        if z.size < 64:
            return -120.0, [], []
        if self.use_cpp_fft:
            return self._process_iq_cpp_fft(z)

        all_events: list[dict[str, Any]] = []
        all_candidates: list[dict[str, Any]] = []
        rssis: list[float] = []
        sample_idx = np.arange(z.size, dtype=np.float32)
        for lane in self.lanes:
            base_offset_hz = float(lane["offset_hz"])
            for mix_path in lane.get("mix_paths", []):
                offset_hz = base_offset_hz + float(mix_path.get("freq_adjust_hz", 0))
                phase_step = float((-2.0 * np.pi * offset_hz) / float(self.sample_rate_sps))
                phase0 = float(mix_path.get("mix_phase_rad", 0.0))
                rot = np.exp(1j * (phase0 + (phase_step * sample_idx))).astype(np.complex64)
                mix_path["mix_phase_rad"] = float((phase0 + (phase_step * float(z.size))) % (2.0 * np.pi))
                mixed = z * rot
                lane_samples_by_offset = self._decimate_lane(mix_path, mixed)
                if not lane_samples_by_offset:
                    continue
                for phase_path in mix_path.get("phase_paths", []):
                    sample_offset = int(phase_path.get("sample_offset", 0))
                    lane_samples = lane_samples_by_offset.get(sample_offset)
                    if lane_samples is None or lane_samples.size < 64:
                        continue
                    detector = phase_path["detector"]
                    rssi, events, candidates = detector.process_complex(lane_samples)
                    for key, value in detector.stats.items():
                        if key == "target_access_best_distance":
                            current = int(self.stats.get(key, 68))
                            self.stats[key] = min(current, int(value))
                            detector.stats[key] = 68
                            continue
                        self.stats[key] = int(self.stats.get(key, 0)) + int(value)
                        detector.stats[key] = 0
                    rssis.append(rssi)
                    all_events.extend(events)
                    all_candidates.extend(candidates)

        all_events = self._dedupe_classic_events(all_events)
        all_candidates = self._dedupe_classic_candidates(all_candidates)
        bank_rssi = max(rssis) if rssis else -120.0
        return bank_rssi, all_events[:80], all_candidates[:80]

    def _process_iq_cpp_fft(self, z: np.ndarray) -> tuple[float, list[dict[str, Any]], list[dict[str, Any]]]:
        if self._fft_tail.size:
            z = np.concatenate((self._fft_tail, z))
        usable = (z.size // self.decim) * self.decim
        self._fft_tail = z[usable:].astype(np.complex64, copy=False)
        if usable <= 0:
            return -120.0, [], []

        frames = z[:usable].reshape(-1, self.decim)
        bins = np.fft.fft(frames, axis=1).astype(np.complex64, copy=False)
        all_events: list[dict[str, Any]] = []
        all_candidates: list[dict[str, Any]] = []
        rssis: list[float] = []

        for idx, lane in enumerate(self.lanes):
            fft_bin = (idx + (self.decim // 2)) % self.decim
            lane_samples = bins[:, fft_bin]
            if lane_samples.size < 2:
                continue
            prev = lane_samples[:-1]
            cur = lane_samples[1:]
            cross = (prev.real * cur.imag) - (prev.imag * cur.real)
            bits = [1 if value > 0 else 0 for value in cross.tolist()]
            rssi = float(10.0 * np.log10(float(np.mean(np.abs(lane_samples) ** 2)) + 1e-12))
            detector = lane["cpp_detector"]
            _, events, candidates = detector.process_classic_cpp_bits(bits, rssi)
            for event in events:
                event["btcsniffer_bin"] = idx
                event["demod"] = "cpp-fft"
            for candidate in candidates:
                candidate["btcsniffer_bin"] = idx
                candidate["demod"] = "cpp-fft"
            for key, value in detector.stats.items():
                if key == "target_access_best_distance":
                    current = int(self.stats.get(key, 68))
                    self.stats[key] = min(current, int(value))
                    detector.stats[key] = 68
                    continue
                self.stats[key] = int(self.stats.get(key, 0)) + int(value)
                detector.stats[key] = 0
            rssis.append(rssi)
            all_events.extend(events)
            all_candidates.extend(candidates)

        all_events = self._dedupe_classic_events(all_events)
        all_candidates = self._dedupe_classic_candidates(all_candidates)
        bank_rssi = max(rssis) if rssis else -120.0
        return bank_rssi, all_events[:80], all_candidates[:80]

    @staticmethod
    def _event_rank(event: dict[str, Any]) -> tuple[float, ...]:
        return (
            1.0 if not bool(event.get("repaired", False)) else 0.0,
            -float(event.get("candidate_count") or 99),
            float(event.get("processed_packets") or 0),
            float(event.get("header_perfect_triplets") or 0),
            float(event.get("rssi_dbfs") or -120.0),
        )

    @classmethod
    def _dedupe_classic_events(cls, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
        passthrough: list[dict[str, Any]] = []
        for event in events:
            if event.get("kind") != "classic_lap":
                passthrough.append(event)
                continue
            ts_bucket = int(round(float(event.get("ts_us") or 0) / 80.0))
            key = (
                str(event.get("lap") or ""),
                int(event.get("channel") or -1),
                ts_bucket,
            )
            existing = deduped.get(key)
            if existing is None or cls._event_rank(event) > cls._event_rank(existing):
                deduped[key] = event
        lap_events = list(deduped.values())
        lap_events.sort(key=lambda item: float(item.get("seen_at") or 0), reverse=True)
        passthrough.sort(key=lambda item: float(item.get("seen_at") or 0), reverse=True)
        return lap_events + passthrough

    @classmethod
    def _dedupe_classic_candidates(cls, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
        for candidate in candidates:
            ts_bucket = int(round(float(candidate.get("ts_us") or 0) / 80.0))
            key = (
                str(candidate.get("lap") or ""),
                str(candidate.get("uap_hex") or candidate.get("uap") or ""),
                int(candidate.get("channel") or -1),
                ts_bucket,
            )
            existing = deduped.get(key)
            if existing is None or cls._event_rank(candidate) > cls._event_rank(existing):
                deduped[key] = candidate
        rows = list(deduped.values())
        rows.sort(
            key=lambda item: (
                float(item.get("candidate_count") or 99),
                -float(item.get("processed_packets") or 0),
                -float(item.get("rssi_dbfs") or -120.0),
            )
        )
        return rows

    @staticmethod
    def _sample_phase_offsets(decim: int) -> list[int]:
        if decim <= 1:
            return [0]
        count = min(4, decim)
        if count == decim:
            return list(range(decim))
        offsets = sorted({min(decim - 1, int(round((idx * decim) / count))) for idx in range(count)})
        if 0 not in offsets:
            offsets.insert(0, 0)
        return offsets

    @staticmethod
    def _freq_offset_adjustments_hz() -> list[int]:
        # Small CFO sweep around each nominal 1 MHz channel center.
        return [-40_000, 0, 40_000]

    def _decimate_lane(self, path_state: dict[str, Any], z: np.ndarray) -> dict[int, np.ndarray]:
        history = path_state.get("filter_state")
        if not isinstance(history, np.ndarray):
            history = np.empty(0, dtype=np.complex64)
        if history.size:
            z = np.concatenate((history, z))
        taps = self._lane_filter_taps
        if z.size < taps.size:
            path_state["filter_state"] = z[-(taps.size - 1) :].astype(np.complex64, copy=False)
            return {}
        filtered_i = np.convolve(z.real.astype(np.float32, copy=False), taps, mode="valid")
        filtered_q = np.convolve(z.imag.astype(np.float32, copy=False), taps, mode="valid")
        path_state["filter_state"] = z[-(taps.size - 1) :].astype(np.complex64, copy=False)
        filtered = (filtered_i + 1j * filtered_q).astype(np.complex64)
        if self.decim <= 1:
            return {0: filtered}
        outputs: dict[int, np.ndarray] = {}
        for sample_offset in self.sample_phase_offsets:
            if sample_offset >= filtered.size:
                continue
            available = filtered.size - sample_offset
            usable = (available // self.decim) * self.decim
            if usable <= 0:
                continue
            outputs[sample_offset] = filtered[sample_offset : sample_offset + usable : self.decim].astype(np.complex64, copy=False)
        return outputs


class CombinedBluetoothDetector:
    def __init__(self, sample_rate_sps: int, center_freq_hz: int, bank_start_channel: int) -> None:
        self.sample_rate_sps = int(sample_rate_sps)
        self.center_freq_hz = int(center_freq_hz)
        self.classic = WideClassicDetector(sample_rate_sps, center_freq_hz, bank_start_channel)
        self.ble_lanes: list[dict[str, Any]] = []
        self.stats = self.classic.stats
        self.ble_decim = max(1, int(round(self.sample_rate_sps / BLE_ADV_SAMPLE_RATE_SPS)))
        for channel, freq_hz in BLE_ADV_CHANNELS.items():
            offset_hz = float(freq_hz - self.center_freq_hz)
            if abs(offset_hz) > (self.sample_rate_sps / 2.0) - 1_200_000:
                continue
            self.ble_lanes.append(
                {
                    "channel": channel,
                    "freq_hz": freq_hz,
                    "offset_hz": offset_hz,
                    "detector": BluetoothDetector(BLE_ADV_SAMPLE_RATE_SPS, "ble", freq_hz, channel),
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
        for lane in self.ble_lanes:
            rot = np.exp((-2j * np.pi * float(lane["offset_hz"]) / float(self.sample_rate_sps)) * sample_idx).astype(np.complex64)
            mixed = z * rot
            lane_samples = self._decimate(mixed, self.ble_decim)
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


app = Flask(__name__, static_folder=str(PROJECT_ROOT / "ui" / "frontend"), static_url_path="")
app.logger.setLevel(logging.INFO)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
state = ExplorerState()
fm_playback = FmPlaybackState()
state_lock = threading.Lock()
csv_log_lock = threading.Lock()
identity_cache_lock = threading.Lock()
worker_stop = threading.Event()
worker_thread: threading.Thread | None = None
worker_stops: dict[str, threading.Event] = {}
worker_threads: dict[str, threading.Thread] = {}
fm_worker_stop = threading.Event()
fm_worker_thread: threading.Thread | None = None
fm_audio_q: queue.Queue[bytes] = queue.Queue(maxsize=96)
fm_pending_thread: threading.Thread | None = None
fm_request_serial = 0
inquiry_process: subprocess.Popen[str] | None = None
btc_engine_process: subprocess.Popen[str] | None = None
btc_engine_thread: threading.Thread | None = None
btc_engine_stop = threading.Event()
rf_sentinel_process: subprocess.Popen[str] | None = None
rf_sentinel_thread: threading.Thread | None = None
rf_sentinel_stop = threading.Event()
devices_cache_lock = threading.Lock()
devices_cache: list[dict[str, Any]] = []
devices_cache_updated_at = 0.0
shutdown_lock = threading.Lock()
shutdown_complete = False
ble_identity_cache: dict[str, dict[str, Any]] = {}
company_identifier_lut: dict[str, str] = {}
uuid16_identifier_lut: dict[str, str] = {}
UUID16_VENDOR_OVERRIDES = {
    "0xFCB2": "Apple, Inc.",
    "0xFEED": "Tile, Inc.",
}

CSV_COMMON_COLUMNS = [
    "run_id",
    "observed_at_iso",
    "observed_at_epoch",
    "logged_at_iso",
    "scanner_source",
    "protocol",
    "kind",
    "identity",
    "device_type",
    "device_type_detail",
    "mac",
    "name",
    "source_address",
    "destination_address",
    "bssid",
    "ssid",
    "wifi_role",
    "channel",
    "center_freq_hz",
    "frequency_hz",
    "frequency_mhz",
    "rssi_dbfs",
    "rssi_dbm",
    "confidence",
    "detail",
    "payload_hex",
    "raw_json",
]

CSV_PROTOCOL_COLUMNS = {
    "btle": [
        "address",
        "address_type",
        "uuid16",
        "uuid16_names",
        "manufacturer_id",
        "manufacturer_name",
        "appearance_category",
        "appearance_name",
    ],
    "btc": [
        "lap",
        "uap",
        "nap",
        "full_mac",
        "status",
        "target",
        "candidate_count",
        "processed_packets",
        "broken_packets",
        "repaired",
        "repair_distance",
    ],
    "zigbee": ["pan_id", "fcs_ok", "fcs_hex", "decoded_text", "sequence_number", "psdu_hex"],
    "tpms": ["protocol_variant", "sensor_id"],
    "wifi": ["ssid_visible", "count"],
    "fm": ["power_dbfs", "noise_dbfs", "excess_db", "audio_rms", "pilot_db", "rds_subcarrier_db", "stereo_likely", "rds_likely"],
    "lfmf": [
        "frequency_khz",
        "carrier_dbfs",
        "carrier_snr_db",
        "excess_db",
        "audio_dbfs",
        "modulation_pct",
        "band",
        "band_label",
        "active",
    ],
}

CSV_COMBINED_COLUMNS = CSV_COMMON_COLUMNS + sorted({col for cols in CSV_PROTOCOL_COLUMNS.values() for col in cols})
CSV_PROTOCOL_FILE_NAMES = {
    "BTLE": "btle.csv",
    "BTC": "btc.csv",
    "ZIGBEE": "zigbee.csv",
    "TPMS": "tpms.csv",
    "WIFI": "wifi.csv",
    "FM": "fm.csv",
    "LFMF": "lfmf.csv",
}
CSV_LOGGABLE_KINDS = {
    "ble_adv",
    "classic_lap",
    "zigbee_frame",
    "tpms_frame",
    "wifi_frame",
    "fm_station",
    "lfmf_signal",
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


def _btc_log(message: str, *args: Any, level: int = logging.INFO) -> None:
    try:
        app.logger.log(level, f"[BTC] {message}", *args)
    except Exception:
        pass


def _log_http_error(status_code: int, handler_name: str, payload: dict[str, Any], exc: Exception | None = None) -> None:
    path = request.path if request else "?"
    method = request.method if request else "?"
    detail = payload.get("detail") or payload.get("error") or ""
    if exc is not None:
        app.logger.warning(
            "[HTTP %d] handler=%s method=%s path=%s error=%s exc=%s",
            status_code,
            handler_name,
            method,
            path,
            detail,
            exc,
        )
    else:
        app.logger.warning(
            "[HTTP %d] handler=%s method=%s path=%s error=%s",
            status_code,
            handler_name,
            method,
            path,
            detail,
        )


def _json_error(status_code: int, handler_name: str, **payload: Any):
    _log_http_error(status_code, handler_name, payload)
    return jsonify(payload), status_code


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
    key = str(uuid16 or "").upper().replace("X", "x")
    return UUID16_VENDOR_OVERRIDES.get(key) or uuid16_identifier_lut.get(key, "")


def _uuid16_names(uuid16_values: list[str]) -> list[str]:
    return list(dict.fromkeys(name for uuid in uuid16_values for name in [_uuid16_name(uuid)] if name))


def _canonical_ble_vendor(name: str) -> str:
    value = str(name or "").strip()
    lowered = value.lower()
    if "apple" in lowered:
        return "Apple, Inc."
    if "microsoft" in lowered:
        return "Microsoft"
    if "tile" in lowered:
        return "Tile, Inc."
    return value


def _manufacturer_from_uuid16(uuid16_values: list[str]) -> dict[str, Any] | None:
    for uuid in uuid16_values:
        name = _canonical_ble_vendor(_uuid16_name(uuid))
        if not name:
            continue
        return {
            "company_id": "",
            "company_name": name,
            "data": "",
            "source": "uuid16",
            "uuid16": str(uuid).upper().replace("X", "x"),
        }
    return None


def _ble_identity_label(name: str, uuid16_names: list[str], manufacturer: dict[str, Any] | None, mac: str) -> str:
    local_name = str(name or "").strip()
    if local_name:
        return local_name
    manufacturer_name = _canonical_ble_vendor(str((manufacturer or {}).get("company_name") or ""))
    manufacturer_source = str((manufacturer or {}).get("source") or "")
    if uuid16_names:
        first_uuid_name = str(uuid16_names[0] or "").strip()
        if manufacturer_name == "Apple, Inc." and manufacturer_source == "uuid16":
            return "AirTag"
        return first_uuid_name
    if manufacturer_name:
        if manufacturer_name == "Apple, Inc." and manufacturer_source == "uuid16":
            return "AirTag"
        return manufacturer_name
    return mac


def _ble_device_type_label(
    name: str,
    uuid16_names: list[str],
    manufacturer: dict[str, Any] | None,
    appearance: dict[str, Any] | None,
) -> str:
    local_name = str(name or "").strip()
    if local_name:
        return ""
    manufacturer_name = _canonical_ble_vendor(str((manufacturer or {}).get("company_name") or ""))
    manufacturer_source = str((manufacturer or {}).get("source") or "")
    if manufacturer_name == "Apple, Inc." and manufacturer_source == "uuid16":
        return "AirTag"
    if appearance and str(appearance.get("label") or "").strip():
        return str(appearance.get("label") or "").strip()
    if manufacturer_name == "Tile, Inc.":
        return "Tracker"
    return ""


def _ble_device_type_detail(
    uuid16_names: list[str],
    manufacturer: dict[str, Any] | None,
    appearance: dict[str, Any] | None,
) -> str:
    manufacturer_name = _canonical_ble_vendor(str((manufacturer or {}).get("company_name") or ""))
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
        if uuid16_names:
            return "Tile UUID16 service"
        if manufacturer_data:
            return "Tile manufacturer frame"
        return "Tile tracker"
    if appearance and str(appearance.get("code") or "").strip():
        return str(appearance.get("code") or "").strip()
    return ""


def _ble_identity_source(name: str, uuid16_names: list[str], manufacturer: dict[str, Any] | None) -> str:
    manufacturer_name = str((manufacturer or {}).get("company_name") or "")
    if name:
        return "Local name"
    if uuid16_names:
        label = uuid16_names[0]
        if _canonical_ble_vendor(label) == "Apple, Inc.":
            return "AirTag inferred from UUID16 service"
        return f"{label} UUID16 service"
    if manufacturer_name:
        if (manufacturer or {}).get("source") == "uuid16":
            if _canonical_ble_vendor(manufacturer_name) == "Apple, Inc.":
                return "AirTag inferred from UUID16 service"
            return f"{manufacturer_name} inferred from UUID16 service"
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
        appearance = value.get("appearance") if isinstance(value.get("appearance"), dict) else None
        if manufacturer and manufacturer.get("company_id") and not manufacturer.get("company_name"):
            manufacturer = dict(manufacturer)
            manufacturer["company_name"] = _company_name(str(manufacturer.get("company_id")))
        uuid16 = value.get("uuid16") if isinstance(value.get("uuid16"), list) else []
        if not manufacturer:
            manufacturer = _manufacturer_from_uuid16(uuid16)
        out[mac] = {
            "mac": mac,
            "name": str(value.get("name") or "").strip(),
            "address_type": str(value.get("address_type") or "").strip(),
            "uuid16": uuid16,
            "uuid16_names": _uuid16_names(uuid16),
            "manufacturer": manufacturer,
            "appearance": appearance,
            "identity_source": str(value.get("identity_source") or ""),
            "device_type": str(value.get("device_type") or ""),
            "device_type_detail": str(value.get("device_type_detail") or ""),
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
    appearance: dict[str, Any] | None = None,
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
            row["manufacturer"] = row.get("manufacturer") or _manufacturer_from_uuid16(merged_uuid16)
        if appearance:
            row["appearance"] = appearance
        else:
            row.setdefault("appearance", row.get("appearance") if isinstance(row.get("appearance"), dict) else None)
        row["identity_source"] = _ble_identity_source(str(row.get("name") or ""), row["uuid16_names"], row.get("manufacturer"))
        row["device_type"] = _ble_device_type_label(
            str(row.get("name") or ""),
            row["uuid16_names"],
            row.get("manufacturer") if isinstance(row.get("manufacturer"), dict) else None,
            row.get("appearance") if isinstance(row.get("appearance"), dict) else None,
        )
        row["device_type_detail"] = _ble_device_type_detail(
            row["uuid16_names"],
            row.get("manufacturer") if isinstance(row.get("manufacturer"), dict) else None,
            row.get("appearance") if isinstance(row.get("appearance"), dict) else None,
        )
        row["first_seen_at"] = float(row.get("first_seen_at") or seen_at)
        row["last_seen_at"] = seen_at
        row["seen_count"] = int(row.get("seen_count") or 0) + 1
        ble_identity_cache[normalized] = row
        _save_ble_identity_cache()
        return dict(row)


company_identifier_lut.update(_load_company_identifier_lut())
uuid16_identifier_lut.update(_load_uuid16_identifier_lut())
ble_identity_cache.update(_load_ble_identity_cache())


def _btcsniffer_binary() -> Path:
    if BTC_SNIFFER_BINARY.exists():
        return BTC_SNIFFER_BINARY
    fallback = BTC_SNIFFER_ROOT / "build" / "btsniffer"
    return fallback if fallback.exists() else BTC_SNIFFER_BINARY


def _native_arch_tokens() -> tuple[str, ...]:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return ("x86-64", "x86_64", "amd64")
    if machine in {"aarch64", "arm64"}:
        return ("aarch64", "arm64")
    if machine.startswith("arm"):
        return ("arm",)
    return (machine,)


def _binary_arch_matches_host(binary: Path) -> tuple[bool, str]:
    if not binary.exists():
        return False, "missing"
    file_tool = shutil.which("file")
    if not file_tool:
        return True, "file tool unavailable"
    try:
        result = subprocess.run(
            [file_tool, "-b", str(binary)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        return True, f"file check failed: {exc}"
    description = f"{result.stdout} {result.stderr}".strip().lower()
    if result.returncode != 0 or not description:
        return True, description or f"file returned {result.returncode}"
    if "elf" not in description:
        return True, description
    expected = _native_arch_tokens()
    if any(token in description for token in expected):
        return True, description
    return False, description


def _btcsniffer_build_inputs() -> list[Path]:
    inputs = [BTC_SNIFFER_ROOT / "CMakeLists.txt"]
    inputs.extend(sorted((BTC_SNIFFER_ROOT / "src").glob("*.cpp")))
    inputs.extend(sorted((BTC_SNIFFER_ROOT / "src").glob("*.hpp")))
    return [path for path in inputs if path.exists()]


def _btcsniffer_cache_matches_source(build_dir: Path) -> bool:
    cache = build_dir / "CMakeCache.txt"
    if not cache.exists():
        return True
    try:
        text = cache.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    source_line = f"btcexplorer-sniffer_SOURCE_DIR:STATIC={BTC_SNIFFER_ROOT}"
    home_line = f"CMAKE_HOME_DIRECTORY:INTERNAL={BTC_SNIFFER_ROOT}"
    return source_line in text or home_line in text


def _btcsniffer_rebuild_reason(binary: Path) -> str | None:
    if not binary.exists():
        return "binary missing"
    if not os.access(binary, os.X_OK):
        return "binary is not executable"
    arch_ok, arch_detail = _binary_arch_matches_host(binary)
    if not arch_ok:
        return f"binary architecture does not match host ({arch_detail})"
    build_dir = BTC_SNIFFER_ROOT / "build"
    if not _btcsniffer_cache_matches_source(build_dir):
        return "CMake cache points at a different source directory"
    try:
        binary_mtime = binary.stat().st_mtime
    except OSError:
        return "binary stat failed"
    newest_input = max((path.stat().st_mtime for path in _btcsniffer_build_inputs()), default=0.0)
    if newest_input > binary_mtime:
        return "source is newer than binary"
    return None


def _build_btcsniffer_binary(reason: str) -> Path:
    if not BTC_SNIFFER_AUTO_BUILD:
        raise RuntimeError(f"btcsniffer rebuild required but BTC_SNIFFER_AUTO_BUILD is disabled: {reason}")
    cmake = shutil.which("cmake")
    if not cmake:
        raise RuntimeError(f"btcsniffer rebuild required ({reason}) but cmake was not found")
    build_dir = BTC_SNIFFER_ROOT / "build"
    with btcsniffer_build_lock:
        binary = _btcsniffer_binary()
        second_reason = _btcsniffer_rebuild_reason(binary)
        if second_reason is None:
            return binary
        _btc_log("rebuilding btcsniffer: %s", second_reason)
        if build_dir.exists() and not _btcsniffer_cache_matches_source(build_dir):
            _btc_log("removing stale btcsniffer build directory: %s", build_dir)
            shutil.rmtree(build_dir)
        build_dir.mkdir(parents=True, exist_ok=True)
        configure = subprocess.run(
            [cmake, "-S", str(BTC_SNIFFER_ROOT), "-B", str(build_dir)],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if configure.returncode != 0:
            raise RuntimeError(
                "btcsniffer cmake configure failed\n"
                f"stdout:\n{configure.stdout[-4000:]}\n"
                f"stderr:\n{configure.stderr[-4000:]}"
            )
        jobs = os.getenv("BTC_SNIFFER_BUILD_JOBS", str(max(1, min(4, os.cpu_count() or 1))))
        build = subprocess.run(
            [cmake, "--build", str(build_dir), "--parallel", jobs],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if build.returncode != 0:
            raise RuntimeError(
                "btcsniffer build failed\n"
                f"stdout:\n{build.stdout[-4000:]}\n"
                f"stderr:\n{build.stderr[-4000:]}"
            )
        built_binary = BTC_SNIFFER_ROOT / "build" / "btcexplorer-sniffer"
        if not built_binary.exists():
            raise RuntimeError(f"btcsniffer build completed but binary is missing: {built_binary}")
        built_binary.chmod(built_binary.stat().st_mode | 0o111)
        arch_ok, arch_detail = _binary_arch_matches_host(built_binary)
        if not arch_ok:
            raise RuntimeError(f"btcsniffer rebuilt but architecture still mismatches host: {arch_detail}")
        _btc_log("btcsniffer rebuild complete: %s", built_binary)
        return built_binary


def _ensure_btcsniffer_binary() -> Path:
    binary = _btcsniffer_binary()
    reason = _btcsniffer_rebuild_reason(binary)
    if reason is None:
        return binary
    return _build_btcsniffer_binary(reason)


def _btcsniffer_driver_from_device(device_id: str) -> str:
    lowered = device_id.lower()
    if lowered.startswith("bladerf"):
        return "bladerf"
    if lowered.startswith("hackrf"):
        return "hackrf"
    if lowered.startswith("sidekiq"):
        return "sidekiq"
    return "bladerf"


def _btc_max_bandwidth_mhz_for_device(device_id: str) -> int:
    driver = _btcsniffer_driver_from_device(device_id)
    if driver == "hackrf":
        return 20
    if driver == "bladerf":
        return 60
    if driver == "sidekiq":
        return 60
    return 20


def _device_max_rate_mhz(device: dict[str, Any]) -> int:
    try:
        rate = int(round(float(device.get("max_sample_rate_sps") or 0) / 1_000_000.0))
    except (TypeError, ValueError):
        rate = 0
    if rate > 0:
        return rate
    return _btc_max_bandwidth_mhz_for_device(str(device.get("id") or ""))


def _pick_ism24_bluetooth_device(devices: list[dict[str, Any]], allowed_devices: set[str] | None = None) -> str:
    candidates = [
        dev
        for dev in devices
        if str(dev.get("id") or "").strip()
        and (not allowed_devices or str(dev.get("id") or "").strip() in allowed_devices)
        and int(dev.get("freq_min_hz") or 0) <= 2_402_000_000
        and int(dev.get("freq_max_hz") or 0) >= 2_480_000_000
    ]
    if not candidates:
        candidates = [
            dev
            for dev in devices
            if str(dev.get("id") or "").strip()
            and (not allowed_devices or str(dev.get("id") or "").strip() in allowed_devices)
        ]
    if not candidates:
        return ""
    wide = [dev for dev in candidates if _device_max_rate_mhz(dev) >= 60]
    pool = wide or candidates
    best = max(pool, key=lambda dev: (_device_max_rate_mhz(dev), "bladerf" in str(dev.get("id") or dev.get("label") or "").lower()))
    return str(best.get("id") or "")


def _pick_non_bluetooth_hop_device(
    devices: list[dict[str, Any]],
    bluetooth_device_id: str,
    allowed_devices: set[str] | None = None,
) -> str:
    blocked = str(bluetooth_device_id or "").strip()
    candidates = [
        dev
        for dev in devices
        if str(dev.get("id") or "").strip()
        and str(dev.get("id") or "").strip() != blocked
        and str(dev.get("id") or "").strip() != "wlan0"
        and (not allowed_devices or str(dev.get("id") or "").strip() in allowed_devices)
    ]
    if not candidates:
        return ""
    return _pick_device(candidates, "hackrf", "sidekiq")


def _tail_text(path: Path, max_lines: int = 20) -> str:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-max_lines:])


def _classic_center_for_channel(channel: int) -> int:
    return BT_CLASSIC_CHANNELS.get(channel, BT_CLASSIC_CHANNELS[0])


def _btcsniffer_event_from_line(line: str, center_freq_hz: int, bank_start_channel: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    now = time.time()
    events: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    prefix = re.search(r"\[\s*(?P<channel>\d+)\]\s+(?P<ts>\d+)\s+us\s+--\s+(?P<lap>[0-9A-Fa-f]{6})\s+--\s+(?P<msg>.*)", line)
    if not prefix:
        return events, candidates

    bin_index = int(prefix.group("channel"))
    channel = bank_start_channel + bin_index
    ts_us = int(prefix.group("ts"))
    lap = prefix.group("lap").upper()
    msg = prefix.group("msg").strip()
    freq_hz = _classic_center_for_channel(channel)

    resolved = re.search(
        r"RESOLVED UAP:LAP\s+(?P<uap>[0-9A-Fa-f]{2}):(?P<lap>[0-9A-Fa-f]{6})(?:.*tracking(?:\s+for)?\s+(?P<tracking>\d+)\s+us)?",
        msg,
    )
    if resolved:
        event = {
            "kind": "classic_lap",
            "protocol": "BTC",
            "source": "btcsniffer",
            "seen_at": now,
            "channel": channel,
            "btcsniffer_bin": bin_index,
            "center_freq_hz": freq_hz,
            "bank_center_freq_hz": center_freq_hz,
            "rssi_dbfs": -120.0,
            "lap": resolved.group("lap").upper(),
            "uap": resolved.group("uap").upper(),
            "status": "resolved",
            "candidate_count": 1,
            "processed_packets": 1,
            "ts_us": ts_us,
            "tracking_us": int(resolved.group("tracking") or 0),
        }
        events.append(event)
        candidates.append({**event, "uap_hex": event["uap"], "score": 0.99})
        return events, candidates

    two_left = re.search(
        r"Only two UAP left \((?P<uap0>[0-9A-Fa-f]{2}) and (?P<uap1>[0-9A-Fa-f]{2})\).*tracking for\s+(?P<tracking>\d+)\s+us",
        msg,
    )
    if two_left:
        event = {
            "kind": "classic_lap",
            "protocol": "BTC",
            "source": "btcsniffer",
            "seen_at": now,
            "channel": channel,
            "btcsniffer_bin": bin_index,
            "center_freq_hz": freq_hz,
            "bank_center_freq_hz": center_freq_hz,
            "rssi_dbfs": -120.0,
            "lap": lap,
            "uap": None,
            "status": "brute_forcing",
            "candidate_count": 2,
            "processed_packets": 1,
            "ts_us": ts_us,
            "tracking_us": int(two_left.group("tracking")),
        }
        events.append(event)
        candidates.append(
            {
                **event,
                "uap_hex": f"{two_left.group('uap0').upper()} / {two_left.group('uap1').upper()}",
                "score": 0.82,
                "notes": [f"btcsniffer narrowed LAP {lap} to two UAPs."],
            }
        )
        return events, candidates

    narrowed = re.search(r"(?P<count>\d+)\s+possible UAPs remaining\s+\[(?P<uaps>[0-9A-Fa-f ]+)\]", msg)
    if narrowed:
        count = int(narrowed.group("count"))
        event = {
            "kind": "classic_lap",
            "protocol": "BTC",
            "source": "btcsniffer",
            "seen_at": now,
            "channel": channel,
            "btcsniffer_bin": bin_index,
            "center_freq_hz": freq_hz,
            "bank_center_freq_hz": center_freq_hz,
            "rssi_dbfs": -120.0,
            "lap": lap,
            "uap": None,
            "status": "brute_forcing",
            "candidate_count": count,
            "processed_packets": 1,
            "ts_us": ts_us,
        }
        events.append(event)
        candidates.append({**event, "uap_hex": "Pending", "score": 0.68, "notes": [f"Remaining UAPs: {narrowed.group('uaps').strip()}"]})
        return events, candidates

    if "Initialized" in msg:
        event = {
            "kind": "classic_lap",
            "protocol": "BTC",
            "source": "btcsniffer",
            "seen_at": now,
            "channel": channel,
            "btcsniffer_bin": bin_index,
            "center_freq_hz": freq_hz,
            "bank_center_freq_hz": center_freq_hz,
            "rssi_dbfs": -120.0,
            "lap": lap,
            "uap": None,
            "status": "initialized",
            "candidate_count": 32,
            "processed_packets": 1,
            "ts_us": ts_us,
        }
        events.append(event)
        return events, candidates

    init_failed = re.search(r"lap init failed lap=(?P<lap>[0-9A-Fa-f]{6}) channel=(?P<channel>\d+) ts_us=(?P<ts>\d+) valid_uaps=(?P<valid>\d+)", msg)
    if init_failed:
        event = {
            "kind": "classic_lap",
            "protocol": "BTC",
            "source": "btcsniffer",
            "seen_at": now,
            "channel": channel,
            "btcsniffer_bin": bin_index,
            "center_freq_hz": freq_hz,
            "bank_center_freq_hz": center_freq_hz,
            "rssi_dbfs": -120.0,
            "lap": init_failed.group("lap").upper(),
            "uap": None,
            "status": "init_failed",
            "candidate_count": int(init_failed.group("valid")),
            "processed_packets": 1,
            "cannot_init": 1,
            "ts_us": int(init_failed.group("ts")),
        }
        events.append(event)
        return events, candidates

    fhs = re.search(r"PASSIVE FHS BD_ADDR\s+(?P<addr>(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})", msg)
    if fhs:
        addr = fhs.group("addr").upper()
        parts = addr.split(":")
        event = {
            "kind": "classic_lap",
            "protocol": "BTC",
            "source": "btcsniffer",
            "seen_at": now,
            "channel": channel,
            "btcsniffer_bin": bin_index,
            "center_freq_hz": freq_hz,
            "bank_center_freq_hz": center_freq_hz,
            "rssi_dbfs": -120.0,
            "lap": "".join(parts[3:6]),
            "uap": parts[2],
            "nap": "".join(parts[0:2]),
            "mac": addr,
            "status": "passive_fhs",
            "candidate_count": 1,
            "processed_packets": 1,
            "ts_us": ts_us,
        }
        events.append(event)
        candidates.append({**event, "uap_hex": event["uap"], "score": 1.0})
        return events, candidates

    return events, candidates


def _btcsniffer_event_from_json(payload: dict[str, Any], center_freq_hz: int, bank_start_channel: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    event_type = str(payload.get("type") or "")
    now = time.time()
    events: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []

    if event_type == "metrics":
        access_hits = int(payload.get("access_hits") or 0)
        lap_events = int(payload.get("lap_events") or 0)
        resolved_events = int(payload.get("resolved_events") or 0)
        fhs_events = int(payload.get("fhs_events") or 0)
        with state_lock:
            state.decoder_stats["preamble_hits"] = int(payload.get("preamble_hits") or 0)
            state.decoder_stats["barker_hits"] = int(payload.get("barker_hits") or 0)
            state.decoder_stats["access_code_hits"] = access_hits
            state.decoder_stats["access_code_mismatch"] = int(payload.get("access_rejects") or 0)
            state.decoder_stats["lap_hits"] = lap_events + resolved_events + fhs_events
            state.decoder_stats["btcsniffer_packets_seen"] = int(payload.get("packets_seen") or 0)
            state.decoder_stats["btcsniffer_samples_processed"] = int(payload.get("samples_processed") or 0)
            state.decoder_stats["btcsniffer_solved_laps"] = int(payload.get("solved_laps") or 0)
            state.decoder_stats["btcsniffer_active_laps"] = int(payload.get("active_laps") or 0)
            state.decoder_stats["btcsniffer_bins"] = int(payload.get("bins") or 0)
            state.decoder_stats["fhs_attempts"] = int(payload.get("fhs_attempts") or 0)
            state.decoder_stats["fhs_inquiry_attempts"] = int(payload.get("fhs_inquiry_attempts") or 0)
            state.decoder_stats["fhs_solved_lap_attempts"] = int(payload.get("fhs_solved_lap_attempts") or 0)
            state.decoder_stats["fhs_truncated"] = int(payload.get("fhs_truncated") or 0)
            state.decoder_stats["fhs_header_matches"] = int(payload.get("fhs_header_matches") or 0)
            state.decoder_stats["fhs_type_matches"] = int(payload.get("fhs_type_matches") or 0)
            state.decoder_stats["fhs_payload_decodes"] = int(payload.get("fhs_payload_decodes") or 0)
            state.decoder_stats["fhs_fec_rejects"] = int(payload.get("fhs_fec_rejects") or 0)
            state.decoder_stats["fhs_address_rejects"] = int(payload.get("fhs_address_rejects") or 0)
            state.decoder_stats["fhs_packet_types"] = list(payload.get("fhs_packet_types") or [])
            state.classic_bursts_seen = max(state.classic_bursts_seen, lap_events + resolved_events + fhs_events)
        return events, candidates

    if event_type == "config":
        with state_lock:
            state.decoder_stats["btcsniffer_bins"] = int(payload.get("bins") or 0)
            state.decoder_stats["btcsniffer_sample_rate"] = int(float(payload.get("sample_rate") or 0))
        return events, candidates

    try:
        bin_index = int(payload.get("channel"))
    except (TypeError, ValueError):
        bin_index = 0
    channel = bank_start_channel + bin_index
    freq_hz = _classic_center_for_channel(channel)
    lap = str(payload.get("lap") or "").upper()
    ts_us = int(payload.get("ts_us") or 0)
    rssi_dbfs = float(payload.get("rssi_dbfs", -120.0))

    base = {
        "kind": "classic_lap",
        "protocol": "BTC",
        "source": "btcexplorer-sniffer",
        "seen_at": now,
        "channel": channel,
        "btcsniffer_bin": bin_index,
        "center_freq_hz": freq_hz,
        "bank_center_freq_hz": center_freq_hz,
        "rssi_dbfs": round(rssi_dbfs, 1),
        "lap": lap,
        "ts_us": ts_us,
        "processed_packets": 1,
    }

    if event_type == "lap_initialized":
        event = {**base, "uap": None, "status": "initialized", "candidate_count": int(payload.get("candidate_count") or 32)}
        events.append(event)
        return events, candidates

    if event_type == "lap_narrowed":
        event = {**base, "uap": None, "status": "brute_forcing", "candidate_count": int(payload.get("candidate_count") or 0)}
        events.append(event)
        candidates.append({**event, "uap_hex": "Pending", "score": 0.68, "notes": [f"Remaining UAPs: {payload.get('uaps') or ''}"]})
        return events, candidates

    if event_type == "lap_two_uap_left":
        event = {
            **base,
            "uap": None,
            "status": "brute_forcing",
            "candidate_count": 2,
            "tracking_us": int(payload.get("tracking_us") or 0),
        }
        events.append(event)
        candidates.append({**event, "uap_hex": f"{payload.get('uap0')} / {payload.get('uap1')}", "score": 0.82})
        return events, candidates

    if event_type == "lap_resolved":
        uap = str(payload.get("uap") or "").upper()
        event = {
            **base,
            "uap": uap,
            "status": "resolved",
            "candidate_count": 1,
            "tracking_us": int(payload.get("tracking_us") or 0),
        }
        events.append(event)
        candidates.append({**event, "uap_hex": uap, "score": 0.99})
        return events, candidates

    if event_type == "passive_fhs_bdaddr":
        address = str(payload.get("address") or "").upper()
        event = {
            **base,
            "uap": str(payload.get("uap") or "").upper(),
            "nap": str(payload.get("nap") or "").upper(),
            "mac": address,
            "status": "passive_fhs",
            "candidate_count": 1,
        }
        events.append(event)
        candidates.append({**event, "uap_hex": event["uap"], "score": 1.0})
        return events, candidates

    return events, candidates


def _btcsniffer_loop(proc: subprocess.Popen[str], center_freq_hz: int, bank_start_channel: int) -> None:
    _btc_log(
        "sniffer loop attached center=%.3f MHz bank_start=%d pid=%s",
        float(center_freq_hz) / 1_000_000.0,
        bank_start_channel,
        proc.pid,
    )
    with state_lock:
        state.worker_alive_by_mode["classic"] = True
        state.worker_alive = True
        state.worker_errors["classic"] = ""
        state.worker_error = ""
    try:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            if btc_engine_stop.is_set():
                break
            line = raw_line.strip()
            if not line:
                continue
            events: list[dict[str, Any]] = []
            candidates: list[dict[str, Any]] = []
            json_start = line.find("{")
            if json_start < 0:
                _btc_log("%s", line)
            if json_start > 0:
                text_part = line[:json_start].strip()
                json_part = line[json_start:].strip()
                if text_part:
                    _btc_log("%s", text_part)
                    text_events, text_candidates = _btcsniffer_event_from_line(text_part, center_freq_hz, bank_start_channel)
                    events.extend(text_events)
                    candidates.extend(text_candidates)
                try:
                    json_events, json_candidates = _btcsniffer_event_from_json(json.loads(json_part), center_freq_hz, bank_start_channel)
                    events.extend(json_events)
                    candidates.extend(json_candidates)
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
            elif line.startswith("{"):
                try:
                    events, candidates = _btcsniffer_event_from_json(json.loads(line), center_freq_hz, bank_start_channel)
                except (json.JSONDecodeError, TypeError, ValueError):
                    events, candidates = [], []
            else:
                events, candidates = _btcsniffer_event_from_line(line, center_freq_hz, bank_start_channel)
            with state_lock:
                state.chunks_seen += 1
                state.chunks_by_mode["classic"] = int(state.chunks_by_mode.get("classic", 0)) + 1
                state.last_rssi_dbfs = state.rssi_by_mode.get("classic", state.last_rssi_dbfs)
                state.decoder_stats["btcsniffer_lines"] = int(state.decoder_stats.get("btcsniffer_lines", 0)) + 1
            _append_detections(events, candidates)
    except Exception as exc:
        _btc_log("sniffer loop error: %s", exc, level=logging.ERROR)
        with state_lock:
            state.worker_errors["classic"] = f"btcsniffer error: {exc}"
            state.worker_error = f"btcsniffer error: {exc}"
    finally:
        rc = proc.poll()
        _btc_log("sniffer loop exiting pid=%s rc=%s stop=%s", proc.pid, rc, int(btc_engine_stop.is_set()))
        with state_lock:
            state.worker_alive_by_mode["classic"] = False
            state.worker_alive = any(state.worker_alive_by_mode.values())
            if not btc_engine_stop.is_set() and rc not in {None, 0}:
                state.worker_errors["classic"] = f"btcsniffer exited with code {rc}"
                state.worker_error = f"btcsniffer exited with code {rc}"


def _start_btcsniffer_engine(device_id: str, center_freq_hz: int, bandwidth_mhz: int, bank_start_channel: int) -> dict[str, Any]:
    global btc_engine_process, btc_engine_thread
    binary = _ensure_btcsniffer_binary()
    if not binary.exists():
        raise RuntimeError(f"btcsniffer binary not found: {binary}")
    BTC_SNIFFER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(binary),
        "--driver",
        _btcsniffer_driver_from_device(device_id),
        "--freq-mhz",
        f"{center_freq_hz / 1_000_000.0:.3f}MHz",
        "--bandwidth-mhz",
        f"{int(bandwidth_mhz)}MHz",
        "--log",
        str(BTC_SNIFFER_LOG_PATH),
        "--jsonl-stdout",
    ]
    _btc_log(
        "launch device=%s driver=%s center=%.3f MHz bandwidth=%d MHz bank_start=%d binary=%s",
        device_id,
        _btcsniffer_driver_from_device(device_id),
        float(center_freq_hz) / 1_000_000.0,
        int(bandwidth_mhz),
        int(bank_start_channel),
        binary,
    )
    _btc_log("command: %s", " ".join(cmd))
    btc_engine_stop.clear()
    proc = subprocess.Popen(
        cmd,
        cwd=str(BTC_SNIFFER_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    btc_engine_process = proc
    btc_engine_thread = threading.Thread(target=_btcsniffer_loop, args=(proc, center_freq_hz, bank_start_channel), daemon=True)
    btc_engine_thread.start()
    time.sleep(0.25)
    if proc.poll() not in {None, 0}:
        log_tail = _tail_text(BTC_SNIFFER_LOG_PATH)
        detail = f"btcsniffer exited immediately with code {proc.returncode}"
        if log_tail:
            detail = f"{detail}\n{log_tail}"
        _btc_log("launch failed: %s", detail, level=logging.ERROR)
        raise RuntimeError(detail)
    return {
        "engine": "btcsniffer",
        "stream_id": "btcsniffer",
        "device_id": device_id,
        "center_freq_hz": center_freq_hz,
        "sample_rate_sps": int(bandwidth_mhz) * 1_000_000,
        "lna_gain_db": 0,
        "vga_gain_db": 0,
        "channel": int(bank_start_channel),
        "body": {"engine": "btcsniffer", "command": cmd, "log": str(BTC_SNIFFER_LOG_PATH)},
    }


def _stop_btcsniffer_engine() -> None:
    global btc_engine_process, btc_engine_thread
    proc = btc_engine_process
    btc_engine_process = None
    btc_engine_stop.set()
    if proc is not None:
        _btc_log("stop requested pid=%s", proc.pid)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
    if btc_engine_thread and btc_engine_thread.is_alive():
        btc_engine_thread.join(timeout=2)
    _btc_log("stop complete")
    btc_engine_thread = None


def _gateway_streams() -> list[dict[str, Any]]:
    try:
        resp = requests.get(f"{_gateway_base()}/streams", headers=_gateway_headers(), timeout=5)
        if resp.status_code >= 400:
            return []
        body = resp.json()
        return body if isinstance(body, list) else []
    except requests.RequestException:
        return []


def _gateway_stream_for_device(device_id: str | None) -> dict[str, Any] | None:
    requested = str(device_id or "").strip()
    if not requested:
        return None
    for stream in _gateway_streams():
        cfg = stream.get("config", {}) or {}
        if str(cfg.get("device_id", "")).strip() == requested:
            return stream
    return None


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


def _gateway_get_json(path: str) -> Any:
    resp = requests.get(f"{_gateway_base()}{path}", headers=_gateway_headers(), timeout=5)
    if resp.status_code >= 400:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _gateway_stop_path(path: str) -> None:
    try:
        requests.post(f"{_gateway_base()}{path}", headers=_gateway_headers(), timeout=3)
    except requests.RequestException:
        pass


def _force_release_gateway_device(device_id: str) -> None:
    requested = str(device_id or "").strip()
    if not requested:
        return
    stopped = 0
    devices = _gateway_get_json("/devices")
    if isinstance(devices, list):
        for device in devices:
            if str(device.get("id") or "").strip() != requested:
                continue
            owner = str(device.get("occupied_by") or "").strip()
            owner_id = str(device.get("occupied_id") or "").strip()
            if owner == "stream" and owner_id:
                _gateway_stop_path(f"/streams/{owner_id}/stop")
                stopped += 1
            elif owner == "sweep" and owner_id:
                _gateway_stop_path(f"/sweeps/{owner_id}/stop")
                stopped += 1
            elif owner == "iq_sweep" and owner_id:
                _gateway_stop_path(f"/iq-sweeps/{owner_id}/stop")
                stopped += 1
            elif owner == "tx" and owner_id:
                _gateway_stop_path(f"/tx/{owner_id}/stop")
                stopped += 1
    for path, id_key, stop_prefix in (
        ("/streams", "stream_id", "/streams"),
        ("/sweeps", "sweep_id", "/sweeps"),
        ("/iq-sweeps", "iq_sweep_id", "/iq-sweeps"),
        ("/tx", "tx_id", "/tx"),
    ):
        sessions = _gateway_get_json(path)
        if not isinstance(sessions, list):
            continue
        for session in sessions:
            cfg = session.get("config") if isinstance(session.get("config"), dict) else {}
            if str(cfg.get("device_id") or "").strip() != requested:
                continue
            session_id = str(session.get(id_key) or "").strip()
            if session_id:
                _gateway_stop_path(f"{stop_prefix}/{session_id}/stop")
                stopped += 1
    if stopped:
        with state_lock:
            _append_scanner_log(f"[ui] force-released {requested} gateway sessions for FM playback")


def _drain_fm_audio_queue() -> None:
    while not fm_audio_q.empty():
        try:
            fm_audio_q.get_nowait()
        except queue.Empty:
            break


def _fm_playback_status_payload() -> dict[str, Any]:
    return {
        "running": fm_playback.running,
        "pending": fm_playback.pending,
        "pending_freq_mhz": fm_playback.pending_freq_mhz,
        "pending_device_id": fm_playback.pending_device_id,
        "device_id": fm_playback.device_id,
        "freq_mhz": fm_playback.freq_mhz,
        "sample_rate_sps": fm_playback.sample_rate_sps,
        "lna_gain_db": fm_playback.lna_gain_db,
        "vga_gain_db": fm_playback.vga_gain_db,
        "stream_id": fm_playback.stream_id,
        "worker_alive": fm_playback.worker_alive,
        "worker_error": fm_playback.worker_error,
        "last_audio_rms": fm_playback.last_audio_rms,
        "produced_chunks": fm_playback.produced_chunks,
        "served_chunks": fm_playback.served_chunks,
        "queued_chunks": fm_audio_q.qsize(),
    }


def _fm_busy_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return "resource busy" in text or "already in use" in text or "409" in text


def _runtime_enabled_protocols() -> set[str]:
    protocols = state.decoder_stats.get("enabled_protocols")
    if isinstance(protocols, list):
        live = {str(item).strip().lower() for item in protocols} & RF_SENTINEL_PROTOCOLS
        if live:
            return live
    control = _read_rf_sentinel_control()
    control_protocols = control.get("protocols")
    if isinstance(control_protocols, list):
        live = {str(item).strip().lower() for item in control_protocols} & RF_SENTINEL_PROTOCOLS
        if live:
            return live
    return set(_read_ui_config().get("protocols", [])) & RF_SENTINEL_PROTOCOLS


def _current_fm_scanner_device_id() -> str:
    assignments = dict(state.scanner_assignments or {})
    for assignment in assignments.values():
        if str(assignment.get("protocol") or "").lower() == "fm":
            device_id = str(assignment.get("device_id") or "").strip()
            if device_id:
                return device_id
    hop_device = str(state.device_ids.get("hop") or state.device_ids.get("radio_b") or "").strip()
    if hop_device:
        return hop_device
    return ""


def _pause_fm_scanner_for_playback() -> None:
    protocols = _runtime_enabled_protocols()
    if "fm" not in protocols:
        return
    protocols.discard("fm")
    existing = _read_rf_sentinel_control()
    devices = existing.get("devices") if isinstance(existing.get("devices"), list) else None
    enabled_devices = {str(item).strip() for item in devices if str(item).strip()} if devices is not None else None
    control = _write_rf_sentinel_control(
        protocols,
        enabled_devices=enabled_devices,
        zigbee_follow_channel=RF_SENTINEL_NO_CHANGE,
    )
    with state_lock:
        state.decoder_stats["enabled_protocols"] = sorted(protocols)
        state.decoder_stats["follow"] = _follow_state_for_protocols(control, protocols)
        fm_playback.scanner_protocol_paused = True
        _append_scanner_log("[ui] FM scanner paused for playback lock")


def _restore_fm_scanner_after_playback() -> None:
    if not fm_playback.scanner_protocol_paused:
        return
    fm_playback.scanner_protocol_paused = False
    protocols = _runtime_enabled_protocols()
    saved_protocols = set(_read_ui_config().get("protocols", [])) & RF_SENTINEL_PROTOCOLS
    if "fm" not in saved_protocols:
        return
    protocols.add("fm")
    existing = _read_rf_sentinel_control()
    devices = existing.get("devices") if isinstance(existing.get("devices"), list) else None
    enabled_devices = {str(item).strip() for item in devices if str(item).strip()} if devices is not None else None
    control = _write_rf_sentinel_control(
        protocols,
        enabled_devices=enabled_devices,
        zigbee_follow_channel=RF_SENTINEL_NO_CHANGE,
    )
    with state_lock:
        state.decoder_stats["enabled_protocols"] = sorted(protocols)
        state.decoder_stats["follow"] = _follow_state_for_protocols(control, protocols)
        _append_scanner_log("[ui] FM scanner restored after playback")


def _device_available(device_id: str) -> bool:
    requested = str(device_id or "").strip()
    if not requested:
        return False
    for device in _available_devices():
        if str(device.get("id") or "").strip() == requested:
            return not bool(device.get("occupied"))
    return False


def _wait_for_device_available(device_id: str, timeout_s: float = 5.0) -> bool:
    deadline = time.time() + max(0.1, float(timeout_s))
    while time.time() < deadline:
        if _device_available(device_id):
            return True
        time.sleep(0.15)
    return _device_available(device_id)


def _preferred_fm_playback_device(requested_device_id: str = "") -> str:
    devices = _available_devices()
    requested = str(requested_device_id or "").strip()
    if requested:
        for device in devices:
            if str(device.get("id") or "").strip() == requested and not bool(device.get("occupied")):
                return requested
        raise RuntimeError(f"resource busy: SDR {requested} is not free for FM playback")
    for preferred in ("hackrf", "sidekiq", "bladerf"):
        for device in devices:
            dev_id = str(device.get("id") or "").strip()
            haystack = f"{dev_id} {str(device.get('label') or '')}".lower()
            if preferred in haystack and not bool(device.get("occupied")):
                return dev_id
    for device in devices:
        dev_id = str(device.get("id") or "").strip()
        if dev_id and not bool(device.get("occupied")):
            return dev_id
    raise RuntimeError("No free SDR is available for FM playback")


def _start_fm_playback_now(freq_mhz: float, requested_device_id: str = "") -> None:
    global fm_worker_thread
    requested = str(requested_device_id or "").strip() or _current_fm_scanner_device_id()
    active_stream = _gateway_stream_for_device(requested)
    if requested and not _device_available(requested):
        _pause_fm_scanner_for_playback()
        if active_stream is None:
            _force_release_gateway_device(requested)
            _wait_for_device_available(requested, timeout_s=2.0)
            active_stream = _gateway_stream_for_device(requested)
    picked_device_id = requested if active_stream is not None else _preferred_fm_playback_device(requested)
    target_freq_hz = int(round(float(freq_mhz) * 1_000_000.0))
    target_rate = 2_000_000
    target_lna = 32
    target_vga = 32
    if active_stream is not None:
        stream_id = str(active_stream.get("stream_id") or "").strip()
        body, actual_rate, actual_lna, actual_vga = _retune_gateway_stream(
            stream_id,
            picked_device_id,
            target_freq_hz,
            target_rate,
            target_lna,
            target_vga,
        )
    else:
        _stop_duplicate_gateway_streams(picked_device_id)
        body, actual_rate, actual_lna, actual_vga = _start_gateway_stream(
            picked_device_id,
            target_freq_hz,
            target_rate,
            target_lna,
            target_vga,
        )
    _drain_fm_audio_queue()
    stream_id = str(body.get("stream_id") or "")
    if fm_playback.running and fm_playback.stream_id == stream_id and fm_worker_thread and fm_worker_thread.is_alive():
        fm_playback.pending = False
        fm_playback.pending_freq_mhz = 0.0
        fm_playback.pending_device_id = ""
        fm_playback.device_id = picked_device_id
        fm_playback.freq_mhz = float(freq_mhz)
        fm_playback.sample_rate_sps = actual_rate
        fm_playback.lna_gain_db = actual_lna
        fm_playback.vga_gain_db = actual_vga
        fm_playback.worker_error = ""
        fm_playback.last_audio_rms = 0.0
        fm_playback.produced_chunks = 0
        fm_playback.served_chunks = 0
        fm_playback.empty_audio_polls = 0
        return
    fm_worker_stop.clear()
    fm_playback.running = True
    fm_playback.pending = False
    fm_playback.pending_freq_mhz = 0.0
    fm_playback.pending_device_id = ""
    fm_playback.device_id = picked_device_id
    fm_playback.freq_mhz = float(freq_mhz)
    fm_playback.sample_rate_sps = actual_rate
    fm_playback.lna_gain_db = actual_lna
    fm_playback.vga_gain_db = actual_vga
    fm_playback.stream_id = stream_id
    fm_playback.worker_error = ""
    fm_playback.last_audio_rms = 0.0
    fm_playback.produced_chunks = 0
    fm_playback.served_chunks = 0
    fm_playback.empty_audio_polls = 0
    fm_worker_thread = threading.Thread(target=_fm_worker_loop, args=(fm_playback.stream_id, actual_rate), daemon=True)
    fm_worker_thread.start()


def _stop_fm_playback() -> None:
    global fm_worker_thread, fm_request_serial
    fm_request_serial += 1
    fm_worker_stop.set()
    if fm_worker_thread and fm_worker_thread.is_alive():
        fm_worker_thread.join(timeout=2.0)
    fm_worker_thread = None
    if fm_playback.stream_id:
        _stop_gateway_stream(fm_playback.stream_id)
    _drain_fm_audio_queue()
    fm_playback.running = False
    fm_playback.pending = False
    fm_playback.pending_freq_mhz = 0.0
    fm_playback.pending_device_id = ""
    fm_playback.device_id = ""
    fm_playback.freq_mhz = 0.0
    fm_playback.stream_id = ""
    fm_playback.worker_alive = False
    fm_playback.worker_error = ""
    fm_playback.last_audio_rms = 0.0
    fm_playback.produced_chunks = 0
    fm_playback.served_chunks = 0
    fm_playback.empty_audio_polls = 0


def _fm_worker_loop(stream_id: str, sample_rate_sps: int) -> None:
    demod = FmAudioDemod(sample_rate_sps)
    pcm_accum = bytearray()
    target_chunk_bytes = 16384
    headers = []
    token = _gateway_token()
    if token:
        headers.append(f"Authorization: Bearer {token}")
        headers.append(f"x-api-key: {token}")
    fm_playback.worker_alive = True
    fm_playback.worker_error = ""
    try:
        while not fm_worker_stop.is_set() and fm_playback.stream_id == stream_id:
            ws = websocket.WebSocket()
            try:
                ws.connect(_ws_url_for_stream(stream_id), timeout=8, header=headers)
                ws.settimeout(1.0)
                while not fm_worker_stop.is_set() and fm_playback.stream_id == stream_id:
                    try:
                        chunk = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    except WebSocketConnectionClosedException:
                        fm_playback.worker_error = "FM websocket closed"
                        break
                    if not isinstance(chunk, (bytes, bytearray)):
                        continue
                    pcm = demod.process_iq_i8(bytes(chunk))
                    if not pcm:
                        continue
                    pcm_accum.extend(pcm)
                    if len(pcm_accum) < target_chunk_bytes:
                        continue
                    out = bytes(pcm_accum)
                    pcm_accum.clear()
                    audio_i16 = np.frombuffer(out, dtype=np.int16)
                    if audio_i16.size:
                        fm_playback.last_audio_rms = float(np.sqrt(np.mean((audio_i16.astype(np.float32) / 32768.0) ** 2)))
                    fm_playback.produced_chunks += 1
                    try:
                        fm_audio_q.put(out, timeout=0.1)
                    except queue.Full:
                        try:
                            fm_audio_q.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            fm_audio_q.put_nowait(out)
                        except queue.Full:
                            pass
            except Exception as exc:
                fm_playback.worker_error = f"FM websocket error: {exc}"
            finally:
                try:
                    ws.close()
                except Exception:
                    pass
            if not fm_worker_stop.is_set() and fm_playback.stream_id == stream_id:
                fm_worker_stop.wait(0.5)
    finally:
        if fm_playback.stream_id == stream_id:
            fm_playback.worker_alive = False


def _fm_pending_loop(request_serial: int, freq_mhz: float, requested_device_id: str) -> None:
    while request_serial == fm_request_serial and not fm_worker_stop.is_set():
        try:
            _start_fm_playback_now(freq_mhz, requested_device_id)
            return
        except Exception as exc:
            if not _fm_busy_error(exc):
                if request_serial == fm_request_serial:
                    fm_playback.pending = False
                    fm_playback.worker_error = f"FM start failed: {exc}"
                    _restore_fm_scanner_after_playback()
                return
            if request_serial == fm_request_serial:
                fm_playback.pending = True
                fm_playback.pending_freq_mhz = float(freq_mhz)
                fm_playback.pending_device_id = str(requested_device_id or "")
                fm_playback.worker_error = "FM waiting for SDR availability"
            time.sleep(0.5)


def _start_fm_pending_thread(request_serial: int, freq_mhz: float, device_id: str) -> None:
    global fm_pending_thread
    fm_pending_thread = threading.Thread(
        target=_fm_pending_loop,
        args=(request_serial, float(freq_mhz), device_id),
        daemon=True,
    )
    fm_pending_thread.start()


def _printable_hex_text(hex_text: Any) -> str:
    try:
        raw = bytes.fromhex(str(hex_text or ""))
    except ValueError:
        return ""
    cleaned = bytes(value for value in raw if value in (9, 10, 13) or 32 <= value <= 126)
    return cleaned.decode("utf-8", errors="ignore").strip()


def _is_broadcast_or_multicast_mac(mac: str) -> bool:
    normalized = mac.strip().lower()
    if not normalized or normalized in {"ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00"}:
        return True
    try:
        first_octet = int(normalized.split(":", 1)[0], 16)
    except (ValueError, IndexError):
        return False
    return bool(first_octet & 0x01)


def _wifi_role(frame_type: str, source_mac: str, destination_mac: str, bssid: str, ssid: str) -> str:
    lowered = frame_type.lower()
    if "beacon" in lowered or "probe_response" in lowered:
        return "ap"
    if bssid and source_mac and source_mac.lower() == bssid.lower():
        return "ap"
    if "probe_request" in lowered:
        return "station"
    if source_mac and not _is_broadcast_or_multicast_mac(source_mac):
        return "station"
    if destination_mac and not _is_broadcast_or_multicast_mac(destination_mac):
        return "station"
    return "ap" if ssid or bssid else "station"


def _clean_wifi_ssid(value: Any) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if not text:
        return ""
    return "".join(char for char in text if char in "\t " or 32 <= ord(char) <= 126).strip()


def _real_rssi(value: Any) -> float | None:
    try:
        rssi = float(value)
    except (TypeError, ValueError):
        return None
    if rssi <= -119.9:
        return None
    return rssi


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def _iso_from_epoch(value: Any) -> str:
    try:
        epoch = float(value)
    except (TypeError, ValueError):
        epoch = time.time()
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch))
    millis = int((epoch - int(epoch)) * 1000)
    return f"{base}.{millis:03d}Z"


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, separators=(",", ":"), sort_keys=True)
    return str(value)


def _csv_protocol_key(event: dict[str, Any]) -> str:
    protocol = str(event.get("protocol") or "").strip().upper()
    if protocol == "BLE":
        return "BTLE"
    if protocol:
        return protocol
    return {
        "ble_adv": "BTLE",
        "classic_lap": "BTC",
        "zigbee_frame": "ZIGBEE",
        "tpms_frame": "TPMS",
        "wifi_frame": "WIFI",
        "fm_station": "FM",
        "lfmf_signal": "LFMF",
    }.get(str(event.get("kind") or ""), "")


def _csv_loggable_event(event: dict[str, Any]) -> bool:
    kind = str(event.get("kind") or "").strip()
    if kind not in CSV_LOGGABLE_KINDS:
        return False
    event_type = str(event.get("type") or "").strip().lower()
    if event_type in {"status", "control", "info", "debug"}:
        return False
    if kind == "ble_adv":
        return bool(event.get("address") or event.get("mac"))
    if kind == "classic_lap":
        return bool(event.get("lap"))
    if kind == "zigbee_frame":
        return bool(event.get("identity") or event.get("source_address") or event.get("destination_address") or event.get("payload_hex") or event.get("psdu_hex"))
    if kind == "tpms_frame":
        return bool(event.get("identity") or event.get("mac") or event.get("payload_hex"))
    if kind == "wifi_frame":
        return bool(event.get("identity") or event.get("source_address") or event.get("destination_address") or event.get("bssid") or event.get("ssid"))
    if kind == "fm_station":
        return bool(event.get("frequency_hz") or event.get("center_freq_hz") or event.get("identity"))
    if kind == "lfmf_signal":
        return bool(event.get("frequency_hz") or event.get("center_freq_hz") or event.get("identity"))
    return True


def _csv_event_row(event: dict[str, Any], columns: list[str]) -> dict[str, str]:
    observed_at = float(event.get("seen_at") or time.time())
    protocol = _csv_protocol_key(event)
    manufacturer = event.get("manufacturer") if isinstance(event.get("manufacturer"), dict) else {}
    appearance = event.get("appearance") if isinstance(event.get("appearance"), dict) else {}
    row_values: dict[str, Any] = {
        "run_id": state.csv_run_id,
        "observed_at_iso": _iso_from_epoch(observed_at),
        "observed_at_epoch": f"{observed_at:.6f}",
        "logged_at_iso": _iso_from_epoch(time.time()),
        "scanner_source": event.get("scanner_source"),
        "protocol": protocol,
        "kind": event.get("kind"),
        "identity": event.get("identity") or event.get("name") or event.get("address") or event.get("mac"),
        "device_type": event.get("device_type"),
        "device_type_detail": event.get("device_type_detail") or event.get("protocol_variant"),
        "mac": event.get("mac") or event.get("address") or event.get("full_mac"),
        "name": event.get("name"),
        "source_address": event.get("source_address"),
        "destination_address": event.get("destination_address"),
        "bssid": event.get("bssid"),
        "ssid": event.get("ssid"),
        "wifi_role": event.get("wifi_role"),
        "channel": event.get("channel"),
        "center_freq_hz": event.get("center_freq_hz"),
        "frequency_hz": event.get("frequency_hz"),
        "frequency_mhz": event.get("frequency_mhz"),
        "rssi_dbfs": event.get("rssi_dbfs") or event.get("last_rssi_dbfs"),
        "rssi_dbm": event.get("rssi_dbm"),
        "confidence": event.get("confidence"),
        "detail": event.get("detail") or event.get("status"),
        "payload_hex": event.get("payload_hex") or event.get("psdu_hex") or event.get("hex"),
        "raw_json": event,
        "address": event.get("address") or event.get("mac"),
        "address_type": event.get("address_type"),
        "uuid16": event.get("uuid16"),
        "uuid16_names": event.get("uuid16_names"),
        "manufacturer_id": manufacturer.get("id"),
        "manufacturer_name": manufacturer.get("name"),
        "appearance_category": appearance.get("category"),
        "appearance_name": appearance.get("name"),
        "lap": event.get("lap"),
        "uap": event.get("uap"),
        "nap": event.get("nap"),
        "full_mac": event.get("full_mac"),
        "status": event.get("status"),
        "target": event.get("target"),
        "candidate_count": event.get("candidate_count"),
        "processed_packets": event.get("processed_packets"),
        "broken_packets": event.get("broken_packets"),
        "repaired": event.get("repaired"),
        "repair_distance": event.get("repair_distance"),
        "pan_id": event.get("pan_id"),
        "fcs_ok": event.get("fcs_ok"),
        "fcs_hex": event.get("fcs_hex"),
        "decoded_text": event.get("decoded_text"),
        "sequence_number": event.get("sequence_number"),
        "psdu_hex": event.get("psdu_hex"),
        "protocol_variant": event.get("protocol_variant") or event.get("device_type_detail"),
        "sensor_id": event.get("sensor_id") or event.get("mac") or event.get("identity"),
        "ssid_visible": event.get("ssid_visible"),
        "count": event.get("count"),
        "power_dbfs": event.get("power_dbfs"),
        "noise_dbfs": event.get("noise_dbfs"),
        "excess_db": event.get("excess_db"),
        "audio_rms": event.get("audio_rms"),
        "pilot_db": event.get("pilot_db"),
        "rds_subcarrier_db": event.get("rds_subcarrier_db"),
        "stereo_likely": event.get("stereo_likely"),
        "rds_likely": event.get("rds_likely"),
        "frequency_khz": event.get("frequency_khz"),
        "carrier_dbfs": event.get("carrier_dbfs"),
        "carrier_snr_db": event.get("carrier_snr_db"),
        "audio_dbfs": event.get("audio_dbfs"),
        "modulation_pct": event.get("modulation_pct"),
        "band": event.get("band"),
        "band_label": event.get("band_label"),
        "active": event.get("active"),
    }
    return {column: _csv_cell(row_values.get(column)) for column in columns}


def _write_csv_schema(run_dir: Path, run_id: str) -> None:
    schema = {
        "run_id": run_id,
        "created_at": _iso_from_epoch(time.time()),
        "description": "RF Sentinel observation CSVs. Each row is one normalized protocol observation.",
        "folder": str(run_dir),
        "files": {
            "combined.csv": {
                "protocols": sorted(CSV_PROTOCOL_FILE_NAMES),
                "columns": CSV_COMBINED_COLUMNS,
            },
            **{
                file_name: {
                    "protocol": protocol,
                    "columns": CSV_COMMON_COLUMNS + CSV_PROTOCOL_COLUMNS.get(protocol.lower(), []),
                }
                for protocol, file_name in CSV_PROTOCOL_FILE_NAMES.items()
            },
        },
    }
    (run_dir / "schema.json").write_text(json.dumps(schema, indent=2, sort_keys=True), encoding="utf-8")


def _write_csv_header(path: Path, columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore").writeheader()


def _initialize_csv_files(run_dir: Path) -> None:
    _write_csv_header(run_dir / "combined.csv", CSV_COMBINED_COLUMNS)
    for protocol, file_name in CSV_PROTOCOL_FILE_NAMES.items():
        columns = CSV_COMMON_COLUMNS + CSV_PROTOCOL_COLUMNS.get(protocol.lower(), [])
        _write_csv_header(run_dir / file_name, columns)


def _csv_run_epoch(path: Path) -> float:
    run_id = path.name.split("-", 1)[0]
    try:
        return float(calendar.timegm(time.strptime(run_id, "%Y%m%dT%H%M%SZ")))
    except ValueError:
        try:
            return path.stat().st_mtime
        except OSError:
            return time.time()


def _archive_run_dir(run_dir: Path) -> Path | None:
    RF_SENTINEL_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = RF_SENTINEL_ARCHIVE_DIR / f"{run_dir.name}.zip"
    if archive_path.exists():
        suffix = 1
        while archive_path.exists():
            suffix += 1
            archive_path = RF_SENTINEL_ARCHIVE_DIR / f"{run_dir.name}-{suffix:02d}.zip"
    tmp_path = archive_path.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for child in sorted(run_dir.rglob("*")):
                if child.is_file():
                    archive.write(child, arcname=str(Path(run_dir.name) / child.relative_to(run_dir)))
        tmp_path.replace(archive_path)
        shutil.rmtree(run_dir)
        app.logger.info("csv_run_archived source=%s archive=%s", run_dir, archive_path)
        return archive_path
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        app.logger.warning("csv_run_archive_failed path=%s error=%s", run_dir, exc)
        return None


def _trim_csv_archives(max_mb: int = RF_SENTINEL_CSV_ARCHIVE_MAX_MB) -> None:
    max_bytes = max(1, int(max_mb)) * 1024 * 1024
    try:
        archives = [path for path in RF_SENTINEL_ARCHIVE_DIR.glob("*.zip") if path.is_file()]
    except OSError as exc:
        app.logger.warning("csv_archive_list_failed path=%s error=%s", RF_SENTINEL_ARCHIVE_DIR, exc)
        return
    archive_stats: list[tuple[float, int, Path]] = []
    for path in archives:
        try:
            stat = path.stat()
        except OSError:
            continue
        archive_stats.append((stat.st_mtime, stat.st_size, path))
    total_bytes = sum(size for _, size, _ in archive_stats)
    for _, size, path in sorted(archive_stats, key=lambda item: item[0]):
        if total_bytes <= max_bytes:
            break
        try:
            path.unlink()
            total_bytes -= size
            app.logger.info("csv_archive_pruned path=%s max_mb=%s", path, max_mb)
        except OSError as exc:
            app.logger.warning("csv_archive_prune_failed path=%s error=%s", path, exc)


def _archive_old_csv_runs(
    retention_days: int = RF_SENTINEL_CSV_RETENTION_DAYS,
    max_archive_mb: int = RF_SENTINEL_CSV_ARCHIVE_MAX_MB,
) -> None:
    cutoff = time.time() - (max(1, int(retention_days)) * 86400)
    try:
        entries = list(RF_SENTINEL_RUNS_DIR.iterdir())
    except FileNotFoundError:
        return
    except OSError as exc:
        app.logger.warning("csv_retention_list_failed path=%s error=%s", RF_SENTINEL_RUNS_DIR, exc)
        return
    for entry in entries:
        if not entry.is_dir():
            continue
        if _csv_run_epoch(entry) >= cutoff:
            continue
        _archive_run_dir(entry)
    _trim_csv_archives(max_archive_mb)


def _start_csv_run() -> None:
    _archive_old_csv_runs()
    base_run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_id = base_run_id
    run_dir = RF_SENTINEL_RUNS_DIR / run_id
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_id = f"{base_run_id}-{suffix:02d}"
        run_dir = RF_SENTINEL_RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_csv_schema(run_dir, run_id)
    _initialize_csv_files(run_dir)
    state.csv_run_id = run_id
    state.csv_log_dir = str(run_dir)


def _append_csv_rows(events: list[dict[str, Any]]) -> None:
    loggable_events = [event for event in events if _csv_loggable_event(event)]
    if not loggable_events or not state.csv_log_dir:
        return
    run_dir = Path(state.csv_log_dir)
    with csv_log_lock:
        for file_name, columns, rows in _csv_batches(loggable_events):
            path = run_dir / file_name
            needs_header = not path.exists() or path.stat().st_size == 0
            with path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
                if needs_header:
                    writer.writeheader()
                for row in rows:
                    writer.writerow(row)


def _csv_batches(events: list[dict[str, Any]]) -> list[tuple[str, list[str], list[dict[str, str]]]]:
    combined_rows = [_csv_event_row(event, CSV_COMBINED_COLUMNS) for event in events]
    batches: list[tuple[str, list[str], list[dict[str, str]]]] = [("combined.csv", CSV_COMBINED_COLUMNS, combined_rows)]
    for protocol, file_name in CSV_PROTOCOL_FILE_NAMES.items():
        protocol_events = [event for event in events if _csv_protocol_key(event) == protocol]
        if not protocol_events:
            continue
        columns = CSV_COMMON_COLUMNS + CSV_PROTOCOL_COLUMNS.get(protocol.lower(), [])
        batches.append((file_name, columns, [_csv_event_row(event, columns) for event in protocol_events]))
    return batches


def _scanner_json_to_events(source: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    now = float(payload.get("timestamp") or payload.get("seen_at") or time.time())
    protocol = str(payload.get("protocol") or "").lower()
    kind = str(payload.get("kind") or "").lower()
    source_protocol = str(source or "").rsplit(":", 1)[-1].lower()

    if kind == "ble_adv" or protocol in {"ble", "btle"}:
        payload.setdefault("kind", "ble_adv")
        payload.setdefault("seen_at", now)
        return [payload]

    if kind == "classic_lap" or protocol in {"btc", "bluetooth_classic", "classic"} or (source_protocol == "btc" and payload.get("lap")):
        payload.setdefault("kind", "classic_lap")
        payload.setdefault("seen_at", now)
        payload.setdefault("status", payload.get("type") or "observed")
        return [payload]

    if protocol == "ieee802154" or source_protocol == "zigbee":
        mac = payload.get("mac") if isinstance(payload.get("mac"), dict) else {}
        payload_hex = str(mac.get("payload_hex") or payload.get("payload_hex") or "")
        fcs_ok = _optional_bool(payload.get("fcs_ok"))
        if fcs_ok is False and not RF_SENTINEL_KEEP_BAD_FCS:
            return []
        source_address = str(mac.get("source_address") or "").strip()
        destination_address = str(mac.get("destination_address") or "").strip()
        pan_id = mac.get("source_pan_id") or mac.get("destination_pan_id")
        text = _printable_hex_text(payload_hex)
        return [
            {
                "kind": "zigbee_frame",
                "protocol": "ZIGBEE",
                "seen_at": now,
                "identity": source_address or destination_address or f"802.15.4 CH {payload.get('channel') or '?'}",
                "mac": source_address or destination_address or "",
                "source_address": source_address,
                "destination_address": destination_address,
                "pan_id": pan_id,
                "detail": text or str(mac.get("frame_type") or "802.15.4 frame"),
                "decoded_text": text,
                "device_type": "802.15.4",
                "device_type_detail": str(mac.get("frame_type") or ""),
                "channel": payload.get("channel"),
                "center_freq_hz": payload.get("center_freq_hz"),
                "last_rssi_dbfs": _real_rssi(payload.get("rssi_dbfs")),
                "confidence": payload.get("confidence"),
                "fcs_ok": fcs_ok,
                "fcs_hex": mac.get("fcs_hex"),
                "payload_hex": payload_hex,
                "psdu_hex": payload.get("psdu_hex"),
                "sequence_number": mac.get("sequence_number"),
            }
        ]

    if protocol in {"tpms", "subghz"} or source_protocol == "tpms":
        fields = payload.get("decoded_fields") if isinstance(payload.get("decoded_fields"), dict) else {}
        sensor_id = str(fields.get("sensor_id") or fields.get("id") or payload.get("hex") or "").strip()
        detail_bits = [
            str(payload.get("protocol_variant") or "TPMS"),
            f"confidence={payload.get('confidence')}" if payload.get("confidence") is not None else "",
        ]
        return [
            {
                "kind": "tpms_frame",
                "protocol": "TPMS",
                "seen_at": now,
                "identity": sensor_id or "TPMS sensor",
                "mac": sensor_id,
                "detail": " · ".join(bit for bit in detail_bits if bit),
                "device_type": "TPMS",
                "device_type_detail": str(payload.get("protocol_variant") or ""),
                "center_freq_hz": payload.get("center_freq_hz"),
                "last_rssi_dbfs": payload.get("rssi_dbfs") or payload.get("burst_peak_dbfs"),
                "confidence": payload.get("confidence"),
                "payload_hex": payload.get("hex"),
            }
        ]

    if protocol == "wifi" or source_protocol == "wifi":
        source_mac = str(payload.get("source") or payload.get("mac_sa") or "").strip()
        destination_mac = str(payload.get("destination") or payload.get("mac_da") or "").strip()
        bssid = str(payload.get("bssid") or "").strip()
        ssid = _clean_wifi_ssid(payload.get("ssid"))
        frame_type = str(payload.get("kind") or "wifi").strip()
        role = _wifi_role(frame_type, source_mac, destination_mac, bssid, ssid)
        ssid_visible = bool(ssid)
        ap_identifier = bssid or source_mac or destination_mac
        identity = ssid if role == "ap" and ssid_visible else (
            f"Hidden SSID {ap_identifier}" if role == "ap" and ap_identifier else (
                "Hidden SSID" if role == "ap" else (source_mac or destination_mac or bssid or "WiFi station")
            )
        )
        device_type = "Access Point" if role == "ap" else "Station"
        identity_source = (
            "SSID advertised in 802.11 management frame."
            if role == "ap" and ssid_visible
            else "No SSID value observed; showing BSSID/MAC identifier."
            if role == "ap"
            else "Observed as WiFi station/client traffic."
        )
        return [
            {
                "kind": "wifi_frame",
                "protocol": "WIFI",
                "seen_at": now,
                "identity": identity,
                "mac": source_mac or bssid or destination_mac,
                "wifi_role": role,
                "source_address": source_mac,
                "destination_address": destination_mac,
                "bssid": bssid,
                "ssid": ssid,
                "ssid_visible": ssid_visible,
                "identity_source": identity_source,
                "detail": str(payload.get("raw") or frame_type),
                "device_type": device_type,
                "device_type_detail": frame_type,
                "channel": payload.get("channel"),
                "center_freq_hz": int(payload.get("frequency_mhz") or 0) * 1_000_000 if payload.get("frequency_mhz") else None,
                "last_rssi_dbfs": payload.get("rssi_dbm"),
                "rssi_dbm": payload.get("rssi_dbm"),
                "count": payload.get("count"),
            }
        ]

    if protocol == "fm" or source_protocol == "fm":
        frequency_hz = payload.get("frequency_hz")
        frequency_mhz = payload.get("frequency_mhz")
        try:
            frequency_hz_int = int(frequency_hz)
        except (TypeError, ValueError):
            try:
                frequency_hz_int = int(float(frequency_mhz) * 1_000_000)
            except (TypeError, ValueError):
                frequency_hz_int = 0
        label = str(payload.get("identity") or "").strip()
        if not label and frequency_hz_int:
            label = f"FM {frequency_hz_int / 1_000_000:.1f} MHz"
        detail_bits = [
            f"power {float(payload.get('power_dbfs')):.1f} dBFS" if payload.get("power_dbfs") is not None else "",
            f"pilot {float(payload.get('pilot_db')):.1f} dB" if payload.get("pilot_db") is not None else "",
            f"RDS {float(payload.get('rds_subcarrier_db')):.1f} dB" if payload.get("rds_subcarrier_db") is not None else "",
        ]
        return [
            {
                "kind": "fm_station",
                "protocol": "FM",
                "seen_at": now,
                "identity": label or "FM station",
                "mac": f"{frequency_hz_int}" if frequency_hz_int else label,
                "detail": " · ".join(bit for bit in detail_bits if bit) or "FM broadcast station",
                "device_type": "Broadcast FM",
                "device_type_detail": "Stereo pilot detected" if payload.get("stereo_likely") else "Mono/unknown stereo",
                "center_freq_hz": frequency_hz_int or None,
                "frequency_hz": frequency_hz_int or None,
                "frequency_mhz": payload.get("frequency_mhz"),
                "last_rssi_dbfs": payload.get("rssi_dbfs") or payload.get("power_dbfs"),
                "power_dbfs": payload.get("power_dbfs"),
                "noise_dbfs": payload.get("noise_dbfs"),
                "excess_db": payload.get("excess_db"),
                "audio_rms": payload.get("audio_rms"),
                "pilot_db": payload.get("pilot_db"),
                "rds_subcarrier_db": payload.get("rds_subcarrier_db"),
                "stereo_likely": payload.get("stereo_likely"),
                "rds_likely": payload.get("rds_likely"),
            }
        ]

    if protocol == "lfmf" or source_protocol == "lfmf":
        frequency_hz = payload.get("frequency_hz") or payload.get("freq_hz")
        frequency_khz = payload.get("frequency_khz") or payload.get("freq_khz")
        try:
            frequency_hz_int = int(frequency_hz)
        except (TypeError, ValueError):
            try:
                frequency_hz_int = int(float(frequency_khz) * 1000)
            except (TypeError, ValueError):
                frequency_hz_int = 0
        label = f"{frequency_hz_int / 1000:.1f} kHz" if frequency_hz_int else "VLF/LF/MF signal"
        band_label = str(payload.get("band_label") or payload.get("band") or "VLF/LF/MF").strip()
        detail_bits = [
            band_label,
            f"carrier {float(payload.get('carrier_dbfs')):.1f} dBFS" if payload.get("carrier_dbfs") is not None else "",
            f"SNR {float(payload.get('carrier_snr_db')):.1f} dB" if payload.get("carrier_snr_db") is not None else "",
            f"excess {float(payload.get('excess_db')):.1f} dB" if payload.get("excess_db") is not None else "",
            f"mod {float(payload.get('modulation_pct')):.1f}%" if payload.get("modulation_pct") is not None else "",
        ]
        return [
            {
                "kind": "lfmf_signal",
                "protocol": "LFMF",
                "seen_at": now,
                "identity": f"{band_label} {label}",
                "mac": str(frequency_hz_int or label),
                "detail": " · ".join(bit for bit in detail_bits if bit),
                "device_type": "VLF/LF/MF signal",
                "device_type_detail": band_label,
                "center_freq_hz": frequency_hz_int or None,
                "frequency_hz": frequency_hz_int or None,
                "frequency_khz": frequency_khz,
                "last_rssi_dbfs": payload.get("carrier_dbfs") or payload.get("power_dbfs"),
                "power_dbfs": payload.get("power_dbfs"),
                "carrier_dbfs": payload.get("carrier_dbfs"),
                "carrier_snr_db": payload.get("carrier_snr_db"),
                "excess_db": payload.get("excess_db"),
                "audio_dbfs": payload.get("audio_dbfs"),
                "modulation_pct": payload.get("modulation_pct"),
                "band": payload.get("band"),
                "band_label": band_label,
                "active": payload.get("active"),
            }
        ]

    return []


def _append_detections(events: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> None:
    if not events and not candidates:
        return
    for item in [*events, *candidates]:
        if item.get("kind") == "classic_lap" or item.get("lap"):
            uap_value = item.get("uap")
            uap_hex = str(item.get("uap_hex") or "")
            if uap_value in {None, "", "Pending"} and re.fullmatch(r"[0-9A-Fa-f]{2}", uap_hex):
                uap_value = uap_hex.upper()
            item.setdefault("nap", "XXXX")
            item["uap"] = str(uap_value or "XX").upper()
            item["full_mac"] = _classic_full_mac(item.get("nap"), item.get("uap"), item.get("lap"))
            item.setdefault("mac", item["full_mac"])
    _append_csv_rows(events)
    with state_lock:
        for event in events:
            state.bursts_seen += 1 if event["kind"].endswith("burst") else 0
            state.ble_packets_seen += 1 if event["kind"] == "ble_adv" else 0
            state.classic_bursts_seen += 1 if event["kind"] in {"classic_burst", "classic_lap"} else 0
            mode_key = {
                "ble_adv": "ble",
                "classic_burst": "classic",
                "classic_lap": "classic",
                "zigbee_frame": "zigbee",
                "tpms_frame": "tpms",
                "wifi_frame": "wifi",
                "fm_station": "fm",
                "lfmf_signal": "lfmf",
            }.get(str(event.get("kind") or ""))
            if mode_key:
                rssi = _real_rssi(event.get("rssi_dbfs", event.get("last_rssi_dbfs")))
                if rssi is not None:
                    state.rssi_by_mode[mode_key] = round(rssi, 1)
                    state.last_rssi_dbfs = round(rssi, 1)
            if event["kind"] in {"classic_burst", "classic_lap"}:
                try:
                    rssi = float(event.get("rssi_dbfs"))
                    if rssi > -119.9:
                        state.rssi_by_mode["classic"] = round(rssi, 1)
                        state.last_rssi_dbfs = round(rssi, 1)
                        state.noise_floor_dbfs = round((state.noise_floor_dbfs * 0.92) + (rssi * 0.08), 1)
                except (TypeError, ValueError):
                    pass
            if event["kind"] in {"ble_adv", "classic_lap", "zigbee_frame", "tpms_frame", "wifi_frame", "fm_station", "lfmf_signal"}:
                _upsert_discovery_row(event)
            if event["kind"] == "classic_lap":
                _upsert_classic_address(event)
            if event["kind"] in {"classic_burst", "classic_lap"}:
                _upsert_channel_activity(event)
        state.detections = (events + state.detections)[:240]
        if candidates:
            state.classic_candidates = (candidates + state.classic_candidates)[:64]


def _classic_full_mac(nap: Any = None, uap: Any = None, lap: Any = None) -> str:
    nap_clean = re.sub(r"[^0-9A-Fa-f]", "", str(nap or "")).upper()
    uap_clean = re.sub(r"[^0-9A-Fa-f]", "", str(uap or "")).upper()
    lap_clean = re.sub(r"[^0-9A-Fa-f]", "", str(lap or "")).upper()
    nap_hex = nap_clean[:4] if len(nap_clean) >= 4 else "XXXX"
    uap_hex = uap_clean[:2] if len(uap_clean) >= 2 else "XX"
    lap_hex = lap_clean[:6] if len(lap_clean) >= 6 else "XXXXXX"
    return f"{nap_hex[0:2]}:{nap_hex[2:4]}:{uap_hex}:{lap_hex[0:2]}:{lap_hex[2:4]}:{lap_hex[4:6]}"


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
        appearance = event.get("appearance") if isinstance(event.get("appearance"), dict) else None
        cached = _remember_ble_identity(mac, name, address_type, now, uuid16, manufacturer, appearance)
        name = name or str(cached.get("name") or "").strip()
        uuid16 = list(cached.get("uuid16") or uuid16)
        uuid16_names = list(cached.get("uuid16_names") or _uuid16_names(uuid16))
        manufacturer = cached.get("manufacturer") or manufacturer
        appearance = cached.get("appearance") if isinstance(cached.get("appearance"), dict) else appearance
        if not manufacturer:
            manufacturer = _manufacturer_from_uuid16(uuid16)
        identity = _ble_identity_label(name, uuid16_names, manufacturer, mac)
        identity_source = _ble_identity_source(name, uuid16_names, manufacturer)
        device_type = _ble_device_type_label(name, uuid16_names, manufacturer, appearance)
        device_type_detail = _ble_device_type_detail(uuid16_names, manufacturer, appearance)
        row = {
            "key": f"ble:{mac}",
            "protocol": "BTLE",
            "identity": identity,
            "mac": mac,
            "name": name,
            "uuid16": uuid16,
            "uuid16_names": uuid16_names,
            "manufacturer": manufacturer,
            "appearance": appearance,
            "device_type": device_type,
            "device_type_detail": device_type_detail,
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
        uap = str(event.get("uap") or "XX")
        nap = str(event.get("nap") or "XXXX")
        full_mac = _classic_full_mac(nap, uap, lap)
        target = _classic_test_match(lap, uap)
        identity = full_mac
        detail = str(event.get("status") or "")
        if target:
            identity = f"TEST DONGLE {identity}"
            detail = "target-match" if not detail else f"target-match · {detail}"
        row = {
            "key": f"btc:{lap}:{uap if uap != 'XX' else 'missing'}",
            "protocol": "BTC",
            "identity": identity,
            "mac": full_mac,
            "nap": nap,
            "uap": uap,
            "lap": lap,
            "full_mac": full_mac,
            "detail": detail,
            "target": bool(target),
            "detections": 1,
            "last_seen_at": now,
            "last_rssi_dbfs": event.get("rssi_dbfs"),
            "channel": event.get("channel"),
            "center_freq_hz": event.get("center_freq_hz"),
        }
    elif event.get("kind") == "zigbee_frame":
        source_address = str(event.get("source_address") or "").strip()
        destination_address = str(event.get("destination_address") or "").strip()
        identity = str(event.get("identity") or source_address or destination_address or "802.15.4 frame")
        pan_id = event.get("pan_id")
        key_bits = [source_address or destination_address or identity, str(pan_id or ""), str(event.get("channel") or "")]
        row = {
            "key": f"zigbee:{':'.join(key_bits)}",
            "protocol": "ZIGBEE",
            "identity": identity,
            "mac": source_address or destination_address,
            "source_address": source_address,
            "destination_address": destination_address,
            "pan_id": pan_id,
            "detail": str(event.get("detail") or "802.15.4 frame"),
            "decoded_text": str(event.get("decoded_text") or ""),
            "device_type": str(event.get("device_type") or "802.15.4"),
            "device_type_detail": str(event.get("device_type_detail") or ""),
            "detections": 1,
            "last_seen_at": now,
            "last_rssi_dbfs": _real_rssi(event.get("last_rssi_dbfs", event.get("rssi_dbfs"))),
            "channel": event.get("channel"),
            "center_freq_hz": event.get("center_freq_hz"),
            "confidence": event.get("confidence"),
            "fcs_ok": event.get("fcs_ok"),
            "fcs_hex": event.get("fcs_hex"),
            "payload_hex": event.get("payload_hex") or event.get("psdu_hex"),
        }
    elif event.get("kind") == "tpms_frame":
        identity = str(event.get("identity") or "TPMS sensor")
        row = {
            "key": f"tpms:{identity}",
            "protocol": "TPMS",
            "identity": identity,
            "mac": str(event.get("mac") or identity),
            "detail": str(event.get("detail") or "TPMS frame"),
            "device_type": str(event.get("device_type") or "TPMS"),
            "device_type_detail": str(event.get("device_type_detail") or ""),
            "detections": 1,
            "last_seen_at": now,
            "last_rssi_dbfs": event.get("last_rssi_dbfs") or event.get("rssi_dbfs"),
            "channel": event.get("channel"),
            "center_freq_hz": event.get("center_freq_hz"),
            "confidence": event.get("confidence"),
            "payload_hex": event.get("payload_hex"),
        }
    elif event.get("kind") == "wifi_frame":
        identity = str(event.get("identity") or event.get("ssid") or event.get("mac") or "WiFi frame")
        ssid = _clean_wifi_ssid(event.get("ssid"))
        ssid_visible = bool(event.get("ssid_visible")) and bool(ssid)
        frame_type = str(event.get("device_type_detail") or event.get("detail") or "wifi").strip()
        mac = str(event.get("mac") or "").strip()
        bssid = str(event.get("bssid") or "").strip()
        role = str(event.get("wifi_role") or "station").strip().lower()
        if role not in {"ap", "station"}:
            role = "station"
        source_address = str(event.get("source_address") or "").strip()
        destination_address = str(event.get("destination_address") or "").strip()
        key_mac = (bssid or mac or source_address or identity) if role == "ap" else (source_address or mac or destination_address or identity)
        device_type = "Access Point" if role == "ap" else "Station"
        if role == "ap" and not ssid_visible:
            identity = f"Hidden SSID {key_mac}".strip()
        detail = (
            f"Hidden/unnamed SSID · {frame_type}"
            if role == "ap" and not ssid_visible
            else f"{device_type} {frame_type}".strip()
        )
        identity_source = str(
            event.get("identity_source")
            or ("No SSID value observed; showing BSSID/MAC identifier." if role == "ap" and not ssid_visible else "")
        )
        row = {
            "key": f"wifi:{role}:{key_mac}:{ssid if role == 'ap' else ''}",
            "protocol": "WIFI",
            "identity": identity,
            "mac": mac or bssid,
            "wifi_role": role,
            "source_address": source_address,
            "destination_address": destination_address,
            "bssid": bssid,
            "ssid": ssid,
            "ssid_visible": ssid_visible,
            "name": ssid if role == "ap" and ssid_visible else "",
            "detail": detail,
            "device_type": device_type,
            "device_type_detail": f"Hidden/unnamed SSID · {frame_type}" if role == "ap" and not ssid_visible else frame_type,
            "identity_source": identity_source,
            "detections": 1,
            "last_seen_at": now,
            "last_rssi_dbfs": event.get("last_rssi_dbfs") or event.get("rssi_dbm"),
            "rssi_dbm": event.get("rssi_dbm"),
            "channel": event.get("channel"),
            "center_freq_hz": event.get("center_freq_hz"),
        }
    elif event.get("kind") == "fm_station":
        identity = str(event.get("identity") or "FM station")
        frequency_hz = event.get("frequency_hz") or event.get("center_freq_hz")
        row = {
            "key": f"fm:{frequency_hz or identity}",
            "protocol": "FM",
            "identity": identity,
            "mac": str(frequency_hz or identity),
            "detail": str(event.get("detail") or "FM broadcast station"),
            "device_type": str(event.get("device_type") or "Broadcast FM"),
            "device_type_detail": str(event.get("device_type_detail") or ""),
            "detections": 1,
            "last_seen_at": now,
            "last_rssi_dbfs": event.get("last_rssi_dbfs") or event.get("power_dbfs"),
            "center_freq_hz": frequency_hz,
            "frequency_hz": frequency_hz,
            "frequency_mhz": event.get("frequency_mhz"),
            "power_dbfs": event.get("power_dbfs"),
            "noise_dbfs": event.get("noise_dbfs"),
            "excess_db": event.get("excess_db"),
            "audio_rms": event.get("audio_rms"),
            "pilot_db": event.get("pilot_db"),
            "rds_subcarrier_db": event.get("rds_subcarrier_db"),
            "stereo_likely": event.get("stereo_likely"),
            "rds_likely": event.get("rds_likely"),
        }
    elif event.get("kind") == "lfmf_signal":
        identity = str(event.get("identity") or "VLF/LF/MF signal")
        frequency_hz = event.get("frequency_hz") or event.get("center_freq_hz")
        row = {
            "key": f"lfmf:{frequency_hz or identity}",
            "protocol": "LFMF",
            "identity": identity,
            "mac": str(frequency_hz or identity),
            "detail": str(event.get("detail") or "VLF/LF/MF signal"),
            "device_type": str(event.get("device_type") or "VLF/LF/MF signal"),
            "device_type_detail": str(event.get("device_type_detail") or ""),
            "detections": 1,
            "last_seen_at": now,
            "last_rssi_dbfs": event.get("last_rssi_dbfs") or event.get("carrier_dbfs") or event.get("power_dbfs"),
            "center_freq_hz": frequency_hz,
            "frequency_hz": frequency_hz,
            "frequency_khz": event.get("frequency_khz"),
            "power_dbfs": event.get("power_dbfs"),
            "carrier_dbfs": event.get("carrier_dbfs"),
            "carrier_snr_db": event.get("carrier_snr_db"),
            "excess_db": event.get("excess_db"),
            "audio_dbfs": event.get("audio_dbfs"),
            "modulation_pct": event.get("modulation_pct"),
            "band": event.get("band"),
            "band_label": event.get("band_label"),
            "active": event.get("active"),
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
            if not row.get("manufacturer"):
                row["manufacturer"] = _manufacturer_from_uuid16(row["uuid16"])
            if not row.get("appearance") and existing.get("appearance"):
                row["appearance"] = existing["appearance"]
            row["identity_source"] = _ble_identity_source(
                str(row.get("name") or ""),
                row.get("uuid16_names") or [],
                row.get("manufacturer") if isinstance(row.get("manufacturer"), dict) else None,
            )
            row["device_type"] = _ble_device_type_label(
                str(row.get("name") or ""),
                row.get("uuid16_names") or [],
                row.get("manufacturer") if isinstance(row.get("manufacturer"), dict) else None,
                row.get("appearance") if isinstance(row.get("appearance"), dict) else None,
            )
            row["device_type_detail"] = _ble_device_type_detail(
                row.get("uuid16_names") or [],
                row.get("manufacturer") if isinstance(row.get("manufacturer"), dict) else None,
                row.get("appearance") if isinstance(row.get("appearance"), dict) else None,
            )
        if row.get("protocol") == "BTLE" and row.get("name"):
            row["identity"] = row["name"]
        elif row.get("protocol") == "BTLE":
            row["identity"] = _ble_identity_label(
                str(row.get("name") or ""),
                row.get("uuid16_names") or [],
                row.get("manufacturer") if isinstance(row.get("manufacturer"), dict) else None,
                str(row.get("mac") or ""),
            )
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
    uap = str(event.get("uap") or "XX")
    nap = str(event.get("nap") or "XXXX")
    full_mac = _classic_full_mac(nap, uap, lap)
    target = _classic_test_match(lap, uap)
    row = {
        "lap": lap,
        "uap": uap,
        "nap": nap,
        "full_mac": full_mac,
        "mac": full_mac,
        "status": "target-match" if target else str(event.get("status") or "observed"),
        "target": bool(target),
        "candidate_count": int(event.get("candidate_count") or 0),
        "processed_packets": int(event.get("processed_packets") or 0),
        "broken_packets": int(event.get("broken_packets") or 0),
        "cannot_init": int(event.get("cannot_init") or 0),
        "repaired": bool(event.get("repaired", False)),
        "repair_distance": int(event.get("repair_distance") or 0),
        "header_perfect_triplets": int(event.get("header_perfect_triplets") or 0),
        "header_relaxed": bool(event.get("header_relaxed", False)),
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
        if existing.get("uap") not in {"", None, "XX", "XXX"} and row["uap"] in {"XX", "XXX"}:
            row["uap"] = existing["uap"]
        if existing.get("nap") not in {"", None, "XXXX", "XX:XX"} and row["nap"] == "XXXX":
            row["nap"] = existing["nap"]
        row["full_mac"] = _classic_full_mac(row.get("nap"), row.get("uap"), row.get("lap"))
        row["mac"] = row["full_mac"]
        row["processed_packets"] = max(int(existing.get("processed_packets") or 0), row["processed_packets"])
        row["broken_packets"] = max(int(existing.get("broken_packets") or 0), row["broken_packets"])
        row["cannot_init"] = max(int(existing.get("cannot_init") or 0), row["cannot_init"])
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


def _bluetooth_controllers() -> list[dict[str, str]]:
    proc = subprocess.run(
        ["bluetoothctl", "list"],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    controllers: list[dict[str, str]] = []
    for match in re.finditer(r"Controller\s+([0-9A-Fa-f:]{17})(?:\s+(.+))?", output):
        controllers.append({"mac": match.group(1).upper(), "name": (match.group(2) or "").strip()})
    return controllers


def _enable_discoverable_controller(controller_mac: str | None = None) -> tuple[dict[str, Any], str]:
    select_cmd = [f"select {controller_mac}"] if controller_mac else []
    commands = "\n".join(
        select_cmd
        + [
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
    target["controller"] = match.group(1).upper()
    target["discoverable"] = "Discoverable: yes" in output or "Changing discoverable on succeeded" in output
    target["bluetoothctl_returncode"] = proc.returncode
    return target, output


def _start_bredr_inquiry(exclude_controller: str | None = None) -> tuple[dict[str, Any] | None, str]:
    global inquiry_process
    _stop_bredr_inquiry()
    controllers = _bluetooth_controllers()
    helper = next(
        (controller for controller in controllers if controller["mac"].upper() != str(exclude_controller or "").upper()),
        None,
    )
    if helper is None:
        return None, "No second Bluetooth controller available for active BR/EDR inquiry"
    commands = "\n".join(
        [
            f"select {helper['mac']}",
            "power on",
            "scan bredr on",
        ]
    )
    inquiry_process = subprocess.Popen(
        ["bluetoothctl"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if inquiry_process.stdin:
        inquiry_process.stdin.write(commands + "\n")
        inquiry_process.stdin.flush()
    return helper, f"BR/EDR inquiry running on {helper['mac']}"


def _stop_bredr_inquiry() -> None:
    global inquiry_process
    proc = inquiry_process
    inquiry_process = None
    if proc is None:
        return
    try:
        if proc.stdin:
            proc.stdin.write("scan off\nexit\n")
            proc.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.terminate()


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
                                if key == "target_access_best_distance":
                                    current = int(state.decoder_stats.get(key, 68))
                                    state.decoder_stats[key] = min(current, int(value))
                                    detector.stats[key] = 68
                                    continue
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


def _rf_sentinel_scan_bin() -> str:
    candidate = Path(sys.executable).parent / "rf_sentinel_scan"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("rf_sentinel_scan")
    if found:
        return found
    return str(candidate)


def _read_rf_sentinel_control() -> dict[str, Any]:
    try:
        payload = json.loads(RF_SENTINEL_CONTROL_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_rf_sentinel_control(
    enabled_protocols: set[str] | None = None,
    *,
    enabled_devices: set[str] | None = None,
    zigbee_follow_channel: int | None | object = RF_SENTINEL_NO_CHANGE,
) -> dict[str, Any]:
    RF_SENTINEL_CONTROL_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = _read_rf_sentinel_control()
    if enabled_protocols is not None:
        payload["protocols"] = sorted(enabled_protocols & RF_SENTINEL_PROTOCOLS)
    if enabled_devices is not None:
        payload["devices"] = sorted(str(item).strip() for item in enabled_devices if str(item).strip())
    elif enabled_protocols is not None:
        payload.pop("devices", None)
    if zigbee_follow_channel is not RF_SENTINEL_NO_CHANGE:
        follow = payload.get("follow")
        if not isinstance(follow, dict):
            follow = {}
        if isinstance(zigbee_follow_channel, int):
            follow["zigbee"] = {"channel": zigbee_follow_channel}
        else:
            follow.pop("zigbee", None)
        payload["follow"] = follow
    tmp_path = RF_SENTINEL_CONTROL_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp_path.replace(RF_SENTINEL_CONTROL_PATH)
    return payload


def _follow_state_for_protocols(control: dict[str, Any], protocols: set[str]) -> dict[str, Any]:
    if "zigbee" not in protocols:
        return {}
    follow = control.get("follow") if isinstance(control.get("follow"), dict) else {}
    zigbee = follow.get("zigbee") if isinstance(follow.get("zigbee"), dict) else None
    return {"zigbee": zigbee} if zigbee else {}


def _read_ui_config() -> dict[str, Any]:
    try:
        payload = json.loads(RF_SENTINEL_UI_CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    protocols = payload.get("protocols")
    if not isinstance(protocols, list):
        protocols = sorted(RF_SENTINEL_PROTOCOLS)
    disabled = payload.get("disabled_devices")
    if not isinstance(disabled, list):
        disabled = []
    return {
        "protocols": sorted({str(item).strip().lower() for item in protocols} & RF_SENTINEL_PROTOCOLS),
        "disabled_devices": sorted({str(item).strip() for item in disabled if str(item).strip()}),
    }


def _write_ui_config(protocols: set[str], disabled_devices: set[str]) -> dict[str, Any]:
    RF_SENTINEL_UI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "protocols": sorted(protocols & RF_SENTINEL_PROTOCOLS),
        "disabled_devices": sorted(str(item).strip() for item in disabled_devices if str(item).strip()),
        "updated_at": time.time(),
    }
    tmp_path = RF_SENTINEL_UI_CONFIG_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    tmp_path.replace(RF_SENTINEL_UI_CONFIG_PATH)
    return payload


def _enabled_devices_from_disabled(devices: list[dict[str, Any]], disabled_devices: set[str]) -> set[str]:
    return {
        str(item.get("id") or "").strip()
        for item in devices
        if str(item.get("id") or "").strip() and str(item.get("id") or "").strip() not in disabled_devices
    }


def _has_wifi_device(devices: list[dict[str, Any]], enabled_devices: set[str] | None = None) -> bool:
    for item in devices:
        device_id = str(item.get("id") or "").strip()
        if enabled_devices is not None and device_id not in enabled_devices:
            continue
        text = f"{device_id} {item.get('label') or ''} {item.get('driver') or ''}".lower()
        if "wlan" in text or "wifi" in text or "802.11" in text:
            return True
    return False


def _wifi_interface_from_devices(devices: list[dict[str, Any]], enabled_devices: set[str] | None = None) -> str:
    for item in devices:
        device_id = str(item.get("id") or "").strip()
        if enabled_devices is not None and device_id not in enabled_devices:
            continue
        text = f"{device_id} {item.get('label') or ''} {item.get('driver') or ''}".lower()
        if "wlan" in text or "wifi" in text or "802.11" in text:
            return device_id
    return ""


def _is_sdrplay_device(item: dict[str, Any]) -> bool:
    text = f"{item.get('id') or ''} {item.get('label') or ''} {item.get('driver') or ''}".lower()
    return "sdrplay" in text and ("rsp2" in text or str(item.get("id") or "").lower().startswith("sdrplay:"))


def _has_lfmf_device(devices: list[dict[str, Any]], enabled_devices: set[str] | None = None) -> bool:
    for item in devices:
        device_id = str(item.get("id") or "").strip()
        if enabled_devices is not None and device_id not in enabled_devices:
            continue
        if _is_sdrplay_device(item):
            return True
    return False


def _terminate_process_group(proc: subprocess.Popen[str], timeout_s: float = 4.0) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        proc.terminate()
    deadline = time.time() + timeout_s
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            proc.kill()
        proc.wait(timeout=1)


def _parse_rf_sentinel_line(line: str) -> tuple[str, str]:
    match = re.match(r"^\[(?P<source>[^\]]+)\]\s*(?P<body>.*)$", line.strip())
    if not match:
        return "scanner", line.strip()
    return match.group("source").strip(), match.group("body").strip()


def _append_scanner_log(line: str) -> None:
    text = str(line or "").rstrip()
    if not text:
        return
    print(text, flush=True)
    state.scanner_log.append(text)
    state.scanner_log = state.scanner_log[-300:]
    assignment = _parse_scanner_assignment(text)
    if assignment:
        state.scanner_assignments[assignment["device_id"]] = assignment


def _scanner_protocol_from_job_name(job_name: str) -> str:
    parts = [part for part in str(job_name or "").split(":") if part]
    if not parts:
        return ""
    protocol = parts[-1]
    if protocol == "bluetooth":
        protocol = "btc"
    if protocol.startswith("follow"):
        protocol = "zigbee"
    return protocol.lower()


def _scanner_band_from_command(command: str, protocol: str) -> str:
    text = str(command or "")
    if protocol == "btc":
        match = re.search(r"--center-mhz\s+([0-9.]+)\s+--bandwidth-mhz\s+([0-9]+)", text)
        if match:
            if "bluetooth_scanner" in text:
                return f"2.4 GHz ISM shared BTC+BLE · {match.group(1)} MHz / {match.group(2)} MHz BW"
            return f"{match.group(1)} MHz / {match.group(2)} MHz BW"
    if protocol == "ble":
        if "iq-sweep" in text:
            return "BLE adv 37/38/39"
        match = re.search(r"--channel\s+([0-9]+)", text)
        if match:
            return f"BLE CH {match.group(1)}"
    if protocol == "zigbee":
        match = re.search(r"--channel\s+([0-9]+)", text)
        if match:
            return f"Zigbee CH {match.group(1)}"
        match = re.search(r"--sample-rate-sps\s+([0-9]+)", text)
        if match:
            return f"Zigbee wideband {int(match.group(1)) / 1_000_000:.1f} Msps"
        return "Zigbee wideband"
    if protocol == "tpms":
        if "--auto-hop-known" in text:
            return "315 / 433.92 MHz"
    if protocol == "fm":
        return "87.7-107.9 MHz"
    if protocol == "lfmf":
        match = re.search(r"--band\s+(\S+)", text)
        if match:
            band = match.group(1)
            if band == "vlf-lf-mf":
                return "VLF/LF/MF 3 kHz-3 MHz"
            if band == "1khz-1mhz":
                return "VLF/LF/lower-MF 1 kHz-1 MHz"
            return band.upper()
        return "VLF/LF/MF"
    if protocol == "wifi":
        match = re.search(r"--channels\s+([0-9,]+)", text)
        if match:
            return f"WiFi CH {match.group(1)}"
        return "WiFi monitor"
    return ""


def _parse_scanner_assignment(line: str) -> dict[str, Any] | None:
    text = str(line or "").strip()
    auto_match = re.search(
        r"^\[rf-sentinel\]\s+auto\s+device=(?P<device>\S+)\s+job=(?P<job>\S+)\s+dwell_s=(?P<dwell>[0-9.]+):\s+(?P<command>.+)$",
        text,
    )
    if auto_match:
        job_name = auto_match.group("job")
        protocol = _scanner_protocol_from_job_name(job_name)
        command = auto_match.group("command")
        return {
            "device_id": auto_match.group("device"),
            "job_name": job_name,
            "protocol": protocol,
            "band": _scanner_band_from_command(command, protocol),
            "command": command,
            "dwell_s": float(auto_match.group("dwell")),
            "seen_at": time.time(),
            "mode": "auto",
        }
    cont_match = re.search(r"^\[rf-sentinel\]\s+starting\s+continuous\s+(?P<job>\S+):\s+(?P<command>.+)$", text)
    if cont_match:
        command = cont_match.group("command")
        device_match = re.search(r"--device-id\s+(\S+)", command)
        if not device_match:
            return None
        job_name = cont_match.group("job")
        protocol = _scanner_protocol_from_job_name(job_name)
        return {
            "device_id": device_match.group(1),
            "job_name": job_name,
            "protocol": protocol,
            "band": _scanner_band_from_command(command, protocol),
            "command": command,
            "dwell_s": 0.0,
            "seen_at": time.time(),
            "mode": "continuous",
        }
    hop_match = re.search(
        r"^\[rf-sentinel\]\s+hop\s+group=(?P<group>\S+)\s+job=(?P<job>\S+)\s+dwell_s=(?P<dwell>[0-9.]+):\s+(?P<command>.+)$",
        text,
    )
    if hop_match:
        command = hop_match.group("command")
        job_name = hop_match.group("job")
        protocol = _scanner_protocol_from_job_name(job_name)
        device_match = re.search(r"--device-id\s+(\S+)", command)
        if protocol == "wifi" and not device_match:
            device_match = re.search(r"--interface\s+(\S+)", command)
        if not device_match:
            return None
        return {
            "device_id": device_match.group(1),
            "job_name": job_name,
            "protocol": protocol,
            "band": _scanner_band_from_command(command, protocol),
            "command": command,
            "dwell_s": float(hop_match.group("dwell")),
            "seen_at": time.time(),
            "mode": "hop",
        }
    return None


def _clean_device_id(value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if not text or lowered in {"loading...", "loading", "no sdr devices"}:
        return ""
    if "sdr-gateway is unavailable" in lowered or "no devices" in lowered:
        return ""
    return text


def _rf_sentinel_loop(proc: subprocess.Popen[str]) -> None:
    assert proc.stdout is not None
    with state_lock:
        state.worker_alive = True
        state.worker_alive_by_mode["scanner"] = True
        state.worker_error = ""
        state.worker_errors["scanner"] = ""
    try:
        for raw_line in proc.stdout:
            if rf_sentinel_stop.is_set():
                break
            line = raw_line.strip()
            if not line:
                continue
            source, body = _parse_rf_sentinel_line(line)
            payload = None
            if body.startswith("{"):
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    payload = None
            source_protocol = str(source or "").rsplit(":", 1)[-1].lower()
            payload_protocol = str((payload or {}).get("protocol") or "").lower()
            payload_kind = str((payload or {}).get("kind") or "").lower()
            noisy_packet_line = payload is not None and (
                source_protocol in {"ble", "btc", "bluetooth", "classic", "zigbee", "wifi"}
                or payload_protocol in {"ble", "btle", "btc", "bluetooth_classic", "ieee802154", "wifi"}
                or payload_kind in {"ble_adv", "classic_lap", "zigbee_frame", "wifi_frame"}
                or payload_kind.startswith(("mgmt.", "ctrl.", "data."))
                or str((payload or {}).get("type") or "").lower() == "metrics"
            )
            with state_lock:
                if not noisy_packet_line:
                    _append_scanner_log(line)
                state.chunks_by_mode[source] = int(state.chunks_by_mode.get(source, 0)) + 1
                state.chunks_seen += 1
            if payload is None:
                continue
            events = _scanner_json_to_events(source, payload)
            for event in events:
                event.setdefault("scanner_source", source)
            if events:
                _append_detections(events, [])
        rc = proc.wait()
        if not rf_sentinel_stop.is_set() and rc not in (0, -signal.SIGTERM):
            with state_lock:
                state.worker_error = f"RF Sentinel scanner exited with code {rc}"
                state.worker_errors["scanner"] = state.worker_error
                _append_scanner_log(state.worker_error)
    finally:
        with state_lock:
            state.worker_alive = False
            state.worker_alive_by_mode["scanner"] = False


def _start_rf_sentinel_engine(
    btc_device_id: str,
    hop_device_id: str,
    btc_center_mhz: float,
    btc_bandwidth_mhz: int,
    btc_lna_gain_db: int,
    btc_vga_gain_db: int,
    hop_lna_gain_db: int,
    hop_vga_gain_db: int,
    enabled_protocols: set[str] | None = None,
    enabled_devices: set[str] | None = None,
    sweep_both_radios: bool = False,
) -> dict[str, Any]:
    global rf_sentinel_process, rf_sentinel_thread
    rf_sentinel_stop.clear()
    cmd = [
        _rf_sentinel_scan_bin(),
        "--btc-device-id",
        btc_device_id,
        "--hop-device-id",
        hop_device_id,
        "--btc-center-mhz",
        f"{btc_center_mhz:.3f}",
        "--btc-bandwidth-mhz",
        str(btc_bandwidth_mhz),
        "--btc-lna-gain-db",
        str(btc_lna_gain_db),
        "--btc-vga-gain-db",
        str(btc_vga_gain_db),
        "--ble-lna-gain-db",
        str(hop_lna_gain_db),
        "--ble-vga-gain-db",
        str(hop_vga_gain_db),
    ]
    if sweep_both_radios:
        cmd.extend(
            [
                "--sweep-both-radios",
                "--radio-a-device-id",
                btc_device_id,
                "--radio-b-device-id",
                hop_device_id,
                "--radio-a-btc-bandwidth-mhz",
                str(btc_bandwidth_mhz),
                "--radio-b-btc-bandwidth-mhz",
                "20",
            ]
        )
    protocols = enabled_protocols or set(RF_SENTINEL_PROTOCOLS)
    devices = enabled_devices or set()
    for device_id in sorted(devices):
        cmd.extend(["--allowed-device-id", device_id])
    if "wifi" in protocols:
        wifi_interface = _wifi_interface_from_devices(_available_devices(), enabled_devices)
        if wifi_interface:
            cmd.extend(["--wifi-interface", wifi_interface])
    if "btc" not in protocols:
        cmd.append("--no-btc")
    if "ble" not in protocols:
        cmd.append("--no-ble")
    if "zigbee" not in protocols:
        cmd.append("--no-zigbee")
    if "tpms" not in protocols:
        cmd.append("--no-tpms")
    if "wifi" not in protocols:
        cmd.append("--no-wifi")
    if "fm" not in protocols:
        cmd.append("--no-fm")
    if "lfmf" not in protocols:
        cmd.append("--no-lfmf")
    # Start in discovery mode; only the explicit right-click Follow action locks Zigbee.
    zigbee_follow_channel = None
    control = _write_rf_sentinel_control(
        protocols,
        enabled_devices=devices,
        zigbee_follow_channel=zigbee_follow_channel,
    )
    cmd.extend(["--control-file", str(RF_SENTINEL_CONTROL_PATH)])
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
        cwd=str(PROJECT_ROOT),
        env=env,
    )
    rf_sentinel_process = proc
    rf_sentinel_thread = threading.Thread(target=_rf_sentinel_loop, args=(proc,), daemon=True)
    rf_sentinel_thread.start()
    return {"engine": "rf_sentinel_scan", "command": cmd, "pid": proc.pid, "follow": control.get("follow", {})}


def _stop_rf_sentinel_engine(timeout_s: float = 4.0) -> None:
    global rf_sentinel_process, rf_sentinel_thread
    rf_sentinel_stop.set()
    proc = rf_sentinel_process
    rf_sentinel_process = None
    with state_lock:
        if proc is not None:
            _append_scanner_log("[ui] stopping rf_sentinel_scan")
    if proc is not None and proc.poll() is None:
        _terminate_process_group(proc, timeout_s=timeout_s)
    thread = rf_sentinel_thread
    rf_sentinel_thread = None
    if thread and thread.is_alive():
        thread.join(timeout=2.0)


@app.post("/api/scan/protocols")
def update_scan_protocols():
    payload = request.get_json(silent=True) or {}
    requested_protocols = payload.get("protocols")
    if not isinstance(requested_protocols, list):
        return _json_error(400, "update_scan_protocols", error="protocols must be a list")
    enabled_protocols = {str(item).strip().lower() for item in requested_protocols}
    enabled_protocols &= RF_SENTINEL_PROTOCOLS
    requested_devices = payload.get("devices")
    enabled_devices = None
    if isinstance(requested_devices, list):
        enabled_devices = {str(item).strip() for item in requested_devices if str(item).strip()}
    disabled_devices = set()
    devices_available = _available_devices()
    if enabled_devices is not None:
        known_devices = {str(item.get("id") or "").strip() for item in devices_available if str(item.get("id") or "").strip()}
        disabled_devices = known_devices - enabled_devices
    else:
        disabled_devices = set(_read_ui_config().get("disabled_devices", []))
    if "wifi" in enabled_protocols and not _has_wifi_device(devices_available, enabled_devices):
        enabled_protocols.discard("wifi")
    if "lfmf" in enabled_protocols and not _has_lfmf_device(devices_available, enabled_devices):
        enabled_protocols.discard("lfmf")
    _write_ui_config(enabled_protocols, disabled_devices)
    control = _write_rf_sentinel_control(
        enabled_protocols,
        enabled_devices=enabled_devices,
        zigbee_follow_channel=RF_SENTINEL_NO_CHANGE if "zigbee" in enabled_protocols else None,
    )
    follow_state = _follow_state_for_protocols(control, enabled_protocols)
    with state_lock:
        state.decoder_stats["enabled_protocols"] = sorted(enabled_protocols)
        state.decoder_stats["follow"] = follow_state
        _append_scanner_log(f"[ui] enabled protocols updated: {', '.join(sorted(enabled_protocols)) or 'none'}")
    return jsonify({"ok": True, "protocols": sorted(enabled_protocols)})


@app.post("/api/scan/follow")
def update_scan_follow():
    payload = request.get_json(silent=True) or {}
    protocol = str(payload.get("protocol") or "").strip().lower()
    if protocol != "zigbee":
        return _json_error(400, "update_scan_follow", error="only zigbee follow is supported right now")
    follow = bool(payload.get("follow", True))
    channel_value = payload.get("channel")
    channel: int | None
    if follow:
        try:
            channel = int(channel_value)
        except (TypeError, ValueError):
            return _json_error(400, "update_scan_follow", error="zigbee follow requires a numeric channel")
        if channel < 11 or channel > 26:
            return _json_error(400, "update_scan_follow", error="zigbee channel must be 11-26")
    else:
        channel = None
    control = _write_rf_sentinel_control(zigbee_follow_channel=channel)
    follow_state = control.get("follow") if isinstance(control.get("follow"), dict) else {}
    with state_lock:
        state.decoder_stats["follow"] = follow_state
        if channel is None:
            _append_scanner_log("[ui] zigbee follow cleared")
        else:
            _append_scanner_log(f"[ui] zigbee follow locked channel {channel}")
    return jsonify({"ok": True, "follow": follow_state})


@app.post("/api/fm/play")
def fm_play():
    global fm_pending_thread, fm_request_serial
    payload = request.get_json(silent=True) or {}
    freq_mhz = float(payload.get("freq_mhz", 0.0) or 0.0)
    device_id = str(payload.get("device_id") or "").strip() or _current_fm_scanner_device_id()
    if not 87.5 <= freq_mhz <= 108.0:
        return _json_error(400, "fm_play", error="freq_mhz must be between 87.5 and 108.0")
    try:
        fm_request_serial += 1
        request_serial = fm_request_serial
        fm_playback.pending = True
        fm_playback.pending_freq_mhz = float(freq_mhz)
        fm_playback.pending_device_id = device_id
        fm_playback.worker_error = "FM queued; waiting for SDR availability"
        if device_id and not _device_available(device_id):
            _pause_fm_scanner_for_playback()
            _force_release_gateway_device(device_id)
            _start_fm_pending_thread(request_serial, float(freq_mhz), device_id)
        else:
            try:
                _start_fm_playback_now(freq_mhz, device_id)
            except Exception as exc:
                if not _fm_busy_error(exc):
                    fm_playback.pending = False
                    _restore_fm_scanner_after_playback()
                    return _json_error(409, "fm_play", error=str(exc))
                _start_fm_pending_thread(request_serial, float(freq_mhz), device_id)
    except requests.RequestException as exc:
        return _json_error(503, "fm_play", error="sdr-gateway is unavailable", detail=str(exc))
    return jsonify({"ok": True, "fm_playback": _fm_playback_status_payload()})


@app.post("/api/fm/stop")
def fm_stop():
    _stop_fm_playback()
    return jsonify({"ok": True, "fm_playback": _fm_playback_status_payload()})


@app.get("/api/fm/audio/batch")
def fm_audio_batch():
    if not fm_playback.running:
        return Response(b"", mimetype="application/octet-stream", status=204)
    count = max(1, min(int(request.args.get("count", 6)), 16))
    timeout = max(0.05, min(float(request.args.get("timeout", 0.4)), 2.0))
    chunks: list[bytes] = []
    for idx in range(count):
        try:
            pcm = fm_audio_q.get(timeout=timeout if idx == 0 else 0.02)
        except queue.Empty:
            break
        chunks.append(pcm)
        fm_playback.served_chunks += 1
    if not chunks:
        fm_playback.empty_audio_polls += 1
        return Response(b"", mimetype="application/octet-stream", status=204)
    fm_playback.empty_audio_polls = 0
    return Response(b"".join(chunks), mimetype="application/octet-stream")


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
    state.scanner_log = []


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


def _device_matches(devices: list[dict[str, Any]], device_id: str, pattern: str) -> bool:
    pattern_l = pattern.lower()
    for dev in devices:
        dev_id = str(dev.get("id", ""))
        label = str(dev.get("label", ""))
        if dev_id != device_id:
            continue
        return pattern_l in f"{dev_id} {label}".lower()
    return pattern_l in device_id.lower()


def _fetch_gateway_devices() -> list[dict[str, Any]]:
    global devices_cache, devices_cache_updated_at
    resp = requests.get(
        f"{_gateway_base()}/devices",
        headers=_gateway_headers(),
        timeout=SDR_GATEWAY_DEVICES_TIMEOUT_SECONDS,
    )
    if resp.status_code >= 400:
        resp.raise_for_status()
    body = resp.json()
    devices = body if isinstance(body, list) else []
    devices = [dict(item) for item in devices if isinstance(item, dict)]
    devices.extend(_fetch_gateway_wifi_devices())
    with devices_cache_lock:
        devices_cache = devices
        devices_cache_updated_at = time.time()
    return devices


def _fetch_gateway_wifi_devices() -> list[dict[str, Any]]:
    try:
        resp = requests.get(
            f"{_gateway_base()}/wifi/interfaces",
            headers=_gateway_headers(),
            timeout=min(SDR_GATEWAY_DEVICES_TIMEOUT_SECONDS, 5.0),
        )
        if resp.status_code >= 400:
            return []
        body = resp.json()
    except (requests.RequestException, ValueError):
        return []
    if not isinstance(body, list):
        return []
    devices: list[dict[str, Any]] = []
    for item in body:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        frequency_mhz = item.get("frequency_mhz")
        channel = item.get("channel")
        details = []
        if channel:
            details.append(f"channel {channel}")
        if frequency_mhz:
            details.append(f"{frequency_mhz} MHz")
        if item.get("type"):
            details.append(str(item.get("type")))
        devices.append(
            {
                "id": name,
                "driver": "wifi",
                "label": f"WiFi interface {name}",
                "serial": item.get("mac"),
                "freq_min_hz": 2_400_000_000,
                "freq_max_hz": 5_900_000_000,
                "max_sample_rate_sps": 0,
                "notes": "802.11 monitor/capture source from sdr-gateway"
                + (f" ({', '.join(details)})" if details else ""),
                "occupied": False,
                "occupied_by": None,
                "occupied_id": None,
                "up": bool(item.get("up")),
                "channel": channel,
                "frequency_mhz": frequency_mhz,
            }
        )
    return devices


def _cached_gateway_devices() -> tuple[list[dict[str, Any]], float]:
    with devices_cache_lock:
        return [dict(item) for item in devices_cache], float(devices_cache_updated_at)


def _available_devices() -> list[dict[str, Any]]:
    try:
        return _fetch_gateway_devices()
    except requests.RequestException:
        cached, _ = _cached_gateway_devices()
        return cached


def _stop_scan(stop_gateway: bool = True) -> None:
    global worker_thread, worker_threads, worker_stops
    _stop_fm_playback()
    _stop_bredr_inquiry()
    _stop_rf_sentinel_engine()
    _stop_btcsniffer_engine()
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
        state.btc_engine = ""
        state.btc_engine_command = []
        state.btc_engine_log = ""
        state.worker_errors = {}
        state.gateway_start_response = None
        state.decoder_stats["follow"] = {}
        state.scanner_assignments = {}
        state.test_target = None
        state.test_target_error = ""


def _shutdown_gateway_device_ids() -> set[str]:
    device_ids = {str(item or "").strip() for item in state.device_ids.values()}
    for assignment in (state.scanner_assignments or {}).values():
        if isinstance(assignment, dict):
            device_ids.add(str(assignment.get("device_id") or "").strip())
    if fm_playback.device_id:
        device_ids.add(str(fm_playback.device_id).strip())
    return {device_id for device_id in device_ids if device_id}


def shutdown() -> None:
    global shutdown_complete
    with shutdown_lock:
        if shutdown_complete:
            return
        shutdown_complete = True
    device_ids = _shutdown_gateway_device_ids()
    try:
        _append_scanner_log("[ui] shutting down; releasing gateway sessions")
        _stop_scan(stop_gateway=True)
        for device_id in device_ids:
            _force_release_gateway_device(device_id)
    except Exception as exc:
        app.logger.warning("UI shutdown cleanup failed: %s", exc)


def _channel_freq(mode: str, channel: int) -> int:
    if mode in {"classic", "both"}:
        start_hz = BT_CLASSIC_CHANNELS.get(channel, BT_CLASSIC_CHANNELS[0])
        return int(start_hz + ((BT_CLASSIC_BANK_SIZE - 1) * BT_CLASSIC_LANE_SPACING_HZ / 2.0))
    return BLE_ADV_CHANNELS.get(channel, BLE_ADV_CHANNELS[37])


def _btc_bank_start_from_center(center_freq_hz: int, bandwidth_mhz: int = BT_CLASSIC_BANK_SIZE) -> int:
    center_mhz = float(center_freq_hz) / 1_000_000.0
    start = int(round(center_mhz - 2402.0 - ((float(bandwidth_mhz) - 1.0) / 2.0)))
    return max(0, min(78 - (bandwidth_mhz - 1), start))


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


def _retune_gateway_stream(
    stream_id: str,
    device_id: str,
    center_freq_hz: int,
    sample_rate_sps: int,
    lna_gain_db: int,
    vga_gain_db: int,
) -> tuple[dict[str, Any], int, int, int]:
    if not stream_id:
        raise RuntimeError("No gateway stream is available to retune")
    resp = requests.post(
        f"{_gateway_base()}/streams/{stream_id}/retune",
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


@app.get("/resources/<path:filename>")
def resources(filename: str):
    return send_from_directory(PROJECT_ROOT / "ui" / "resources", filename)


@app.get("/api/devices")
def devices():
    try:
        return jsonify(_fetch_gateway_devices())
    except requests.RequestException as exc:
        cached, updated_at = _cached_gateway_devices()
        if cached:
            response = jsonify(cached)
            response.headers["X-RF-Sentinel-Warning"] = "using cached SDR device list; sdr-gateway /devices request failed"
            response.headers["X-RF-Sentinel-Cache-Age"] = f"{max(0.0, time.time() - updated_at):.1f}"
            response.headers["X-RF-Sentinel-Gateway-Error"] = str(exc)[:300]
            return response
        return jsonify({"error": "sdr-gateway is unavailable", "detail": str(exc), "gateway_base": _gateway_base()}), 503


@app.get("/api/config")
def get_config():
    return jsonify(_read_ui_config())


@app.post("/api/config")
def update_config():
    payload = request.get_json(silent=True) or {}
    requested_protocols = payload.get("protocols")
    if not isinstance(requested_protocols, list):
        return _json_error(400, "update_config", error="protocols must be a list")
    protocols = {str(item).strip().lower() for item in requested_protocols} & RF_SENTINEL_PROTOCOLS
    requested_disabled = payload.get("disabled_devices")
    if not isinstance(requested_disabled, list):
        requested_disabled = []
    disabled_devices = {str(item).strip() for item in requested_disabled if str(item).strip()}
    devices_available = _available_devices()
    enabled_devices = _enabled_devices_from_disabled(devices_available, disabled_devices)
    if "wifi" in protocols and not _has_wifi_device(devices_available, enabled_devices):
        protocols.discard("wifi")
    if "lfmf" in protocols and not _has_lfmf_device(devices_available, enabled_devices):
        protocols.discard("lfmf")
    config = _write_ui_config(protocols, disabled_devices)
    control = _write_rf_sentinel_control(
        protocols,
        enabled_devices=enabled_devices,
        zigbee_follow_channel=RF_SENTINEL_NO_CHANGE if "zigbee" in protocols else None,
    )
    follow_state = _follow_state_for_protocols(control, protocols)
    with state_lock:
        state.decoder_stats["enabled_protocols"] = sorted(protocols)
        state.decoder_stats["follow"] = follow_state
        _append_scanner_log(f"[ui] config saved: {', '.join(sorted(protocols)) or 'none'}")
    return jsonify({"ok": True, **config})


@app.errorhandler(BadRequest)
def handle_bad_request(exc: BadRequest):
    payload = {"error": "bad request", "detail": str(exc)}
    _log_http_error(400, request.endpoint or "unknown", payload, exc)
    return jsonify(payload), 400


@app.post("/api/scan/start")
def start_scan():
    global worker_thread, worker_threads, worker_stops
    try:
        payload = request.get_json(force=True) or {}
    except BadRequest as exc:
        return _json_error(400, "start_scan", error="invalid JSON payload", detail=str(exc))
    device_id = _clean_device_id(payload.get("device_id", ""))
    btc_device_id = _clean_device_id(payload.get("btc_device_id", ""))
    raw_btle_device_id = payload.get("btle_device_id", "") or payload.get("hop_device_id", "")
    btle_device_id = _clean_device_id(raw_btle_device_id)
    explicit_btle_device = bool(_clean_device_id(raw_btle_device_id))
    mode = str(payload.get("mode", "classic")).strip().lower()
    channel = int(payload.get("channel", 37 if mode != "classic" else 0))
    btc_center_mhz = float(payload.get("btc_center_mhz", 2442.0))
    sample_rate_sps = int(payload.get("sample_rate_sps", 60_000_000 if mode in {"classic", "both"} else BLE_ADV_SAMPLE_RATE_SPS))
    lna_gain_db = int(payload.get("lna_gain_db", 24))
    vga_gain_db = int(payload.get("vga_gain_db", 28))
    btc_lna_gain_db = int(payload.get("btc_lna_gain_db", lna_gain_db))
    btc_vga_gain_db = int(payload.get("btc_vga_gain_db", vga_gain_db))
    btle_lna_gain_db = int(payload.get("btle_lna_gain_db", lna_gain_db))
    btle_vga_gain_db = int(payload.get("btle_vga_gain_db", vga_gain_db))
    btc_target_mac = str(payload.get("btc_target_mac", "")).strip()
    preserve_detections = bool(payload.get("preserve_detections", False))
    btc_engine = str(payload.get("btc_engine", BTC_ENGINE_DEFAULT) or BTC_ENGINE_DEFAULT).strip().lower()
    saved_config = _read_ui_config()
    requested_protocols = payload.get("protocols")
    if isinstance(requested_protocols, list):
        enabled_protocols = {str(item).strip().lower() for item in requested_protocols}
    else:
        enabled_protocols = {str(item).strip().lower() for item in saved_config.get("protocols", [])}
    enabled_protocols &= RF_SENTINEL_PROTOCOLS
    requested_devices = payload.get("devices")
    if isinstance(requested_devices, list):
        enabled_devices = {str(item).strip() for item in requested_devices if str(item).strip()}
    else:
        disabled_devices = {str(item).strip() for item in saved_config.get("disabled_devices", []) if str(item).strip()}
        enabled_devices = _enabled_devices_from_disabled(_available_devices(), disabled_devices)
    sweep_both_radios = bool(payload.get("sweep_both_radios", mode == "sentinel"))
    single_radio_bluetooth_requested = bool(payload.get("single_radio_bluetooth") or payload.get("bluetooth_single_radio"))

    if mode not in {"ble", "classic", "both", "sentinel"}:
        return _json_error(400, "start_scan", error="mode must be ble, classic, both, or sentinel")
    if mode == "sentinel" and not enabled_protocols:
        return _json_error(400, "start_scan", error="select at least one protocol")
    if btc_engine not in {"btcsniffer", "python"}:
        return _json_error(400, "start_scan", error="btc_engine must be btcsniffer or python")
    if mode == "ble" and channel not in BLE_ADV_CHANNELS:
        return _json_error(400, "start_scan", error="BLE channel must be 37, 38, or 39")
    if mode in {"classic", "both", "sentinel"}:
        sample_rate_sps = max(1_000_000, min(60_000_000, sample_rate_sps))
        btc_center_mhz = max(2402.0, min(2480.0, btc_center_mhz))

    devices_available = _available_devices()
    if "wifi" in enabled_protocols and not _has_wifi_device(devices_available, enabled_devices):
        enabled_protocols.discard("wifi")
    if "lfmf" in enabled_protocols and not _has_lfmf_device(devices_available, enabled_devices):
        enabled_protocols.discard("lfmf")
    if mode == "sentinel" and not enabled_protocols:
        return _json_error(400, "start_scan", error="select at least one available protocol")
    combined_bluetooth_protocols = "btc" in enabled_protocols and "ble" in enabled_protocols
    if mode in {"both", "sentinel"} and combined_bluetooth_protocols:
        combined_device_id = _pick_ism24_bluetooth_device(devices_available, enabled_devices)
        if combined_device_id:
            btc_device_id = combined_device_id
            other_sdr_protocols = enabled_protocols & {"zigbee", "tpms", "fm"}
            alternate_hop_device_id = _pick_non_bluetooth_hop_device(devices_available, combined_device_id, enabled_devices)
            btle_device_id = alternate_hop_device_id if mode == "sentinel" and other_sdr_protocols and alternate_hop_device_id else combined_device_id
            combined_rate_mhz = max(1, min(BT_CLASSIC_BANK_SIZE, _btc_max_bandwidth_mhz_for_device(combined_device_id)))
            device_meta = next((dev for dev in devices_available if str(dev.get("id") or "") == combined_device_id), None)
            if device_meta is not None:
                combined_rate_mhz = max(1, min(BT_CLASSIC_BANK_SIZE, _device_max_rate_mhz(device_meta)))
            sample_rate_sps = combined_rate_mhz * 1_000_000
            single_radio_bluetooth_requested = True
            _append_scanner_log(
                f"[ui] 2.4GHz ISM Bluetooth uses {combined_device_id} at {combined_rate_mhz} MHz "
                f"({'wideband' if combined_rate_mhz >= 60 else 'best available'})"
            )
            if alternate_hop_device_id and btle_device_id == alternate_hop_device_id:
                _append_scanner_log(f"[ui] non-Bluetooth SDR hopping uses {alternate_hop_device_id}")
            elif mode == "sentinel" and other_sdr_protocols:
                disabled = sorted(other_sdr_protocols)
                enabled_protocols -= other_sdr_protocols
                _append_scanner_log(
                    f"[ui] disabled {', '.join(disabled)} because no second SDR is available while BTC+BLE owns {combined_device_id}"
                )
    if mode in {"classic", "both", "sentinel"} and not btc_device_id:
        btc_device_id = _pick_device(devices_available, "bladerf")
    if mode in {"ble", "both", "sentinel"} and not btle_device_id:
        btle_device_id = _pick_device(devices_available, "hackrf", device_id or "sidekiq")
    if mode == "both" and btc_engine == "python" and btc_device_id and (single_radio_bluetooth_requested or not explicit_btle_device):
        btle_device_id = btc_device_id
    if mode == "classic" and not btc_device_id:
        return _json_error(400, "start_scan", error="btc_device_id is required")
    if mode == "ble" and not btle_device_id:
        return _json_error(400, "start_scan", error="btle_device_id is required")
    if mode == "both" and (not btc_device_id or not btle_device_id):
        return _json_error(400, "start_scan", error="both btc_device_id and btle_device_id are required")
    if mode == "sentinel" and (not btc_device_id or not btle_device_id):
        return _json_error(400, "start_scan", error="both btc_device_id and btle_device_id are required")

    btc_bandwidth_mhz = max(1, min(BT_CLASSIC_BANK_SIZE, int(round(sample_rate_sps / 1_000_000.0))))
    if mode in {"classic", "both", "sentinel"} and btc_device_id:
        btc_bandwidth_mhz = min(btc_bandwidth_mhz, _btc_max_bandwidth_mhz_for_device(btc_device_id))
        sample_rate_sps = btc_bandwidth_mhz * 1_000_000

    btc_center_freq_hz = int(round(btc_center_mhz * 1_000_000.0))
    btc_bank_start_channel = _btc_bank_start_from_center(btc_center_freq_hz, btc_bandwidth_mhz)
    center_freq_hz = btc_center_freq_hz if mode in {"classic", "both"} else _channel_freq(mode, channel)
    single_radio_bluetooth = mode == "both" and btc_engine == "python" and bool(btc_device_id) and btc_device_id == btle_device_id
    if state.running:
        _stop_scan()
    _start_csv_run()
    if btc_device_id:
        _stop_duplicate_gateway_streams(btc_device_id)
    if btle_device_id and btle_device_id != btc_device_id:
        _stop_duplicate_gateway_streams(btle_device_id)

    btc_test_target: dict[str, Any] | None = None
    btc_test_error = ""
    if mode in {"classic", "both"}:
        btc_test_target = _configured_btc_target(btc_target_mac)
        if btc_target_mac and btc_test_target is None:
            btc_test_error = "BTC target MAC is invalid; set BTC_TARGET_MAC if needed"
        _stop_bredr_inquiry()
    else:
        _stop_bredr_inquiry()

    if mode == "sentinel":
        try:
            scanner_body = _start_rf_sentinel_engine(
                btc_device_id=btc_device_id,
                hop_device_id=btle_device_id,
                btc_center_mhz=btc_center_mhz,
                btc_bandwidth_mhz=btc_bandwidth_mhz,
                btc_lna_gain_db=btc_lna_gain_db,
                btc_vga_gain_db=btc_vga_gain_db,
                hop_lna_gain_db=btle_lna_gain_db,
                hop_vga_gain_db=btle_vga_gain_db,
                enabled_protocols=enabled_protocols,
                enabled_devices=enabled_devices,
                sweep_both_radios=sweep_both_radios,
            )
        except RuntimeError as exc:
            return _json_error(400, "start_scan", error="scan start failed", detail=str(exc))
        with state_lock:
            if preserve_detections:
                _reset_live_stats_keep_discoveries()
            else:
                _reset_stats()
            state.running = True
            state.mode = "sentinel"
            state.stream_id = None
            state.stream_ids = {}
            state.device_id = btc_device_id
            state.device_ids = {"classic": btc_device_id, "hop": btle_device_id, "radio_a": btc_device_id, "radio_b": btle_device_id}
            state.scanner_assignments = {}
            state.center_freq_hz = btc_center_freq_hz
            state.sample_rate_sps = btc_bandwidth_mhz * 1_000_000
            state.lna_gain_db = btc_lna_gain_db
            state.vga_gain_db = btc_vga_gain_db
            state.channel = btc_bank_start_channel
            state.channels_by_mode = {"classic": btc_bank_start_channel}
            state.gateway_start_response = {"scanner": scanner_body}
            state.btc_engine = "rf_sentinel_scan"
            state.btc_engine_command = list(scanner_body.get("command", []))
            state.btc_engine_log = ""
            _append_scanner_log(f"[ui] started {' '.join(state.btc_engine_command)}")
            _append_scanner_log("[ui] RF Sentinel scanner mode active")
            state.worker_error = ""
            state.worker_errors = {}
            state.worker_alive = True
            state.worker_alive_by_mode = {"scanner": True}
            state.test_target = btc_test_target
            state.test_target_error = btc_test_error
            state.decoder_stats["enabled_protocols"] = sorted(enabled_protocols)
            state.decoder_stats["sweep_both_radios"] = bool(sweep_both_radios)
            control = _read_rf_sentinel_control()
            state.decoder_stats["follow"] = _follow_state_for_protocols(control, enabled_protocols)
        return jsonify(
            {
                "ok": True,
                "mode": "sentinel",
                "scanner": scanner_body,
                "devices": {"classic": btc_device_id, "hop": btle_device_id, "radio_a": btc_device_id, "radio_b": btle_device_id},
                "test_target": btc_test_target,
                "test_target_error": btc_test_error,
            }
        )

    try:
        started: dict[str, dict[str, Any]] = {}
        if single_radio_bluetooth:
            body, actual_rate, actual_lna, actual_vga = _start_gateway_stream(
                btc_device_id,
                center_freq_hz,
                sample_rate_sps,
                btc_lna_gain_db,
                btc_vga_gain_db,
            )
            started["both"] = {
                "engine": "python-combined",
                "body": body,
                "stream_id": body["stream_id"],
                "device_id": btc_device_id,
                "center_freq_hz": center_freq_hz,
                "sample_rate_sps": actual_rate,
                "lna_gain_db": actual_lna,
                "vga_gain_db": actual_vga,
                "channel": btc_bank_start_channel,
            }
        elif mode in {"classic", "both"}:
            if btc_engine == "btcsniffer":
                started["classic"] = _start_btcsniffer_engine(
                    btc_device_id,
                    center_freq_hz,
                    btc_bandwidth_mhz,
                    btc_bank_start_channel,
                )
            else:
                body, actual_rate, actual_lna, actual_vga = _start_gateway_stream(
                    btc_device_id,
                    center_freq_hz,
                    sample_rate_sps,
                    btc_lna_gain_db,
                    btc_vga_gain_db,
                )
                started["classic"] = {
                    "engine": "python",
                    "body": body,
                    "stream_id": body["stream_id"],
                    "device_id": btc_device_id,
                    "center_freq_hz": center_freq_hz,
                    "sample_rate_sps": actual_rate,
                    "lna_gain_db": actual_lna,
                    "vga_gain_db": actual_vga,
                    "channel": btc_bank_start_channel,
                }
        if mode in {"ble", "both"} and not single_radio_bluetooth:
            ble_channel = int(payload.get("ble_channel", 37))
            ble_center = BLE_ADV_CHANNELS.get(ble_channel, BLE_ADV_CHANNELS[37])
            body, actual_rate, actual_lna, actual_vga = _start_gateway_stream(
                btle_device_id,
                ble_center,
                BLE_ADV_SAMPLE_RATE_SPS,
                btle_lna_gain_db,
                btle_vga_gain_db,
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
        _stop_btcsniffer_engine()
        return jsonify({"error": "sdr-gateway is unavailable", "detail": str(exc), "gateway_base": _gateway_base()}), 503
    except RuntimeError as exc:
        _stop_btcsniffer_engine()
        return _json_error(400, "start_scan", error="scan start failed", detail=str(exc))

    worker_stop.clear()
    with state_lock:
        if preserve_detections:
            _reset_live_stats_keep_discoveries()
        else:
            _reset_stats()
        state.running = True
        state.mode = mode
        primary = started.get("classic") or started.get("ble") or started.get("both")
        state.stream_id = primary["stream_id"] if primary else None
        if "both" in started:
            state.stream_ids = {
                "both": started["both"]["stream_id"],
                "classic": started["both"]["stream_id"],
                "ble": started["both"]["stream_id"],
            }
        else:
            state.stream_ids = {key: value["stream_id"] for key, value in started.items()}
        state.device_id = primary["device_id"] if primary else None
        if "both" in started:
            state.device_ids = {
                "both": started["both"]["device_id"],
                "classic": started["both"]["device_id"],
                "ble": started["both"]["device_id"],
            }
        else:
            state.device_ids = {key: value["device_id"] for key, value in started.items()}
        state.center_freq_hz = int(primary["center_freq_hz"]) if primary else center_freq_hz
        state.sample_rate_sps = int(primary["sample_rate_sps"]) if primary else sample_rate_sps
        state.lna_gain_db = int(primary["lna_gain_db"]) if primary else lna_gain_db
        state.vga_gain_db = int(primary["vga_gain_db"]) if primary else vga_gain_db
        state.channel = btc_bank_start_channel if mode in {"classic", "both"} else channel
        if "both" in started:
            state.channels_by_mode = {
                "both": btc_bank_start_channel,
                "classic": btc_bank_start_channel,
                "ble": 0,
            }
        else:
            state.channels_by_mode = {key: int(value["channel"]) for key, value in started.items()}
        state.gateway_start_response = {key: value["body"] for key, value in started.items()}
        state.btc_engine = str((started.get("classic") or started.get("both") or {}).get("engine", "")) if mode in {"classic", "both"} else ""
        state.btc_engine_command = list(started.get("classic", {}).get("body", {}).get("command", []))
        state.btc_engine_log = str(started.get("classic", {}).get("body", {}).get("log", ""))
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
        if cfg.get("engine") == "btcsniffer":
            continue
        stop = threading.Event()
        worker_stops[protocol] = stop
        worker_mode = "both" if protocol == "both" else ("classic" if protocol == "classic" else "ble")
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
    devices = _available_devices()
    ui_config = _read_ui_config()
    with state_lock:
        enabled_protocols = {str(item).lower() for item in state.decoder_stats.get("enabled_protocols", ui_config.get("protocols", []))}
        follow_target = _follow_state_for_protocols({"follow": state.decoder_stats.get("follow", {})}, enabled_protocols)
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
                "decoder_stats": {**state.decoder_stats, "follow": follow_target},
                "follow_target": follow_target,
                "test_target": state.test_target,
                "test_target_error": state.test_target_error,
                "btc_engine": state.btc_engine,
                "btc_engine_command": state.btc_engine_command,
                "btc_engine_log": state.btc_engine_log,
                "scanner_log": state.scanner_log[-160:],
                "scanner_assignments": state.scanner_assignments,
                "csv_run_id": state.csv_run_id,
                "csv_log_dir": state.csv_log_dir,
                "ui_config": ui_config,
                "fm_playback": _fm_playback_status_payload(),
                "available_devices": devices,
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
    try:
        app.run(host=host, port=port, threaded=True)
    except KeyboardInterrupt:
        print("\n[ui] Ctrl+C received, disconnecting from sdr-gateway...", file=sys.stderr)
    finally:
        shutdown()
