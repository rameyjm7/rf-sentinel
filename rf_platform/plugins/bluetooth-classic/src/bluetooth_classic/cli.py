from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BINARY = PLUGIN_ROOT / "build" / "btcexplorer-sniffer"
DEFAULT_LOG = PLUGIN_ROOT / "btcexplorer-sniffer.log"

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


def _ensure_binary(auto_build: bool = True) -> Path:
    binary = DEFAULT_BINARY
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
    binary = _ensure_binary(auto_build=not args.no_auto_build)
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
    ]
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
            f"using device={args.device_id} driver={driver} center={float(args.center_mhz):.3f}MHz "
            f"bandwidth={int(args.bandwidth_mhz)}MHz lna={args.lna_gain_db} vga={args.vga_gain_db} amp={args.amp_gain_db}",
            file=sys.stderr,
            flush=True,
        )

    stop_requested = False

    def _stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True
        if proc.poll() is None:
            proc.terminate()

    proc = subprocess.Popen(cmd, cwd=str(PLUGIN_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
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
        if stop_requested and proc.poll() is None:
            proc.terminate()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bluetooth_classic", description="Bluetooth Classic SDR scanner")
    subparsers = parser.add_subparsers(dest="command")
    listen = subparsers.add_parser("listen", help="run the Bluetooth Classic native sniffer")
    listen.add_argument("--device-id", default="hackrf:0")
    listen.add_argument("--driver", default="hackrf", help="SoapySDR driver fallback if --device-id is generic")
    listen.add_argument("--center-mhz", type=float, default=2442.0)
    listen.add_argument("--bandwidth-mhz", type=int, default=20)
    listen.add_argument("--seconds", type=float, default=0.5, help="IQ buffer seconds per processing pass")
    listen.add_argument("--lna-gain-db", type=float, default=40.0)
    listen.add_argument("--vga-gain-db", type=float, default=40.0)
    listen.add_argument("--amp-gain-db", type=float, default=0.0)
    listen.add_argument("--log", type=Path, default=DEFAULT_LOG)
    listen.add_argument("--events-path", type=Path, default=None)
    listen.add_argument("--json", action="store_true")
    listen.add_argument("--csv", action="store_true")
    listen.add_argument("--metrics", action="store_true")
    listen.add_argument("--raw", action="store_true", help="also print non-JSON native sniffer lines")
    listen.add_argument("--show-init-failed", action="store_true")
    listen.add_argument("--no-auto-build", action="store_true")

    build = subparsers.add_parser("build", help="build the native Bluetooth Classic sniffer")
    build.add_argument("--no-auto-build", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "listen":
        return _run_listen(args)
    if args.command == "build":
        print(_ensure_binary(auto_build=not args.no_auto_build))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
