from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_BTC_DEVICE = "bladerf:0"
DEFAULT_HOP_DEVICE = "hackrf:0"
DEFAULT_BTC_CENTER_MHZ = 2442.0
DEFAULT_BTC_BANDWIDTH_MHZ = 60
DEFAULT_BLE_DWELL_S = 0.5
DEFAULT_JOB_DWELL_S = 20.0


@dataclass(frozen=True)
class ScanJob:
    name: str
    command: list[str]
    dwell_s: float
    continuous: bool = False


class ProcessSupervisor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self.stop_requested = threading.Event()

    def start(self, job: ScanJob) -> subprocess.Popen[str]:
        proc = subprocess.Popen(
            job.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
            env=os.environ.copy(),
        )
        with self._lock:
            self._processes[job.name] = proc
        threading.Thread(target=self._pipe_output, args=(job.name, proc), daemon=True).start()
        return proc

    def stop(self, name: str, timeout_s: float = 4.0) -> None:
        with self._lock:
            proc = self._processes.pop(name, None)
        if proc is None or proc.poll() is not None:
            return
        _terminate_process_group(proc, timeout_s=timeout_s)

    def stop_all(self) -> None:
        self.stop_requested.set()
        with self._lock:
            names = list(self._processes)
        for name in names:
            self.stop(name)

    def _pipe_output(self, name: str, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            text = line.rstrip()
            if text:
                print(f"[{name}] {text}", flush=True)
        rc = proc.wait()
        with self._lock:
            if self._processes.get(name) is proc:
                self._processes.pop(name, None)
        if not self.stop_requested.is_set() and rc not in (0, -signal.SIGTERM):
            print(f"[{name}] exited rc={rc}", file=sys.stderr, flush=True)


def _terminate_process_group(proc: subprocess.Popen[str], timeout_s: float) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + timeout_s
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=1)


def _bin(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    candidate = Path(sys.executable).resolve().parent / name
    return str(candidate)


def _build_jobs(args: argparse.Namespace) -> tuple[list[ScanJob], list[ScanJob]]:
    continuous: list[ScanJob] = []
    cycled: list[ScanJob] = []

    if not args.no_btc:
        continuous.append(
            ScanJob(
                name="btc",
                continuous=True,
                dwell_s=0.0,
                command=[
                    _bin("bluetooth_classic"),
                    "listen",
                    "--device-id",
                    args.btc_device_id,
                    "--center-mhz",
                    f"{args.btc_center_mhz:.3f}",
                    "--bandwidth-mhz",
                    str(args.btc_bandwidth_mhz),
                    "--seconds",
                    f"{args.btc_seconds:.3f}",
                    "--lna-gain-db",
                    str(args.btc_lna_gain_db),
                    "--vga-gain-db",
                    str(args.btc_vga_gain_db),
                    "--amp-gain-db",
                    str(args.btc_amp_gain_db),
                    "--json",
                ],
            )
        )

    if not args.no_ble:
        cycled.append(
            ScanJob(
                name="ble",
                dwell_s=args.ble_slice_s,
                command=[
                    _bin("ble_scanner"),
                    "iq-sweep",
                    "--device-id",
                    args.hop_device_id,
                    "--replace-existing",
                    "--json",
                    "--dwell-s",
                    str(args.ble_dwell_s),
                    "--lna-gain-db",
                    str(args.ble_lna_gain_db),
                    "--vga-gain-db",
                    str(args.ble_vga_gain_db),
                ],
            )
        )

    if not args.no_zigbee:
        cycled.append(
            ScanJob(
                name="zigbee",
                dwell_s=args.zigbee_slice_s,
                command=[
                    _bin("zigbee_802154"),
                    "listen",
                    "--device-id",
                    args.hop_device_id,
                    "--json",
                    "--no-debug-bursts",
                    "--max-frames",
                    "0",
                ],
            )
        )

    if not args.no_tpms:
        cycled.append(
            ScanJob(
                name="tpms",
                dwell_s=args.tpms_slice_s,
                command=[
                    _bin("tpms_stack"),
                    "listen",
                    "--device-id",
                    args.hop_device_id,
                    "--auto-hop-known",
                    "--stream-duration-seconds",
                    str(max(1, int(args.tpms_slice_s))),
                    "--json",
                    "--lna-gain-db",
                    str(args.tpms_lna_gain_db),
                    "--vga-gain-db",
                    str(args.tpms_vga_gain_db),
                ],
            )
        )

    return continuous, cycled


def _run(args: argparse.Namespace) -> int:
    continuous, cycled = _build_jobs(args)
    supervisor = ProcessSupervisor()

    def stop(_signum: int, _frame: object) -> None:
        supervisor.stop_all()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    if not cycled and not continuous:
        print("No protocols enabled.", file=sys.stderr)
        return 2

    for job in continuous:
        print(f"[rf-sentinel] starting continuous {job.name}: {_format_command(job.command)}", flush=True)
        supervisor.start(job)

    try:
        if not cycled:
            while not supervisor.stop_requested.is_set():
                time.sleep(0.25)
            return 0

        cycle_index = 0
        while not supervisor.stop_requested.is_set():
            job = cycled[cycle_index % len(cycled)]
            cycle_index += 1
            print(
                f"[rf-sentinel] hop job={job.name} dwell_s={job.dwell_s:.1f}: {_format_command(job.command)}",
                flush=True,
            )
            proc = supervisor.start(job)
            deadline = time.time() + max(1.0, float(job.dwell_s))
            while time.time() < deadline and not supervisor.stop_requested.is_set():
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
            supervisor.stop(job.name)
            if args.once and cycle_index >= len(cycled):
                break
        return 0
    finally:
        supervisor.stop_all()


def _format_command(command: Iterable[str]) -> str:
    return " ".join(command)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rf_sentinel_scan",
        description="Run RF Sentinel protocol scanners together across two SDRs.",
    )
    parser.add_argument("--btc-device-id", default=DEFAULT_BTC_DEVICE)
    parser.add_argument("--hop-device-id", default=DEFAULT_HOP_DEVICE, help="SDR used for BLE/Zigbee/TPMS time-sliced hopping")
    parser.add_argument("--btc-center-mhz", type=float, default=DEFAULT_BTC_CENTER_MHZ)
    parser.add_argument("--btc-bandwidth-mhz", type=int, default=DEFAULT_BTC_BANDWIDTH_MHZ)
    parser.add_argument("--btc-seconds", type=float, default=0.5)
    parser.add_argument("--btc-lna-gain-db", type=float, default=40.0)
    parser.add_argument("--btc-vga-gain-db", type=float, default=40.0)
    parser.add_argument("--btc-amp-gain-db", type=float, default=0.0)

    parser.add_argument("--ble-slice-s", type=float, default=DEFAULT_JOB_DWELL_S)
    parser.add_argument("--ble-dwell-s", type=float, default=DEFAULT_BLE_DWELL_S)
    parser.add_argument("--ble-lna-gain-db", type=int, default=40)
    parser.add_argument("--ble-vga-gain-db", type=int, default=40)

    parser.add_argument("--zigbee-slice-s", type=float, default=DEFAULT_JOB_DWELL_S)

    parser.add_argument("--tpms-slice-s", type=float, default=DEFAULT_JOB_DWELL_S)
    parser.add_argument("--tpms-lna-gain-db", type=int, default=16)
    parser.add_argument("--tpms-vga-gain-db", type=int, default=20)

    parser.add_argument("--no-btc", action="store_true")
    parser.add_argument("--no-ble", action="store_true")
    parser.add_argument("--no-zigbee", action="store_true")
    parser.add_argument("--no-tpms", action="store_true")
    parser.add_argument("--once", action="store_true", help="run one BLE/Zigbee/TPMS cycle and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
