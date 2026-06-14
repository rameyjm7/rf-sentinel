from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from .dsp import FmQualityDemod, band_power_dbfs, dbfs, iq_i8_to_complex, station_grid, window_plan
from .gateway import GatewayClient


DEFAULT_START_HZ = 87_500_000
DEFAULT_STOP_HZ = 108_000_000
DEFAULT_SAMPLE_RATE_SPS = 20_000_000


@dataclass
class StationCandidate:
    freq_hz: int
    power_dbfs: float
    noise_dbfs: float
    excess_db: float
    samples: int = 0
    audio_rms: float = 0.0
    pilot_db: float = -120.0
    rds_subcarrier_db: float = -120.0
    source_center_hz: int = 0
    scored: bool = False


def _json_print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, separators=(",", ":")), flush=True)


def _capture_iq(
    client: GatewayClient,
    *,
    device_id: str,
    center_freq_hz: int,
    sample_rate_sps: int,
    dwell_s: float,
    lna_gain_db: int,
    vga_gain_db: int,
    amp_enable: bool,
) -> bytes:
    created_stream = False
    stream = client.stream_for_device(device_id)
    if stream is not None:
        stream = client.retune_stream(
            stream.stream_id,
            device_id=device_id,
            center_freq_hz=int(center_freq_hz),
            sample_rate_sps=int(sample_rate_sps),
            lna_gain_db=int(lna_gain_db),
            vga_gain_db=int(vga_gain_db),
            amp_enable=bool(amp_enable),
            baseband_filter_hz=int(sample_rate_sps),
        )
    else:
        stream = client.start_stream(
            device_id=device_id,
            center_freq_hz=int(center_freq_hz),
            sample_rate_sps=int(sample_rate_sps),
            lna_gain_db=int(lna_gain_db),
            vga_gain_db=int(vga_gain_db),
            amp_enable=bool(amp_enable),
            baseband_filter_hz=int(sample_rate_sps),
        )
        created_stream = True
    chunks: list[bytes] = []
    deadline = time.monotonic() + max(0.05, float(dwell_s))
    try:
        for chunk in client.iter_iq_chunks(stream.stream_id, keep_stream=True, deadline_monotonic=deadline):
            chunks.append(bytes(chunk))
            if time.monotonic() >= deadline:
                break
    finally:
        if created_stream:
            client.stop_stream(stream.stream_id)
            time.sleep(0.12)
    return b"".join(chunks)


def _discover_candidates(args: argparse.Namespace, client: GatewayClient, device_id: str) -> list[StationCandidate]:
    if str(args.discovery_mode) in {"auto", "wideband"} and int(args.sample_rate_sps) >= 8_000_000:
        try:
            candidates = _discover_candidates_wideband(args, client, device_id)
            if candidates or str(args.discovery_mode) == "wideband":
                return candidates
        except Exception as exc:
            if args.debug:
                print(f"fm_wideband_discovery_error error={exc}", file=sys.stderr, flush=True)
            if str(args.discovery_mode) == "wideband":
                return []
    if str(args.discovery_mode) in {"auto", "sweep"}:
        try:
            candidates = _discover_candidates_sweep(args, client, device_id)
            if candidates or str(args.discovery_mode) == "sweep":
                return candidates
        except Exception as exc:
            if args.debug:
                print(f"fm_sweep_discovery_error error={exc}", file=sys.stderr, flush=True)
            if str(args.discovery_mode) == "sweep":
                return []
    return _discover_candidates_iq(args, client, device_id)


def _score_station_iq(
    args: argparse.Namespace,
    station: StationCandidate,
    iq: np.ndarray,
    *,
    center_freq_hz: int,
) -> StationCandidate:
    if iq.size < 8192:
        return station
    max_decode_samples = min(
        iq.size,
        max(131_072, min(int(float(args.sample_rate_sps) * float(args.decode_dwell_s)), 524_288)),
    )
    segment = iq[-max_decode_samples:].astype(np.complex64, copy=False)
    sample_rate = float(args.sample_rate_sps)
    offset_hz = float(station.freq_hz - center_freq_hz)
    n = np.arange(segment.size, dtype=np.float32)
    phase = np.exp(np.complex64(-2j * np.pi * offset_hz / sample_rate) * n).astype(np.complex64, copy=False)
    shifted = (segment * phase).astype(np.complex64, copy=False)
    nfft = 1 << int(np.floor(np.log2(shifted.size)))
    if nfft >= 8192:
        work = shifted[-nfft:]
        spectrum = np.fft.fft(work)
        freqs = np.fft.fftfreq(nfft, d=1.0 / sample_rate)
        cutoff_hz = max(120_000.0, float(args.channel_width_hz) * 0.85)
        spectrum[np.abs(freqs) > cutoff_hz] = 0.0
        filtered = np.fft.ifft(spectrum).astype(np.complex64, copy=False)
    else:
        filtered = shifted
    station.power_dbfs = round(dbfs(float(np.sqrt(np.mean(np.abs(filtered) ** 2))), floor=-120.0), 1)
    station.samples = int(filtered.size)
    demod = FmQualityDemod(int(args.sample_rate_sps))
    demod.process_iq(filtered)
    station.audio_rms = round(float(demod.audio_rms), 5)
    station.pilot_db = round(float(demod.pilot_db), 1)
    station.rds_subcarrier_db = round(float(demod.rds_subcarrier_db), 1)
    station.scored = True
    return station


def _discover_candidates_wideband(args: argparse.Namespace, client: GatewayClient, device_id: str) -> list[StationCandidate]:
    grid = station_grid(int(args.start_freq_hz), int(args.stop_freq_hz), int(args.station_step_hz))
    stations: dict[int, StationCandidate] = {}
    captures: dict[int, np.ndarray] = {}
    centers = window_plan(int(args.start_freq_hz), int(args.stop_freq_hz), int(args.sample_rate_sps), usable_fraction=0.98)
    if args.debug:
        print(
            f"fm_wideband plan windows={len(centers)} sample_rate={int(args.sample_rate_sps)} "
            f"band={int(args.start_freq_hz)}-{int(args.stop_freq_hz)}",
            file=sys.stderr,
            flush=True,
        )
    for center_hz in centers:
        try:
            raw = _capture_iq(
                client,
                device_id=device_id,
                center_freq_hz=center_hz,
                sample_rate_sps=int(args.sample_rate_sps),
                dwell_s=float(args.discovery_dwell_s),
                lna_gain_db=int(args.lna_gain_db),
                vga_gain_db=int(args.vga_gain_db),
                amp_enable=bool(args.amp_enable),
            )
        except Exception as exc:
            if args.debug:
                print(f"fm_wideband_error center={center_hz/1e6:.3f} error={exc}", file=sys.stderr, flush=True)
            time.sleep(0.15)
            continue
        iq = iq_i8_to_complex(raw)
        captures[int(center_hz)] = iq
        half_bw = float(args.sample_rate_sps) * 0.49
        visible = [freq for freq in grid if abs(freq - center_hz) <= half_bw]
        if not visible or iq.size < 4096:
            continue
        powers = [band_power_dbfs(iq, int(args.sample_rate_sps), freq - center_hz, width_hz=float(args.channel_width_hz)) for freq in visible]
        noise = float(np.median(powers)) if powers else -120.0
        for freq, power in zip(visible, powers):
            excess = float(power - noise)
            if power < float(args.min_power_dbfs) or excess < float(args.active_threshold_db):
                continue
            prev = stations.get(freq)
            if prev is None or power > prev.power_dbfs:
                stations[freq] = StationCandidate(
                    freq_hz=int(freq),
                    power_dbfs=round(float(power), 1),
                    noise_dbfs=round(noise, 1),
                    excess_db=round(excess, 1),
                    samples=iq.size,
                    source_center_hz=int(center_hz),
                )
        if args.debug:
            strongest = sorted(zip(visible, powers), key=lambda item: item[1], reverse=True)[:8]
            summary = ";".join(f"{freq/1e6:.1f}:{power:.1f}" for freq, power in strongest)
            print(
                f"fm_wideband_window center={center_hz/1e6:.3f} samples={iq.size} noise={noise:.1f} top={summary}",
                file=sys.stderr,
                flush=True,
            )
    ranked = sorted(stations.values(), key=lambda item: item.power_dbfs, reverse=True)[: int(args.max_stations)]
    scored: list[StationCandidate] = []
    for station in ranked:
        capture = captures.get(int(station.source_center_hz))
        if capture is None:
            scored.append(station)
            continue
        scored.append(_score_station_iq(args, station, capture, center_freq_hz=int(station.source_center_hz)))
    return scored


def _discover_candidates_sweep(args: argparse.Namespace, client: GatewayClient, device_id: str) -> list[StationCandidate]:
    grid = station_grid(int(args.start_freq_hz), int(args.stop_freq_hz), int(args.station_step_hz))
    sweep_id = client.start_sweep(
        device_id=device_id,
        start_freq_hz=int(args.start_freq_hz),
        stop_freq_hz=int(args.stop_freq_hz),
        bin_width_hz=int(args.sweep_bin_width_hz),
        lna_gain_db=int(args.lna_gain_db),
        vga_gain_db=int(args.vga_gain_db),
        amp_enable=bool(args.amp_enable),
    )
    try:
        deadline = time.monotonic() + max(0.1, float(args.discovery_dwell_s))
        samples: list[dict] = []
        while time.monotonic() < deadline:
            samples = client.sweep_samples(sweep_id)
            if samples:
                break
            time.sleep(0.2)
    finally:
        client.stop_sweep(sweep_id)
        time.sleep(0.12)
    station_bins: dict[int, list[float]] = {freq: [] for freq in grid}
    for sample in samples:
        try:
            hz_low = int(sample["hz_low"])
            hz_high = int(sample["hz_high"])
            values = [float(value) for value in sample.get("db_values", [])]
        except (KeyError, TypeError, ValueError):
            continue
        if not values or hz_high <= hz_low:
            continue
        bin_width = float(hz_high - hz_low) / float(len(values))
        for freq in grid:
            if freq < hz_low - int(args.channel_width_hz) or freq > hz_high + int(args.channel_width_hz):
                continue
            center_bin = int(round((freq - hz_low) / bin_width))
            radius = max(1, int(round((float(args.channel_width_hz) / 2.0) / bin_width)))
            start = max(0, center_bin - radius)
            stop = min(len(values), center_bin + radius + 1)
            if start < stop:
                station_bins[freq].append(float(max(values[start:stop])))
    powers = [(freq, max(values)) for freq, values in station_bins.items() if values]
    if not powers:
        if args.debug:
            print("fm_sweep samples=0 active=none", file=sys.stderr, flush=True)
        return []
    noise = float(np.median([power for _, power in powers]))
    candidates: list[StationCandidate] = []
    for freq, power in powers:
        excess = float(power - noise)
        if power < float(args.min_power_dbfs) or excess < float(args.active_threshold_db):
            continue
        candidates.append(
            StationCandidate(
                freq_hz=int(freq),
                power_dbfs=round(float(power), 1),
                noise_dbfs=round(noise, 1),
                excess_db=round(excess, 1),
                samples=len(samples),
            )
        )
    candidates.sort(key=lambda item: item.power_dbfs, reverse=True)
    if args.debug:
        strongest = sorted(powers, key=lambda item: item[1], reverse=True)[:8]
        summary = ";".join(f"{freq/1e6:.1f}:{power:.1f}" for freq, power in strongest)
        active = ";".join(f"{item.freq_hz/1e6:.1f}:{item.excess_db:.1f}" for item in candidates[:8]) or "none"
        print(
            f"fm_sweep samples={len(samples)} noise={noise:.1f} top={summary} active={active}",
            file=sys.stderr,
            flush=True,
        )
    return candidates[: int(args.max_stations)]


def _discover_candidates_iq(args: argparse.Namespace, client: GatewayClient, device_id: str) -> list[StationCandidate]:
    grid = station_grid(int(args.start_freq_hz), int(args.stop_freq_hz), int(args.station_step_hz))
    stations: dict[int, StationCandidate] = {}
    centers = window_plan(int(args.start_freq_hz), int(args.stop_freq_hz), int(args.sample_rate_sps))
    if args.debug:
        print(
            f"fm_scan plan windows={len(centers)} sample_rate={int(args.sample_rate_sps)} "
            f"band={int(args.start_freq_hz)}-{int(args.stop_freq_hz)}",
            file=sys.stderr,
            flush=True,
        )
    for center_hz in centers:
        try:
            raw = _capture_iq(
                client,
                device_id=device_id,
                center_freq_hz=center_hz,
                sample_rate_sps=int(args.sample_rate_sps),
                dwell_s=float(args.discovery_dwell_s),
                lna_gain_db=int(args.lna_gain_db),
                vga_gain_db=int(args.vga_gain_db),
                amp_enable=bool(args.amp_enable),
            )
        except Exception as exc:
            if args.debug:
                print(f"fm_window_error center={center_hz/1e6:.3f} error={exc}", file=sys.stderr, flush=True)
            time.sleep(0.25)
            continue
        iq = iq_i8_to_complex(raw)
        half_bw = float(args.sample_rate_sps) * 0.42
        visible = [freq for freq in grid if abs(freq - center_hz) <= half_bw]
        if not visible or iq.size < 4096:
            continue
        powers = [band_power_dbfs(iq, int(args.sample_rate_sps), freq - center_hz, width_hz=float(args.channel_width_hz)) for freq in visible]
        noise = float(np.median(powers)) if powers else -120.0
        for freq, power in zip(visible, powers):
            excess = float(power - noise)
            if power < float(args.min_power_dbfs) or excess < float(args.active_threshold_db):
                continue
            prev = stations.get(freq)
            if prev is None or power > prev.power_dbfs:
                stations[freq] = StationCandidate(
                    freq_hz=int(freq),
                    power_dbfs=round(float(power), 1),
                    noise_dbfs=round(noise, 1),
                    excess_db=round(excess, 1),
                    samples=iq.size,
                )
        if args.debug:
            strongest = sorted(zip(visible, powers), key=lambda item: item[1], reverse=True)[:5]
            summary = ";".join(f"{freq/1e6:.1f}:{power:.1f}" for freq, power in strongest)
            print(f"fm_window center={center_hz/1e6:.3f} samples={iq.size} noise={noise:.1f} top={summary}", file=sys.stderr, flush=True)
    return sorted(stations.values(), key=lambda item: item.power_dbfs, reverse=True)[: int(args.max_stations)]


def _score_station(args: argparse.Namespace, client: GatewayClient, device_id: str, station: StationCandidate) -> StationCandidate:
    raw = _capture_iq(
        client,
        device_id=device_id,
        center_freq_hz=station.freq_hz,
        sample_rate_sps=int(args.sample_rate_sps),
        dwell_s=float(args.decode_dwell_s),
        lna_gain_db=int(args.lna_gain_db),
        vga_gain_db=int(args.vga_gain_db),
        amp_enable=bool(args.amp_enable),
    )
    iq = iq_i8_to_complex(raw)
    if iq.size:
        station.power_dbfs = round(dbfs(float(np.sqrt(np.mean(np.abs(iq) ** 2))), floor=-120.0), 1)
        station.samples = int(iq.size)
    demod = FmQualityDemod(int(args.sample_rate_sps))
    chunk_size = max(4096, int(args.sample_rate_sps // 10) * 2)
    for offset in range(0, len(raw), chunk_size):
        demod.process(raw[offset : offset + chunk_size])
    station.audio_rms = round(float(demod.audio_rms), 5)
    station.pilot_db = round(float(demod.pilot_db), 1)
    station.rds_subcarrier_db = round(float(demod.rds_subcarrier_db), 1)
    station.scored = True
    return station


def _station_payload(station: StationCandidate) -> dict[str, Any]:
    freq_mhz = station.freq_hz / 1_000_000.0
    return {
        "protocol": "fm",
        "kind": "fm_station",
        "timestamp": time.time(),
        "frequency_hz": station.freq_hz,
        "frequency_mhz": round(freq_mhz, 1),
        "identity": f"FM {freq_mhz:.1f} MHz",
        "power_dbfs": station.power_dbfs,
        "noise_dbfs": station.noise_dbfs,
        "excess_db": station.excess_db,
        "rssi_dbfs": station.power_dbfs,
        "audio_rms": station.audio_rms,
        "pilot_db": station.pilot_db,
        "rds_subcarrier_db": station.rds_subcarrier_db,
        "stereo_likely": station.pilot_db >= 8.0,
        "rds_likely": station.rds_subcarrier_db >= 6.0,
        "samples": station.samples,
    }


def _run_scan(args: argparse.Namespace) -> int:
    client = GatewayClient(args.base_url, args.token)
    device_id = args.device_id or client.resolve_default_device_id()
    if args.debug:
        print(
            f"fm_scan device={device_id} sr={int(args.sample_rate_sps)} "
            f"gain=L{int(args.lna_gain_db)} V{int(args.vga_gain_db)}",
            file=sys.stderr,
            flush=True,
        )
    candidates = _discover_candidates(args, client, device_id)
    if not candidates and args.debug:
        print("fm_scan active=none", file=sys.stderr, flush=True)
    for station in candidates:
        scored = station
        if not station.scored:
            try:
                scored = _score_station(args, client, device_id, station)
            except Exception as exc:
                if args.debug:
                    print(f"fm_decode_error freq={station.freq_hz} error={exc}", file=sys.stderr, flush=True)
                scored = station
        payload = _station_payload(scored)
        if args.json:
            _json_print(payload)
        else:
            print(
                f"fm station {payload['frequency_mhz']:.1f}MHz "
                f"power={payload['power_dbfs']:.1f}dBFS excess={payload['excess_db']:.1f}dB "
                f"pilot={payload['pilot_db']:.1f}dB rds={payload['rds_subcarrier_db']:.1f}dB",
                flush=True,
            )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fm_broadcast", description="FM broadcast discovery using sdr-gateway IQ streams.")
    sub = parser.add_subparsers(dest="command")
    scan = sub.add_parser("scan", help="sweep broadcast FM and report active stations")
    scan.add_argument("--base-url", default=None)
    scan.add_argument("--token", default=None)
    scan.add_argument("--device-id", default="")
    scan.add_argument("--start-freq-hz", type=int, default=DEFAULT_START_HZ)
    scan.add_argument("--stop-freq-hz", type=int, default=DEFAULT_STOP_HZ)
    scan.add_argument("--station-step-hz", type=int, default=200_000)
    scan.add_argument("--sample-rate-sps", type=int, default=DEFAULT_SAMPLE_RATE_SPS)
    scan.add_argument("--channel-width-hz", type=int, default=160_000)
    scan.add_argument("--discovery-mode", choices=("auto", "wideband", "sweep", "iq"), default="auto")
    scan.add_argument("--sweep-bin-width-hz", type=int, default=100_000)
    scan.add_argument("--discovery-dwell-s", type=float, default=0.18)
    scan.add_argument("--decode-dwell-s", type=float, default=0.35)
    scan.add_argument("--active-threshold-db", type=float, default=8.0)
    scan.add_argument("--min-power-dbfs", type=float, default=-55.0)
    scan.add_argument("--max-stations", type=int, default=12)
    scan.add_argument("--lna-gain-db", type=int, default=32)
    scan.add_argument("--vga-gain-db", type=int, default=32)
    scan.add_argument("--amp-enable", action="store_true")
    scan.add_argument("--json", action="store_true")
    scan.add_argument("--debug", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "scan":
        return _run_scan(args)
    if args.command is None:
        parser.print_help()
        return 2
    parser.error(f"unknown command {args.command}")
    return 2
