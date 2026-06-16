from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import requests
import websocket


DEFAULT_GATEWAY_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_CENTER_FREQ_HZ = 751_000_000
DEFAULT_SAMPLE_RATE_SPS = 20_000_000
DEFAULT_BANDWIDTH_HZ = 20_000_000


@dataclass(frozen=True)
class CellularBand:
    name: str
    downlink_start_hz: int
    downlink_stop_hz: int
    uplink_start_hz: int | None = None
    uplink_stop_hz: int | None = None
    notes: str = ""
    technology: str = "Cellular"
    cellular_type: str = "Cellular"
    likely_operator: str = ""
    operator_confidence: str = ""
    likely_mcc: str = ""
    likely_mnc: str = ""
    likely_plmn: str = ""


@dataclass
class Detection:
    seen_at: float
    device_id: str
    center_freq_hz: int
    frequency_hz: int
    frequency_mhz: float
    offset_hz: int
    power_dbfs: float
    noise_floor_dbfs: float
    excess_db: float
    occupied_width_hz: int
    band: str
    link: str
    cellular_type: str
    technology: str
    likely_operator: str
    operator_confidence: str
    likely_mcc: str
    likely_mnc: str
    likely_plmn: str
    plmn_source: str
    decoded_mcc: str
    decoded_mnc: str
    decoded_plmn: str
    classification: str
    notes: str
    lte_sync_status: str = "not_attempted"
    lte_pss_detected: bool = False
    lte_n_id_2: int | None = None
    lte_pss_metric: float = 0.0
    lte_cell_id_status: str = "requires_sss"
    lte_mib_status: str = "requires_pbch"
    lte_sib1_status: str = "requires_mib_and_pdsch"
    decoded_plmn_source: str = ""
    target: bool = False


CELLULAR_BANDS: tuple[CellularBand, ...] = (
    CellularBand("3GPP Band 71 / 600 MHz", 617_000_000, 652_000_000, 663_000_000, 698_000_000, "LTE/5G low-band", "LTE/NR", "Low-band cellular", "T-Mobile US", "likely in US", "310", "260", "310-260"),
    CellularBand("3GPP Band 12 / 700 MHz Lower", 729_000_000, 746_000_000, 699_000_000, 716_000_000, "LTE low-band", "LTE", "Low-band cellular", "AT&T / regional 700 MHz licensees", "possible in US", "310", "410", "310-410"),
    CellularBand("3GPP Band 13 / 700 MHz Upper C", 746_000_000, 756_000_000, 777_000_000, 787_000_000, "LTE low-band; 751 MHz sits here", "LTE", "Low-band cellular", "Verizon Wireless", "likely in US", "311", "480", "311-480"),
    CellularBand("3GPP Band 14 / FirstNet", 758_000_000, 768_000_000, 788_000_000, 798_000_000, "Public-safety LTE", "LTE", "Public-safety broadband", "FirstNet / AT&T", "likely in US", "313", "100", "313-100"),
    CellularBand("3GPP Band 5 / 850 MHz Cellular", 869_000_000, 894_000_000, 824_000_000, 849_000_000, "Cellular 850", "LTE/NR", "Cellular 850", "AT&T / Verizon / regional cellular licensees", "possible in US", "", "", ""),
    CellularBand("3GPP Band 2 / PCS 1900", 1_930_000_000, 1_990_000_000, 1_850_000_000, 1_910_000_000, "PCS LTE/NR", "LTE/NR", "PCS cellular", "US mobile operator", "ambiguous", "", "", ""),
    CellularBand("3GPP Band 4 / AWS-1", 2_110_000_000, 2_155_000_000, 1_710_000_000, 1_755_000_000, "AWS LTE/NR", "LTE/NR", "AWS cellular", "US mobile operator", "ambiguous", "", "", ""),
    CellularBand("3GPP Band 66 / AWS-3", 2_110_000_000, 2_200_000_000, 1_710_000_000, 1_780_000_000, "AWS LTE/NR", "LTE/NR", "AWS cellular", "US mobile operator", "ambiguous", "", "", ""),
    CellularBand("3GPP Band 7 / 2600 MHz", 2_620_000_000, 2_690_000_000, 2_500_000_000, 2_570_000_000, "LTE/NR", "LTE/NR", "Mid-band cellular", "mobile operator", "ambiguous"),
    CellularBand("3GPP Band 41 / 2.5 GHz TDD", 2_496_000_000, 2_690_000_000, None, None, "TDD LTE/NR", "LTE/NR TDD", "2.5 GHz TDD cellular", "T-Mobile US / 2.5 GHz licensee", "likely in US"),
    CellularBand("3GPP n77 / C-band", 3_300_000_000, 4_200_000_000, None, None, "5G NR TDD", "5G NR", "C-band 5G", "5G C-band operator", "ambiguous"),
    CellularBand("3GPP n78 / C-band", 3_300_000_000, 3_800_000_000, None, None, "5G NR TDD", "5G NR", "C-band 5G", "5G C-band operator", "ambiguous"),
    CellularBand("3GPP n79 / 4.7 GHz", 4_400_000_000, 5_000_000_000, None, None, "5G NR TDD", "5G NR", "4.7 GHz 5G", "5G operator", "ambiguous"),
)

LTE_SAMPLE_RATES_SPS = (1_920_000, 3_840_000, 7_680_000, 15_360_000, 30_720_000)
LTE_PSS_ROOTS = (25, 29, 34)
LTE_DECODE_LADDER = "PSS -> SSS/PCI -> PBCH/MIB -> PDCCH/PDSCH SIB1 -> decoded MCC/MNC"


class GatewayStream:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.base_url = str(args.gateway_base_url or os.getenv("SDR_GATEWAY_BASE_URL") or DEFAULT_GATEWAY_BASE_URL).rstrip("/")
        self.token = str(args.gateway_token or os.getenv("SDR_GATEWAY_API_TOKEN") or "").strip()
        self.session = requests.Session()
        self.stream_id = ""
        self.ws: websocket.WebSocket | None = None

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def start(self) -> None:
        payload = {
            "device_id": str(self.args.device_id),
            "center_freq_hz": int(self.args.center_freq_hz),
            "sample_rate_sps": int(self.args.sample_rate_sps),
            "lna_gain_db": int(self.args.lna_gain_db),
            "vga_gain_db": int(self.args.vga_gain_db),
            "amp_enable": bool(self.args.amp_enable),
            "baseband_filter_hz": int(self.args.bandwidth_hz),
            "duration_seconds": None,
            "num_samples": None,
        }
        resp = self.session.post(f"{self.base_url}/streams/start", headers=self.headers(), json=payload, timeout=15)
        if resp.status_code == 409 and bool(self.args.replace_existing):
            self._stop_existing_for_device(str(self.args.device_id))
            resp = self.session.post(f"{self.base_url}/streams/start", headers=self.headers(), json=payload, timeout=15)
        resp.raise_for_status()
        self.stream_id = str(resp.json()["stream_id"])
        self.ws = websocket.WebSocket()
        self.ws.connect(self._ws_url(), timeout=8, header=[f"Authorization: Bearer {self.token}"] if self.token else None)

    def capture_iq(self, seconds: float) -> np.ndarray:
        target_bytes = max(2048, int(float(self.args.sample_rate_sps) * float(seconds)) * 2)
        deadline = time.monotonic() + max(2.0, float(seconds) + 2.0)
        chunks: list[bytes] = []
        captured = 0
        ws = self.ws
        if ws is None:
            return np.empty(0, dtype=np.complex64)
        while captured < target_bytes and time.monotonic() < deadline:
            message = ws.recv()
            if isinstance(message, str) or not message:
                continue
            chunk = bytes(message)
            chunks.append(chunk)
            captured += len(chunk)
        if not chunks:
            return np.empty(0, dtype=np.complex64)
        return _i8_to_complex(b"".join(chunks)[:target_bytes])

    def close(self) -> None:
        ws = self.ws
        self.ws = None
        if ws is not None:
            with contextlib.suppress(Exception):
                ws.close()
        if self.stream_id:
            with contextlib.suppress(Exception):
                self.session.post(f"{self.base_url}/streams/{self.stream_id}/stop", headers=self.headers(), timeout=5)
            self.stream_id = ""

    def _ws_url(self) -> str:
        if self.base_url.startswith("https://"):
            root = "wss://" + self.base_url[len("https://") :]
        elif self.base_url.startswith("http://"):
            root = "ws://" + self.base_url[len("http://") :]
        else:
            root = "ws://" + self.base_url
        token = requests.utils.quote(self.token, safe="") if self.token else ""
        suffix = f"?keep=1&start=oldest&token={token}" if token else "?keep=1&start=oldest"
        return f"{root}/ws/iq/{self.stream_id}{suffix}"

    def _stop_existing_for_device(self, device_id: str) -> None:
        with contextlib.suppress(Exception):
            resp = self.session.get(f"{self.base_url}/streams", headers=self.headers(), timeout=5)
            resp.raise_for_status()
            streams = resp.json()
            if not isinstance(streams, list):
                return
            for item in streams:
                if not isinstance(item, dict):
                    continue
                config = item.get("config") or {}
                if str(config.get("device_id") or "") != device_id:
                    continue
                stream_id = str(item.get("stream_id") or "")
                if stream_id:
                    self.session.post(f"{self.base_url}/streams/{stream_id}/stop", headers=self.headers(), timeout=3)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                check = self.session.get(f"{self.base_url}/streams", headers=self.headers(), timeout=3)
                check.raise_for_status()
                active = False
                for item in check.json():
                    if not isinstance(item, dict):
                        continue
                    config = item.get("config") or {}
                    if str(config.get("device_id") or "") == device_id:
                        active = True
                        break
                if not active:
                    return
                time.sleep(0.1)


def _i8_to_complex(payload: bytes) -> np.ndarray:
    raw = np.frombuffer(payload, dtype=np.int8)
    if raw.size < 2:
        return np.empty(0, dtype=np.complex64)
    usable = raw.size - (raw.size % 2)
    values = raw[:usable].astype(np.float32) / 128.0
    return (values[0::2] + 1j * values[1::2]).astype(np.complex64, copy=False)


def _analyze_iq(args: argparse.Namespace, iq: np.ndarray) -> list[Detection]:
    samples = np.asarray(iq, dtype=np.complex64)
    if samples.size < 4096:
        return []
    nfft = min(int(args.fft_size), 1 << int(np.floor(np.log2(samples.size))))
    nfft = max(4096, nfft)
    segments = []
    step = nfft // 2
    window = np.hanning(nfft).astype(np.float32)
    for start in range(0, max(1, samples.size - nfft + 1), step):
        chunk = samples[start : start + nfft]
        if chunk.size < nfft:
            break
        spectrum = np.fft.fftshift(np.fft.fft((chunk - np.mean(chunk)) * window))
        segments.append(20.0 * np.log10(np.abs(spectrum) / float(nfft) + 1e-12))
        if len(segments) >= int(args.max_fft_segments):
            break
    if not segments:
        return []
    power_db = np.median(np.vstack(segments), axis=0)
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, d=1.0 / float(args.sample_rate_sps)))
    noise_floor = float(np.median(power_db))
    threshold = noise_floor + float(args.active_threshold_db)
    mask = power_db >= threshold
    detections: list[Detection] = []
    for start, stop in _mask_regions(mask):
        if stop <= start:
            continue
        region_power = power_db[start : stop + 1]
        region_freqs = freqs[start : stop + 1]
        peak_idx = int(np.argmax(region_power))
        peak_offset = int(round(float(region_freqs[peak_idx])))
        frequency_hz = int(args.center_freq_hz) + peak_offset
        occupied_width = int(round(float(region_freqs[-1] - region_freqs[0])))
        if occupied_width < int(args.min_occupied_width_hz):
            continue
        peak_power = float(region_power[peak_idx])
        band = _label_cellular_band(frequency_hz)
        detections.append(
            Detection(
                seen_at=time.time(),
                device_id=str(args.device_id),
                center_freq_hz=int(args.center_freq_hz),
                frequency_hz=frequency_hz,
                frequency_mhz=round(frequency_hz / 1_000_000.0, 6),
                offset_hz=peak_offset,
                power_dbfs=round(peak_power, 1),
                noise_floor_dbfs=round(noise_floor, 1),
                excess_db=round(peak_power - noise_floor, 1),
                occupied_width_hz=max(0, occupied_width),
                band=band.name,
                link=band.link,
                cellular_type=band.cellular_type,
                technology=band.technology,
                likely_operator=band.likely_operator,
                operator_confidence=band.operator_confidence,
                likely_mcc=band.likely_mcc,
                likely_mnc=band.likely_mnc,
                likely_plmn=band.likely_plmn,
                plmn_source="spectrum_allocation_hint" if band.likely_plmn else "",
                decoded_mcc="",
                decoded_mnc="",
                decoded_plmn="",
                classification="Passive cellular spectrum activity",
                notes=band.notes,
            )
        )
    detections.sort(key=lambda row: row.excess_db, reverse=True)
    if not detections and bool(args.include_candidates):
        detections = _top_spectral_candidates(args, freqs, power_db, noise_floor)
    if bool(args.target_report):
        target = _target_frequency_detection(args, freqs, power_db, noise_floor)
        if target is not None:
            guard_hz = max(int(args.target_width_hz), int(args.candidate_guard_hz))
            detections = [row for row in detections if abs(row.frequency_hz - target.frequency_hz) > guard_hz]
            detections.insert(0, target)
    _annotate_lte_sync(args, samples, detections)
    return detections[: int(args.top)]


def _top_spectral_candidates(args: argparse.Namespace, freqs: np.ndarray, power_db: np.ndarray, noise_floor: float) -> list[Detection]:
    if freqs.size < 3 or power_db.size < 3:
        return []
    smooth_bins = max(3, int(args.candidate_smooth_bins))
    kernel = np.ones(smooth_bins, dtype=np.float32) / float(smooth_bins)
    smoothed = np.convolve(power_db, kernel, mode="same")
    order = np.argsort(smoothed)[::-1]
    rows: list[Detection] = []
    used_offsets: list[int] = []
    bin_width_hz = abs(float(freqs[1] - freqs[0])) if freqs.size > 1 else 0.0
    guard_hz = max(float(args.candidate_guard_hz), bin_width_hz * 8.0)
    for idx in order:
        offset_hz = int(round(float(freqs[int(idx)])))
        if any(abs(offset_hz - used) < guard_hz for used in used_offsets):
            continue
        power = float(smoothed[int(idx)])
        excess = power - noise_floor
        if excess < float(args.candidate_threshold_db):
            break
        frequency_hz = int(args.center_freq_hz) + offset_hz
        band = _label_cellular_band(frequency_hz)
        used_offsets.append(offset_hz)
        rows.append(
            Detection(
                seen_at=time.time(),
                device_id=str(args.device_id),
                center_freq_hz=int(args.center_freq_hz),
                frequency_hz=frequency_hz,
                frequency_mhz=round(frequency_hz / 1_000_000.0, 6),
                offset_hz=offset_hz,
                power_dbfs=round(power, 1),
                noise_floor_dbfs=round(noise_floor, 1),
                excess_db=round(excess, 1),
                occupied_width_hz=int(round(max(bin_width_hz, float(args.min_occupied_width_hz)))),
                band=band.name,
                link=band.link,
                cellular_type=band.cellular_type,
                technology=band.technology,
                likely_operator=band.likely_operator,
                operator_confidence=band.operator_confidence,
                likely_mcc=band.likely_mcc,
                likely_mnc=band.likely_mnc,
                likely_plmn=band.likely_plmn,
                plmn_source="spectrum_allocation_hint" if band.likely_plmn else "",
                decoded_mcc="",
                decoded_mnc="",
                decoded_plmn="",
                classification="Passive cellular spectrum candidate",
                notes=band.notes,
            )
        )
        if len(rows) >= int(args.top):
            break
    return rows


def _target_frequency_detection(args: argparse.Namespace, freqs: np.ndarray, power_db: np.ndarray, noise_floor: float) -> Detection | None:
    if freqs.size < 3 or power_db.size < 3:
        return None
    target_freq_hz = int(args.target_freq_hz)
    center_hz = int(args.center_freq_hz)
    sample_rate = int(args.sample_rate_sps)
    if target_freq_hz < center_hz - sample_rate // 2 or target_freq_hz > center_hz + sample_rate // 2:
        return None
    target_offset = target_freq_hz - center_hz
    half_width = max(1, int(args.target_width_hz) // 2)
    mask = np.abs(freqs - float(target_offset)) <= float(half_width)
    if not np.any(mask):
        return None
    region_power = power_db[mask]
    region_freqs = freqs[mask]
    peak_idx = int(np.argmax(region_power))
    peak_offset = int(round(float(region_freqs[peak_idx])))
    peak_freq_hz = center_hz + peak_offset
    peak_power = float(region_power[peak_idx])
    excess = peak_power - noise_floor
    if excess < float(args.target_threshold_db):
        return None
    band = _label_cellular_band(target_freq_hz)
    notes = band.notes
    if notes:
        notes = f"{notes}; target={target_freq_hz / 1_000_000.0:.3f} MHz"
    else:
        notes = f"target={target_freq_hz / 1_000_000.0:.3f} MHz"
    return Detection(
        seen_at=time.time(),
        device_id=str(args.device_id),
        center_freq_hz=center_hz,
        frequency_hz=peak_freq_hz,
        frequency_mhz=round(peak_freq_hz / 1_000_000.0, 6),
        offset_hz=peak_offset,
        power_dbfs=round(peak_power, 1),
        noise_floor_dbfs=round(noise_floor, 1),
        excess_db=round(excess, 1),
        occupied_width_hz=int(args.target_width_hz),
        band=band.name,
        link=band.link,
        cellular_type=band.cellular_type,
        technology=band.technology,
        likely_operator=band.likely_operator,
        operator_confidence=band.operator_confidence,
        likely_mcc=band.likely_mcc,
        likely_mnc=band.likely_mnc,
        likely_plmn=band.likely_plmn,
        plmn_source="spectrum_allocation_hint" if band.likely_plmn else "",
        decoded_mcc="",
        decoded_mnc="",
        decoded_plmn="",
        classification="Passive cellular target-frequency activity",
        notes=notes,
        target=True,
    )


def _annotate_lte_sync(args: argparse.Namespace, samples: np.ndarray, rows: list[Detection]) -> None:
    if not bool(getattr(args, "lte_sync", True)):
        for row in rows:
            row.lte_sync_status = "disabled"
        return
    if samples.size < 4096:
        for row in rows:
            row.lte_sync_status = "insufficient_iq"
        return
    for row in rows:
        technology = f"{row.technology} {row.band}".lower()
        if "lte" not in technology:
            row.lte_sync_status = "not_lte_band"
            continue
        probe_frequency_hz = int(getattr(args, "target_freq_hz", row.frequency_hz)) if row.target else int(row.frequency_hz)
        offset_hz = int(probe_frequency_hz - int(args.center_freq_hz))
        result = _lte_pss_probe(
            samples=samples,
            sample_rate_sps=int(args.sample_rate_sps),
            offset_hz=offset_hz,
            threshold=float(args.lte_pss_threshold),
            max_segments=int(args.lte_sync_max_segments),
        )
        row.lte_sync_status = result["status"]
        row.lte_pss_detected = bool(result["detected"])
        row.lte_n_id_2 = result["n_id_2"]
        row.lte_pss_metric = round(float(result["metric"]), 3)
        row.lte_cell_id_status = "requires_sss_for_full_pci" if row.lte_pss_detected else "requires_pss"
        row.lte_mib_status = "requires_pbch_after_pss_sss" if row.lte_pss_detected else "requires_sync"
        row.lte_sib1_status = "requires_mib_and_pdsch_for_plmn" if row.lte_pss_detected else "requires_sync"
        row.decoded_plmn_source = "not_decoded_sib1_required"
        if row.lte_pss_detected:
            row.classification = "Passive LTE synchronization signal candidate"
            ladder = f"LTE PSS N_id_2={row.lte_n_id_2} metric={row.lte_pss_metric:.3f}; {LTE_DECODE_LADDER}"
            row.notes = f"{row.notes}; {ladder}" if row.notes else ladder


def _lte_pss_probe(
    *,
    samples: np.ndarray,
    sample_rate_sps: int,
    offset_hz: int,
    threshold: float,
    max_segments: int,
) -> dict[str, Any]:
    sample_rate = float(sample_rate_sps)
    if abs(float(offset_hz)) > sample_rate / 2.0 - 600_000.0:
        return {"status": "outside_passband", "detected": False, "n_id_2": None, "metric": 0.0}
    nfft = int(round(sample_rate / 15_000.0))
    if nfft < 128:
        return {"status": "sample_rate_too_low_for_lte_pss", "detected": False, "n_id_2": None, "metric": 0.0}
    nfft = min(nfft, int(samples.size))
    if nfft < 128:
        return {"status": "insufficient_iq", "detected": False, "n_id_2": None, "metric": 0.0}

    bin_hz = sample_rate / float(nfft)
    center_bin = nfft // 2 + int(round(float(offset_hz) / bin_hz))
    subcarrier_step = max(1, int(round(15_000.0 / bin_hz)))
    rel_bins = np.concatenate((np.arange(-31, 0), np.arange(1, 32))) * subcarrier_step
    bins = center_bin + rel_bins
    if int(np.min(bins)) < 0 or int(np.max(bins)) >= nfft:
        return {"status": "pss_bins_outside_fft", "detected": False, "n_id_2": None, "metric": 0.0}

    seqs = [_lte_pss_sequence(n_id_2) for n_id_2 in range(3)]
    window = np.hanning(nfft).astype(np.float32)
    step = max(1, nfft // 4)
    best_metric = 0.0
    best_n_id_2: int | None = None
    segments = 0
    for start in range(0, max(1, samples.size - nfft + 1), step):
        chunk = samples[start : start + nfft]
        if chunk.size < nfft:
            break
        spectrum = np.fft.fftshift(np.fft.fft((chunk - np.mean(chunk)) * window))
        vector = spectrum[bins]
        energy = np.abs(vector)
        if not np.all(np.isfinite(energy)) or float(np.mean(energy)) <= 1e-12:
            continue
        # Equalize amplitude roughly so the correlation is driven by PSS phase structure.
        vector = vector / np.maximum(energy, 1e-12)
        for n_id_2, seq in enumerate(seqs):
            candidate = float(abs(np.vdot(seq, vector)) / float(seq.size))
            if candidate > best_metric:
                best_metric = candidate
                best_n_id_2 = int(n_id_2)
        segments += 1
        if segments >= max(1, max_segments):
            break

    status = "pss_detected" if best_metric >= threshold else "pss_not_detected"
    if not _is_lte_standard_sample_rate(sample_rate_sps):
        status = f"{status}_nonstandard_sample_rate"
    return {"status": status, "detected": best_metric >= threshold, "n_id_2": best_n_id_2, "metric": best_metric}


def _lte_pss_sequence(n_id_2: int) -> np.ndarray:
    root = LTE_PSS_ROOTS[int(n_id_2)]
    values: list[complex] = []
    for n in range(31):
        values.append(complex(np.exp(-1j * np.pi * root * n * (n + 1) / 63.0)))
    for n in range(31, 62):
        values.append(complex(np.exp(-1j * np.pi * root * (n + 1) * (n + 2) / 63.0)))
    return np.asarray(values, dtype=np.complex64)


def _is_lte_standard_sample_rate(sample_rate_sps: int) -> bool:
    sample_rate = float(sample_rate_sps)
    return any(abs(sample_rate - float(rate)) / float(rate) < 0.01 for rate in LTE_SAMPLE_RATES_SPS)


def _mask_regions(mask: np.ndarray) -> list[tuple[int, int]]:
    regions: list[tuple[int, int]] = []
    start: int | None = None
    for idx, active in enumerate(mask):
        if active and start is None:
            start = idx
        elif not active and start is not None:
            regions.append((start, idx - 1))
            start = None
    if start is not None:
        regions.append((start, len(mask) - 1))
    return regions


@dataclass(frozen=True)
class CellularBandLabel:
    name: str
    link: str
    notes: str
    technology: str
    cellular_type: str
    likely_operator: str
    operator_confidence: str
    likely_mcc: str
    likely_mnc: str
    likely_plmn: str


def _label_cellular_band(freq_hz: int) -> CellularBandLabel:
    for band in CELLULAR_BANDS:
        if band.downlink_start_hz <= freq_hz <= band.downlink_stop_hz:
            return CellularBandLabel(
                band.name,
                "downlink",
                band.notes,
                band.technology,
                band.cellular_type,
                band.likely_operator,
                band.operator_confidence,
                band.likely_mcc,
                band.likely_mnc,
                band.likely_plmn,
            )
        if band.uplink_start_hz is not None and band.uplink_stop_hz is not None and band.uplink_start_hz <= freq_hz <= band.uplink_stop_hz:
            return CellularBandLabel(
                band.name,
                "uplink",
                band.notes,
                band.technology,
                band.cellular_type,
                band.likely_operator,
                band.operator_confidence,
                band.likely_mcc,
                band.likely_mnc,
                band.likely_plmn,
            )
    return CellularBandLabel(
        "Cellular-adjacent / unknown licensed band",
        "unknown",
        "Outside built-in band table; treat as spectrum awareness only.",
        "Unknown cellular",
        "Unknown licensed-band signal",
        "",
        "",
        "",
        "",
        "",
    )


def _run_scan(args: argparse.Namespace) -> int:
    stream = GatewayStream(args)
    try:
        stream.start()
        iq = stream.capture_iq(float(args.dwell_s))
    finally:
        stream.close()
    detections = _analyze_iq(args, iq)
    _print_detections(args, detections)
    return 0


def _print_detections(args: argparse.Namespace, rows: list[Detection]) -> None:
    if args.jsonl:
        for row in rows:
            payload = asdict(row)
            payload.update(
                {
                    "protocol": "Cellular",
                    "kind": "cellular_spectrum_activity",
                    "passive_only": True,
                    "content_decoded": False,
                }
            )
            print(json.dumps(payload, separators=(",", ":")), flush=True)
        return
    if args.json:
        print(json.dumps({"detections": [asdict(row) for row in rows], "passive_only": True, "content_decoded": False}, indent=2))
        return
    if args.csv:
        fieldnames = list(Detection.__dataclass_fields__.keys())
        writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
        return
    print("Passive cellular spectrum awareness only. No subscriber traffic/content is decoded.")
    print("freq_mhz  link      tech      excess  power   LTE sync              operator / PLMN hint         band")
    for row in rows:
        operator = row.likely_operator or "-"
        plmn = row.likely_plmn or "-"
        if row.lte_pss_detected:
            sync = f"PSS N2={row.lte_n_id_2} {row.lte_pss_metric:.2f}"
        else:
            sync = row.lte_sync_status.replace("_", " ")[:20] or "-"
        print(
            f"{row.frequency_mhz:8.3f}  {row.link:8s}  {row.technology[:8]:8s}  {row.excess_db:6.1f}  "
            f"{row.power_dbfs:6.1f}  {sync[:20]:20s}  {operator[:18]:18s} {plmn:8s}  {row.band}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Passive cellular infrastructure and spectrum-awareness scanner.")
    sub = parser.add_subparsers(dest="command")
    scan = sub.add_parser("scan", help="Capture IQ via sdr-gateway and report cellular-band spectrum activity")
    scan.add_argument("--gateway-base-url", default="")
    scan.add_argument("--gateway-token", default="")
    scan.add_argument("--device-id", default="hackrf:0")
    scan.add_argument("--center-freq-hz", type=int, default=DEFAULT_CENTER_FREQ_HZ)
    scan.add_argument("--sample-rate-sps", type=int, default=DEFAULT_SAMPLE_RATE_SPS)
    scan.add_argument("--bandwidth-hz", type=int, default=DEFAULT_BANDWIDTH_HZ)
    scan.add_argument("--dwell-s", type=float, default=0.35)
    scan.add_argument("--lna-gain-db", type=int, default=24)
    scan.add_argument("--vga-gain-db", type=int, default=30)
    scan.add_argument("--amp-enable", action="store_true")
    scan.add_argument("--replace-existing", action="store_true")
    scan.add_argument("--fft-size", type=int, default=262144)
    scan.add_argument("--max-fft-segments", type=int, default=8)
    scan.add_argument("--active-threshold-db", type=float, default=10.0)
    scan.add_argument("--min-occupied-width-hz", type=int, default=25_000)
    scan.add_argument("--include-candidates", dest="include_candidates", action="store_true", default=True)
    scan.add_argument("--no-include-candidates", dest="include_candidates", action="store_false")
    scan.add_argument("--candidate-threshold-db", type=float, default=3.0)
    scan.add_argument("--candidate-smooth-bins", type=int, default=9)
    scan.add_argument("--candidate-guard-hz", type=int, default=250_000)
    scan.add_argument("--target-freq-hz", type=int, default=DEFAULT_CENTER_FREQ_HZ)
    scan.add_argument("--target-width-hz", type=int, default=500_000)
    scan.add_argument("--target-threshold-db", type=float, default=1.5)
    scan.add_argument("--target-report", dest="target_report", action="store_true", default=True)
    scan.add_argument("--no-target-report", dest="target_report", action="store_false")
    scan.add_argument("--lte-sync", dest="lte_sync", action="store_true", default=True, help="run a passive LTE PSS sync probe on LTE-band candidates")
    scan.add_argument("--no-lte-sync", dest="lte_sync", action="store_false", help="skip LTE PSS sync probing and report spectrum only")
    scan.add_argument("--lte-pss-threshold", type=float, default=0.55)
    scan.add_argument("--lte-sync-max-segments", type=int, default=96)
    scan.add_argument("--top", type=int, default=12)
    scan.add_argument("--jsonl", action="store_true")
    scan.add_argument("--json", action="store_true")
    scan.add_argument("--csv", action="store_true")
    scan.set_defaults(func=_run_scan)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"cellular-awareness: {exc}", file=sys.stderr)
        return 1
