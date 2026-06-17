from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable

import requests
import websocket
from websocket import WebSocketConnectionClosedException

from bluetooth_lowenergy.detector import BLE_ADV_CHANNELS, WideBLEAdvertisingDetector


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BINARY = PLUGIN_ROOT / "build" / "btcexplorer-sniffer"
DEFAULT_GATEWAY_BINARY = PLUGIN_ROOT / "build" / "btcexplorer-sniffer-gateway"
DEFAULT_PAGE_BINARY = PLUGIN_ROOT / "build" / "btcexplorer-page-stimulus"
DEFAULT_LOG_DIR = Path(os.getenv("RF_SENTINEL_LOG_DIR", "/var/log/rf_sentinel"))
DEFAULT_LOG = DEFAULT_LOG_DIR / "btcexplorer-sniffer.log"
DEFAULT_IQ_DIR = Path(os.getenv("RF_SENTINEL_IQ_DIR", str(DEFAULT_LOG_DIR / "iq")))
DEFAULT_BLUETOOTH_IQ_CAPTURE = DEFAULT_IQ_DIR / "bluetooth_combined.cs8"
ANSI_RESET = "\033[0m"
ANSI_BLE_BLUE = "\033[34m"
ANSI_BTC_CYAN = "\033[36m"

CSV_FIELDS = [
    "seen_at",
    "device_id",
    "driver",
    "center_mhz",
    "bandwidth_mhz",
    "channel",
    "rssi_dbfs",
    "event_type",
    "address",
    "nap",
    "uap",
    "lap",
    "access_lap",
    "candidate_count",
    "uaps",
    "packet",
]


def _btc_bank_start_channel(center_mhz: float, bandwidth_mhz: int) -> int:
    start = int(round(float(center_mhz) - 2402.0 - ((float(bandwidth_mhz) - 1.0) / 2.0)))
    return max(0, min(78, start))


def _normalize_native_json_event(event: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    event_type = str(event.get("type") or "")
    if event_type in {"config", "metrics"}:
        return event
    if not event.get("lap") and event_type != "passive_fhs_bdaddr":
        return event
    event.setdefault("protocol", "btc")
    event.setdefault("kind", "classic_lap")
    try:
        bin_index = int(event.get("channel"))
    except (TypeError, ValueError):
        return event
    event.setdefault("btcsniffer_bin", bin_index)
    event["channel"] = _btc_bank_start_channel(float(args.center_mhz), int(args.bandwidth_mhz)) + bin_index
    if event_type == "page_access_seen":
        event.setdefault("status", "page_access")
        event.setdefault("detail", "page/inquiry access code observed")
    return event


def _page_detection_enabled(args: argparse.Namespace) -> bool:
    disabled = str(os.getenv("RF_SENTINEL_DISABLE_PAGE_DETECTION", "")).strip().lower() in {"1", "true", "yes", "on"}
    return not disabled and not bool(getattr(args, "no_page_detection", False))


def _parse_native_status_line(text: str, args: argparse.Namespace) -> dict[str, Any] | None:
    match = re.search(
        r"\[\s*(?P<bin>\d+)\]\s+(?P<ts>\d+)\s+us\s+--\s+(?P<lap>[0-9A-Fa-f]{6})\s+--\s+(?P<msg>.*)",
        text,
    )
    if not match:
        return None
    bin_index = int(match.group("bin"))
    channel = _btc_bank_start_channel(float(args.center_mhz), int(args.bandwidth_mhz)) + bin_index
    lap = match.group("lap").upper()
    msg = match.group("msg").strip()
    base = {
        "protocol": "btc",
        "channel": channel,
        "bin": bin_index,
        "lap": lap,
        "ts_us": int(match.group("ts")),
        "source": "btcexplorer-sniffer",
        "raw": text,
    }

    resolved = re.search(
        r"RESOLVED UAP:LAP\s+(?P<uap>[0-9A-Fa-f]{2}):(?P<lap>[0-9A-Fa-f]{6})(?:.*tracking(?:\s+for)?\s+(?P<tracking>\d+)\s+us)?",
        msg,
    )
    if resolved:
        return {
            **base,
            "type": "lap_resolved",
            "kind": "classic_lap",
            "lap": resolved.group("lap").upper(),
            "uap": resolved.group("uap").upper(),
            "candidate_count": 1,
            "tracking_us": int(resolved.group("tracking") or 0),
        }

    two_left = re.search(
        r"Only two UAP left \((?P<uap0>[0-9A-Fa-f]{2}) and (?P<uap1>[0-9A-Fa-f]{2})\).*tracking for\s+(?P<tracking>\d+)\s+us",
        msg,
    )
    if two_left:
        return {
            **base,
            "type": "lap_two_uap_left",
            "kind": "classic_lap",
            "uap0": two_left.group("uap0").upper(),
            "uap1": two_left.group("uap1").upper(),
            "candidate_count": 2,
            "tracking_us": int(two_left.group("tracking") or 0),
        }

    narrowed = re.search(r"(?P<count>\d+)\s+possible UAPs remaining\s+\[(?P<uaps>[0-9A-Fa-f ]+)\]", msg)
    if narrowed:
        return {
            **base,
            "type": "lap_narrowed",
            "kind": "classic_lap",
            "candidate_count": int(narrowed.group("count")),
            "uaps": narrowed.group("uaps").strip(),
        }

    if "Initialized" in msg:
        return {**base, "type": "lap_initialized", "kind": "classic_lap", "candidate_count": 32}
    return None


def _device_driver(device_id: str, fallback: str) -> str:
    lowered = str(device_id or "").strip().lower()
    if lowered.startswith("hackrf"):
        return "hackrf"
    if lowered.startswith("bladerf"):
        return "bladerf"
    if lowered.startswith("rtlsdr") or lowered.startswith("rtl"):
        return "rtlsdr"
    if lowered.startswith("airspy"):
        return "airspy"
    return fallback


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
    result = subprocess.run([file_tool, "-b", str(binary)], check=False, capture_output=True, text=True, timeout=5)
    description = f"{result.stdout} {result.stderr}".strip().lower()
    if result.returncode != 0 or not description or "elf" not in description:
        return True, description or f"file returned {result.returncode}"
    if any(token in description for token in _native_arch_tokens()):
        return True, description
    return False, description


def _build_inputs(binary: Path) -> list[Path]:
    paths = [PLUGIN_ROOT / "CMakeLists.txt"]
    if binary.name == "btcexplorer-page-stimulus":
        paths.append(PLUGIN_ROOT / "src" / "page_stimulus.cpp")
    else:
        paths.extend([PLUGIN_ROOT / "src" / "btsniffer.cpp", PLUGIN_ROOT / "src" / "lapnode.cpp"])
        paths.extend(sorted((PLUGIN_ROOT / "src").glob("*.hpp")))
    return [path for path in paths if path.exists()]


def _rebuild_reason(binary: Path) -> str | None:
    if not binary.exists():
        return "binary missing"
    if not os.access(binary, os.X_OK):
        return "binary is not executable"
    arch_ok, arch_detail = _binary_arch_matches_host(binary)
    if not arch_ok:
        return f"binary architecture does not match host ({arch_detail})"
    binary_mtime = binary.stat().st_mtime
    newest_input = max((path.stat().st_mtime for path in _build_inputs(binary)), default=0.0)
    if newest_input > binary_mtime:
        return "source is newer than binary"
    return None


def _ensure_binary(auto_build: bool = True, binary: Path = DEFAULT_BINARY) -> Path:
    reason = _rebuild_reason(binary)
    if reason is None:
        return binary
    if not auto_build:
        raise RuntimeError(f"Bluetooth Classic sniffer rebuild required but auto-build is disabled: {reason}")
    cmake = shutil.which("cmake")
    if not cmake:
        raise RuntimeError(f"Bluetooth Classic sniffer rebuild required ({reason}) but cmake was not found")
    build_dir = PLUGIN_ROOT / "build"
    if build_dir.exists() and not os.access(build_dir, os.W_OK):
        build_dir = PLUGIN_ROOT / "build-user"
        binary = build_dir / binary.name
        existing_reason = _rebuild_reason(binary)
        if existing_reason is None:
            return binary
        reason = f"{reason}; default build dir not writable, using build-user ({existing_reason})"
    build_dir.mkdir(parents=True, exist_ok=True)
    print(f"building bluetooth-classic native sniffer: {reason}", file=sys.stderr, flush=True)
    configure = subprocess.run([cmake, "-S", str(PLUGIN_ROOT), "-B", str(build_dir)], check=False, capture_output=True, text=True, timeout=120)
    if configure.returncode != 0:
        raise RuntimeError(f"cmake configure failed\nstdout:\n{configure.stdout[-4000:]}\nstderr:\n{configure.stderr[-4000:]}")
    jobs = os.getenv("BTC_SNIFFER_BUILD_JOBS", str(max(1, min(4, os.cpu_count() or 1))))
    build = subprocess.run([cmake, "--build", str(build_dir), "--parallel", jobs], check=False, capture_output=True, text=True, timeout=300)
    if build.returncode != 0:
        raise RuntimeError(f"cmake build failed\nstdout:\n{build.stdout[-4000:]}\nstderr:\n{build.stderr[-4000:]}")
    binary.chmod(binary.stat().st_mode | 0o111)
    return binary


def _gateway_base(base_url: str | None = None) -> str:
    return (base_url or os.getenv("SDR_GATEWAY_BASE_URL", "http://127.0.0.1:8080")).rstrip("/")


def _gateway_token(token: str | None = None) -> str:
    explicit = (token or "").strip()
    if explicit:
        return explicit
    return (os.getenv("SDR_GATEWAY_API_TOKEN", "") or "").strip()


def _gateway_headers(token: str | None = None) -> dict[str, str]:
    resolved = _gateway_token(token)
    return {"Authorization": f"Bearer {resolved}"} if resolved else {}


def _ws_url_for_stream(
    base_url: str | None,
    stream_id: str,
    token: str | None = None,
    *,
    start: str = "latest",
) -> str:
    base = _gateway_base(base_url)
    ws_base = "wss://" + base[len("https://") :] if base.startswith("https://") else "ws://" + base[len("http://") :]
    suffix = f"?keep=1&start={start}"
    resolved_token = _gateway_token(token)
    if resolved_token:
        suffix += f"&token={resolved_token}"
    return f"{ws_base}/ws/iq/{stream_id}{suffix}"


def _start_gateway_stream(args: argparse.Namespace) -> str:
    sample_rate_sps = int(args.bandwidth_mhz) * 1_000_000
    payload = {
        "device_id": args.device_id,
        "center_freq_hz": int(round(float(args.center_mhz) * 1_000_000.0)),
        "sample_rate_sps": sample_rate_sps,
        "lna_gain_db": int(args.lna_gain_db),
        "vga_gain_db": int(args.vga_gain_db),
        "amp_enable": bool(args.amp_gain_db and float(args.amp_gain_db) > 0.0),
        "baseband_filter_hz": sample_rate_sps,
        "duration_seconds": None,
        "num_samples": None,
    }
    resp = requests.post(
        f"{_gateway_base(args.gateway_base_url)}/streams/start",
        headers=_gateway_headers(args.gateway_token),
        json=payload,
        timeout=12,
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        detail = ""
        try:
            body = resp.json()
            detail = str(body.get("detail") or body.get("error") or "")
        except (ValueError, AttributeError):
            detail = resp.text.strip()
        message = f"sdr-gateway stream start failed: HTTP {resp.status_code}"
        if detail:
            message += f": {detail}"
        raise RuntimeError(message) from exc
    return str(resp.json()["stream_id"])


def _stop_gateway_stream(base_url: str | None, token: str | None, stream_id: str) -> None:
    try:
        requests.post(f"{_gateway_base(base_url)}/streams/{stream_id}/stop", headers=_gateway_headers(token), timeout=5)
    except requests.RequestException:
        pass


def _path_from_arg(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    return Path(text).expanduser() if text else None


def _resolve_iq_capture_path(args: argparse.Namespace) -> Path:
    return _path_from_arg(getattr(args, "iq_capture_path", None)) or DEFAULT_BLUETOOTH_IQ_CAPTURE


def _resolve_iq_playback_path(args: argparse.Namespace) -> Path:
    return (
        _path_from_arg(getattr(args, "iq_playback_path", None))
        or _path_from_arg(getattr(args, "iq_capture_path", None))
        or DEFAULT_BLUETOOTH_IQ_CAPTURE
    )


def _iq_metadata(args: argparse.Namespace, *, mode: str, path: Path, bytes_written: int = 0) -> dict[str, Any]:
    sample_rate_sps = int(float(args.bandwidth_mhz) * 1_000_000.0)
    return {
        "format": "cs8",
        "sample_layout": "interleaved_i8_iq",
        "mode": mode,
        "path": str(path),
        "bytes_written": int(bytes_written),
        "created_at": time.time(),
        "device_id": str(args.device_id),
        "driver": _device_driver(args.device_id, getattr(args, "driver", "")),
        "center_freq_hz": int(round(float(args.center_mhz) * 1_000_000.0)),
        "center_mhz": float(args.center_mhz),
        "bandwidth_hz": sample_rate_sps,
        "bandwidth_mhz": int(args.bandwidth_mhz),
        "sample_rate_sps": sample_rate_sps,
        "protocols": ["bluetooth_classic", "bluetooth_lowenergy"],
        "decoder": "bluetooth_scanner combined",
    }


def _write_iq_metadata(args: argparse.Namespace, *, mode: str, path: Path, bytes_written: int = 0) -> Path:
    metadata_path = path.with_suffix(path.suffix + ".json")
    metadata_path.write_text(
        json.dumps(_iq_metadata(args, mode=mode, path=path, bytes_written=bytes_written), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return metadata_path


def _iter_iq_playback_chunks(path: Path, chunk_bytes: int = 131_072) -> Iterable[bytes]:
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(max(1, int(chunk_bytes)))
            if not chunk:
                break
            yield chunk


def _bridge_gateway_to_stdin(
    *,
    base_url: str | None,
    token: str | None,
    stream_id: str,
    stdin: Any,
    stop: threading.Event,
    raw: bool,
    label: str = "btc",
) -> None:
    ws = None
    chunks = 0
    byte_count = 0
    last_report = time.monotonic()
    try:
        ws = websocket.create_connection(_ws_url_for_stream(base_url, stream_id, token), timeout=8)
        ws.settimeout(1.0)
        if raw:
            print(f"{label} gateway bridge stream_id={stream_id} -> stdin", file=sys.stderr, flush=True)
        while not stop.is_set():
            try:
                chunk = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not isinstance(chunk, (bytes, bytearray)):
                continue
            stdin.write(chunk)
            stdin.flush()
            chunks += 1
            byte_count += len(chunk)
            now = time.monotonic()
            if raw and now - last_report >= 5.0:
                print(f"{label} gateway bridge chunks={chunks} bytes={byte_count}", file=sys.stderr, flush=True)
                last_report = now
    except (BrokenPipeError, WebSocketConnectionClosedException):
        stop.set()
    except Exception as exc:
        if not stop.is_set():
            print(f"{label} gateway bridge error={exc}", file=sys.stderr, flush=True)
            stop.set()
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


def _combined_iq_source_worker(
    *,
    args: argparse.Namespace,
    stream_id: str | None,
    stdin: Any,
    stop: threading.Event,
    ble_chunks: "queue.Queue[bytes | None]",
    events: "queue.Queue[dict[str, Any]]",
) -> None:
    mode = str(getattr(args, "rf_input_mode", "live") or "live").lower()
    raw = bool(args.raw)
    chunks = 0
    byte_count = 0
    last_report = time.monotonic()
    capture_handle = None
    capture_path: Path | None = None
    ws = None

    def forward(chunk: bytes) -> None:
        nonlocal chunks, byte_count, last_report
        if not chunk:
            return
        stdin.write(chunk)
        stdin.flush()
        if capture_handle is not None:
            capture_handle.write(chunk)
        try:
            ble_chunks.put(chunk, timeout=0.25)
        except queue.Full:
            events.put({"protocol": "ble", "type": "warning", "message": "BLE IQ queue full; dropping IQ chunk"})
        chunks += 1
        byte_count += len(chunk)
        now = time.monotonic()
        if raw and now - last_report >= 5.0:
            print(f"combined iq {mode} chunks={chunks} bytes={byte_count}", file=sys.stderr, flush=True)
            last_report = now

    try:
        if mode == "playback":
            playback_path = _resolve_iq_playback_path(args)
            events.put({"protocol": "iq", "type": "status", "message": f"playback={playback_path}"})
            for chunk in _iter_iq_playback_chunks(playback_path, getattr(args, "iq_chunk_bytes", 131_072)):
                if stop.is_set():
                    break
                forward(chunk)
            return

        if mode == "capture":
            capture_path = _resolve_iq_capture_path(args)
            capture_path.parent.mkdir(parents=True, exist_ok=True)
            capture_handle = capture_path.open("wb")
            _write_iq_metadata(args, mode=mode, path=capture_path, bytes_written=0)
            events.put({"protocol": "iq", "type": "status", "message": f"capture={capture_path}"})

        if stream_id is None:
            raise RuntimeError("live/capture mode requires an sdr-gateway stream")
        ws = websocket.create_connection(_ws_url_for_stream(args.gateway_base_url, stream_id, args.gateway_token), timeout=8)
        ws.settimeout(1.0)
        if raw:
            print(f"combined iq {mode} stream_id={stream_id}", file=sys.stderr, flush=True)
        while not stop.is_set():
            try:
                chunk = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not isinstance(chunk, (bytes, bytearray)):
                continue
            forward(bytes(chunk))
            max_bytes = int(getattr(args, "iq_capture_max_bytes", 0) or 0)
            if mode == "capture" and max_bytes > 0 and byte_count >= max_bytes:
                events.put({"protocol": "iq", "type": "status", "message": f"capture_limit_reached bytes={byte_count}"})
                stop.set()
                break
    except (BrokenPipeError, WebSocketConnectionClosedException):
        stop.set()
    except Exception as exc:
        if not stop.is_set():
            events.put({"protocol": "iq", "type": "error", "message": str(exc)})
            stop.set()
    finally:
        try:
            stdin.close()
        except Exception:
            pass
        if capture_handle is not None:
            try:
                capture_handle.flush()
                capture_handle.close()
            except Exception:
                pass
        if capture_path is not None:
            try:
                _write_iq_metadata(args, mode=mode, path=capture_path, bytes_written=byte_count)
            except OSError:
                pass
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        try:
            ble_chunks.put(None, timeout=0.25)
        except queue.Full:
            pass


def _csv_row(event: dict[str, Any], args: argparse.Namespace, packet: str) -> dict[str, Any]:
    return {
        "seen_at": f"{time.time():.6f}",
        "device_id": args.device_id,
        "driver": _device_driver(args.device_id, args.driver),
        "center_mhz": f"{float(args.center_mhz):.3f}",
        "bandwidth_mhz": int(args.bandwidth_mhz),
        "channel": event.get("channel", ""),
        "rssi_dbfs": event.get("rssi_dbfs", ""),
        "event_type": event.get("type", ""),
        "address": event.get("address", ""),
        "nap": event.get("nap", ""),
        "uap": event.get("uap", ""),
        "lap": event.get("lap", ""),
        "access_lap": event.get("access_lap", ""),
        "candidate_count": event.get("candidate_count", ""),
        "uaps": event.get("uaps", ""),
        "packet": packet,
    }


def _run_listen(args: argparse.Namespace) -> int:
    binary = _ensure_binary(
        auto_build=not args.no_auto_build,
        binary=DEFAULT_GATEWAY_BINARY if args.source == "gateway" else DEFAULT_BINARY,
    )
    driver = _device_driver(args.device_id, args.driver)
    args.log.parent.mkdir(parents=True, exist_ok=True)
    gateway_stream_id = ""
    gateway_stop = threading.Event()
    gateway_thread: threading.Thread | None = None
    if args.source == "gateway":
        gateway_stream_id = _start_gateway_stream(args)

    cmd = [
        str(binary),
        "--driver",
        driver,
        "--freq-mhz",
        f"{float(args.center_mhz):.3f}MHz",
        "--bandwidth-mhz",
        f"{int(args.bandwidth_mhz)}MHz",
        "--seconds",
        f"{float(args.seconds):.3f}",
        "--lna-gain-db",
        f"{float(args.lna_gain_db):.1f}",
        "--vga-gain-db",
        f"{float(args.vga_gain_db):.1f}",
        "--amp-gain-db",
        f"{float(args.amp_gain_db):.1f}",
        "--log",
        str(args.log),
        "--jsonl-stdout",
    ]
    if args.source == "gateway":
        cmd.extend(["--input-stdin", "--input-format", "cs8"])
    if args.show_init_failed:
        cmd.append("--show-init-failed")
    if getattr(args, "debug_target_lap", ""):
        cmd.extend(["--debug-target-lap", _clean_hex(args.debug_target_lap, 6, "--debug-target-lap")])
    if getattr(args, "expected_bdaddr", ""):
        cmd.extend(["--expected-bdaddr", _clean_bdaddr(args.expected_bdaddr)])
    if getattr(args, "debug_fhs_rejects", False):
        cmd.append("--debug-fhs-rejects")
    if getattr(args, "fhs_max_fec_errors", 0):
        cmd.extend(["--fhs-max-fec-errors", str(int(args.fhs_max_fec_errors))])
    if getattr(args, "debug_energy_bin", -1) is not None and int(getattr(args, "debug_energy_bin", -1)) >= 0:
        cmd.extend(["--debug-energy-bin", str(int(args.debug_energy_bin))])
    if args.events_path:
        cmd.extend(["--events", str(args.events_path)])

    if args.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
    else:
        writer = None
        print(
            f"using source={args.source} device={args.device_id} driver={driver} center={float(args.center_mhz):.3f}MHz "
            f"bandwidth={int(args.bandwidth_mhz)}MHz lna={args.lna_gain_db} vga={args.vga_gain_db} amp={args.amp_gain_db}",
            file=sys.stderr,
            flush=True,
        )

    stop_requested = False

    def _stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True
        gateway_stop.set()
        if proc.poll() is None:
            proc.terminate()

    proc = subprocess.Popen(
        cmd,
        cwd=str(PLUGIN_ROOT),
        stdin=subprocess.PIPE if args.source == "gateway" else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    if args.source == "gateway":
        assert proc.stdin is not None
        gateway_thread = threading.Thread(
            target=_bridge_gateway_to_stdin,
            kwargs={
                "base_url": args.gateway_base_url,
                "token": args.gateway_token,
                "stream_id": gateway_stream_id,
                "stdin": proc.stdin.buffer,
                "stop": gateway_stop,
                "raw": bool(args.raw),
            },
            daemon=True,
        )
        gateway_thread.start()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            text = line.strip()
            if not text:
                continue
            event: dict[str, Any] | None = None
            if text.startswith("{"):
                try:
                    event = json.loads(text)
                    event = _normalize_native_json_event(event, args)
                except json.JSONDecodeError:
                    event = None
            if event is None:
                event = _parse_native_status_line(text, args)
            if event is None:
                if args.raw:
                    print(text, flush=True)
                continue
            event_type = str(event.get("type") or "")
            if event_type == "page_access_seen" and not _page_detection_enabled(args):
                continue
            if event_type == "passive_fhs_bdaddr" and not getattr(args, "log_passive_fhs_bdaddr", False):
                continue
            if event_type == "metrics" and not args.metrics:
                continue
            if args.json:
                print(json.dumps(event, separators=(",", ":")), flush=True)
            elif writer is not None:
                if event_type != "metrics":
                    writer.writerow(_csv_row(event, args, text))
                    sys.stdout.flush()
            elif event_type == "config":
                continue
            elif event_type == "metrics":
                fhs_best = ""
                if event.get("fhs_expected_best_address"):
                    fhs_best = (
                        f" expected_best={event.get('fhs_expected_best_address')}"
                        f"/{event.get('fhs_expected_best_bit_errors')}bit"
                        f"/fec{event.get('fhs_expected_best_fec_errors')}"
                    )
                print(
                    f"metrics packets={event.get('packets_seen')} access={event.get('access_hits')} "
                    f"lap={event.get('lap_events')} resolved={event.get('resolved_events')} "
                    f"fhs={event.get('fhs_events')} fhs_attempts={event.get('fhs_attempts')}"
                    f" expected_hits={event.get('fhs_expected_payload_matches', 0)}{fhs_best}",
                    flush=True,
                )
            elif event_type == "passive_fhs_bdaddr":
                print(
                    f"bdaddr address={event.get('address')} ch={event.get('channel')} "
                    f"rssi={event.get('rssi_dbfs')} access_lap={event.get('access_lap')} "
                    f"verification={event.get('verification', 'unchecked')} "
                    f"fec_errors={event.get('fec_errors', 0)}",
                    flush=True,
                )
            elif event_type == "fhs_reject":
                print(
                    f"fhs_reject reason={event.get('reason')} address={event.get('address', '')} "
                    f"verification={event.get('verification', '')} ch={event.get('channel')} "
                    f"rssi={event.get('rssi_dbfs')}",
                    flush=True,
                )
            elif event_type == "page_access_seen":
                print(
                    f"page_access_seen lap={event.get('lap')} ch={event.get('channel')} "
                    f"rssi={event.get('rssi_dbfs')} ts_us={event.get('ts_us')}",
                    flush=True,
                )
            elif event_type == "debug_bin_energy":
                print(
                    f"debug_bin_energy bin={event.get('bin')} rssi={event.get('rssi_dbfs')}",
                    flush=True,
                )
            elif event_type in {"lap_initialized", "lap_resolved", "lap_seen"} or event.get("lap"):
                print(
                    f"classic type={event_type} lap={event.get('lap')} uap={event.get('uap', '')} "
                    f"ch={event.get('channel', '')} rssi={event.get('rssi_dbfs', '')} "
                    f"candidates={event.get('candidate_count', '')} uaps={event.get('uaps', '')}",
                    flush=True,
                )
        return proc.wait()
    finally:
        gateway_stop.set()
        if stop_requested and proc.poll() is None:
            proc.terminate()
        if gateway_stream_id:
            _stop_gateway_stream(args.gateway_base_url, args.gateway_token, gateway_stream_id)
        if gateway_thread is not None:
            gateway_thread.join(timeout=1.0)


def _combined_ble_worker(
    args: argparse.Namespace,
    chunks: "queue.Queue[bytes | None]",
    stop: threading.Event,
    events: "queue.Queue[dict[str, Any]]",
) -> None:
    detector = WideBLEAdvertisingDetector(
        sample_rate_sps=int(args.bandwidth_mhz) * 1_000_000,
        center_freq_hz=int(round(float(args.center_mhz) * 1_000_000.0)),
        channels=list(args.ble_channels or sorted(BLE_ADV_CHANNELS)),
        channel_rate_sps=int(args.ble_channel_rate_sps),
    )
    visible = ",".join(str(lane["channel"]) for lane in detector.lanes) or "-"
    events.put({"protocol": "ble", "type": "status", "message": f"visible_ble_channels={visible}"})
    try:
        while not stop.is_set():
            try:
                chunk = chunks.get(timeout=0.5)
            except queue.Empty:
                continue
            if chunk is None:
                return
            _, decoded = detector.process_iq_i8(chunk)
            for event in decoded:
                if event.get("kind") != "ble_adv" and not args.debug_bursts:
                    continue
                event["protocol"] = "ble"
                event["device_id"] = args.device_id
                event["window"] = "combined"
                events.put(event)
                if args.max_events and event.get("kind") == "ble_adv":
                    args._events_seen = getattr(args, "_events_seen", 0) + 1
                    if args._events_seen >= args.max_events:
                        stop.set()
                        return
    except Exception as exc:
        if not stop.is_set():
            events.put({"protocol": "ble", "type": "error", "message": str(exc)})
            stop.set()


def _combined_btc_stdout_worker(proc: subprocess.Popen[str], args: argparse.Namespace, stop: threading.Event, events: "queue.Queue[dict[str, Any]]") -> None:
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            text = line.strip()
            if not text:
                continue
            if text.startswith("{"):
                try:
                    event = json.loads(text)
                    event = _normalize_native_json_event(event, args)
                    if str(event.get("type") or "") == "page_access_seen" and not _page_detection_enabled(args):
                        continue
                    if str(event.get("type") or "") == "passive_fhs_bdaddr" and not getattr(args, "log_passive_fhs_bdaddr", False):
                        continue
                    events.put(event)
                    continue
                except json.JSONDecodeError:
                    pass
            event = _parse_native_status_line(text, args)
            if event is not None:
                events.put(event)
            if args.raw:
                events.put({"protocol": "btc", "type": "raw", "message": text})
    finally:
        stop.set()


def _print_combined_event(event: dict[str, Any], args: argparse.Namespace) -> None:
    protocol = str(event.get("protocol") or "")
    if args.json:
        print(json.dumps(event, separators=(",", ":"), sort_keys=True), flush=True)
        return
    ble_prefix = f"{ANSI_BLE_BLUE}[ble]{ANSI_RESET}"
    btc_prefix = f"{ANSI_BTC_CYAN}[btc]{ANSI_RESET}"
    if protocol == "ble" and event.get("kind") == "ble_adv":
        name = f" name={json.dumps(event.get('name'))}" if event.get("name") else ""
        manufacturer = event.get("manufacturer") if isinstance(event.get("manufacturer"), dict) else {}
        company = f" company={json.dumps(manufacturer.get('company_name'))}" if manufacturer.get("company_name") else ""
        print(
            f"{ble_prefix} adv ch={event.get('channel')} addr={event.get('address')} type={event.get('address_type')} "
            f"pdu={event.get('pdu_type')} rssi_dbfs={event.get('rssi_dbfs')}{name}{company}",
            flush=True,
        )
        return
    if protocol == "ble" and event.get("type") == "status":
        print(f"{ble_prefix} status {event.get('message')}", file=sys.stderr, flush=True)
        return
    if protocol == "btc":
        event_type = str(event.get("type") or "")
        if event_type == "metrics" and not args.metrics:
            return
        if event_type == "config":
            return
        if event_type == "raw":
            print(f"{btc_prefix} {event.get('message') or ''}", flush=True)
            return
        if event_type == "passive_fhs_bdaddr":
            print(
                f"{btc_prefix} bdaddr address={event.get('address')} ch={event.get('channel')} "
                f"rssi={event.get('rssi_dbfs')} access_lap={event.get('access_lap')}",
                flush=True,
            )
            return
        if event_type in {"lap_initialized", "lap_resolved", "lap_seen"} or event.get("lap"):
            print(
                f"{btc_prefix} type={event_type} lap={event.get('lap')} uap={event.get('uap', '')} "
                f"ch={event.get('channel', '')} rssi={event.get('rssi_dbfs', '')}",
                flush=True,
            )
            return
    if args.raw:
        prefix = btc_prefix if protocol == "btc" else ble_prefix if protocol == "ble" else protocol
        print(f"{prefix} event {event}", flush=True)


def _run_combined(args: argparse.Namespace) -> int:
    binary = _ensure_binary(auto_build=not args.no_auto_build, binary=DEFAULT_GATEWAY_BINARY)
    args.log.parent.mkdir(parents=True, exist_ok=True)
    mode = str(getattr(args, "rf_input_mode", "live") or "live").lower()
    stream_id = None if mode == "playback" else _start_gateway_stream(args)
    stop = threading.Event()
    events: "queue.Queue[dict[str, Any]]" = queue.Queue()
    ble_chunks: "queue.Queue[bytes | None]" = queue.Queue(maxsize=max(1, int(getattr(args, "iq_queue_chunks", 64) or 64)))
    driver = _device_driver(args.device_id, args.driver)
    cmd = [
        str(binary),
        "--driver",
        driver,
        "--freq-mhz",
        f"{float(args.center_mhz):.3f}MHz",
        "--bandwidth-mhz",
        f"{int(args.bandwidth_mhz)}MHz",
        "--seconds",
        f"{float(args.seconds):.3f}",
        "--lna-gain-db",
        f"{float(args.lna_gain_db):.1f}",
        "--vga-gain-db",
        f"{float(args.vga_gain_db):.1f}",
        "--amp-gain-db",
        f"{float(args.amp_gain_db):.1f}",
        "--log",
        str(args.log),
        "--jsonl-stdout",
        "--input-stdin",
        "--input-format",
        "cs8",
    ]
    if args.show_init_failed:
        cmd.append("--show-init-failed")
    if getattr(args, "debug_target_lap", ""):
        cmd.extend(["--debug-target-lap", _clean_hex(args.debug_target_lap, 6, "--debug-target-lap")])
    if getattr(args, "expected_bdaddr", ""):
        cmd.extend(["--expected-bdaddr", _clean_bdaddr(args.expected_bdaddr)])
    if getattr(args, "debug_fhs_rejects", False):
        cmd.append("--debug-fhs-rejects")
    if getattr(args, "fhs_max_fec_errors", 0):
        cmd.extend(["--fhs-max-fec-errors", str(int(args.fhs_max_fec_errors))])
    if getattr(args, "debug_energy_bin", -1) is not None and int(getattr(args, "debug_energy_bin", -1)) >= 0:
        cmd.extend(["--debug-energy-bin", str(int(args.debug_energy_bin))])

    print(
        f"using source=gateway-combined mode={mode} stream_id={stream_id or '-'} device={args.device_id} driver={driver} "
        f"center={float(args.center_mhz):.3f}MHz bandwidth={int(args.bandwidth_mhz)}MHz "
        f"lna={args.lna_gain_db} vga={args.vga_gain_db}",
        file=sys.stderr,
        flush=True,
    )
    proc = subprocess.Popen(
        cmd,
        cwd=str(PLUGIN_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdin is not None
    threads = [
        threading.Thread(
            target=_combined_iq_source_worker,
            kwargs={
                "args": args,
                "stream_id": stream_id,
                "stdin": proc.stdin.buffer,
                "stop": stop,
                "ble_chunks": ble_chunks,
                "events": events,
            },
            daemon=True,
        ),
        threading.Thread(target=_combined_btc_stdout_worker, args=(proc, args, stop, events), daemon=True),
        threading.Thread(target=_combined_ble_worker, args=(args, ble_chunks, stop, events), daemon=True),
    ]

    def _stop(_signum: int, _frame: Any) -> None:
        stop.set()
        if proc.poll() is None:
            proc.terminate()

    previous_int = signal.signal(signal.SIGINT, _stop)
    previous_term = signal.signal(signal.SIGTERM, _stop)
    try:
        for thread in threads:
            thread.start()
        while not stop.is_set():
            try:
                event = events.get(timeout=0.25)
            except queue.Empty:
                if proc.poll() is not None:
                    stop.set()
                continue
            _print_combined_event(event, args)
        while True:
            try:
                event = events.get_nowait()
            except queue.Empty:
                break
            _print_combined_event(event, args)
        return proc.poll() or 0
    finally:
        stop.set()
        if proc.poll() is None:
            proc.terminate()
        if stream_id:
            _stop_gateway_stream(args.gateway_base_url, args.gateway_token, stream_id)
        for thread in threads:
            thread.join(timeout=1.0)
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)


def _clean_hex(value: str, width: int, name: str) -> str:
    cleaned = "".join(ch for ch in str(value or "").upper() if ch in "0123456789ABCDEF")
    if len(cleaned) != width:
        raise RuntimeError(f"{name} must be exactly {width} hex characters")
    return cleaned


def _clean_bdaddr(value: str, name: str = "--expected-bdaddr") -> str:
    cleaned = _clean_hex(value, 12, name)
    return ":".join(cleaned[idx : idx + 2] for idx in range(0, 12, 2))


def _run_page_stimulus(args: argparse.Namespace) -> int:
    if not args.lab_authorized:
        raise RuntimeError("--lab-authorized is required for active RF page stimulus")
    target_lap = _clean_hex(args.target_lap, 6, "--target-lap")
    target_uap = _clean_hex(args.target_uap, 2, "--target-uap") if args.target_uap else ""
    expected_bdaddr = _clean_bdaddr(args.expected_bdaddr) if args.expected_bdaddr else ""
    if args.timeout_s <= 0.0 or args.timeout_s > 60.0:
        raise RuntimeError("--timeout-s must be >0 and <=60")

    rx_binary = _ensure_binary(auto_build=not args.no_auto_build, binary=DEFAULT_GATEWAY_BINARY if args.source == "gateway" else DEFAULT_BINARY)
    tx_binary = _ensure_binary(auto_build=not args.no_auto_build, binary=DEFAULT_PAGE_BINARY)

    rx_device = args.rx_device_id or args.device_id
    tx_device = args.tx_device_id or args.device_id
    rx_driver = _device_driver(rx_device, args.driver)
    tx_driver = _device_driver(tx_device, args.tx_driver or rx_driver)
    stop = threading.Event()

    rx_cmd = [
        sys.executable,
        "-m",
        "bluetooth_classic.cli",
        "listen",
        "--source",
        args.source,
        "--device-id",
        rx_device,
        "--driver",
        rx_driver,
        "--center-mhz",
        f"{float(args.center_mhz):.3f}",
        "--bandwidth-mhz",
        str(int(args.bandwidth_mhz)),
        "--seconds",
        f"{float(args.seconds):.3f}",
        "--lna-gain-db",
        f"{float(args.lna_gain_db):.1f}",
        "--vga-gain-db",
        f"{float(args.vga_gain_db):.1f}",
        "--amp-gain-db",
        f"{float(args.amp_gain_db):.1f}",
        "--json",
        "--debug-target-lap",
        target_lap,
    ]
    if expected_bdaddr:
        rx_cmd.extend(["--expected-bdaddr", expected_bdaddr])
    if args.debug_fhs_rejects:
        rx_cmd.append("--debug-fhs-rejects")
    if args.fhs_max_fec_errors:
        rx_cmd.extend(["--fhs-max-fec-errors", str(int(args.fhs_max_fec_errors))])
    debug_bin = -1
    channel_text = str(args.page_channels or "").strip()
    if re.fullmatch(r"\d+", channel_text):
        debug_bin = int(channel_text) - _btc_bank_start_channel(float(args.center_mhz), int(args.bandwidth_mhz))
        if 0 <= debug_bin < int(args.bandwidth_mhz):
            rx_cmd.extend(["--debug-energy-bin", str(debug_bin)])
    if args.gateway_base_url:
        rx_cmd.extend(["--gateway-base-url", args.gateway_base_url])
    if args.gateway_token:
        rx_cmd.extend(["--gateway-token", args.gateway_token])
    if args.raw:
        rx_cmd.append("--raw")

    tx_cmd = [
        str(tx_binary),
        "--lab-authorized",
        "--driver",
        tx_driver,
        "--device-id",
        tx_device,
        "--target-lap",
        target_lap,
        "--channels",
        args.page_channels,
        "--seconds",
        f"{float(args.timeout_s):.3f}",
        "--dwell-ms",
        f"{float(args.page_dwell_ms):.3f}",
        "--guard-us",
        f"{float(args.page_guard_us):.3f}",
        "--sample-rate-sps",
        str(int(args.tx_sample_rate_sps)),
        "--tx-gain-db",
        f"{float(args.tx_gain_db):.1f}",
        "--tx-vga-gain-db",
        f"{float(args.tx_vga_gain_db):.1f}",
        "--amplitude",
        f"{float(args.tx_amplitude):.3f}",
        "--fsk-polarity",
        args.fsk_polarity,
        "--edge-mode",
        args.edge_mode,
    ]
    if args.dry_run:
        tx_cmd.append("--dry-run")

    if not args.json:
        target = f"{target_uap + ':' if target_uap else ''}{target_lap}"
        print(
            f"lab page-stimulus target={target} rx={rx_device}/{rx_driver} tx={tx_device}/{tx_driver} "
            f"rx_center={float(args.center_mhz):.3f}MHz rx_bw={int(args.bandwidth_mhz)}MHz timeout={args.timeout_s:.1f}s",
            file=sys.stderr,
            flush=True,
        )

    rx_proc = subprocess.Popen(
        rx_cmd,
        cwd=str(PLUGIN_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    # Give the receiver a moment to open before the active stimulus starts.
    time.sleep(max(0.1, float(args.rx_settle_s)))
    tx_proc = subprocess.Popen(
        tx_cmd,
        cwd=str(PLUGIN_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    def terminate_children() -> None:
        stop.set()
        for proc in (tx_proc, rx_proc):
            if proc.poll() is None:
                proc.terminate()
        deadline = time.monotonic() + 1.5
        for proc in (tx_proc, rx_proc):
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if proc.poll() is None:
                proc.kill()

    def _stop(_signum: int, _frame: Any) -> None:
        terminate_children()

    previous_int = signal.signal(signal.SIGINT, _stop)
    previous_term = signal.signal(signal.SIGTERM, _stop)
    tx_lines: "queue.Queue[str]" = queue.Queue()
    rx_lines: "queue.Queue[str]" = queue.Queue()

    def tx_reader() -> None:
        assert tx_proc.stdout is not None
        for line in tx_proc.stdout:
            tx_lines.put(line.rstrip())

    def rx_reader() -> None:
        assert rx_proc.stdout is not None
        for line in rx_proc.stdout:
            rx_lines.put(line.rstrip())

    threading.Thread(target=tx_reader, daemon=True).start()
    threading.Thread(target=rx_reader, daemon=True).start()
    deadline = time.monotonic() + float(args.timeout_s) + float(args.rx_tail_s)
    recovered: dict[str, Any] | None = None
    rx_started = False
    rx_json_events = 0
    last_rx_notice = time.monotonic()
    try:
        while time.monotonic() < deadline and not stop.is_set():
            while True:
                try:
                    tx_line = tx_lines.get_nowait()
                except queue.Empty:
                    break
                if tx_line:
                    print(f"[tx] {tx_line}", file=sys.stderr, flush=True)
            try:
                text = rx_lines.get(timeout=0.1)
            except queue.Empty:
                now = time.monotonic()
                if now - last_rx_notice >= 2.0 and not rx_started and rx_json_events == 0:
                    last_rx_notice = now
                    rx_rc = rx_proc.poll()
                    tx_rc = tx_proc.poll()
                    if rx_rc is not None:
                        print(f"[rx] receiver exited rc={rx_rc}", file=sys.stderr, flush=True)
                        break
                    print(f"[rx] waiting for receiver telemetry tx_rc={tx_rc}", file=sys.stderr, flush=True)
                if rx_proc.poll() is not None and tx_proc.poll() is not None:
                    break
                continue
            text = text.strip()
            if not text:
                continue
            event = None
            if text.startswith("{"):
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    event = None
            if event is None:
                lower_text = text.lower()
                important_rx_line = (
                    "error" in lower_text
                    or "failed" in lower_text
                    or "traceback" in lower_text
                    or "exception" in lower_text
                    or "conflict" in lower_text
                    or "stream_id=" in lower_text
                    or text.startswith("using source=")
                )
                if args.raw or important_rx_line:
                    print(f"[rx] {text}", flush=True)
                if text.startswith("using source=") or "stream_id=" in lower_text:
                    rx_started = True
                continue
            event_type = str(event.get("type") or "")
            rx_json_events += 1
            if args.json:
                print(json.dumps({"type": "rx", "event": event}, separators=(",", ":")), flush=True)
            elif args.raw and event_type != "metrics":
                print(f"[rx] {event}", flush=True)
            if event_type == "page_access_seen":
                if args.show_page_seen and not args.json:
                    print(
                        f"page_seen lap={event.get('lap')} ch={event.get('channel')} "
                        f"rssi_dbfs={event.get('rssi_dbfs')} ts_us={event.get('ts_us')}",
                        flush=True,
                    )
                continue
            if event_type == "debug_bin_energy":
                rx_started = True
                if not args.json:
                    print(
                        f"rx_energy bin={event.get('bin')} rssi_dbfs={event.get('rssi_dbfs')}",
                        flush=True,
                    )
                continue
            if event_type == "fhs_reject":
                if args.raw and not args.json:
                    print(
                        f"fhs_reject reason={event.get('reason')} address={event.get('address')} "
                        f"verification={event.get('verification')} ch={event.get('channel')} "
                        f"rssi_dbfs={event.get('rssi_dbfs')} errors={event.get('errors', 0)}",
                        flush=True,
                    )
                continue
            if event_type != "passive_fhs_bdaddr":
                continue
            lap = str(event.get("lap") or "").upper()
            uap = str(event.get("uap") or "").upper()
            if lap == target_lap and (not target_uap or uap == target_uap):
                verification = str(event.get("verification") or "unchecked")
                if expected_bdaddr and verification != "match":
                    if args.json:
                        print(json.dumps({"type": "page_stimulus_result", "status": "fhs_mismatch", **event}, separators=(",", ":")), flush=True)
                    else:
                        print(
                            f"fhs_mismatch decoded={event.get('address')} expected={expected_bdaddr} "
                            f"nap={event.get('nap')} uap={uap} lap={lap} channel={event.get('channel')} "
                            f"rssi_dbfs={event.get('rssi_dbfs')}",
                            flush=True,
                        )
                    continue
                recovered = event
                status = "verified_fhs" if verification == "match" else "recovered"
                if args.json:
                    print(json.dumps({"type": "page_stimulus_result", "status": status, **event}, separators=(",", ":")), flush=True)
                else:
                    print(
                        f"{status} bd_addr={event.get('address')} nap={event.get('nap')} uap={uap} lap={lap} "
                        f"channel={event.get('channel')} rssi_dbfs={event.get('rssi_dbfs')} verification={verification}",
                        flush=True,
                    )
                break
        if recovered is None:
            if args.json:
                print(json.dumps({"type": "page_stimulus_result", "status": "timeout", "target_lap": target_lap, "target_uap": target_uap}, separators=(",", ":")), flush=True)
            else:
                print(f"page stimulus timed out target={target_uap + ':' if target_uap else ''}{target_lap}; no matching FHS decoded", flush=True)
            return 1
        return 0
    finally:
        terminate_children()
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)


def _add_common_gateway_capture_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--device-id", default="hackrf:0")
    parser.add_argument("--driver", default="hackrf", help="SoapySDR driver fallback if --device-id is generic")
    parser.add_argument("--gateway-base-url", default=None)
    parser.add_argument("--gateway-token", default=None)
    parser.add_argument("--center-mhz", type=float, default=2442.0)
    parser.add_argument("--bandwidth-mhz", type=int, default=20)
    parser.add_argument("--seconds", type=float, default=0.5, help="IQ buffer seconds per processing pass")
    parser.add_argument("--lna-gain-db", type=float, default=40.0)
    parser.add_argument("--vga-gain-db", type=float, default=40.0)
    parser.add_argument("--amp-gain-db", type=float, default=0.0)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--metrics", action="store_true")
    parser.add_argument("--raw", action="store_true", help="also print non-JSON native sniffer lines")
    parser.add_argument("--show-init-failed", action="store_true")
    parser.add_argument("--debug-target-lap", default="", help="emit page_access_seen when this LAP access code is observed")
    parser.add_argument("--no-page-detection", action="store_true", help="suppress page/inquiry access-code events for legacy BTC+BLE behavior")
    parser.add_argument("--log-passive-fhs-bdaddr", action="store_true", help="forward passive FHS BD_ADDR events from native decoder")
    parser.add_argument("--expected-bdaddr", default="", help="full Bluetooth address used to mark FHS events as match/mismatch")
    parser.add_argument("--debug-fhs-rejects", action="store_true", help="emit limited diagnostics for FHS-shaped packets rejected by validation")
    parser.add_argument("--fhs-max-fec-errors", type=int, default=0, help="allow this many uncorrectable FHS payload FEC blocks before rejecting")
    parser.add_argument("--debug-energy-bin", type=int, default=-1, help="emit debug_bin_energy for this 1 MHz bin")
    parser.add_argument(
        "--rf-input-mode",
        choices=("live", "capture", "playback"),
        default=os.getenv("RF_SENTINEL_RF_INPUT_MODE", "live"),
        help="global RF input mode: live SDR, capture SDR IQ, or replay captured IQ",
    )
    parser.add_argument(
        "--iq-capture-path",
        "--rf-capture-path",
        dest="iq_capture_path",
        type=Path,
        default=_path_from_arg(os.getenv("RF_SENTINEL_IQ_CAPTURE_PATH")),
        help="CS8 IQ recording path used in capture mode",
    )
    parser.add_argument(
        "--iq-playback-path",
        "--rf-playback-path",
        dest="iq_playback_path",
        type=Path,
        default=_path_from_arg(os.getenv("RF_SENTINEL_IQ_PLAYBACK_PATH")),
        help="CS8 IQ recording path used in playback mode",
    )
    parser.add_argument("--iq-capture-max-bytes", type=int, default=int(os.getenv("RF_SENTINEL_IQ_CAPTURE_MAX_BYTES", "0") or 0))
    parser.add_argument("--iq-chunk-bytes", type=int, default=int(os.getenv("RF_SENTINEL_IQ_CHUNK_BYTES", "131072") or 131_072))
    parser.add_argument("--iq-queue-chunks", type=int, default=int(os.getenv("RF_SENTINEL_IQ_QUEUE_CHUNKS", "64") or 64))
    parser.add_argument("--no-auto-build", action="store_true")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bluetooth_classic", description="Bluetooth Classic SDR scanner")
    subparsers = parser.add_subparsers(dest="command")
    listen = subparsers.add_parser("listen", help="run the Bluetooth Classic native sniffer")
    _add_common_gateway_capture_args(listen)
    listen.add_argument("--source", choices=("sdr", "gateway"), default="sdr", help="read directly from SDR or from an sdr-gateway stream")
    listen.add_argument("--events-path", type=Path, default=None)
    listen.add_argument("--csv", action="store_true")

    combined = subparsers.add_parser("combined", help="decode BLE and Bluetooth Classic from one sdr-gateway stream")
    _add_common_gateway_capture_args(combined)
    combined.set_defaults(source="gateway")
    combined.add_argument("--ble-channels", type=int, nargs="*", default=[37, 38, 39], choices=sorted(BLE_ADV_CHANNELS))
    combined.add_argument("--ble-channel-rate-sps", type=int, default=2_000_000)
    combined.add_argument("--debug-bursts", action="store_true")
    combined.add_argument("--max-events", type=int, default=0)

    page = subparsers.add_parser("page-stimulus", help="lab-only active page stimulus and passive FHS/NAP recovery")
    _add_common_gateway_capture_args(page)
    page.set_defaults(source="gateway")
    page.set_defaults(center_mhz=2442.0)
    page.set_defaults(bandwidth_mhz=60)
    page.add_argument("--source", choices=("sdr", "gateway"), default="gateway", help="receiver source")
    page.add_argument("--lab-authorized", action="store_true", help="required: confirms owned/authorized lab target")
    page.add_argument("--target-lap", required=True, help="target LAP, 6 hex chars")
    page.add_argument("--target-uap", default="", help="optional expected UAP, 2 hex chars; match any UAP if omitted")
    page.add_argument("--rx-device-id", default="", help="receiver SDR device; defaults to --device-id")
    page.add_argument("--tx-device-id", default="", help="transmitter SDR device; defaults to --device-id")
    page.add_argument("--tx-driver", default="", help="transmitter Soapy driver fallback")
    page.add_argument("--tx-sample-rate-sps", type=int, default=4_000_000)
    page.add_argument("--tx-gain-db", type=float, default=0.0)
    page.add_argument("--tx-vga-gain-db", type=float, default=20.0)
    page.add_argument("--tx-amplitude", type=float, default=0.35)
    page.add_argument("--fsk-polarity", choices=("normal", "inverted", "auto"), default="auto")
    page.add_argument("--edge-mode", choices=("hard", "shaped"), default="hard")
    page.add_argument("--page-channels", default="all", help="TX channels: all, 0-78, or comma/range list")
    page.add_argument("--page-dwell-ms", type=float, default=6.0)
    page.add_argument("--page-guard-us", type=float, default=80.0)
    page.add_argument("--timeout-s", type=float, default=10.0)
    page.add_argument("--rx-settle-s", type=float, default=0.4)
    page.add_argument("--rx-tail-s", type=float, default=1.0)
    page.add_argument("--dry-run", action="store_true")
    page.add_argument("--show-page-seen", action="store_true", default=True, help="print RX observations of the target LAP access code")

    build = subparsers.add_parser("build", help="build the native Bluetooth Classic sniffer")
    build.add_argument("--no-auto-build", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "listen":
        return _run_listen(args)
    if args.command == "combined":
        return _run_combined(args)
    if args.command == "page-stimulus":
        return _run_page_stimulus(args)
    if args.command == "build":
        print(_ensure_binary(auto_build=not args.no_auto_build))
        print(_ensure_binary(auto_build=not args.no_auto_build, binary=DEFAULT_GATEWAY_BINARY))
        print(_ensure_binary(auto_build=not args.no_auto_build, binary=DEFAULT_PAGE_BINARY))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
