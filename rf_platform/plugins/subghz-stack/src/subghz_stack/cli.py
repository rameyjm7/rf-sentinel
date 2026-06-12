from __future__ import annotations

import argparse
import math
import json
import threading
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .analyzer import SignalAnalyzer
from .decoder import BurstDetector, ProtocolDecoder, build_decoder
from .gateway import GatewayClient, StreamConfig, StreamHandle


DEFAULT_CENTER_FREQ_HZ = 315_000_000
DEFAULT_SAMPLE_RATE_SPS = 2_000_000
DEFAULT_AUTO_HOP_DWELL_MS = 2_000
KNOWN_TPMS_FREQS_HZ = [315_000_000, 433_920_000]
DEFAULT_WIDEBAND_SAMPLE_RATE_SPS = 4_000_000
DEFAULT_315_BAND_START_HZ = 314_800_000
DEFAULT_315_BAND_END_HZ = 315_200_000
DEFAULT_433_BAND_START_HZ = 433_050_000
DEFAULT_433_BAND_END_HZ = 434_790_000
DEFAULT_BIN_WIDTH_HZ = 250_000
DEFAULT_CHANNEL_RATE_SPS = 500_000
MAX_PREVIEW_CHARS = 64
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_RESET = "\033[0m"


@dataclass
class CandidateObservation:
    first_seen: float
    last_seen: float
    count: int
    emitted: bool = False


@dataclass
class CandidateFamily:
    key: str
    first_seen: float
    last_seen: float
    count: int
    bit_length: int
    frequency_hz: int
    strategy: str
    unit_samples: int
    prefix: str
    checksum_hint: str


class CandidateRepeater:
    def __init__(self, min_repeats: int = 2, window_seconds: float = 20.0) -> None:
        self.min_repeats = max(1, int(min_repeats))
        self.window_seconds = float(window_seconds)
        self._seen: dict[str, CandidateObservation] = {}

    def observe(self, result, now: float) -> tuple[bool, int]:
        self._prune(now)
        key = self._key(result)
        current = self._seen.get(key)
        if current is None:
            current = CandidateObservation(first_seen=now, last_seen=now, count=1)
            self._seen[key] = current
        else:
            current.last_seen = now
            current.count += 1
        should_emit = current.count >= self.min_repeats and not current.emitted
        if should_emit:
            current.emitted = True
        return should_emit, current.count

    def _key(self, result) -> str:
        hex_string = result.hex_string
        prefix = hex_string[: min(16, len(hex_string))]
        return "|".join(
            [
                str(int(result.burst.center_freq_hz)),
                result.candidate.strategy,
                str(len(result.bits)),
                str(int(round(result.candidate.unit_samples))),
                prefix,
            ]
        )

    def _prune(self, now: float) -> None:
        stale = [key for key, value in self._seen.items() if now - value.last_seen > self.window_seconds]
        for key in stale:
            self._seen.pop(key, None)


class CandidateFamilyTracker:
    def __init__(self, window_seconds: float = 120.0) -> None:
        self.window_seconds = float(window_seconds)
        self._families: dict[str, CandidateFamily] = {}

    def observe(self, result, now: float) -> CandidateFamily:
        self._prune(now)
        prefix = result.hex_string[: min(8, len(result.hex_string))]
        unit_samples = int(round(result.candidate.unit_samples))
        checksum_hint = _checksum_hint(result.hex_string)
        current = self._find_match(
            frequency_hz=int(result.burst.center_freq_hz),
            strategy=result.candidate.strategy,
            bit_length=len(result.bits),
            unit_samples=unit_samples,
            prefix=prefix,
        )
        if current is None:
            key = "|".join(
                [
                    str(int(result.burst.center_freq_hz)),
                    result.candidate.strategy,
                    str(len(result.bits)),
                    str(unit_samples),
                    prefix,
                ]
            )
            current = CandidateFamily(
                key=key,
                first_seen=now,
                last_seen=now,
                count=1,
                bit_length=len(result.bits),
                frequency_hz=int(result.burst.center_freq_hz),
                strategy=result.candidate.strategy,
                unit_samples=unit_samples,
                prefix=prefix,
                checksum_hint=checksum_hint,
            )
            self._families[current.key] = current
            return current
        current.last_seen = now
        current.count += 1
        current.bit_length = int(round((current.bit_length + len(result.bits)) / 2.0))
        current.unit_samples = int(round((current.unit_samples + unit_samples) / 2.0))
        current.checksum_hint = checksum_hint
        return current

    def summary_lines(self, now: float, limit: int = 6) -> list[str]:
        self._prune(now)
        families = self.top_families(limit=limit)
        lines: list[str] = []
        for family in families:
            age_s = now - family.last_seen
            span_s = family.last_seen - family.first_seen
            lines.append(
                f"candidate_family freq={family.frequency_hz/1e6:.3f}MHz "
                f"bits_len={family.bit_length} strategy={family.strategy} "
                f"unit={family.unit_samples} prefix={family.prefix or '-'} "
                f"count={family.count} check={family.checksum_hint} "
                f"last_seen_s={age_s:.1f} span_s={span_s:.1f}"
            )
        return lines

    def top_families(self, limit: int = 6) -> list[CandidateFamily]:
        families = sorted(
            self._families.values(),
            key=lambda item: (item.count, item.last_seen, item.bit_length),
            reverse=True,
        )
        return families[: max(1, int(limit))]

    def snapshot(self, now: float, limit: int = 6) -> list[dict[str, Any]]:
        self._prune(now)
        out: list[dict[str, Any]] = []
        for family in self.top_families(limit=limit):
            out.append(
                {
                    "frequency_hz": family.frequency_hz,
                    "bit_length": family.bit_length,
                    "strategy": family.strategy,
                    "unit_samples": family.unit_samples,
                    "prefix": family.prefix,
                    "count": family.count,
                    "checksum_hint": family.checksum_hint,
                    "last_seen_s": round(now - family.last_seen, 3),
                    "span_s": round(family.last_seen - family.first_seen, 3),
                }
            )
        return out

    def _find_match(
        self,
        frequency_hz: int,
        strategy: str,
        bit_length: int,
        unit_samples: int,
        prefix: str,
    ) -> CandidateFamily | None:
        prefix_root = prefix[:6]
        for family in self._families.values():
            if family.frequency_hz != frequency_hz or family.strategy != strategy:
                continue
            if abs(family.bit_length - bit_length) > 4:
                continue
            if abs(family.unit_samples - unit_samples) > 2:
                continue
            if family.prefix[:6] != prefix_root:
                continue
            return family
        return None

    def _prune(self, now: float) -> None:
        stale = [key for key, value in self._families.items() if now - value.last_seen > self.window_seconds]
        for key in stale:
            self._families.pop(key, None)


def _checksum_hint(hex_string: str) -> str:
    try:
        payload = bytes.fromhex(hex_string)
    except ValueError:
        return "-"
    if len(payload) < 3:
        return "-"
    body = payload[:-1]
    check = payload[-1]
    if (sum(body) & 0xFF) == check:
        return "sum8"
    xor_value = 0
    for value in body:
        xor_value ^= value
    if xor_value == check:
        return "xor8"
    for poly, label in ((0x07, "crc8"), (0x31, "crc8-31"), (0x1D, "crc8-1d")):
        if _crc8(body, poly) == check:
            return label
    return "-"


def _protocol_detail_text(result) -> str:
    variant = getattr(result, "protocol_variant", None)
    fields = getattr(result, "decoded_fields", None) or {}
    if not variant and not fields:
        return ""
    parts: list[str] = []
    if variant:
        parts.append(f"family={variant}")
    for key in ("id", "pressure_kpa", "pressure_psi", "temperature_c", "temperature_f", "integrity"):
        value = fields.get(key)
        if value is not None and value != "":
            parts.append(f"{key}={value}")
    return " ".join(parts)


def _crc8(data: bytes, poly: int, init: int = 0x00) -> int:
    crc = init & 0xFF
    for value in data:
        crc ^= value
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ poly) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc & 0xFF


@dataclass
class SessionStats:
    started_at: float
    chunk_count: int = 0
    burst_count: int = 0
    packet_count: int = 0
    candidate_count: int = 0
    rejected_count: int = 0
    last_packet_at: float | None = None
    last_packet_freq_hz: int | None = None
    last_packet_bits_len: int | None = None
    last_packet_checksum: str | None = None


@dataclass(frozen=True)
class SweepProfile:
    frequencies_hz: list[int]
    dwell_ms: int | None = None


@dataclass(frozen=True)
class WidebandBandProfile:
    name: str
    band_start_hz: int
    band_end_hz: int
    center_freq_hz: int | None = None
    protocol: str = "tpms"


@dataclass(frozen=True)
class WidebandScanTarget:
    band_name: str
    window_name: str
    band_start_hz: int
    band_end_hz: int
    center_freq_hz: int
    protocol: str
    sample_rate_sps: int


class JsonTelemetry:
    def __init__(self, stats_path: Path | None = None, events_path: Path | None = None) -> None:
        self.stats_path = stats_path
        self.events_path = events_path
        if self.stats_path is not None:
            self.stats_path.parent.mkdir(parents=True, exist_ok=True)
        if self.events_path is not None:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)

    def write_event(self, event: dict[str, Any]) -> None:
        if self.events_path is None:
            return
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")

    def write_stats(self, payload: dict[str, Any]) -> None:
        if self.stats_path is None:
            return
        self.stats_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _session_snapshot(stats: SessionStats, family_tracker: CandidateFamilyTracker, now: float, limit: int = 8) -> dict[str, Any]:
    return {
        "started_at": round(stats.started_at, 3),
        "elapsed_s": round(now - stats.started_at, 3),
        "chunks": stats.chunk_count,
        "bursts": stats.burst_count,
        "packets": stats.packet_count,
        "candidates": stats.candidate_count,
        "rejected": stats.rejected_count,
        "last_packet_at": round(stats.last_packet_at, 3) if stats.last_packet_at is not None else None,
        "last_packet_freq_hz": stats.last_packet_freq_hz,
        "last_packet_bits_len": stats.last_packet_bits_len,
        "last_packet_checksum": stats.last_packet_checksum,
        "top_families": family_tracker.snapshot(now, limit=limit),
    }


def _emit_session_event(
    event: dict[str, Any],
    *,
    emit_event: Callable[[dict[str, Any]], None] | None,
    telemetry: JsonTelemetry,
    stats: SessionStats,
    family_tracker: CandidateFamilyTracker,
) -> None:
    payload = dict(event)
    payload.setdefault("timestamp", round(time.time(), 3))
    if emit_event is not None:
        emit_event(payload)
    telemetry.write_event(payload)
    telemetry.write_stats(_session_snapshot(stats, family_tracker, time.time()))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="subghz-stack", description="CLI-first Sub-GHz protocol analyzer built on sdr-gateway.")
    parser.add_argument("--gateway", default=None, help="Gateway base URL, default comes from SDR_GATEWAY_BASE_URL")
    parser.add_argument("--token", default=None, help="Gateway API token, default comes from SDR_GATEWAY_API_TOKEN")

    subparsers = parser.add_subparsers(dest="command", required=True)

    devices = subparsers.add_parser("devices", help="List visible SDR devices")
    devices.add_argument("--json", action="store_true", help="Emit JSON instead of a table")

    analyze = subparsers.add_parser("analyze", help="Profile burst families and modulation hints")
    analyze.add_argument("--device-id", default=None, help="Device id from sdr-gateway /devices")
    analyze.add_argument("--center-freq-hz", type=int, default=DEFAULT_CENTER_FREQ_HZ)
    analyze.add_argument("--sample-rate-sps", type=int, default=DEFAULT_SAMPLE_RATE_SPS)
    analyze.add_argument("--lna-gain-db", type=int, default=16)
    analyze.add_argument("--vga-gain-db", type=int, default=20)
    analyze.add_argument("--amp-enable", action="store_true")
    analyze.add_argument("--baseband-filter-hz", type=int, default=None)
    analyze.add_argument("--save-dir", type=Path, default=None, help="Save raw burst captures for later analysis")
    analyze.add_argument("--json", action="store_true", help="Emit JSON snapshots instead of text summaries")
    analyze.add_argument("--family-bin-ms", type=float, default=1.0, help="Bucket burst families by this duration bin")
    analyze.add_argument("--summary-interval-s", type=float, default=10.0, help="How often to print family summaries")

    listen = subparsers.add_parser("listen", help="Stream IQ and decode TPMS bursts")
    _add_stream_arguments(listen, include_json=True)
    listen.add_argument("--json", action="store_true", help="Emit JSON lines")
    listen.add_argument("--keep-stream", action="store_true", default=True, help="Keep the gateway stream open on websocket disconnect")
    listen.add_argument("--no-keep-stream", dest="keep_stream", action="store_false", help="Allow the gateway stream to close when the websocket ends")

    monitor = subparsers.add_parser("monitor", help="Run a Textual live monitor for TPMS hunting")
    _add_stream_arguments(monitor, include_json=False)
    monitor.add_argument("--keep-stream", action="store_true", default=True, help="Keep the gateway stream open on websocket disconnect")
    monitor.add_argument("--no-keep-stream", dest="keep_stream", action="store_false", help="Allow the gateway stream to close when the websocket ends")

    wideband = subparsers.add_parser("wideband-monitor", help="Run a wideband Textual monitor using digital channel bins")
    wideband.add_argument("--device-id", default=None, help="Device id from sdr-gateway /devices")
    wideband.add_argument("--protocol", choices=["tpms", "lora"], default="tpms", help="Protocol decoder to use per digital bin")
    wideband.add_argument("--center-freq-hz", type=int, default=None, help="Wideband capture center; defaults to the midpoint of band-start/end")
    wideband.add_argument("--band-start-hz", type=int, default=DEFAULT_433_BAND_START_HZ, help="Start of the wideband region to analyze")
    wideband.add_argument("--band-end-hz", type=int, default=DEFAULT_433_BAND_END_HZ, help="End of the wideband region to analyze")
    wideband.add_argument(
        "--band-spec",
        action="append",
        default=[],
        help="Custom band entry as name:start_hz:end_hz[:protocol]; may be passed more than once",
    )
    wideband.add_argument(
        "--auto-hunt-known-bands",
        action="store_true",
        help="Alternate across known bands for the chosen protocol (TPMS: 315/433, LoRa: 902-928)",
    )
    wideband.add_argument(
        "--auto-hunt-all-known-bands",
        action="store_true",
        help="Alternate across mixed known bands: 315 TPMS, 433 TPMS, and 902-928 LoRa",
    )
    wideband.add_argument("--band-dwell-s", type=float, default=12.0, help="How long to scan each wideband region before switching bands")
    wideband.add_argument("--focus-hold-s", type=float, default=45.0, help="How long to stay on a band after activity is detected")
    wideband.add_argument("--focus-min-candidates", type=int, default=2, help="Focus a band after this many candidate hits in one bin")
    wideband.add_argument("--sample-rate-sps", type=int, default=DEFAULT_WIDEBAND_SAMPLE_RATE_SPS, help="Wideband SDR sample rate")
    wideband.add_argument("--bin-width-hz", type=int, default=DEFAULT_BIN_WIDTH_HZ, help="Digital bin spacing / channel width")
    wideband.add_argument("--channel-rate-sps", type=int, default=DEFAULT_CHANNEL_RATE_SPS, help="Per-bin downsampled channel rate")
    wideband.add_argument("--lna-gain-db", type=int, default=16)
    wideband.add_argument("--vga-gain-db", type=int, default=20)
    wideband.add_argument("--amp-enable", action="store_true")
    wideband.add_argument("--baseband-filter-hz", type=int, default=None)
    wideband.add_argument("--stream-duration-seconds", type=int, default=None, help="Stop the SDR stream automatically after N seconds")
    wideband.add_argument("--max-packets", type=int, default=0, help="Stop after N decoded packets; 0 means run until interrupted")
    wideband.add_argument("--save-dir", type=Path, default=None, help="Save decoded bursts as .iq/.json pairs")
    wideband.add_argument("--min-repeats", type=int, default=2, help="Require N similar candidate packets before printing them")
    wideband.add_argument("--candidate-summary-interval-s", type=float, default=15.0)
    wideband.add_argument("--candidate-summary-limit", type=int, default=6)
    wideband.add_argument("--stats-json-path", type=Path, default=None)
    wideband.add_argument("--events-jsonl-path", type=Path, default=None)
    wideband.add_argument("--keep-stream", action="store_true", default=True, help="Keep the gateway stream open on websocket disconnect")
    wideband.add_argument("--no-keep-stream", dest="keep_stream", action="store_false", help="Allow the gateway stream to close when the websocket ends")

    return parser


def _add_stream_arguments(parser: argparse.ArgumentParser, include_json: bool) -> None:
    parser.add_argument("--device-id", default=None, help="Device id from sdr-gateway /devices")
    parser.add_argument("--protocol", choices=["tpms", "lora"], default="tpms", help="Protocol decoder to use")
    parser.add_argument("--center-freq-hz", type=int, default=DEFAULT_CENTER_FREQ_HZ)
    parser.add_argument(
        "--sweep-profile",
        type=Path,
        default=None,
        help="Path to a JSON sweep profile with either frequencies_hz or start/end/step settings",
    )
    parser.add_argument(
        "--auto-hop-known",
        action="store_true",
        help="Auto-hop across known TPMS bands (currently 315.000MHz and 433.920MHz)",
    )
    parser.add_argument(
        "--hop-freq-hz",
        type=int,
        action="append",
        default=[],
        help="Additional center frequency to hop to; can be passed more than once",
    )
    parser.add_argument(
        "--hop-dwell-ms",
        type=int,
        default=None,
        help="Per-frequency dwell time while hopping, in milliseconds; defaults to 2000 with --auto-hop-known, else 750",
    )
    parser.add_argument("--sample-rate-sps", type=int, default=DEFAULT_SAMPLE_RATE_SPS)
    parser.add_argument("--lna-gain-db", type=int, default=16)
    parser.add_argument("--vga-gain-db", type=int, default=20)
    parser.add_argument("--amp-enable", action="store_true")
    parser.add_argument("--baseband-filter-hz", type=int, default=None)
    parser.add_argument("--stream-duration-seconds", type=int, default=None, help="Stop the SDR stream automatically after N seconds")
    parser.add_argument("--max-packets", type=int, default=0, help="Stop after N decoded packets; 0 means run until interrupted")
    parser.add_argument("--save-dir", type=Path, default=None, help="Save decoded bursts as .iq/.json pairs")
    parser.add_argument("--min-repeats", type=int, default=2, help="Require N similar candidate packets before printing them")
    parser.add_argument(
        "--candidate-summary-interval-s",
        type=float,
        default=15.0,
        help="How often to print candidate family summaries while listening",
    )
    parser.add_argument(
        "--candidate-summary-limit",
        type=int,
        default=6,
        help="How many candidate families to show per summary",
    )
    parser.add_argument(
        "--stats-json-path",
        type=Path,
        default=None,
        help="Write rolling session stats JSON to this file",
    )
    parser.add_argument(
        "--events-jsonl-path",
        type=Path,
        default=None,
        help="Append structured session events as JSON lines to this file",
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    client = GatewayClient(base_url=args.gateway, token=args.token)

    if args.command == "devices":
        return cmd_devices(client, emit_json=args.json)
    if args.command == "analyze":
        return cmd_analyze(client, args)
    if args.command == "listen":
        return cmd_listen(client, args)
    if args.command == "monitor":
        return cmd_monitor(client, args)
    if args.command == "wideband-monitor":
        return cmd_wideband_monitor(client, args)
    parser.error(f"Unknown command: {args.command}")
    return 2


def cmd_devices(client: GatewayClient, emit_json: bool) -> int:
    devices = client.list_devices()
    if emit_json:
        payload = [
            {
                "id": device.id,
                "driver": device.driver,
                "label": device.label,
                "serial": device.serial,
                "freq_min_hz": device.freq_min_hz,
                "freq_max_hz": device.freq_max_hz,
                "max_sample_rate_sps": device.max_sample_rate_sps,
                "notes": device.notes,
                "occupied": device.occupied,
            }
            for device in devices
        ]
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if not devices:
        print("No SDR devices found.", file=sys.stderr)
        return 1

    print(f"{'ID':<14} {'DRIVER':<10} {'LABEL':<26} {'OCCUPIED':<10} NOTES")
    for device in devices:
        occupied = "yes" if device.occupied else "no"
        notes = device.notes or ""
        print(f"{device.id:<14} {device.driver:<10} {device.label:<26} {occupied:<10} {notes}")
    return 0


def cmd_listen(client: GatewayClient, args: argparse.Namespace) -> int:
    try:
        device_id = args.device_id or client.resolve_default_device_id()
    except Exception as exc:
        print(f"Failed to resolve a default device: {exc}", file=sys.stderr)
        return 1
    try:
        sweep_profile = _resolve_sweep_profile(args)
    except Exception as exc:
        print(f"Invalid sweep profile: {exc}", file=sys.stderr)
        return 1

    decoder = build_decoder(args.protocol)
    repeater = CandidateRepeater(min_repeats=args.min_repeats)
    family_tracker = CandidateFamilyTracker()
    started_at = time.time()
    stats = SessionStats(started_at=started_at)
    telemetry = JsonTelemetry(stats_path=args.stats_json_path, events_path=args.events_jsonl_path)

    hop_frequencies = _build_hop_frequencies(
        center_freq_hz=args.center_freq_hz,
        hop_freq_hz=args.hop_freq_hz,
        auto_hop_known=bool(getattr(args, "auto_hop_known", False)),
        sweep_profile=sweep_profile,
    )
    hop_dwell_ms = _resolve_hop_dwell_ms(args)
    dwell_samples = 0
    if len(hop_frequencies) > 1:
        dwell_samples = max(1, int(math.ceil((hop_dwell_ms / 1000.0) * int(args.sample_rate_sps))))
        if not args.json:
            freq_text = ", ".join(f"{freq/1e6:.3f}MHz" for freq in hop_frequencies)
            print(
                f"Hopping across {freq_text} dwell_ms={hop_dwell_ms}",
                file=sys.stderr,
                flush=True,
            )

    try:
        config = StreamConfig(
            device_id=device_id,
            center_freq_hz=int(hop_frequencies[0]),
            sample_rate_sps=int(args.sample_rate_sps),
            lna_gain_db=int(args.lna_gain_db),
            vga_gain_db=int(args.vga_gain_db),
            amp_enable=bool(args.amp_enable),
            baseband_filter_hz=args.baseband_filter_hz,
            duration_seconds=args.stream_duration_seconds if len(hop_frequencies) == 1 else None,
            num_samples=None,
        )
        return _run_live_stream(
            client=client,
            initial_config=config,
            hop_frequencies=hop_frequencies,
            dwell_samples=dwell_samples,
            args=args,
            decoder=decoder,
            repeater=repeater,
            family_tracker=family_tracker,
            stats=stats,
            telemetry=telemetry,
            emit_event=None,
            print_human=not args.json,
        )
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Listen loop failed: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_monitor(client: GatewayClient, args: argparse.Namespace) -> int:
    try:
        from .tui import TpmsMonitorApp
    except Exception as exc:
        print(f"Unable to start Textual monitor: {exc}", file=sys.stderr)
        return 1

    event_queue: deque[dict[str, Any]] = deque()
    queue_lock = threading.Lock()
    stop_event = threading.Event()

    def emit_event(event: dict[str, Any]) -> None:
        with queue_lock:
            event_queue.append(event)

    def start_capture() -> int:
        try:
            device_id = args.device_id or client.resolve_default_device_id()
        except Exception as exc:
            emit_event({"type": "error", "message": f"Failed to resolve a default device: {exc}"})
            return 1
        try:
            sweep_profile = _resolve_sweep_profile(args)
        except Exception as exc:
            emit_event({"type": "error", "message": f"Invalid sweep profile: {exc}"})
            return 1

        decoder = build_decoder(args.protocol)
        repeater = CandidateRepeater(min_repeats=args.min_repeats)
        family_tracker = CandidateFamilyTracker()
        started_at = time.time()
        stats = SessionStats(started_at=started_at)
        telemetry = JsonTelemetry(stats_path=args.stats_json_path, events_path=args.events_jsonl_path)

        hop_frequencies = _build_hop_frequencies(
            center_freq_hz=args.center_freq_hz,
            hop_freq_hz=args.hop_freq_hz,
            auto_hop_known=bool(getattr(args, "auto_hop_known", False)),
            sweep_profile=sweep_profile,
        )
        hop_dwell_ms = _resolve_hop_dwell_ms(args)
        dwell_samples = 0
        if len(hop_frequencies) > 1:
            dwell_samples = max(1, int(math.ceil((hop_dwell_ms / 1000.0) * int(args.sample_rate_sps))))

        try:
            config = StreamConfig(
                device_id=device_id,
                center_freq_hz=int(hop_frequencies[0]),
                sample_rate_sps=int(args.sample_rate_sps),
                lna_gain_db=int(args.lna_gain_db),
                vga_gain_db=int(args.vga_gain_db),
                amp_enable=bool(args.amp_enable),
                baseband_filter_hz=args.baseband_filter_hz,
                duration_seconds=args.stream_duration_seconds if len(hop_frequencies) == 1 else None,
                num_samples=None,
            )
            return _run_live_stream(
                client=client,
                initial_config=config,
                hop_frequencies=hop_frequencies,
                dwell_samples=dwell_samples,
                args=args,
                decoder=decoder,
                repeater=repeater,
                family_tracker=family_tracker,
                stats=stats,
                telemetry=telemetry,
                emit_event=emit_event,
                print_human=False,
                stop_event=stop_event,
            )
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            emit_event({"type": "error", "message": str(exc)})
            return 1

    app = TpmsMonitorApp(
        start_capture=start_capture,
        event_queue=event_queue,
        queue_lock=queue_lock,
        stop_event=stop_event,
    )
    app.run()
    return 0


def cmd_wideband_monitor(client: GatewayClient, args: argparse.Namespace) -> int:
    try:
        from .tui import TpmsMonitorApp
        from .wideband import WidebandBinRuntime, build_bin_plans, flush_wideband_runtimes, process_wideband_chunk
    except Exception as exc:
        print(f"Unable to start wideband monitor: {exc}", file=sys.stderr)
        return 1

    event_queue: deque[dict[str, Any]] = deque()
    queue_lock = threading.Lock()
    stop_event = threading.Event()

    def emit_event(event: dict[str, Any]) -> None:
        with queue_lock:
            event_queue.append(event)

    def start_capture() -> int:
        try:
            device_id = args.device_id or client.resolve_default_device_id()
            band_profiles = _resolve_wideband_band_profiles(args)
            scan_targets = _build_wideband_scan_targets(
                band_profiles=band_profiles,
                sample_rate_sps=int(args.sample_rate_sps),
                bin_width_hz=int(args.bin_width_hz),
            )
        except Exception as exc:
            emit_event({"type": "error", "message": f"Wideband setup failed: {exc}"})
            return 1

        stats = SessionStats(started_at=time.time())
        telemetry = JsonTelemetry(stats_path=args.stats_json_path, events_path=args.events_jsonl_path)
        family_tracker = CandidateFamilyTracker()
        repeaters: dict[int, CandidateRepeater] = {}
        last_status_at = stats.started_at
        last_summary_at = stats.started_at
        target_index = 0
        focus_band_name: str | None = None
        focus_until = 0.0
        stream: StreamHandle | None = None
        try:
            while not stop_event.is_set():
                target = scan_targets[target_index]
                plans = build_bin_plans(
                    center_freq_hz=target.center_freq_hz,
                    sample_rate_sps=int(target.sample_rate_sps),
                    band_start_hz=target.band_start_hz,
                    band_end_hz=target.band_end_hz,
                    bin_width_hz=int(args.bin_width_hz),
                    channel_rate_sps=int(args.channel_rate_sps),
                )
                runtimes = [WidebandBinRuntime(plan, protocol=target.protocol) for plan in plans]
                for runtime in runtimes:
                    repeaters.setdefault(runtime.plan.center_freq_hz, CandidateRepeater(min_repeats=args.min_repeats))
                try:
                    config = StreamConfig(
                        device_id=device_id,
                        center_freq_hz=target.center_freq_hz,
                        sample_rate_sps=int(target.sample_rate_sps),
                        lna_gain_db=int(args.lna_gain_db),
                        vga_gain_db=int(args.vga_gain_db),
                        amp_enable=bool(args.amp_enable),
                        baseband_filter_hz=args.baseband_filter_hz,
                        duration_seconds=args.stream_duration_seconds,
                        num_samples=None,
                    )
                    previous_stream_id = stream.stream_id if stream is not None else None
                    if stream is None:
                        stream = client.start_stream(config)
                    else:
                        stream = client.retune_stream(stream.stream_id, config)
                except Exception as exc:
                    emit_event({"type": "error", "message": f"Unable to start wideband stream: {exc}"})
                    return 1

                action = "Wideband" if previous_stream_id is None else "Wideband retune"
                event_type = "startup" if previous_stream_id is None else "retune"
                startup_message = (
                    f"{action} band={target.band_name} window={target.window_name} protocol={target.protocol} "
                    f"stream={stream.stream_id} center={target.center_freq_hz/1e6:.3f}MHz rate={target.sample_rate_sps} "
                    f"band={target.band_start_hz/1e6:.3f}-{target.band_end_hz/1e6:.3f}MHz bins={len(runtimes)}"
                )
                _emit_session_event(
                    {
                        "type": event_type,
                        "message": startup_message,
                        "stream_id": stream.stream_id,
                        "device_id": stream.device_id,
                        "frequency_hz": target.center_freq_hz,
                        "sample_rate_sps": stream.sample_rate_sps,
                        "band_name": target.band_name,
                        "window_name": target.window_name,
                        "band_start_hz": target.band_start_hz,
                        "band_end_hz": target.band_end_hz,
                        "bin_count": len(runtimes),
                        "protocol": target.protocol,
                        "restart": previous_stream_id is not None and stream.stream_id != previous_stream_id,
                    },
                    emit_event=emit_event,
                    telemetry=telemetry,
                    stats=stats,
                    family_tracker=family_tracker,
                )
                band_started_at = time.time()
                best_focus_score = 0
                active_focus_freq_hz: int | None = None
                try:
                    for chunk in client.iter_iq_chunks(stream.stream_id, keep_stream=bool(args.keep_stream)):
                        if stop_event.is_set():
                            break
                        stats.chunk_count += 1
                        processed = process_wideband_chunk(
                            raw_chunk=chunk,
                            input_sample_rate_sps=int(target.sample_rate_sps),
                            runtimes=runtimes,
                        )
                        for runtime, burst, result in processed:
                            stats.burst_count += 1
                            if result is None:
                                stats.rejected_count += 1
                                runtime.stats.rejected_count += 1
                                debug = runtime.decoder.debug_info()
                                if debug is not None:
                                    _emit_session_event(
                                        {
                                            "type": "reject",
                                            "frequency_hz": burst.center_freq_hz,
                                            "burst_ms": round(debug.burst_ms, 3),
                                            "peak": round(debug.peak, 4),
                                            "average": round(debug.average, 4),
                                            "runs": debug.collapsed_run_count,
                                            "reject_reason": debug.reject_reason,
                                        },
                                        emit_event=emit_event,
                                        telemetry=telemetry,
                                        stats=stats,
                                        family_tracker=family_tracker,
                                    )
                                continue
                            now = time.time()
                            check_hint = _checksum_hint(result.hex_string)
                            runtime.stats.candidate_count += 1
                            stats.candidate_count += 1
                            repeater = repeaters[runtime.plan.center_freq_hz]
                            should_emit, repeat_count = repeater.observe(result, now)
                            family = family_tracker.observe(result, now)
                            candidate_event = {
                                "type": "candidate",
                                "frequency_hz": result.burst.center_freq_hz,
                                "bits_len": len(result.bits),
                                "hex": result.hex_string,
                                "repeat_count": repeat_count,
                                "min_repeats": int(args.min_repeats),
                                "checksum_hint": check_hint,
                                "strategy": result.candidate.strategy,
                                "unit_samples": round(result.candidate.unit_samples, 2),
                                "family_count": family.count,
                                "family_prefix": family.prefix,
                                "band_name": target.band_name,
                                "window_name": target.window_name,
                                "protocol": target.protocol,
                                "protocol_variant": result.protocol_variant,
                                "decoded_fields": result.decoded_fields,
                            }
                            _emit_session_event(
                                candidate_event,
                                emit_event=emit_event,
                                telemetry=telemetry,
                                stats=stats,
                                family_tracker=family_tracker,
                            )
                            focus_score = runtime.stats.packet_count * 10 + runtime.stats.candidate_count
                            if should_emit:
                                runtime.stats.packet_count += 1
                                runtime.stats.last_bits_len = len(result.bits)
                                runtime.stats.last_checksum_hint = check_hint
                                stats.packet_count += 1
                                stats.last_packet_at = now
                                stats.last_packet_freq_hz = int(result.burst.center_freq_hz)
                                stats.last_packet_bits_len = len(result.bits)
                                stats.last_packet_checksum = check_hint
                                if args.save_dir is not None:
                                    result.burst.stream_id = stream.stream_id
                                    runtime.decoder.save_burst(result, Path(args.save_dir))
                                elapsed = now - stats.started_at
                                _emit_session_event(
                                    {
                                        "type": "packet",
                                        "packet_index": stats.packet_count,
                                        "frequency_hz": result.burst.center_freq_hz,
                                        "confidence": round(result.confidence, 4),
                                        "modulation": result.modulation,
                                        "repeat_count": repeat_count,
                                        "bits": result.bits,
                                        "bits_len": len(result.bits),
                                        "hex": result.hex_string,
                                        "hex_len": len(result.hex_string),
                                        "checksum_hint": check_hint,
                                        "protocol_variant": result.protocol_variant,
                                        "decoded_fields": result.decoded_fields,
                                        "burst_ms": round(result.burst.duration_seconds * 1000.0, 3),
                                        "strategy": result.candidate.strategy,
                                        "unit_samples": round(result.candidate.unit_samples, 2),
                                        "elapsed_s": round(elapsed, 3),
                                        "band_name": target.band_name,
                                        "window_name": target.window_name,
                                        "protocol": target.protocol,
                                    },
                                    emit_event=emit_event,
                                    telemetry=telemetry,
                                    stats=stats,
                                    family_tracker=family_tracker,
                                )
                                focus_score += 10
                                if args.max_packets and stats.packet_count >= int(args.max_packets):
                                    return 0

                            if focus_score > best_focus_score and (
                                runtime.stats.candidate_count >= int(args.focus_min_candidates) or runtime.stats.packet_count > 0
                            ):
                                best_focus_score = focus_score
                                active_focus_freq_hz = runtime.plan.center_freq_hz
                                focus_band_name = target.band_name
                                focus_until = now + float(args.focus_hold_s)
                                _emit_session_event(
                                    {
                                        "type": "focus",
                                        "message": (
                                            f"Focusing band={target.band_name} bin={runtime.plan.center_freq_hz/1e6:.3f}MHz "
                                            f"hold_s={float(args.focus_hold_s):.1f}"
                                        ),
                                        "band_name": target.band_name,
                                        "window_name": target.window_name,
                                        "frequency_hz": runtime.plan.center_freq_hz,
                                        "focus_until": round(focus_until, 3),
                                        "protocol": target.protocol,
                                    },
                                    emit_event=emit_event,
                                    telemetry=telemetry,
                                    stats=stats,
                                    family_tracker=family_tracker,
                                )

                        now = time.time()
                        if now - last_summary_at >= float(args.candidate_summary_interval_s):
                            _emit_session_event(
                                {
                                    "type": "family_summary",
                                    "families": family_tracker.snapshot(now, limit=int(args.candidate_summary_limit)),
                                },
                                emit_event=emit_event,
                                telemetry=telemetry,
                                stats=stats,
                                family_tracker=family_tracker,
                            )
                            _emit_session_event(
                                {
                                    "type": "wideband_bins",
                                    "bins": [
                                        {
                                            "frequency_hz": runtime.stats.center_freq_hz,
                                            "candidates": runtime.stats.candidate_count,
                                            "packets": runtime.stats.packet_count,
                                            "rejected": runtime.stats.rejected_count,
                                            "checksum_hint": runtime.stats.last_checksum_hint,
                                            "bits_len": runtime.stats.last_bits_len,
                                            "focused": runtime.stats.center_freq_hz == active_focus_freq_hz,
                                            "band_name": target.band_name,
                                            "window_name": target.window_name,
                                            "protocol": target.protocol,
                                        }
                                        for runtime in runtimes
                                    ],
                                },
                                emit_event=emit_event,
                                telemetry=telemetry,
                                stats=stats,
                                family_tracker=family_tracker,
                            )
                            last_summary_at = now
                        if now - last_status_at >= 10.0:
                            _emit_session_event(
                                {
                                    "type": "status",
                                    "elapsed_s": round(now - stats.started_at, 3),
                                    "chunks": stats.chunk_count,
                                    "bursts": stats.burst_count,
                                    "decoded": stats.packet_count,
                                    "candidates": stats.candidate_count,
                                    "rejected": stats.rejected_count,
                                    "band_name": target.band_name,
                                    "window_name": target.window_name,
                                    "focused_frequency_hz": active_focus_freq_hz,
                                    "protocol": target.protocol,
                                },
                                emit_event=emit_event,
                                telemetry=telemetry,
                                stats=stats,
                                family_tracker=family_tracker,
                            )
                            last_status_at = now
                        should_switch = now - band_started_at >= float(args.band_dwell_s)
                        if focus_band_name == target.band_name and now < focus_until:
                            should_switch = False
                        if should_switch:
                            break
                except Exception as exc:
                    emit_event({"type": "error", "message": str(exc)})
                    return 1
                finally:
                    for runtime, burst, result in flush_wideband_runtimes(runtimes):
                        if result is None:
                            continue
                if len(scan_targets) == 1:
                    break
                if focus_band_name == target.band_name and time.time() < focus_until:
                    continue
                focus_band_name = None
                active_focus_freq_hz = None
                target_index = (target_index + 1) % len(scan_targets)
        except Exception as exc:
            emit_event({"type": "error", "message": str(exc)})
            return 1
        finally:
            if stream is not None:
                try:
                    client.stop_stream(stream.stream_id)
                except Exception:
                    pass
        return 0

    app = TpmsMonitorApp(
        start_capture=start_capture,
        event_queue=event_queue,
        queue_lock=queue_lock,
        stop_event=stop_event,
    )
    app.run()
    return 0


def cmd_analyze(client: GatewayClient, args: argparse.Namespace) -> int:
    try:
        device_id = args.device_id or client.resolve_default_device_id()
    except Exception as exc:
        print(f"Failed to resolve a default device: {exc}", file=sys.stderr)
        return 1

    config = StreamConfig(
        device_id=device_id,
        center_freq_hz=int(args.center_freq_hz),
        sample_rate_sps=int(args.sample_rate_sps),
        lna_gain_db=int(args.lna_gain_db),
        vga_gain_db=int(args.vga_gain_db),
        amp_enable=bool(args.amp_enable),
        baseband_filter_hz=args.baseband_filter_hz,
    )
    analyzer = SignalAnalyzer(family_bin_ms=args.family_bin_ms)

    try:
        stream = client.start_stream(config)
    except Exception as exc:
        print(f"Unable to start SDR stream: {exc}", file=sys.stderr)
        return 1

    detector = BurstDetector(
        sample_rate_sps=stream.sample_rate_sps,
        center_freq_hz=stream.center_freq_hz,
        stream_id=stream.stream_id,
    )
    started_at = time.time()
    last_summary_at = started_at
    burst_counter = 0

    print(
        f"Analyzing device={stream.device_id} stream={stream.stream_id} "
        f"freq={stream.center_freq_hz/1e6:.3f}MHz rate={stream.sample_rate_sps} sps "
        f"lna={config.lna_gain_db} vga={config.vga_gain_db} amp={int(config.amp_enable)}",
        file=sys.stderr,
        flush=True,
    )

    try:
        for chunk in client.iter_iq_chunks(stream.stream_id, keep_stream=True):
            bursts = detector.ingest(chunk)
            for burst in bursts:
                burst_counter += 1
                family, hint = analyzer.observe(burst)
                if args.save_dir is not None:
                    _save_analyzer_burst(burst, Path(args.save_dir) / "analyzer", family.duration_bin_ms, hint)
                if not args.json:
                    print(
                        f"burst#{burst_counter} freq={burst.center_freq_hz/1e6:.3f}MHz "
                        f"dur_ms={burst.duration_seconds*1000:.2f} hint={hint} "
                        f"family_count={family.count} peak={burst.peak:.3f} avg={burst.average:.3f}",
                        file=sys.stderr,
                        flush=True,
                    )
            now = time.time()
            if now - last_summary_at >= float(args.summary_interval_s):
                if args.json:
                    print(json.dumps(analyzer.snapshot(), sort_keys=True))
                else:
                    elapsed = now - started_at
                    print(
                        f"summary elapsed_s={elapsed:.1f} total_bursts={analyzer.total_bursts}",
                        file=sys.stderr,
                        flush=True,
                    )
                    for line in analyzer.summary_lines():
                        print(line, file=sys.stderr, flush=True)
                last_summary_at = now
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Analyze loop failed: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            client.stop_stream(stream.stream_id)
        except Exception:
            pass
    return 0


def _build_hop_frequencies(
    center_freq_hz: int,
    hop_freq_hz: list[int],
    auto_hop_known: bool = False,
    sweep_profile: SweepProfile | None = None,
) -> list[int]:
    frequencies: list[int] = []
    if sweep_profile is not None:
        values = [int(value) for value in sweep_profile.frequencies_hz]
    elif auto_hop_known:
        values = [int(value) for value in KNOWN_TPMS_FREQS_HZ]
    else:
        values = [int(center_freq_hz)]
        values.extend(int(value) for value in hop_freq_hz)
    for freq in values:
        if freq not in frequencies:
            frequencies.append(freq)
    return frequencies


def _load_sweep_profile(path: Path) -> SweepProfile:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Sweep profile must be a JSON object")

    dwell_value = payload.get("dwell_ms")
    dwell_ms = int(dwell_value) if dwell_value is not None else None

    frequencies_value = payload.get("frequencies_hz")
    if isinstance(frequencies_value, list) and frequencies_value:
        frequencies_hz = [int(value) for value in frequencies_value]
        return SweepProfile(frequencies_hz=frequencies_hz, dwell_ms=dwell_ms)

    start_value = payload.get("start_freq_hz")
    end_value = payload.get("end_freq_hz")
    step_value = payload.get("step_hz")
    if start_value is None or end_value is None or step_value is None:
        raise ValueError("Sweep profile needs either frequencies_hz or start_freq_hz/end_freq_hz/step_hz")

    start_freq_hz = int(start_value)
    end_freq_hz = int(end_value)
    step_hz = int(step_value)
    if step_hz <= 0:
        raise ValueError("step_hz must be > 0")

    frequencies_hz: list[int] = []
    if start_freq_hz <= end_freq_hz:
        current = start_freq_hz
        while current <= end_freq_hz:
            frequencies_hz.append(int(current))
            current += step_hz
    else:
        current = start_freq_hz
        while current >= end_freq_hz:
            frequencies_hz.append(int(current))
            current -= step_hz
    if not frequencies_hz:
        raise ValueError("Sweep profile produced no frequencies")
    return SweepProfile(frequencies_hz=frequencies_hz, dwell_ms=dwell_ms)


def _resolve_sweep_profile(args: argparse.Namespace) -> SweepProfile | None:
    sweep_profile_path = getattr(args, "sweep_profile", None)
    if sweep_profile_path is None:
        return None
    return _load_sweep_profile(Path(sweep_profile_path))


def _resolve_hop_dwell_ms(args: argparse.Namespace) -> int:
    sweep_profile = _resolve_sweep_profile(args)
    if args.hop_dwell_ms is not None:
        return int(args.hop_dwell_ms)
    if sweep_profile is not None and sweep_profile.dwell_ms is not None:
        return int(sweep_profile.dwell_ms)
    if bool(getattr(args, "auto_hop_known", False)):
        return DEFAULT_AUTO_HOP_DWELL_MS
    return 750


def _known_wideband_band_profiles() -> list[WidebandBandProfile]:
    return [
        WidebandBandProfile(
            name="315",
            band_start_hz=DEFAULT_315_BAND_START_HZ,
            band_end_hz=DEFAULT_315_BAND_END_HZ,
            center_freq_hz=(DEFAULT_315_BAND_START_HZ + DEFAULT_315_BAND_END_HZ) // 2,
            protocol="tpms",
        ),
        WidebandBandProfile(
            name="433",
            band_start_hz=DEFAULT_433_BAND_START_HZ,
            band_end_hz=DEFAULT_433_BAND_END_HZ,
            center_freq_hz=(DEFAULT_433_BAND_START_HZ + DEFAULT_433_BAND_END_HZ) // 2,
            protocol="tpms",
        ),
    ]


def _known_lora_band_profiles() -> list[WidebandBandProfile]:
    return [
        WidebandBandProfile(
            name="915",
            band_start_hz=902_000_000,
            band_end_hz=928_000_000,
            center_freq_hz=None,
            protocol="lora",
        )
    ]


def _known_mixed_subghz_band_profiles() -> list[WidebandBandProfile]:
    return [*_known_wideband_band_profiles(), *_known_lora_band_profiles()]


def _parse_wideband_band_spec(raw_value: str, default_protocol: str) -> WidebandBandProfile:
    parts = [part.strip() for part in str(raw_value).split(":") if part.strip()]
    if len(parts) not in {3, 4}:
        raise ValueError("band-spec must look like name:start_hz:end_hz[:protocol]")
    name = parts[0]
    band_start_hz = int(parts[1])
    band_end_hz = int(parts[2])
    protocol = parts[3].lower() if len(parts) == 4 else default_protocol
    if protocol not in {"tpms", "lora"}:
        raise ValueError(f"Unsupported protocol in band-spec: {protocol}")
    if band_end_hz <= band_start_hz:
        raise ValueError("band-spec end_hz must be greater than start_hz")
    return WidebandBandProfile(
        name=name,
        band_start_hz=band_start_hz,
        band_end_hz=band_end_hz,
        center_freq_hz=None,
        protocol=protocol,
    )


def _build_wideband_scan_targets(
    *,
    band_profiles: list[WidebandBandProfile],
    sample_rate_sps: int,
    bin_width_hz: int,
) -> list[WidebandScanTarget]:
    if sample_rate_sps <= 0:
        raise ValueError("sample_rate_sps must be > 0")
    if bin_width_hz <= 0:
        raise ValueError("bin_width_hz must be > 0")
    targets: list[WidebandScanTarget] = []
    for profile in band_profiles:
        profile_sample_rate_sps = _wideband_profile_sample_rate_sps(profile, default_sample_rate_sps=int(sample_rate_sps))
        max_window_span_hz = int(profile_sample_rate_sps)
        overlap_hz = min(int(bin_width_hz), max(0, max_window_span_hz - 1))
        step_hz = max(1, max_window_span_hz - overlap_hz)
        band_span_hz = int(profile.band_end_hz - profile.band_start_hz)
        if band_span_hz <= max_window_span_hz:
            center_freq_hz = (
                int(profile.center_freq_hz)
                if profile.center_freq_hz is not None
                else int((profile.band_start_hz + profile.band_end_hz) // 2)
            )
            targets.append(
                WidebandScanTarget(
                    band_name=profile.name,
                    window_name=profile.name,
                    band_start_hz=int(profile.band_start_hz),
                    band_end_hz=int(profile.band_end_hz),
                    center_freq_hz=center_freq_hz,
                    protocol=profile.protocol,
                    sample_rate_sps=profile_sample_rate_sps,
                )
            )
            continue
        window_index = 0
        window_start_hz = int(profile.band_start_hz)
        while window_start_hz < int(profile.band_end_hz):
            window_end_hz = min(int(profile.band_end_hz), window_start_hz + max_window_span_hz)
            window_index += 1
            targets.append(
                WidebandScanTarget(
                    band_name=profile.name,
                    window_name=f"{profile.name}[{window_index}]",
                    band_start_hz=window_start_hz,
                    band_end_hz=window_end_hz,
                    center_freq_hz=int((window_start_hz + window_end_hz) // 2),
                    protocol=profile.protocol,
                    sample_rate_sps=profile_sample_rate_sps,
                )
            )
            if window_end_hz >= int(profile.band_end_hz):
                break
            window_start_hz += step_hz
    if not targets:
        raise ValueError("No wideband scan targets were produced")
    return targets


def _wideband_profile_sample_rate_sps(profile: WidebandBandProfile, default_sample_rate_sps: int) -> int:
    if (
        profile.protocol == "lora"
        and int(profile.band_start_hz) >= 900_000_000
        and int(profile.band_end_hz) <= 930_000_000
    ):
        return 20_000_000
    return int(default_sample_rate_sps)


def _resolve_wideband_band_profiles(args: argparse.Namespace) -> list[WidebandBandProfile]:
    band_specs = list(getattr(args, "band_spec", []) or [])
    if band_specs:
        return [_parse_wideband_band_spec(value, default_protocol=str(args.protocol)) for value in band_specs]
    if bool(getattr(args, "auto_hunt_all_known_bands", False)):
        return _known_mixed_subghz_band_profiles()
    if bool(getattr(args, "auto_hunt_known_bands", False)):
        if str(args.protocol).strip().lower() == "lora":
            return _known_lora_band_profiles()
        return _known_wideband_band_profiles()
    band_start_hz = int(args.band_start_hz)
    band_end_hz = int(args.band_end_hz)
    center_freq_hz = int(args.center_freq_hz) if args.center_freq_hz is not None else None
    return [
        WidebandBandProfile(
            name=f"{band_start_hz/1e6:.3f}-{band_end_hz/1e6:.3f}",
            band_start_hz=band_start_hz,
            band_end_hz=band_end_hz,
            center_freq_hz=center_freq_hz,
            protocol=str(args.protocol).strip().lower(),
        )
    ]


def _run_live_stream(
    client: GatewayClient,
    initial_config: StreamConfig,
    hop_frequencies: list[int],
    dwell_samples: int,
    args: argparse.Namespace,
    decoder: ProtocolDecoder,
    repeater: CandidateRepeater,
    family_tracker: CandidateFamilyTracker,
    stats: SessionStats,
    telemetry: JsonTelemetry,
    emit_event: Callable[[dict[str, Any]], None] | None,
    print_human: bool,
    stop_event: threading.Event | None = None,
) -> int:
    emit_json = bool(getattr(args, "json", False))
    try:
        stream = client.start_stream(initial_config)
    except Exception as exc:
        raise RuntimeError(f"Unable to start SDR stream: {exc}") from exc

    active_freq_index = 0
    active_config = initial_config
    detector = BurstDetector(sample_rate_sps=stream.sample_rate_sps, center_freq_hz=stream.center_freq_hz, stream_id=stream.stream_id)
    samples_since_hop = 0
    last_status_at = stats.started_at
    last_candidate_summary_at = stats.started_at

    startup_message = (
        f"Listening on device={stream.device_id} stream={stream.stream_id} "
        f"freq={stream.center_freq_hz/1e6:.3f}MHz rate={stream.sample_rate_sps} sps "
        f"lna={active_config.lna_gain_db} vga={active_config.vga_gain_db} amp={int(active_config.amp_enable)}"
    )
    if print_human:
        print(startup_message, file=sys.stderr, flush=True)
    _emit_session_event(
        {
            "type": "startup",
            "message": startup_message,
            "stream_id": stream.stream_id,
            "device_id": stream.device_id,
            "frequency_hz": stream.center_freq_hz,
            "sample_rate_sps": stream.sample_rate_sps,
            "lna_gain_db": active_config.lna_gain_db,
            "vga_gain_db": active_config.vga_gain_db,
            "amp_enable": bool(active_config.amp_enable),
        },
        emit_event=emit_event,
        telemetry=telemetry,
        stats=stats,
        family_tracker=family_tracker,
    )

    try:
        while True:
            restart_required = False
            for chunk in client.iter_iq_chunks(stream.stream_id, keep_stream=bool(args.keep_stream)):
                if stop_event is not None and stop_event.is_set():
                    break
                stats.chunk_count += 1
                samples_since_hop += len(chunk) // 2
                bursts = detector.ingest(chunk)
                stats.burst_count += len(bursts)
                for burst in bursts:
                    result = decoder.decode(burst)
                    if result is None:
                        stats.rejected_count += 1
                        debug = decoder.debug_info()
                        if args.save_dir is not None:
                            decoder.save_rejected_burst(burst, Path(args.save_dir) / "rejected", debug)
                        if print_human and debug is not None:
                            print(
                                f"burst freq={burst.center_freq_hz/1e6:.3f}MHz "
                                f"dur_ms={debug.burst_ms:.2f} peak={debug.peak:.3f} avg={debug.average:.3f} "
                                f"runs={debug.collapsed_run_count} hi_med={debug.median_high_samples:.1f} "
                                f"lo_med={debug.median_low_samples:.1f} reject={debug.reject_reason}",
                                file=sys.stderr,
                                flush=True,
                            )
                        if debug is not None:
                            _emit_session_event(
                                {
                                    "type": "reject",
                                    "frequency_hz": burst.center_freq_hz,
                                    "burst_ms": round(debug.burst_ms, 3),
                                    "peak": round(debug.peak, 4),
                                    "average": round(debug.average, 4),
                                    "runs": debug.collapsed_run_count,
                                    "reject_reason": debug.reject_reason,
                                },
                                emit_event=emit_event,
                                telemetry=telemetry,
                                stats=stats,
                                family_tracker=family_tracker,
                            )
                        continue
                    now = time.time()
                    should_emit, repeat_count = repeater.observe(result, now)
                    family = family_tracker.observe(result, now)
                    stats.candidate_count += 1
                    check_hint = _checksum_hint(result.hex_string)
                    detail_text = _protocol_detail_text(result)
                    if not should_emit:
                        candidate_event = {
                            "type": "candidate",
                            "frequency_hz": result.burst.center_freq_hz,
                            "bits_len": len(result.bits),
                            "hex": result.hex_string,
                            "repeat_count": repeat_count,
                            "min_repeats": int(args.min_repeats),
                            "checksum_hint": check_hint,
                            "strategy": result.candidate.strategy,
                            "unit_samples": round(result.candidate.unit_samples, 2),
                            "family_count": family.count,
                            "family_prefix": family.prefix,
                            "protocol_variant": result.protocol_variant,
                            "decoded_fields": result.decoded_fields,
                        }
                        _emit_session_event(
                            candidate_event,
                            emit_event=emit_event,
                            telemetry=telemetry,
                            stats=stats,
                            family_tracker=family_tracker,
                        )
                        if print_human:
                            line = (
                                f"candidate freq={result.burst.center_freq_hz/1e6:.3f}MHz "
                                f"bits_len={len(result.bits)} hex={_preview_text(result.hex_string)} "
                                f"repeat={repeat_count}/{args.min_repeats} check={check_hint}"
                            )
                            if detail_text:
                                line = f"{line} {detail_text}"
                            if repeat_count < args.min_repeats:
                                line = f"{ANSI_YELLOW}{line}{ANSI_RESET}"
                            print(line, file=sys.stderr, flush=True)
                        continue
                    stats.packet_count += 1
                    stats.last_packet_at = now
                    stats.last_packet_freq_hz = int(result.burst.center_freq_hz)
                    stats.last_packet_bits_len = len(result.bits)
                    stats.last_packet_checksum = check_hint
                    if args.save_dir is not None:
                        decoder.save_burst(result, Path(args.save_dir))
                    elapsed = time.time() - stats.started_at
                    if emit_json:
                        print(decoder.decode_to_json(result))
                    else:
                        line = (
                            f"packet={stats.packet_count} freq={result.burst.center_freq_hz/1e6:.3f}MHz "
                            f"conf={result.confidence:.2f} mod={result.modulation} "
                            f"repeat={repeat_count} "
                            f"bits={_preview_text(result.bits)} bits_len={len(result.bits)} "
                            f"hex={_preview_text(result.hex_string)} hex_len={len(result.hex_string)} "
                            f"check={check_hint} "
                            f"burst_ms={result.burst.duration_seconds*1000:.2f} strategy={result.candidate.strategy} "
                            f"unit={result.candidate.unit_samples:.1f} elapsed_s={elapsed:.1f}"
                        )
                        if detail_text:
                            line = f"{line} {detail_text}"
                        if repeat_count >= args.min_repeats and print_human:
                            line = f"{ANSI_GREEN}{line}{ANSI_RESET}"
                        if print_human:
                            print(line)
                    _emit_session_event(
                        {
                            "type": "packet",
                            "packet_index": stats.packet_count,
                            "frequency_hz": result.burst.center_freq_hz,
                            "confidence": round(result.confidence, 4),
                            "modulation": result.modulation,
                            "repeat_count": repeat_count,
                            "bits": result.bits,
                            "bits_len": len(result.bits),
                            "hex": result.hex_string,
                            "hex_len": len(result.hex_string),
                            "checksum_hint": check_hint,
                            "protocol_variant": result.protocol_variant,
                            "decoded_fields": result.decoded_fields,
                            "burst_ms": round(result.burst.duration_seconds * 1000.0, 3),
                            "strategy": result.candidate.strategy,
                            "unit_samples": round(result.candidate.unit_samples, 2),
                            "elapsed_s": round(elapsed, 3),
                        },
                        emit_event=emit_event,
                        telemetry=telemetry,
                        stats=stats,
                        family_tracker=family_tracker,
                    )
                    if args.max_packets and stats.packet_count >= int(args.max_packets):
                        return 0
                if len(hop_frequencies) > 1 and samples_since_hop >= dwell_samples:
                    for burst in detector.flush():
                        result = decoder.decode(burst)
                        if result is None:
                            continue
                        now = time.time()
                        should_emit, repeat_count = repeater.observe(result, now)
                        family_tracker.observe(result, now)
                        if not should_emit:
                            continue
                        stats.packet_count += 1
                        if args.save_dir is not None:
                            decoder.save_burst(result, Path(args.save_dir))
                        if emit_json:
                            print(decoder.decode_to_json(result))
                        else:
                            elapsed = time.time() - stats.started_at
                            check_hint = _checksum_hint(result.hex_string)
                            detail_text = _protocol_detail_text(result)
                            line = (
                                f"packet={stats.packet_count} freq={result.burst.center_freq_hz/1e6:.3f}MHz "
                                f"conf={result.confidence:.2f} mod={result.modulation} "
                                f"repeat={repeat_count} "
                                f"bits={_preview_text(result.bits)} bits_len={len(result.bits)} "
                                f"hex={_preview_text(result.hex_string)} hex_len={len(result.hex_string)} "
                                f"check={check_hint} "
                                f"burst_ms={result.burst.duration_seconds*1000:.2f} strategy={result.candidate.strategy} "
                                f"unit={result.candidate.unit_samples:.1f} elapsed_s={elapsed:.1f}"
                            )
                            if detail_text:
                                line = f"{line} {detail_text}"
                            if repeat_count >= args.min_repeats and print_human:
                                line = f"{ANSI_GREEN}{line}{ANSI_RESET}"
                            if print_human:
                                print(line)
                    active_freq_index = (active_freq_index + 1) % len(hop_frequencies)
                    active_config = StreamConfig(
                        device_id=active_config.device_id,
                        center_freq_hz=int(hop_frequencies[active_freq_index]),
                        sample_rate_sps=active_config.sample_rate_sps,
                        lna_gain_db=active_config.lna_gain_db,
                        vga_gain_db=active_config.vga_gain_db,
                        amp_enable=active_config.amp_enable,
                        baseband_filter_hz=active_config.baseband_filter_hz,
                    )
                    previous_stream_id = stream.stream_id
                    stream = client.retune_stream(stream.stream_id, active_config)
                    detector = BurstDetector(sample_rate_sps=stream.sample_rate_sps, center_freq_hz=stream.center_freq_hz, stream_id=stream.stream_id)
                    samples_since_hop = 0
                    action = "Retuned" if stream.stream_id == previous_stream_id else "Restarted"
                    retune_message = f"{action} stream={stream.stream_id} freq={stream.center_freq_hz/1e6:.3f}MHz"
                    if print_human:
                        print(retune_message, file=sys.stderr, flush=True)
                    _emit_session_event(
                        {
                            "type": "retune",
                            "message": retune_message,
                            "stream_id": stream.stream_id,
                            "frequency_hz": stream.center_freq_hz,
                            "restart": stream.stream_id != previous_stream_id,
                        },
                        emit_event=emit_event,
                        telemetry=telemetry,
                        stats=stats,
                        family_tracker=family_tracker,
                    )
                    if stream.stream_id != previous_stream_id:
                        restart_required = True
                        break
                now = time.time()
                if print_human and now - last_candidate_summary_at >= float(args.candidate_summary_interval_s):
                    for line in family_tracker.summary_lines(now, limit=int(args.candidate_summary_limit)):
                        print(line, file=sys.stderr, flush=True)
                if now - last_candidate_summary_at >= float(args.candidate_summary_interval_s):
                    _emit_session_event(
                        {
                            "type": "family_summary",
                            "families": family_tracker.snapshot(now, limit=int(args.candidate_summary_limit)),
                        },
                        emit_event=emit_event,
                        telemetry=telemetry,
                        stats=stats,
                        family_tracker=family_tracker,
                    )
                    last_candidate_summary_at = now
                if now - last_status_at >= 10.0:
                    elapsed = now - stats.started_at
                    status_event = {
                        "type": "status",
                        "elapsed_s": round(elapsed, 3),
                        "chunks": stats.chunk_count,
                        "bursts": stats.burst_count,
                        "decoded": stats.packet_count,
                        "candidates": stats.candidate_count,
                        "rejected": stats.rejected_count,
                    }
                    _emit_session_event(
                        status_event,
                        emit_event=emit_event,
                        telemetry=telemetry,
                        stats=stats,
                        family_tracker=family_tracker,
                    )
                    if print_human and stats.packet_count == 0:
                        print(
                            f"status elapsed_s={elapsed:.1f} chunks={stats.chunk_count} bursts={stats.burst_count} decoded=0",
                            file=sys.stderr,
                            flush=True,
                        )
                    last_status_at = now
            if stop_event is not None and stop_event.is_set():
                break
            if not restart_required:
                break
    finally:
        for burst in detector.flush():
            result = decoder.decode(burst)
            if result is None:
                continue
            should_emit, repeat_count = repeater.observe(result, time.time())
            family_tracker.observe(result, time.time())
            if not should_emit:
                continue
            stats.packet_count += 1
            if args.save_dir is not None:
                decoder.save_burst(result, Path(args.save_dir))
            if emit_json:
                print(decoder.decode_to_json(result))
            else:
                elapsed = time.time() - stats.started_at
                check_hint = _checksum_hint(result.hex_string)
                detail_text = _protocol_detail_text(result)
                line = (
                    f"packet={stats.packet_count} freq={result.burst.center_freq_hz/1e6:.3f}MHz "
                    f"conf={result.confidence:.2f} mod={result.modulation} "
                    f"repeat={repeat_count} "
                    f"bits={_preview_text(result.bits)} bits_len={len(result.bits)} "
                    f"hex={_preview_text(result.hex_string)} hex_len={len(result.hex_string)} "
                    f"check={check_hint} "
                    f"burst_ms={result.burst.duration_seconds*1000:.2f} strategy={result.candidate.strategy} "
                    f"unit={result.candidate.unit_samples:.1f} elapsed_s={elapsed:.1f}"
                )
                if detail_text:
                    line = f"{line} {detail_text}"
                if repeat_count >= args.min_repeats and print_human:
                    line = f"{ANSI_GREEN}{line}{ANSI_RESET}"
                if print_human:
                    print(line)
        try:
            client.stop_stream(stream.stream_id)
        except Exception:
            pass
    return 0


def _preview_text(value: str, limit: int = MAX_PREVIEW_CHARS) -> str:
    if len(value) <= limit:
        return value
    head = max(8, limit // 2)
    tail = max(8, limit - head - 3)
    return f"{value[:head]}...{value[-tail:]}"


def _save_analyzer_burst(burst, directory: Path, duration_bin_ms: int, hint: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(burst.ended_at))
    stem = f"family_{int(burst.center_freq_hz)}_{duration_bin_ms}ms_{hint}_{stamp}"
    iq_path = directory / f"{stem}.iq"
    meta_path = directory / f"{stem}.json"
    interleaved = np.empty(burst.iq.size * 2, dtype=np.int8)
    interleaved[0::2] = np.clip(np.rint(burst.iq.real * 127.0), -128, 127).astype(np.int8)
    interleaved[1::2] = np.clip(np.rint(burst.iq.imag * 127.0), -128, 127).astype(np.int8)
    iq_path.write_bytes(interleaved.tobytes())
    meta_path.write_text(
        json.dumps(
            {
                "center_freq_hz": burst.center_freq_hz,
                "sample_rate_sps": burst.sample_rate_sps,
                "burst_duration_ms": round(burst.duration_seconds * 1000.0, 3),
                "burst_peak": round(burst.peak, 4),
                "burst_average": round(burst.average, 4),
                "duration_bin_ms": duration_bin_ms,
                "hint": hint,
                "saved_iq_path": iq_path.name,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
