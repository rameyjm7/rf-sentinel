from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .dsp import AMMetrics, channel_grid, cs16_to_complex, measure_am_channel


DEFAULT_START_KHZ = 530
DEFAULT_STOP_KHZ = 1700
DEFAULT_STEP_KHZ = 10
DEFAULT_SAMPLE_RATE_SPS = 250_000
DEFAULT_BANDWIDTH_HZ = 80_000
DEFAULT_BAND = "am"


@dataclass(frozen=True)
class BandPreset:
    label: str
    start_khz: int
    stop_khz: int
    step_khz: int
    description: str


BAND_PRESETS: dict[str, BandPreset] = {
    "vlf": BandPreset("VLF", 3, 30, 1, "Very Low Frequency, 3-30 kHz"),
    "lf": BandPreset("LF", 30, 300, 5, "Low Frequency, 30-300 kHz"),
    "mf": BandPreset("MF", 300, 3000, 10, "Medium Frequency, 300 kHz-3 MHz"),
    "am": BandPreset("AM broadcast", 530, 1700, 10, "Medium-wave AM broadcast band"),
    "1khz-1mhz": BandPreset("1 kHz-1 MHz", 1, 1000, 5, "VLF/LF/lower-MF survey range"),
    "vlf-lf-mf": BandPreset("VLF/LF/MF", 3, 3000, 10, "VLF through MF survey range"),
}


@contextlib.contextmanager
def _suppress_native_output(enabled: bool) -> Any:
    if not enabled:
        yield
        return
    sys.stdout.flush()
    sys.stderr.flush()
    saved_stdout = os.dup(1)
    saved_stderr = os.dup(2)
    try:
        with open(os.devnull, "w", encoding="utf-8") as devnull:
            os.dup2(devnull.fileno(), 1)
            os.dup2(devnull.fileno(), 2)
            yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(saved_stdout, 1)
        os.dup2(saved_stderr, 2)
        os.close(saved_stdout)
        os.close(saved_stderr)


@contextlib.contextmanager
def _soapy_log_level(SoapySDR: Any, level: int | None) -> Any:
    if level is None or not hasattr(SoapySDR, "setLogLevel"):
        yield
        return
    previous = None
    try:
        if hasattr(SoapySDR, "getLogLevel"):
            previous = SoapySDR.getLogLevel()
        SoapySDR.setLogLevel(level)
        yield
    finally:
        if previous is not None:
            SoapySDR.setLogLevel(previous)


@dataclass
class ScanResult:
    freq_hz: int
    freq_khz: float
    power_dbfs: float
    carrier_dbfs: float
    carrier_snr_db: float
    audio_dbfs: float
    modulation_pct: float
    excess_db: float
    samples: int
    active: bool


@dataclass(frozen=True)
class ReceiverConfig:
    sample_rate_sps: int
    bandwidth_hz: int
    tune_offset_hz: int
    format: str = "CS16"


@dataclass(frozen=True)
class ScanPlan:
    band: str
    label: str
    start_khz: int
    stop_khz: int
    step_khz: int

    @property
    def channel_count(self) -> int:
        if self.stop_khz < self.start_khz or self.step_khz <= 0:
            return 0
        return ((self.stop_khz - self.start_khz) // self.step_khz) + 1


def _load_soapysdr() -> Any:
    try:
        import SoapySDR  # type: ignore[import-not-found]

        return SoapySDR
    except ImportError:
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        candidates = (
            Path("/usr/local/lib") / version / "site-packages",
            Path("/usr/lib") / version / "site-packages",
            Path("/usr/lib") / version / "dist-packages",
        )
        for path in candidates:
            if path.exists() and str(path) not in sys.path:
                sys.path.append(str(path))
        try:
            import SoapySDR  # type: ignore[import-not-found]

            return SoapySDR
        except ImportError as exc:
            raise RuntimeError(
                "SoapySDR Python bindings are not importable. Install python3-soapysdr or run from the SDR environment."
            ) from exc


def _open_sdrplay(SoapySDR: Any, args: argparse.Namespace) -> Any:
    serial = str(args.serial or os.environ.get("SDRPLAY_SERIAL", "")).strip()
    kwargs: dict[str, str] = {"driver": "sdrplay"}
    if serial:
        kwargs["serial"] = serial
    return SoapySDR.Device(kwargs)


def _try_call(label: str, func: Any, *args: Any, debug: bool = False) -> None:
    try:
        func(*args)
    except Exception as exc:
        if debug:
            print(f"am_broadcast_config_warning setting={label} error={exc}", file=sys.stderr, flush=True)


def _set_named_gain(SoapySDR: Any, dev: Any, name: str, value: float, *, debug: bool = False) -> None:
    try:
        names = set(dev.listGains(SoapySDR.SOAPY_SDR_RX, 0))
    except Exception:
        names = set()
    if name in names:
        _try_call(f"gain:{name}", dev.setGain, SoapySDR.SOAPY_SDR_RX, 0, name, float(value), debug=debug)


def _read_int_setting(label: str, fallback: int, func: Any, *args: Any, debug: bool = False) -> int:
    try:
        value = func(*args)
    except Exception as exc:
        if debug:
            print(f"am_broadcast_readback_warning setting={label} error={exc}", file=sys.stderr, flush=True)
        return int(fallback)
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return int(fallback)


def _configure_receiver(SoapySDR: Any, dev: Any, args: argparse.Namespace) -> tuple[Any, ReceiverConfig]:
    direction = SoapySDR.SOAPY_SDR_RX
    channel = 0
    _try_call("sample_rate", dev.setSampleRate, direction, channel, int(args.sample_rate_sps), debug=args.debug)
    _try_call("bandwidth", dev.setBandwidth, direction, channel, int(args.bandwidth_hz), debug=args.debug)
    _try_call("gain_mode", dev.setGainMode, direction, channel, False, debug=args.debug)
    _set_named_gain(SoapySDR, dev, "RFGR", float(args.rfgr), debug=args.debug)
    _set_named_gain(SoapySDR, dev, "IFGR", float(args.ifgr), debug=args.debug)
    if args.antenna:
        _try_call("antenna", dev.setAntenna, direction, channel, str(args.antenna), debug=args.debug)
    stream = dev.setupStream(direction, SoapySDR.SOAPY_SDR_CS16, [channel])
    dev.activateStream(stream)
    config = ReceiverConfig(
        sample_rate_sps=_read_int_setting(
            "sample_rate",
            int(args.sample_rate_sps),
            dev.getSampleRate,
            direction,
            channel,
            debug=args.debug,
        ),
        bandwidth_hz=_read_int_setting(
            "bandwidth",
            int(args.bandwidth_hz),
            dev.getBandwidth,
            direction,
            channel,
            debug=args.debug,
        ),
        tune_offset_hz=int(args.tune_offset_hz),
    )
    return stream, config


def _resolve_scan_plan(args: argparse.Namespace) -> ScanPlan:
    band = str(args.band)
    preset = BAND_PRESETS.get(band)
    if preset is None:
        preset = BAND_PRESETS[DEFAULT_BAND]
    start_khz = preset.start_khz if args.start_khz is None else int(args.start_khz)
    stop_khz = preset.stop_khz if args.stop_khz is None else int(args.stop_khz)
    step_khz = preset.step_khz if args.step_khz is None else int(args.step_khz)
    label = "Custom" if args.start_khz is not None or args.stop_khz is not None or args.step_khz is not None else preset.label
    return ScanPlan(band=band, label=label, start_khz=start_khz, stop_khz=stop_khz, step_khz=step_khz)


def _capture_channel(SoapySDR: Any, dev: Any, stream: Any, freq_hz: int, args: argparse.Namespace) -> np.ndarray:
    direction = SoapySDR.SOAPY_SDR_RX
    channel = 0
    dev.setFrequency(direction, channel, int(freq_hz + int(args.tune_offset_hz)))
    if args.settle_s > 0:
        time.sleep(float(args.settle_s))

    target_samples = max(1024, int(float(args.sample_rate_sps) * float(args.dwell_s)))
    chunk_samples = min(16384, max(1024, target_samples))
    buf = np.empty(chunk_samples * 2, dtype=np.int16)
    chunks: list[np.ndarray] = []
    captured = 0
    deadline = time.monotonic() + max(0.1, float(args.dwell_s) + 0.5)
    while captured < target_samples and time.monotonic() < deadline:
        result = dev.readStream(stream, [buf], chunk_samples, timeoutUs=int(args.timeout_ms * 1000))
        count = int(getattr(result, "ret", result[0] if isinstance(result, tuple) else -1))
        if count > 0:
            chunks.append(buf[: count * 2].copy())
            captured += count
            continue
        if args.debug:
            print(f"am_broadcast_read_warning freq_khz={freq_hz/1000:.1f} ret={count}", file=sys.stderr, flush=True)
    if not chunks:
        return np.empty(0, dtype=np.complex64)
    return cs16_to_complex(np.concatenate(chunks))


def _scan(args: argparse.Namespace) -> int:
    SoapySDR = _load_soapysdr()
    scan_plan = _resolve_scan_plan(args)
    freqs = channel_grid(scan_plan.start_khz, scan_plan.stop_khz, scan_plan.step_khz)
    if not freqs:
        print("No channels in requested range.", file=sys.stderr)
        return 2
    if scan_plan.channel_count > 400 and not args.yes:
        estimated_s = scan_plan.channel_count * max(0.02, float(args.dwell_s))
        print(
            f"Refusing long scan: {scan_plan.channel_count} channels, about {estimated_s:.0f}s of dwell time. "
            "Use --yes to run it, or increase --step-khz.",
            file=sys.stderr,
        )
        return 2

    quiet_log_level = None if args.show_driver_log else int(getattr(SoapySDR, "SOAPY_SDR_ERROR", 3))
    with _soapy_log_level(SoapySDR, quiet_log_level), _suppress_native_output(not bool(args.show_driver_log)):
        dev = _open_sdrplay(SoapySDR, args)
        stream, receiver_config = _configure_receiver(SoapySDR, dev, args)
        if not args.show_driver_log:
            time.sleep(0.05)
    raw_results: list[tuple[int, AMMetrics]] = []
    try:
        for index, freq_hz in enumerate(freqs, start=1):
            if args.debug:
                print(f"am_broadcast_scan freq_khz={freq_hz/1000:.1f} channel={index}/{len(freqs)}", file=sys.stderr, flush=True)
            iq = _capture_channel(SoapySDR, dev, stream, freq_hz, args)
            raw_results.append(
                (
                    freq_hz,
                    measure_am_channel(
                        iq,
                        int(args.sample_rate_sps),
                        carrier_offset_hz=-float(args.tune_offset_hz),
                        carrier_width_hz=float(args.carrier_width_hz),
                    ),
                )
            )
    finally:
        try:
            dev.deactivateStream(stream)
        finally:
            dev.closeStream(stream)

    noise_floor = float(np.median([metrics.carrier_dbfs for _, metrics in raw_results])) if raw_results else -160.0
    rows = [_to_result(freq_hz, metrics, noise_floor, float(args.active_threshold_db)) for freq_hz, metrics in raw_results]
    if str(args.sort) == "score":
        rows.sort(key=lambda row: (row.active, row.excess_db, row.power_dbfs), reverse=True)
    else:
        rows.sort(key=lambda row: row.freq_hz)
    if args.top > 0:
        rows = rows[: int(args.top)]
    _print_results(rows, args, noise_floor, receiver_config, scan_plan)
    return 0


def _to_result(freq_hz: int, metrics: AMMetrics, noise_floor: float, active_threshold_db: float) -> ScanResult:
    excess = round(float(metrics.carrier_dbfs - noise_floor), 1)
    return ScanResult(
        freq_hz=int(freq_hz),
        freq_khz=round(float(freq_hz) / 1000.0, 1),
        power_dbfs=float(metrics.power_dbfs),
        carrier_dbfs=float(metrics.carrier_dbfs),
        carrier_snr_db=float(metrics.carrier_snr_db),
        audio_dbfs=float(metrics.audio_dbfs),
        modulation_pct=float(metrics.modulation_pct),
        excess_db=excess,
        samples=int(metrics.samples),
        active=bool(excess >= active_threshold_db and metrics.carrier_snr_db >= active_threshold_db and metrics.samples > 0),
    )


def _print_results(
    rows: list[ScanResult],
    args: argparse.Namespace,
    noise_floor: float,
    receiver_config: ReceiverConfig,
    scan_plan: ScanPlan,
) -> None:
    if args.jsonl:
        for row in rows:
            if args.active_only and not row.active:
                continue
            payload = asdict(row)
            payload.update(
                {
                    "protocol": "lfmf",
                    "kind": "lfmf_signal",
                    "band": scan_plan.band,
                    "band_label": scan_plan.label,
                    "frequency_hz": row.freq_hz,
                    "frequency_khz": row.freq_khz,
                    "sample_rate_sps": receiver_config.sample_rate_sps,
                    "bandwidth_hz": receiver_config.bandwidth_hz,
                    "tune_offset_hz": receiver_config.tune_offset_hz,
                    "timestamp": time.time(),
                }
            )
            print(json.dumps(payload, separators=(",", ":")), flush=True)
        return
    if args.json:
        print(
            json.dumps(
                {
                    "noise_floor_dbfs": round(noise_floor, 1),
                    "receiver": asdict(receiver_config),
                    "scan_plan": asdict(scan_plan),
                    "signals": [asdict(row) for row in rows],
                },
                indent=2,
            )
        )
        return
    if args.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=list(asdict(rows[0]).keys()) if rows else list(ScanResult.__dataclass_fields__.keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
        return
    print(
        f"{scan_plan.label} scan  range={scan_plan.start_khz}-{scan_plan.stop_khz} kHz  step={scan_plan.step_khz} kHz  "
        f"sample_rate={receiver_config.sample_rate_sps} sps  bandwidth={receiver_config.bandwidth_hz} Hz  "
        f"carrier_floor={noise_floor:.1f} dBFS  threshold=+{float(args.active_threshold_db):.1f} dB  "
        f"tune_offset={receiver_config.tune_offset_hz} Hz"
    )
    print("freq_khz  status  excess  car_dbfs  car_snr  audio_dbfs  mod_pct  samples")
    for row in rows:
        status = "ACTIVE" if row.active else "-"
        print(
            f"{row.freq_khz:8.1f}  {status:6s}  {row.excess_db:6.1f}  "
            f"{row.carrier_dbfs:8.1f}  {row.carrier_snr_db:7.1f}  "
            f"{row.audio_dbfs:9.1f}  {row.modulation_pct:7.1f}  {row.samples}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan VLF, LF, MF, and AM broadcast signals with an SDRplay receiver.")
    sub = parser.add_subparsers(dest="command")
    scan = sub.add_parser("scan", help="Scan VLF/LF/MF signal bands")
    scan.add_argument("--device-id", default="", help="Optional RF Sentinel device id hint; SDRplay opens through SoapySDR.")
    scan.add_argument("--serial", default="", help="SDRplay serial number. Defaults to SDRPLAY_SERIAL when set.")
    scan.add_argument("--antenna", default="", help="Optional SDRplay antenna name, for example A, B, or Hi-Z.")
    scan.add_argument("--band", choices=sorted(BAND_PRESETS), default=DEFAULT_BAND, help="Band preset to scan.")
    scan.add_argument("--start-khz", type=int, default=None, help=f"Override preset start frequency. Default for AM is {DEFAULT_START_KHZ}.")
    scan.add_argument("--stop-khz", type=int, default=None, help=f"Override preset stop frequency. Default for AM is {DEFAULT_STOP_KHZ}.")
    scan.add_argument("--step-khz", type=int, default=None, help=f"Override preset channel step. Default for AM is {DEFAULT_STEP_KHZ}.")
    scan.add_argument("--sample-rate-sps", type=int, default=DEFAULT_SAMPLE_RATE_SPS)
    scan.add_argument("--bandwidth-hz", type=int, default=DEFAULT_BANDWIDTH_HZ)
    scan.add_argument("--tune-offset-hz", type=int, default=25_000, help="Tune this far above each channel so the AM carrier avoids DC.")
    scan.add_argument("--carrier-width-hz", type=float, default=350.0, help="Narrow FFT window used to score the AM carrier.")
    scan.add_argument("--dwell-s", type=float, default=0.20)
    scan.add_argument("--settle-s", type=float, default=0.04)
    scan.add_argument("--timeout-ms", type=int, default=250)
    scan.add_argument("--rfgr", type=float, default=0.0, help="SDRplay RF gain reduction. Lower usually means more RF gain.")
    scan.add_argument("--ifgr", type=float, default=35.0, help="SDRplay IF gain reduction. Lower usually means more IF gain.")
    scan.add_argument("--active-threshold-db", type=float, default=6.0)
    scan.add_argument("--top", type=int, default=0, help="Only show the top N rows after sorting. 0 shows all.")
    scan.add_argument("--sort", choices=["score", "freq"], default="score")
    scan.add_argument("--json", action="store_true")
    scan.add_argument("--jsonl", action="store_true", help="Emit one compact JSON event per scanned signal.")
    scan.add_argument("--active-only", action="store_true", help="With --jsonl, only emit rows above the active threshold.")
    scan.add_argument("--csv", action="store_true")
    scan.add_argument("--debug", action="store_true")
    scan.add_argument("--show-driver-log", action="store_true", help="Show native SDRplay/SoapySDR startup messages.")
    scan.add_argument("--yes", action="store_true", help="Allow long scans with more than 400 channels.")
    scan.set_defaults(func=_scan)
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
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        if getattr(args, "debug", False):
            raise
        print(f"am-broadcast: {exc}", file=sys.stderr)
        return 1
