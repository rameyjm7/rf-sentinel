from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import requests
import websocket
from websocket import WebSocketConnectionClosedException

from bluetooth_lowenergy.detector import BLE_ADV_CHANNELS, WideBLEAdvertisingDetector


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BINARY = PLUGIN_ROOT / "build" / "btcexplorer-sniffer"
DEFAULT_GATEWAY_BINARY = PLUGIN_ROOT / "build" / "btcexplorer-sniffer-gateway"
DEFAULT_LOG_DIR = Path(os.getenv("RF_SENTINEL_LOG_DIR", "/var/log/rf_sentinel"))
DEFAULT_LOG = DEFAULT_LOG_DIR / "btcexplorer-sniffer.log"
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


def _build_inputs() -> list[Path]:
    paths = [PLUGIN_ROOT / "CMakeLists.txt"]
    paths.extend(sorted((PLUGIN_ROOT / "src").glob("*.cpp")))
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
    newest_input = max((path.stat().st_mtime for path in _build_inputs()), default=0.0)
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
                except json.JSONDecodeError:
                    event = None
            if event is None:
                if args.raw:
                    print(text, flush=True)
                continue
            event_type = str(event.get("type") or "")
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
                print(
                    f"metrics packets={event.get('packets_seen')} access={event.get('access_hits')} "
                    f"lap={event.get('lap_events')} resolved={event.get('resolved_events')} fhs={event.get('fhs_events')}",
                    flush=True,
                )
            elif event_type == "passive_fhs_bdaddr":
                print(
                    f"bdaddr address={event.get('address')} ch={event.get('channel')} "
                    f"rssi={event.get('rssi_dbfs')} access_lap={event.get('access_lap')}",
                    flush=True,
                )
            elif event_type in {"lap_initialized", "lap_resolved"} or event.get("lap"):
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


def _combined_ble_worker(args: argparse.Namespace, stream_id: str, stop: threading.Event, events: "queue.Queue[dict[str, Any]]") -> None:
    detector = WideBLEAdvertisingDetector(
        sample_rate_sps=int(args.bandwidth_mhz) * 1_000_000,
        center_freq_hz=int(round(float(args.center_mhz) * 1_000_000.0)),
        channels=list(args.ble_channels or sorted(BLE_ADV_CHANNELS)),
        channel_rate_sps=int(args.ble_channel_rate_sps),
    )
    visible = ",".join(str(lane["channel"]) for lane in detector.lanes) or "-"
    events.put({"protocol": "ble", "type": "status", "message": f"visible_ble_channels={visible}"})
    ws = None
    try:
        ws = websocket.create_connection(_ws_url_for_stream(args.gateway_base_url, stream_id, args.gateway_token), timeout=8)
        ws.settimeout(1.0)
        while not stop.is_set():
            try:
                chunk = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not isinstance(chunk, (bytes, bytearray)):
                continue
            _, decoded = detector.process_iq_i8(bytes(chunk))
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
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


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
                    event["protocol"] = "btc"
                    events.put(event)
                    continue
                except json.JSONDecodeError:
                    pass
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
        if event_type in {"lap_initialized", "lap_resolved"} or event.get("lap"):
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
    stream_id = _start_gateway_stream(args)
    stop = threading.Event()
    events: "queue.Queue[dict[str, Any]]" = queue.Queue()
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

    print(
        f"using source=gateway-combined stream_id={stream_id} device={args.device_id} driver={driver} "
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
            target=_bridge_gateway_to_stdin,
            kwargs={
                "base_url": args.gateway_base_url,
                "token": args.gateway_token,
                "stream_id": stream_id,
                "stdin": proc.stdin.buffer,
                "stop": stop,
                "raw": bool(args.raw),
                "label": "btc",
            },
            daemon=True,
        ),
        threading.Thread(target=_combined_btc_stdout_worker, args=(proc, args, stop, events), daemon=True),
        threading.Thread(target=_combined_ble_worker, args=(args, stream_id, stop, events), daemon=True),
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
        return proc.poll() or 0
    finally:
        stop.set()
        if proc.poll() is None:
            proc.terminate()
        _stop_gateway_stream(args.gateway_base_url, args.gateway_token, stream_id)
        for thread in threads:
            thread.join(timeout=1.0)
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
    if args.command == "build":
        print(_ensure_binary(auto_build=not args.no_auto_build))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
