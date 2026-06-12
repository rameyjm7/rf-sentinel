from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
import contextlib
import json
import math
import os
import sys
import time
from pathlib import Path

from .decoder import Burst, BurstDetector, IEEE802154Decoder, channel_to_center_freq
from .gateway import GatewayClient, GatewayDevice, StreamConfig
from .wideband import (
    WidebandDetectorConfig,
    WidebandWindowPlan,
    build_wideband_window_plans,
    create_runtimes,
    detect_wideband_bursts,
    flush_wideband_bursts,
)


DEFAULT_CHANNEL = 25
DEFAULT_WIDEBAND_CHANNEL = 25
DEFAULT_SAMPLE_RATE_SPS = 4_000_000
DEFAULT_LISTEN_SAMPLE_RATE_SPS = 8_000_000
DEFAULT_WIDEBAND_CHANNEL_RATE_SPS = 8_000_000
DEFAULT_HACKRF_LNA_GAIN_DB = 16
DEFAULT_HACKRF_VGA_GAIN_DB = 32
DEFAULT_HACKRF_AMP_ENABLE = False
DEFAULT_BASEBAND_FILTER_HZ = 6_000_000
DEFAULT_OPEN_FACTOR = 6.0
DEFAULT_CLOSE_FACTOR = 3.0
DEFAULT_PRE_ROLL_MS = 0.2
DEFAULT_MIN_BURST_MS = 0.05
DEFAULT_MAX_BURST_MS = 5.0
DEFAULT_DECODE_MIN_BURST_MS = 0.8
DEFAULT_DECODE_MIN_PEAK_DBFS = -27.0
DEFAULT_FREQUENCY_SEARCH_HZ = (0, -25_000, 25_000)
DEFAULT_WAVEFORM_PATTERN_CORR_MIN = 0.18
DEFAULT_LIVE_DECODE_QUEUE = 32
DEFAULT_WIDEBAND_DWELL_S = 2.0
DEFAULT_WIDEBAND_DISCOVERY_DWELL_S = 0.75
DEFAULT_WIDEBAND_ACTIVE_DWELL_S = 45.0
DEFAULT_WIDEBAND_RESCAN_INTERVAL_S = 120.0
DEFAULT_WIDEBAND_ACTIVITY_TTL_S = 120.0
DEFAULT_WIDEBAND_MAX_ACTIVE_DECODE_CHANNELS = 1
DEFAULT_OFFLINE_CHUNK_BYTES = 1 << 16
ANSI_GREEN = "\033[32m"
ANSI_RESET = "\033[0m"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zigbee-802154", description="Zigbee / IEEE 802.15.4 receive stack scaffold")
    subparsers = parser.add_subparsers(dest="command", required=True)

    devices = subparsers.add_parser("devices", help="list SDRs visible from sdr-gateway")
    devices.add_argument("--base-url", default=None)
    devices.add_argument("--token", default=None)

    capture = subparsers.add_parser("capture", help="capture raw CS8 IQ to a file for offline 802.15.4 analysis")
    capture.add_argument("--base-url", default=None)
    capture.add_argument("--token", default=None)
    capture.add_argument("--device-id", default=None)
    capture.add_argument("--channel", type=int, default=DEFAULT_CHANNEL)
    capture.add_argument("--center-freq-hz", type=int, default=None)
    capture.add_argument("--sample-rate-sps", type=int, default=DEFAULT_LISTEN_SAMPLE_RATE_SPS)
    capture.add_argument("--seconds", type=float, default=5.0)
    capture.add_argument("--lna-gain-db", type=int, default=None)
    capture.add_argument("--vga-gain-db", type=int, default=None)
    capture.add_argument("--amp-enable", action=argparse.BooleanOptionalAction, default=None)
    capture.add_argument("--baseband-filter-hz", type=int, default=DEFAULT_BASEBAND_FILTER_HZ)
    capture.add_argument("--output", required=True)

    listen = subparsers.add_parser("listen", help="start the live 802.15.4 receiver")
    listen.add_argument("--base-url", default=None)
    listen.add_argument("--token", default=None)
    listen.add_argument("--device-id", default=None)
    listen.add_argument("--channel", type=int, default=DEFAULT_CHANNEL)
    listen.add_argument("--center-freq-hz", type=int, default=None)
    listen.add_argument("--sample-rate-sps", type=int, default=DEFAULT_LISTEN_SAMPLE_RATE_SPS)
    listen.add_argument("--lna-gain-db", type=int, default=None)
    listen.add_argument("--vga-gain-db", type=int, default=None)
    listen.add_argument("--amp-enable", action=argparse.BooleanOptionalAction, default=None)
    listen.add_argument("--baseband-filter-hz", type=int, default=DEFAULT_BASEBAND_FILTER_HZ)
    listen.add_argument("--open-factor", type=float, default=DEFAULT_OPEN_FACTOR)
    listen.add_argument("--close-factor", type=float, default=DEFAULT_CLOSE_FACTOR)
    listen.add_argument("--pre-roll-ms", type=float, default=DEFAULT_PRE_ROLL_MS)
    listen.add_argument("--min-burst-ms", type=float, default=DEFAULT_MIN_BURST_MS)
    listen.add_argument("--max-burst-ms", type=float, default=DEFAULT_MAX_BURST_MS)
    listen.add_argument("--decode-min-burst-ms", type=float, default=DEFAULT_DECODE_MIN_BURST_MS)
    listen.add_argument("--decode-min-peak-dbfs", type=float, default=DEFAULT_DECODE_MIN_PEAK_DBFS)
    listen.add_argument("--debug-bursts", action=argparse.BooleanOptionalAction, default=True)
    listen.add_argument("--debug-skips", action="store_true", help="print every burst rejected by the live prefilter")
    listen.add_argument("--json", action="store_true")
    listen.add_argument("--max-frames", type=int, default=0, help="stop after this many decoded frames; 0 means run forever")
    listen.add_argument("--reconnect-delay-seconds", type=float, default=1.0)
    listen.add_argument("--live-decode-workers", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    listen.add_argument("--live-decode-queue", type=int, default=DEFAULT_LIVE_DECODE_QUEUE)
    _add_decoder_arguments(listen)

    wideband = subparsers.add_parser("wideband-listen", help="sweep the full 2.4 GHz 802.15.4 band using the SDR's widest sample rate")
    wideband.add_argument("--base-url", default=None)
    wideband.add_argument("--token", default=None)
    wideband.add_argument("--device-id", default=None)
    wideband.add_argument(
        "--channel",
        type=int,
        default=DEFAULT_WIDEBAND_CHANNEL,
        help="wideband window anchor channel; defaults to the tested XBee channel",
    )
    wideband.add_argument(
        "--scan-all-windows",
        action="store_true",
        help="retune across every 802.15.4 window instead of staying on the anchor channel window",
    )
    wideband.add_argument(
        "--adaptive-scan",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="periodically discover active channels, then dwell on active windows",
    )
    wideband.add_argument(
        "--decode-all-channels",
        action="store_true",
        help="decode every channel in the active wideband window instead of only the anchor channel",
    )
    wideband.add_argument(
        "--center-active-channel",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="retune active dwell so the selected active channel is at SDR center",
    )
    wideband.add_argument("--sample-rate-sps", type=int, default=0, help="0 means use the device max sample rate")
    wideband.add_argument("--channel-rate-sps", type=int, default=DEFAULT_WIDEBAND_CHANNEL_RATE_SPS)
    wideband.add_argument("--window-dwell-s", type=float, default=DEFAULT_WIDEBAND_DWELL_S)
    wideband.add_argument("--discovery-dwell-s", type=float, default=DEFAULT_WIDEBAND_DISCOVERY_DWELL_S)
    wideband.add_argument("--active-dwell-s", type=float, default=DEFAULT_WIDEBAND_ACTIVE_DWELL_S)
    wideband.add_argument("--rescan-interval-s", type=float, default=DEFAULT_WIDEBAND_RESCAN_INTERVAL_S)
    wideband.add_argument("--activity-ttl-s", type=float, default=DEFAULT_WIDEBAND_ACTIVITY_TTL_S)
    wideband.add_argument("--activity-min-burst-ms", type=float, default=0.5)
    wideband.add_argument("--activity-min-peak-dbfs", type=float, default=-30.0)
    wideband.add_argument(
        "--max-active-decode-channels",
        type=int,
        default=DEFAULT_WIDEBAND_MAX_ACTIVE_DECODE_CHANNELS,
        help="maximum active channels to decode per active window; lower values reduce false-positive backpressure",
    )
    wideband.add_argument(
        "--follow-energy-only",
        action="store_true",
        help="allow adaptive active dwell to follow energy-only channels that have not decoded frames",
    )
    wideband.add_argument("--lna-gain-db", type=int, default=None)
    wideband.add_argument("--vga-gain-db", type=int, default=None)
    wideband.add_argument("--amp-enable", action=argparse.BooleanOptionalAction, default=None)
    wideband.add_argument("--baseband-filter-hz", type=int, default=0, help="0 means follow the wideband sample rate")
    wideband.add_argument("--open-factor", type=float, default=DEFAULT_OPEN_FACTOR)
    wideband.add_argument("--close-factor", type=float, default=DEFAULT_CLOSE_FACTOR)
    wideband.add_argument("--pre-roll-ms", type=float, default=DEFAULT_PRE_ROLL_MS)
    wideband.add_argument("--min-burst-ms", type=float, default=DEFAULT_MIN_BURST_MS)
    wideband.add_argument("--max-burst-ms", type=float, default=DEFAULT_MAX_BURST_MS)
    wideband.add_argument("--decode-min-burst-ms", type=float, default=DEFAULT_DECODE_MIN_BURST_MS)
    wideband.add_argument("--decode-min-peak-dbfs", type=float, default=DEFAULT_DECODE_MIN_PEAK_DBFS)
    wideband.add_argument("--debug-bursts", action="store_true")
    wideband.add_argument("--reconnect-delay-seconds", type=float, default=1.0)
    wideband.add_argument("--live-decode-workers", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    wideband.add_argument("--live-decode-queue", type=int, default=DEFAULT_LIVE_DECODE_QUEUE)
    wideband.add_argument("--json", action="store_true")
    wideband.add_argument("--max-frames", type=int, default=0)
    _add_decoder_arguments(wideband)

    decode_file = subparsers.add_parser("decode-file", help="decode 802.15.4 bursts from a captured CS8 IQ file")
    decode_file.add_argument("--input", required=True)
    decode_file.add_argument("--channel", type=int, default=DEFAULT_CHANNEL)
    decode_file.add_argument("--center-freq-hz", type=int, default=None)
    decode_file.add_argument("--sample-rate-sps", type=int, default=DEFAULT_LISTEN_SAMPLE_RATE_SPS)
    decode_file.add_argument("--open-factor", type=float, default=DEFAULT_OPEN_FACTOR)
    decode_file.add_argument("--close-factor", type=float, default=DEFAULT_CLOSE_FACTOR)
    decode_file.add_argument("--pre-roll-ms", type=float, default=DEFAULT_PRE_ROLL_MS)
    decode_file.add_argument("--min-burst-ms", type=float, default=DEFAULT_MIN_BURST_MS)
    decode_file.add_argument("--max-burst-ms", type=float, default=DEFAULT_MAX_BURST_MS)
    decode_file.add_argument("--chunk-bytes", type=int, default=DEFAULT_OFFLINE_CHUNK_BYTES)
    decode_file.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 1) // 2))
    decode_file.add_argument("--debug-bursts", action="store_true")
    decode_file.add_argument("--json", action="store_true")
    decode_file.add_argument("--max-bursts", type=int, default=0, help="stop after this many detected bursts; 0 means decode all bursts")
    _add_decoder_arguments(decode_file)
    return parser


def _add_decoder_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--symbol-error-limit", type=int, default=10)
    parser.add_argument("--max-chip-hamming-distance", type=int, default=12)
    parser.add_argument("--pattern-chip-error-limit", type=int, default=120)
    parser.add_argument("--phase-search-steps", type=int, default=8)
    parser.add_argument(
        "--frequency-search-hz",
        type=int,
        nargs="*",
        default=list(DEFAULT_FREQUENCY_SEARCH_HZ),
        help="coarse carrier-offset search bins in Hz",
    )
    parser.add_argument("--start-search-symbols", type=int, default=24)
    parser.add_argument("--waveform-pattern-corr-min", type=float, default=DEFAULT_WAVEFORM_PATTERN_CORR_MIN)


def _build_decoder(args: argparse.Namespace) -> IEEE802154Decoder:
    return IEEE802154Decoder(
        symbol_error_limit=int(args.symbol_error_limit),
        phase_search_steps=int(args.phase_search_steps),
        max_chip_hamming_distance=int(args.max_chip_hamming_distance),
        pattern_chip_error_limit=int(args.pattern_chip_error_limit),
        start_search_symbols=int(args.start_search_symbols),
        frequency_search_hz=tuple(int(entry) for entry in args.frequency_search_hz),
        waveform_pattern_corr_min=float(args.waveform_pattern_corr_min),
    )


def _run_devices(args: argparse.Namespace) -> int:
    client = GatewayClient(base_url=args.base_url, token=args.token)
    devices = client.list_devices()
    if not devices:
        print("no devices visible from sdr-gateway", file=sys.stderr)
        return 1
    for device in devices:
        occupied = "occupied" if device.occupied else "idle"
        print(
            f"{device.id} driver={device.driver} label={device.label} "
            f"freq=[{device.freq_min_hz},{device.freq_max_hz}] "
            f"max_rate={device.max_sample_rate_sps} {occupied}"
        )
    return 0


def _capture_metadata_path(output_path: Path) -> Path:
    return output_path.with_suffix(output_path.suffix + ".json")


def _emit_frame(frame, json_mode: bool) -> None:
    if json_mode:
        print(frame.to_json(), flush=True)
        return
    mac_summary = ""
    text_summary = ""
    if frame.mac is not None:
        mac_summary = (
            f" type={frame.mac.frame_type}"
            f" seq={frame.mac.sequence_number if frame.mac.sequence_number is not None else '?'}"
            f" dst={frame.mac.destination_address or '-'}"
            f" src={frame.mac.source_address or '-'}"
        )
        text = _printable_payload_text(frame.mac.payload_hex)
        if text:
            text_summary = f" text={json.dumps(text)}"
    line = (
        f"frame ch={frame.channel or '?'} len={frame.phy_length} "
        f"confidence={frame.confidence:.3f} psdu={frame.hex}{mac_summary}{text_summary}"
    )
    if sys.stdout.isatty():
        line = f"{ANSI_GREEN}{line}{ANSI_RESET}"
    print(line, flush=True)


def _printable_payload_text(payload_hex: str) -> str:
    try:
        payload = bytes.fromhex(payload_hex)
    except ValueError:
        return ""
    runs: list[bytes] = []
    current = bytearray()
    for value in payload:
        if 0x20 <= value <= 0x7E:
            current.append(value)
            continue
        if len(current) >= 3:
            runs.append(bytes(current))
        current.clear()
    if len(current) >= 3:
        runs.append(bytes(current))
    if not runs:
        return ""
    return max(runs, key=len).decode("ascii", errors="replace")


def _dbfs(value: float) -> float:
    clamped = max(float(value), 1e-12)
    return 20.0 * math.log10(clamped)


def _gain_hint(peak: float, average: float) -> str:
    peak_dbfs = _dbfs(peak)
    avg_dbfs = _dbfs(average)
    if peak_dbfs >= -8.0:
        return "lower_gain"
    if peak_dbfs <= -24.0 and avg_dbfs <= -30.0:
        return "raise_gain"
    return "hold"


def _burst_diag_text(diagnostics) -> str:
    if diagnostics is None:
        return ""
    return (
        f" slicer={diagnostics.chip_slicer}"
        f" cfo_hz={diagnostics.frequency_offset_hz:.0f}"
        f" patt_corr={diagnostics.pattern_correlation:.3f}"
        f" phase_deg={diagnostics.phase_degrees:.1f}"
        f" chip_off={diagnostics.chip_offset}"
        f" sym_phase={diagnostics.symbol_phase}"
        f" sfd_hits={diagnostics.sfd_hits}"
        f" pat_miss={diagnostics.best_pattern_mismatches}"
        f" pat_err={diagnostics.best_pattern_error_sum}"
        f" cand_len={diagnostics.best_length if diagnostics.best_length is not None else '-'}"
        f" cand_hex={diagnostics.best_payload_preview_hex or '-'}"
        f" total_err={diagnostics.best_total_error_sum if diagnostics.best_total_error_sum is not None else '-'}"
        f" crc_ok={int(diagnostics.crc_ok)}"
    )


def _rf_hint(channel: int, burst: Burst, diagnostics) -> str:
    duration_ms = burst.duration_seconds * 1000.0
    pattern_corr = float(diagnostics.pattern_correlation) if diagnostics is not None else 0.0
    if int(channel) == 26 and 0.30 <= duration_ms <= 0.55 and pattern_corr < 0.30:
        return "possible_ble_adv39"
    return "-"


def _resolve_device(client: GatewayClient, requested_device_id: str | None) -> GatewayDevice:
    devices = client.list_devices()
    if requested_device_id:
        for device in devices:
            if device.id == requested_device_id:
                return device
        raise RuntimeError(f"Device {requested_device_id} not found via sdr-gateway")
    for device in devices:
        if device.driver.lower() == "hackrf":
            return device
    if devices:
        return devices[0]
    raise RuntimeError("No SDR devices are visible from sdr-gateway")


def _stream_config_for_device(
    *,
    device: GatewayDevice,
    center_freq_hz: int,
    sample_rate_sps: int,
    lna_gain_db: int | None,
    vga_gain_db: int | None,
    amp_enable: bool | None,
    baseband_filter_hz: int | None = None,
) -> StreamConfig:
    driver = device.driver.lower()
    default_lna = 16
    default_vga = 20
    default_amp = False
    if driver == "hackrf":
        default_lna = DEFAULT_HACKRF_LNA_GAIN_DB
        default_vga = DEFAULT_HACKRF_VGA_GAIN_DB
        default_amp = DEFAULT_HACKRF_AMP_ENABLE
    return StreamConfig(
        device_id=device.id,
        center_freq_hz=int(center_freq_hz),
        sample_rate_sps=int(sample_rate_sps),
        lna_gain_db=int(default_lna if lna_gain_db is None else lna_gain_db),
        vga_gain_db=int(default_vga if vga_gain_db is None else vga_gain_db),
        amp_enable=bool(default_amp if amp_enable is None else amp_enable),
        baseband_filter_hz=baseband_filter_hz,
    )


def _run_listen(args: argparse.Namespace) -> int:
    client = GatewayClient(base_url=args.base_url, token=args.token)
    device = _resolve_device(client, args.device_id)
    center_freq_hz = args.center_freq_hz or channel_to_center_freq(args.channel)
    sample_rate_sps = int(args.sample_rate_sps)
    config = _stream_config_for_device(
        device=device,
        center_freq_hz=center_freq_hz,
        sample_rate_sps=sample_rate_sps,
        lna_gain_db=args.lna_gain_db,
        vga_gain_db=args.vga_gain_db,
        amp_enable=args.amp_enable,
        baseband_filter_hz=int(args.baseband_filter_hz) if args.baseband_filter_hz else None,
    )
    print(
        f"using device={device.id} driver={device.driver} sr={config.sample_rate_sps} "
        f"lna={config.lna_gain_db} vga={config.vga_gain_db} amp={int(config.amp_enable)}",
        file=sys.stderr,
    )
    emitted = 0
    skipped = 0
    skip_duration_total_ms = 0.0
    skip_reported_at = time.monotonic()
    decode_backpressure_reported_at = time.monotonic()
    live_workers = max(1, int(args.live_decode_workers))
    live_queue = max(live_workers, int(args.live_decode_queue))
    reconnect_delay = max(0.1, float(args.reconnect_delay_seconds))
    stream = None
    pending = set()

    def decode_live(burst: Burst):
        started = time.perf_counter()
        task_decoder = _build_decoder(args)
        frame = task_decoder.decode(burst)
        return burst, frame, task_decoder.last_diagnostics, time.perf_counter() - started

    def drain_completed() -> bool:
        nonlocal emitted
        completed = [future for future in pending if future.done()]
        for future in completed:
            pending.discard(future)
            try:
                burst, frame, diagnostics, decode_seconds = future.result()
            except Exception as exc:
                if args.debug_bursts:
                    print(f"decode error={exc}", file=sys.stderr, flush=True)
                continue
            if frame is None:
                if args.debug_bursts:
                    print(
                        f"burst ch={args.channel} samples={burst.iq.size} "
                        f"ms={burst.duration_seconds * 1000.0:.3f} "
                        f"peak={burst.peak:.3f} peak_dbfs={_dbfs(burst.peak):.1f} "
                        f"avg={burst.average:.3f} avg_dbfs={_dbfs(burst.average):.1f}"
                        f" gain_hint={_gain_hint(burst.peak, burst.average)}"
                        f" rf_hint={_rf_hint(args.channel, burst, diagnostics)}"
                        f" decode_ms={decode_seconds * 1000.0:.1f}"
                        f"{_burst_diag_text(diagnostics)}",
                        file=sys.stderr,
                    )
                continue
            emitted += 1
            _emit_frame(frame, json_mode=bool(args.json))
            if args.max_frames and emitted >= args.max_frames:
                return True
        return False

    def wait_for_decode_capacity() -> bool:
        nonlocal decode_backpressure_reported_at
        while len(pending) >= live_queue:
            if args.debug_bursts and (time.monotonic() - decode_backpressure_reported_at) >= 5.0:
                print(
                    f"decode_queue ch={args.channel} pending={len(pending)} "
                    f"waiting=1 workers={live_workers}",
                    file=sys.stderr,
                )
                decode_backpressure_reported_at = time.monotonic()
            done, _not_done = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
            if done and drain_completed():
                return True
        return False

    executor = ThreadPoolExecutor(max_workers=live_workers)
    try:
        while True:
            if drain_completed():
                return 0
            try:
                stream = client.start_stream(config)
                detector = BurstDetector(
                    sample_rate_sps=stream.sample_rate_sps,
                    center_freq_hz=stream.center_freq_hz,
                    stream_id=stream.stream_id,
                    pre_roll_ms=float(args.pre_roll_ms),
                    open_factor=float(args.open_factor),
                    close_factor=float(args.close_factor),
                    min_burst_ms=float(args.min_burst_ms),
                    max_burst_ms=float(args.max_burst_ms),
                )
                for chunk in client.iter_iq_chunks(stream.stream_id):
                    if drain_completed():
                        return 0
                    for burst in detector.ingest(chunk):
                        if drain_completed():
                            return 0
                        duration_ms = burst.duration_seconds * 1000.0
                        peak_dbfs = _dbfs(burst.peak)
                        if (
                            duration_ms < float(args.decode_min_burst_ms)
                            or peak_dbfs < float(args.decode_min_peak_dbfs)
                        ):
                            skipped += 1
                            skip_duration_total_ms += duration_ms
                            if args.debug_skips:
                                print(
                                    f"skip ch={args.channel} samples={burst.iq.size} "
                                    f"ms={duration_ms:.3f} peak_dbfs={peak_dbfs:.1f} "
                                    f"reason=live_prefilter",
                                    file=sys.stderr,
                                )
                            elif args.debug_bursts and (time.monotonic() - skip_reported_at) >= 5.0:
                                print(
                                    f"prefilter ch={args.channel} skipped={skipped} "
                                    f"avg_ms={skip_duration_total_ms / skipped:.3f} "
                                    f"min_ms={float(args.decode_min_burst_ms):.3f} "
                                    f"min_peak_dbfs={float(args.decode_min_peak_dbfs):.1f}",
                                    file=sys.stderr,
                                )
                                skipped = 0
                                skip_duration_total_ms = 0.0
                                skip_reported_at = time.monotonic()
                            continue
                        if wait_for_decode_capacity():
                            return 0
                        pending.add(executor.submit(decode_live, burst))
                print(
                    f"stream closed; reconnecting in {reconnect_delay:.1f}s",
                    file=sys.stderr,
                    flush=True,
                )
            except KeyboardInterrupt:
                return 0
            except Exception as exc:
                print(
                    f"stream error={exc}; reconnecting in {reconnect_delay:.1f}s",
                    file=sys.stderr,
                    flush=True,
                )
            finally:
                if stream is not None:
                    with contextlib.suppress(Exception):
                        client.stop_stream(stream.stream_id)
                    stream = None
            time.sleep(reconnect_delay)
    finally:
        for future in pending:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)


def _run_capture(args: argparse.Namespace) -> int:
    client = GatewayClient(base_url=args.base_url, token=args.token)
    device = _resolve_device(client, args.device_id)
    center_freq_hz = args.center_freq_hz or channel_to_center_freq(args.channel)
    sample_rate_sps = int(args.sample_rate_sps)
    config = _stream_config_for_device(
        device=device,
        center_freq_hz=center_freq_hz,
        sample_rate_sps=sample_rate_sps,
        lna_gain_db=args.lna_gain_db,
        vga_gain_db=args.vga_gain_db,
        amp_enable=args.amp_enable,
        baseband_filter_hz=int(args.baseband_filter_hz) if args.baseband_filter_hz else None,
    )
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    print(
        f"capturing device={device.id} driver={device.driver} ch={args.channel} "
        f"center={config.center_freq_hz} sr={config.sample_rate_sps} "
        f"lna={config.lna_gain_db} vga={config.vga_gain_db} amp={int(config.amp_enable)} "
        f"seconds={float(args.seconds):.3f} output={output_path}",
        file=sys.stderr,
    )
    target_samples = max(1, int(round(float(args.seconds) * float(config.sample_rate_sps))))
    stream = client.start_stream(
        StreamConfig(
            device_id=config.device_id,
            center_freq_hz=config.center_freq_hz,
            sample_rate_sps=config.sample_rate_sps,
            lna_gain_db=config.lna_gain_db,
            vga_gain_db=config.vga_gain_db,
            amp_enable=config.amp_enable,
            baseband_filter_hz=config.baseband_filter_hz,
            num_samples=target_samples,
        )
    )
    chunk_count = 0
    byte_count = 0
    try:
        with output_path.open("wb") as handle:
            for chunk in client.iter_iq_chunks(stream.stream_id, keep_stream=False):
                handle.write(chunk)
                byte_count += len(chunk)
                chunk_count += 1
                if chunk_count % 16 == 0:
                    print(
                        f"capture progress bytes={byte_count} complex_samples={byte_count // 2} "
                        f"seconds={(byte_count // 2) / float(config.sample_rate_sps):.3f}",
                        file=sys.stderr,
                    )
        metadata = {
            "format": "cs8",
            "device_id": device.id,
            "driver": device.driver,
            "channel": int(args.channel),
            "center_freq_hz": config.center_freq_hz,
            "sample_rate_sps": config.sample_rate_sps,
            "lna_gain_db": config.lna_gain_db,
            "vga_gain_db": config.vga_gain_db,
            "amp_enable": config.amp_enable,
            "baseband_filter_hz": config.baseband_filter_hz,
            "capture_seconds_requested": float(args.seconds),
            "capture_samples_requested": target_samples,
            "bytes_written": byte_count,
            "complex_samples": byte_count // 2,
        }
        _capture_metadata_path(output_path).write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
        print(
            f"capture complete bytes={byte_count} complex_samples={byte_count // 2} chunks={chunk_count} "
            f"meta={_capture_metadata_path(output_path)}",
            file=sys.stderr,
        )
        return 0
    finally:
        with contextlib.suppress(Exception):
            client.stop_stream(stream.stream_id)


def _run_wideband_listen(args: argparse.Namespace) -> int:
    client = GatewayClient(base_url=args.base_url, token=args.token)
    device = _resolve_device(client, args.device_id)

    sample_rate_sps = int(args.sample_rate_sps) if int(args.sample_rate_sps) > 0 else int(device.max_sample_rate_sps)
    windows = build_wideband_window_plans(sample_rate_sps=sample_rate_sps)
    baseband_filter_hz = int(args.baseband_filter_hz) if int(args.baseband_filter_hz) > 0 else sample_rate_sps
    window_index = 0
    requested_channel = int(args.channel)
    for index, window in enumerate(windows):
        if requested_channel in window.channels:
            window_index = index
            break
    else:
        raise RuntimeError(f"channel {requested_channel} is not covered by the wideband window plan")

    def stream_config_for_window(window):
        return _stream_config_for_device(
            device=device,
            center_freq_hz=window.center_freq_hz,
            sample_rate_sps=sample_rate_sps,
            lna_gain_db=args.lna_gain_db,
            vga_gain_db=args.vga_gain_db,
            amp_enable=args.amp_enable,
            baseband_filter_hz=baseband_filter_hz,
        )

    initial_config = stream_config_for_window(windows[window_index])
    print(
        f"using device={device.id} driver={device.driver} sr={initial_config.sample_rate_sps} "
        f"lna={initial_config.lna_gain_db} vga={initial_config.vga_gain_db} amp={int(initial_config.amp_enable)} "
        f"bb_filter={initial_config.baseband_filter_hz}",
        file=sys.stderr,
    )
    print(
        f"wideband plan sample_rate={sample_rate_sps} windows={len(windows)} "
        f"channels_per_window={[len(window.channels) for window in windows]} "
        f"start_window={window_index} scan_all={int(bool(args.scan_all_windows))} "
        f"adaptive={int(bool(args.adaptive_scan) and not bool(args.scan_all_windows))} "
        f"follow_energy_only={int(bool(args.follow_energy_only))}",
        file=sys.stderr,
    )
    for window in windows:
        print(
            f"window[{window.index}] center={window.center_freq_hz} channels={list(window.channels)}",
            file=sys.stderr,
        )

    detector_config = WidebandDetectorConfig(
        pre_roll_ms=float(args.pre_roll_ms),
        open_factor=float(args.open_factor),
        close_factor=float(args.close_factor),
        min_burst_ms=float(args.min_burst_ms),
        max_burst_ms=float(args.max_burst_ms),
    )
    emitted = 0
    skipped = 0
    skip_duration_total_ms = 0.0
    skip_reported_at = time.monotonic()
    decode_backpressure_reported_at = time.monotonic()
    live_workers = max(1, int(args.live_decode_workers))
    live_queue = max(live_workers, int(args.live_decode_queue))
    reconnect_delay = max(0.1, float(args.reconnect_delay_seconds))
    pending = set()
    stream = None
    adaptive_scan = bool(args.adaptive_scan) and not bool(args.scan_all_windows)
    discovery_mode = adaptive_scan
    discovery_cursor = 0
    active_cursor = 0
    last_discovery_completed_at = 0.0
    active_channels: dict[int, dict[str, float]] = {}
    active_decode_channels: set[int] = {requested_channel}

    def prune_active_channels(now: float) -> None:
        ttl_s = max(1.0, float(args.activity_ttl_s))
        for channel in list(active_channels.keys()):
            if now - float(active_channels[channel].get("last_seen", 0.0)) > ttl_s:
                del active_channels[channel]

    def record_activity(channel: int, burst: Burst, *, decoded_frame: bool = False) -> None:
        duration_ms = burst.duration_seconds * 1000.0
        peak_dbfs = _dbfs(burst.peak)
        if not decoded_frame and (
            duration_ms < float(args.activity_min_burst_ms)
            or peak_dbfs < float(args.activity_min_peak_dbfs)
        ):
            return
        now = time.monotonic()
        entry = active_channels.setdefault(
            int(channel),
            {"score": 0.0, "last_seen": now, "bursts": 0.0, "frames": 0.0},
        )
        entry["score"] = (float(entry.get("score", 0.0)) * 0.98) + (8.0 if decoded_frame else 1.0)
        entry["last_seen"] = now
        entry["bursts"] = float(entry.get("bursts", 0.0)) + 1.0
        if decoded_frame:
            entry["frames"] = float(entry.get("frames", 0.0)) + 1.0

    def window_for_channel(channel: int) -> int:
        for index, window in enumerate(windows):
            if int(channel) in window.channels:
                return index
        return window_index

    def active_window_indices(now: float) -> list[int]:
        prune_active_channels(now)
        scores: dict[int, float] = {}
        for channel, data in active_channels.items():
            if not bool(args.follow_energy_only) and float(data.get("frames", 0.0)) <= 0.0:
                continue
            index = window_for_channel(channel)
            scores[index] = (
                scores.get(index, 0.0)
                + (float(data.get("frames", 0.0)) * 100.0)
                + float(data.get("score", 0.0))
            )
        if not scores:
            return [window_for_channel(requested_channel)]
        return [
            index
            for index, _score in sorted(
                scores.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ]

    def channels_for_window(index: int) -> set[int]:
        now = time.monotonic()
        prune_active_channels(now)
        candidates = [
            int(channel)
            for channel in active_channels
            if int(channel) in windows[index].channels
        ]
        if not candidates and requested_channel in windows[index].channels:
            return {requested_channel}
        max_channels = max(1, int(args.max_active_decode_channels))
        if requested_channel in candidates:
            selected = [requested_channel]
            if max_channels > 1:
                others = [channel for channel in candidates if channel != requested_channel]
                others = sorted(
                    others,
                    key=lambda channel: (
                        float(active_channels.get(int(channel), {}).get("frames", 0.0)),
                        float(active_channels.get(int(channel), {}).get("score", 0.0)),
                    ),
                    reverse=True,
                )
                selected.extend(others[: max_channels - 1])
            return set(selected)

        def channel_rank(channel: int) -> tuple[float, float, int]:
            data = active_channels.get(int(channel), {})
            requested_bonus = 1 if int(channel) == requested_channel else 0
            return (
                float(data.get("frames", 0.0)),
                float(data.get("score", 0.0)),
                requested_bonus,
            )

        ranked = sorted(candidates, key=channel_rank, reverse=True)
        selected = set(ranked[:max_channels])
        if requested_channel in candidates:
            requested_data = active_channels.get(requested_channel, {})
            if float(requested_data.get("frames", 0.0)) > 0:
                selected = {requested_channel, *list(selected)[: max_channels - 1]}
        return selected

    def active_summary() -> str:
        now = time.monotonic()
        prune_active_channels(now)
        if not active_channels:
            return "none"
        parts = []
        for channel, data in sorted(active_channels.items(), key=lambda item: (-item[1].get("score", 0.0), item[0])):
            age = now - float(data.get("last_seen", now))
            parts.append(
                f"ch{channel}:score={float(data.get('score', 0.0)):.1f},"
                f"bursts={int(data.get('bursts', 0.0))},"
                f"frames={int(data.get('frames', 0.0))},age={age:.1f}s"
            )
        return ";".join(parts)

    def selected_center_channel(window, decode_channels: set[int]) -> int | None:
        candidates = [channel for channel in decode_channels if int(channel) in window.channels]
        if not candidates:
            return None
        if requested_channel in candidates:
            return requested_channel
        return max(
            candidates,
            key=lambda channel: float(active_channels.get(int(channel), {}).get("score", 0.0)),
        )

    def effective_window_for_mode(window, mode_label: str, decode_channels: set[int]):
        if not bool(args.center_active_channel):
            return window, None
        if mode_label not in {"active", "park"}:
            return window, None
        center_channel = selected_center_channel(window, decode_channels)
        if center_channel is None:
            return window, None
        center_freq_hz = channel_to_center_freq(center_channel)
        return (
            WidebandWindowPlan(
                index=window.index,
                center_freq_hz=center_freq_hz,
                sample_rate_sps=window.sample_rate_sps,
                channels=window.channels,
            ),
            center_channel,
        )

    def decode_live(channel: int, burst: Burst):
        started = time.perf_counter()
        task_decoder = _build_decoder(args)
        frame = task_decoder.decode(burst)
        return channel, burst, frame, task_decoder.last_diagnostics, time.perf_counter() - started

    def drain_completed() -> bool:
        nonlocal emitted
        completed = [future for future in pending if future.done()]
        for future in completed:
            pending.discard(future)
            try:
                channel, burst, frame, diagnostics, decode_seconds = future.result()
            except Exception as exc:
                if args.debug_bursts:
                    print(f"decode error={exc}", file=sys.stderr, flush=True)
                continue
            if frame is None:
                if args.debug_bursts:
                    print(
                        f"burst ch={channel} samples={burst.iq.size} "
                        f"ms={burst.duration_seconds * 1000.0:.3f} "
                        f"peak={burst.peak:.3f} peak_dbfs={_dbfs(burst.peak):.1f} "
                        f"avg={burst.average:.3f} avg_dbfs={_dbfs(burst.average):.1f}"
                        f" gain_hint={_gain_hint(burst.peak, burst.average)}"
                        f" decode_ms={decode_seconds * 1000.0:.1f}"
                        f"{_burst_diag_text(diagnostics)}",
                        file=sys.stderr,
                    )
                continue
            emitted += 1
            record_activity(channel, burst, decoded_frame=True)
            _emit_frame(frame, json_mode=bool(args.json))
            if args.max_frames and emitted >= args.max_frames:
                return True
        return False

    def wait_for_decode_capacity() -> bool:
        nonlocal decode_backpressure_reported_at
        while len(pending) >= live_queue:
            if args.debug_bursts and (time.monotonic() - decode_backpressure_reported_at) >= 5.0:
                print(
                    f"wideband_decode_queue pending={len(pending)} waiting=1 workers={live_workers}",
                    file=sys.stderr,
                )
                decode_backpressure_reported_at = time.monotonic()
            done, _not_done = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
            if done and drain_completed():
                return True
        return False

    def enqueue_burst(channel: int, burst: Burst) -> bool:
        nonlocal skipped, skip_duration_total_ms, skip_reported_at
        if not bool(args.decode_all_channels) and int(channel) not in active_decode_channels:
            return False
        duration_ms = burst.duration_seconds * 1000.0
        peak_dbfs = _dbfs(burst.peak)
        if duration_ms < float(args.decode_min_burst_ms) or peak_dbfs < float(args.decode_min_peak_dbfs):
            skipped += 1
            skip_duration_total_ms += duration_ms
            if args.debug_bursts and (time.monotonic() - skip_reported_at) >= 5.0:
                print(
                    f"wideband_prefilter skipped={skipped} avg_ms={skip_duration_total_ms / skipped:.3f} "
                    f"min_ms={float(args.decode_min_burst_ms):.3f} "
                    f"min_peak_dbfs={float(args.decode_min_peak_dbfs):.1f}",
                    file=sys.stderr,
                )
                skipped = 0
                skip_duration_total_ms = 0.0
                skip_reported_at = time.monotonic()
            return False
        if wait_for_decode_capacity():
            return True
        pending.add(executor.submit(decode_live, channel, burst))
        return False

    executor = ThreadPoolExecutor(max_workers=live_workers)
    try:
        while True:
            if drain_completed():
                return 0
            current_window = windows[window_index]
            if adaptive_scan:
                if discovery_mode:
                    active_decode_channels = set()
                    dwell_target_s = max(0.1, float(args.discovery_dwell_s))
                    mode_label = "discover"
                else:
                    active_decode_channels = channels_for_window(window_index)
                    dwell_target_s = max(0.5, float(args.active_dwell_s))
                    mode_label = "active"
            else:
                active_decode_channels = set(current_window.channels) if bool(args.decode_all_channels) else {requested_channel}
                dwell_target_s = max(0.1, float(args.window_dwell_s))
                mode_label = "scan" if bool(args.scan_all_windows) else "park"
            runtime_window, centered_channel = effective_window_for_mode(
                current_window,
                mode_label,
                active_decode_channels,
            )
            runtimes = create_runtimes(
                runtime_window,
                channel_rate_sps=int(args.channel_rate_sps),
                detector_config=detector_config,
            )
            elapsed_in_window_s = 0.0
            try:
                if args.debug_bursts:
                    print(
                        f"wideband_window start mode={mode_label} index={current_window.index} "
                        f"center={runtime_window.center_freq_hz} channels={list(current_window.channels)} "
                        f"centered_ch={centered_channel if centered_channel is not None else '-'} "
                        f"decode_channels={sorted(active_decode_channels) if active_decode_channels else '-'} "
                        f"dwell_s={dwell_target_s:.3f} active={active_summary()}",
                        file=sys.stderr,
                        flush=True,
                    )
                stream = client.start_stream(stream_config_for_window(runtime_window))
                for chunk in client.iter_iq_chunks(stream.stream_id):
                    if drain_completed():
                        return 0
                    elapsed_in_window_s += float(len(chunk) // 2) / float(sample_rate_sps)
                    for runtime, burst in detect_wideband_bursts(
                        raw_chunk=chunk,
                        input_sample_rate_sps=sample_rate_sps,
                        runtimes=runtimes,
                    ):
                        record_activity(runtime.plan.channel, burst)
                        if adaptive_scan and discovery_mode:
                            continue
                        if enqueue_burst(runtime.plan.channel, burst):
                            return 0

                    should_advance = elapsed_in_window_s >= dwell_target_s and (
                        adaptive_scan or bool(args.scan_all_windows)
                    )
                    if should_advance:
                        for runtime, burst in flush_wideband_bursts(runtimes):
                            record_activity(runtime.plan.channel, burst)
                            if adaptive_scan and discovery_mode:
                                continue
                            if enqueue_burst(runtime.plan.channel, burst):
                                return 0
                        previous_window_index = current_window.index
                        if adaptive_scan:
                            if discovery_mode:
                                discovery_cursor += 1
                                if discovery_cursor < len(windows):
                                    window_index = discovery_cursor
                                else:
                                    discovery_mode = False
                                    discovery_cursor = 0
                                    active_cursor = 0
                                    last_discovery_completed_at = time.monotonic()
                                    active_indices = active_window_indices(last_discovery_completed_at)
                                    window_index = active_indices[0]
                                    if args.debug_bursts:
                                        print(
                                            f"wideband_activity active={active_summary()} "
                                            f"active_windows={active_indices}",
                                            file=sys.stderr,
                                            flush=True,
                                        )
                            else:
                                now = time.monotonic()
                                if now - last_discovery_completed_at >= max(1.0, float(args.rescan_interval_s)):
                                    discovery_mode = True
                                    discovery_cursor = 0
                                    window_index = 0
                                    if args.debug_bursts:
                                        print(
                                            f"wideband_rescan active={active_summary()}",
                                            file=sys.stderr,
                                            flush=True,
                                        )
                                else:
                                    active_indices = active_window_indices(now)
                                    if not active_indices:
                                        active_indices = [window_for_channel(requested_channel)]
                                    active_cursor = (active_cursor + 1) % len(active_indices)
                                    window_index = active_indices[active_cursor]
                        else:
                            window_index = (window_index + 1) % len(windows)
                        current_window = windows[window_index]
                        if adaptive_scan:
                            if discovery_mode:
                                active_decode_channels = set()
                                dwell_target_s = max(0.1, float(args.discovery_dwell_s))
                                mode_label = "discover"
                            else:
                                active_decode_channels = channels_for_window(window_index)
                                dwell_target_s = max(0.5, float(args.active_dwell_s))
                                mode_label = "active"
                        else:
                            active_decode_channels = set(current_window.channels) if bool(args.decode_all_channels) else {requested_channel}
                            dwell_target_s = max(0.1, float(args.window_dwell_s))
                            mode_label = "scan" if bool(args.scan_all_windows) else "park"
                        runtime_window, centered_channel = effective_window_for_mode(
                            current_window,
                            mode_label,
                            active_decode_channels,
                        )
                        runtimes = create_runtimes(
                            runtime_window,
                            channel_rate_sps=int(args.channel_rate_sps),
                            detector_config=detector_config,
                        )
                        elapsed_in_window_s = 0.0
                        if args.debug_bursts:
                            print(
                                f"wideband_window {'retune' if current_window.index != previous_window_index else 'continue'} "
                                f"mode={mode_label} index={current_window.index} "
                                f"center={runtime_window.center_freq_hz} channels={list(current_window.channels)} "
                                f"centered_ch={centered_channel if centered_channel is not None else '-'} "
                                f"decode_channels={sorted(active_decode_channels) if active_decode_channels else '-'} "
                                f"dwell_s={dwell_target_s:.3f}",
                                file=sys.stderr,
                                flush=True,
                            )
                        if current_window.index == previous_window_index and stream.center_freq_hz == runtime_window.center_freq_hz:
                            continue
                        stream = client.retune_stream(
                            stream.stream_id,
                            stream_config_for_window(runtime_window),
                        )
                print(
                    f"stream closed; reconnecting in {reconnect_delay:.1f}s",
                    file=sys.stderr,
                    flush=True,
                )
            except KeyboardInterrupt:
                return 0
            except Exception as exc:
                print(
                    f"stream error={exc}; reconnecting in {reconnect_delay:.1f}s",
                    file=sys.stderr,
                    flush=True,
                )
            finally:
                if stream is not None:
                    with contextlib.suppress(Exception):
                        client.stop_stream(stream.stream_id)
                    stream = None
            time.sleep(reconnect_delay)
    finally:
        for future in pending:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)


def _run_decode_file(args: argparse.Namespace) -> int:
    input_path = Path(args.input).expanduser().resolve()
    file_size = input_path.stat().st_size
    center_freq_hz = args.center_freq_hz or channel_to_center_freq(args.channel)
    print(
        f"decode-file input={input_path} bytes={file_size} complex_samples={file_size // 2} "
        f"channel={args.channel} center={center_freq_hz} sr={int(args.sample_rate_sps)} "
        f"chunk_bytes={int(args.chunk_bytes)}",
        file=sys.stderr,
    )
    sample_rate_sps = int(args.sample_rate_sps)
    detector = BurstDetector(
        sample_rate_sps=sample_rate_sps,
        center_freq_hz=center_freq_hz,
        stream_id=input_path.stem,
        pre_roll_ms=float(args.pre_roll_ms),
        open_factor=float(args.open_factor),
        close_factor=float(args.close_factor),
        min_burst_ms=float(args.min_burst_ms),
        max_burst_ms=float(args.max_burst_ms),
    )
    emitted = 0
    bursts_seen = 0
    threads = max(1, int(args.threads))

    def handle_burst_result(burst: Burst, frame, diagnostics) -> None:
        nonlocal bursts_seen, emitted
        bursts_seen += 1
        if frame is None:
            if args.debug_bursts:
                print(
                    f"burst file={input_path.name} samples={burst.iq.size} "
                    f"ms={burst.duration_seconds * 1000.0:.3f} "
                    f"peak={burst.peak:.3f} peak_dbfs={_dbfs(burst.peak):.1f} "
                    f"avg={burst.average:.3f} avg_dbfs={_dbfs(burst.average):.1f}"
                    f" gain_hint={_gain_hint(burst.peak, burst.average)}"
                    f" rf_hint={_rf_hint(args.channel, burst, diagnostics)}"
                    f"{_burst_diag_text(diagnostics)}",
                    file=sys.stderr,
                )
            return
        emitted += 1
        _emit_frame(frame, json_mode=bool(args.json))

    def decode_one(burst: Burst):
        decoder = _build_decoder(args)
        frame = decoder.decode(burst)
        return burst, frame, decoder.last_diagnostics

    def process_bursts(bursts: list[Burst]) -> None:
        nonlocal bursts_seen
        if not bursts:
            return
        if args.max_bursts:
            remaining = max(0, int(args.max_bursts) - bursts_seen)
            if remaining <= 0:
                return
            bursts = bursts[:remaining]
        if threads <= 1 or len(bursts) == 1:
            for burst in bursts:
                burst_result, frame, diagnostics = decode_one(burst)
                handle_burst_result(burst_result, frame, diagnostics)
            return
        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = [executor.submit(decode_one, burst) for burst in bursts]
            for future in as_completed(futures):
                burst_result, frame, diagnostics = future.result()
                handle_burst_result(burst_result, frame, diagnostics)

    chunk_bytes = max(2, int(args.chunk_bytes))
    if chunk_bytes % 2:
        chunk_bytes += 1
    sample_cursor = 0
    with input_path.open("rb") as handle:
        while True:
            raw = handle.read(chunk_bytes)
            if not raw:
                break
            process_bursts(detector.ingest(raw, timestamp=float(sample_cursor) / float(sample_rate_sps)))
            if args.max_bursts and bursts_seen >= int(args.max_bursts):
                break
            sample_cursor += len(raw) // 2
    if not args.max_bursts or bursts_seen < int(args.max_bursts):
        process_bursts(detector.flush())
    print(f"decode-file complete bursts={bursts_seen} frames={emitted} threads={threads}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "devices":
        return _run_devices(args)
    if args.command == "capture":
        return _run_capture(args)
    if args.command == "listen":
        return _run_listen(args)
    if args.command == "wideband-listen":
        return _run_wideband_listen(args)
    if args.command == "decode-file":
        return _run_decode_file(args)
    parser.error(f"unsupported command: {args.command}")
    return 2
