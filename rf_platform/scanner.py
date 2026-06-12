from __future__ import annotations

import argparse
import json
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
DEFAULT_JOB_DWELL_S = 8.0
DEFAULT_ZIGBEE_SLICE_S = 16.0
DEFAULT_ZIGBEE_DISCOVERY_SWEEP_S = 2.0
DEFAULT_ZIGBEE_ACTIVE_DWELL_S = 1.0
DEFAULT_ZIGBEE_FOLLOW_SAMPLE_RATE_SPS = 8_000_000


@dataclass(frozen=True)
class ScanJob:
    name: str
    protocol: str
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
    candidate = Path(sys.executable).parent / name
    if candidate.exists():
        return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    return str(candidate)


def _build_jobs(args: argparse.Namespace) -> tuple[list[ScanJob], list[ScanJob]]:
    continuous: list[ScanJob] = []
    cycled: list[ScanJob] = []

    def btc_job(name: str, device_id: str, bandwidth_mhz: int, lna_gain_db: float, vga_gain_db: float) -> ScanJob:
        return ScanJob(
            name=name,
            protocol="btc",
            dwell_s=args.btc_slice_s,
            command=[
                _bin("bluetooth_classic"),
                "listen",
                "--device-id",
                device_id,
                "--center-mhz",
                f"{args.btc_center_mhz:.3f}",
                "--bandwidth-mhz",
                str(int(bandwidth_mhz)),
                "--seconds",
                f"{args.btc_seconds:.3f}",
                "--lna-gain-db",
                str(lna_gain_db),
                "--vga-gain-db",
                str(vga_gain_db),
                "--amp-gain-db",
                str(args.btc_amp_gain_db),
                "--json",
            ],
        )

    def ble_job(name: str, device_id: str) -> ScanJob:
        return ScanJob(
            name=name,
            protocol="ble",
            dwell_s=args.ble_slice_s,
            command=[
                _bin("ble_scanner"),
                "iq-sweep",
                "--device-id",
                device_id,
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

    def zigbee_job(name: str, device_id: str) -> ScanJob:
        job = ScanJob(
            name=name,
            protocol="zigbee",
            dwell_s=args.zigbee_slice_s,
            command=[
                _bin("zigbee_802154"),
                "wideband-listen",
                "--device-id",
                device_id,
                "--json",
                "--max-frames",
                "0",
                "--discovery-mode",
                args.zigbee_discovery_mode,
                "--sample-rate-sps",
                str(args.zigbee_sample_rate_sps),
                "--discovery-sweep-s",
                str(args.zigbee_discovery_sweep_s),
                "--active-dwell-s",
                str(args.zigbee_active_dwell_s),
                "--rescan-interval-s",
                str(args.zigbee_rescan_interval_s),
                "--activity-ttl-s",
                str(args.zigbee_activity_ttl_s),
                "--max-active-decode-channels",
                str(args.zigbee_max_active_decode_channels),
                "--fft-active-threshold-db",
                str(args.zigbee_fft_active_threshold_db),
                "--fft-shape-threshold-db",
                str(args.zigbee_fft_shape_threshold_db),
                "--fft-min-power-db",
                str(args.zigbee_fft_min_power_db),
                "--fft-top-channels",
                str(args.zigbee_fft_top_channels),
                "--lna-gain-db",
                str(args.zigbee_lna_gain_db),
                "--vga-gain-db",
                str(args.zigbee_vga_gain_db),
                "--no-amp-enable",
                "--live-decode-workers",
                str(args.zigbee_live_decode_workers),
                "--live-decode-queue",
                str(args.zigbee_live_decode_queue),
            ],
        )
        if args.zigbee_follow_energy_only:
            job.command.append("--follow-energy-only")
        return job

    def tpms_job(name: str, device_id: str) -> ScanJob:
        return ScanJob(
            name=name,
            protocol="tpms",
            dwell_s=args.tpms_slice_s,
            command=[
                _bin("tpms_stack"),
                "listen",
                "--device-id",
                device_id,
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

    if bool(args.sweep_both_radios):
        radios = [
            ("radio_a", args.radio_a_device_id or args.btc_device_id, args.radio_a_btc_bandwidth_mhz, args.btc_lna_gain_db, args.btc_vga_gain_db),
            ("radio_b", args.radio_b_device_id or args.hop_device_id, args.radio_b_btc_bandwidth_mhz, args.ble_lna_gain_db, args.ble_vga_gain_db),
        ]
        for prefix, device_id, btc_bandwidth_mhz, btc_lna, btc_vga in radios:
            if not device_id:
                continue
            if not args.no_btc:
                cycled.append(btc_job(f"{prefix}:btc", device_id, btc_bandwidth_mhz, btc_lna, btc_vga))
            if not args.no_ble:
                cycled.append(ble_job(f"{prefix}:ble", device_id))
            if not args.no_zigbee:
                cycled.append(zigbee_job(f"{prefix}:zigbee", device_id))
            if not args.no_tpms:
                cycled.append(tpms_job(f"{prefix}:tpms", device_id))
        return continuous, cycled

    if not args.no_btc:
        job = btc_job("btc", args.btc_device_id, args.btc_bandwidth_mhz, args.btc_lna_gain_db, args.btc_vga_gain_db)
        continuous.append(ScanJob(name=job.name, protocol=job.protocol, continuous=True, dwell_s=0.0, command=job.command))

    if not args.no_ble:
        cycled.append(ble_job("ble", args.hop_device_id))

    if not args.no_zigbee:
        cycled.append(zigbee_job("zigbee", args.hop_device_id))

    if not args.no_tpms:
        cycled.append(tpms_job("tpms", args.hop_device_id))

    return continuous, cycled


def _configured_protocols(args: argparse.Namespace) -> set[str]:
    protocols = {"btc", "ble", "zigbee", "tpms"}
    if args.no_btc:
        protocols.discard("btc")
    if args.no_ble:
        protocols.discard("ble")
    if args.no_zigbee:
        protocols.discard("zigbee")
    if args.no_tpms:
        protocols.discard("tpms")
    return protocols


def _control_payload(args: argparse.Namespace) -> dict[str, object]:
    if not args.control_file:
        return {}
    try:
        with open(args.control_file, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[rf-sentinel] control_file ignored error={exc}", file=sys.stderr, flush=True)
        return {}
    return payload if isinstance(payload, dict) else {}


def _enabled_protocols(args: argparse.Namespace) -> set[str]:
    protocols = _configured_protocols(args)
    payload = _control_payload(args)
    requested = payload.get("protocols")
    if not isinstance(requested, list):
        return protocols
    live_protocols = {str(item).strip().lower() for item in requested}
    return protocols & live_protocols


def _zigbee_follow_channel(args: argparse.Namespace) -> int | None:
    payload = _control_payload(args)
    follow = payload.get("follow")
    if not isinstance(follow, dict):
        return None
    zigbee = follow.get("zigbee")
    if not isinstance(zigbee, dict):
        return None
    try:
        channel = int(zigbee.get("channel"))
    except (TypeError, ValueError):
        return None
    if 11 <= channel <= 26:
        return channel
    return None


def _command_value(command: list[str], flag: str, default: str = "") -> str:
    try:
        return command[command.index(flag) + 1]
    except (ValueError, IndexError):
        return default


def _materialize_job(args: argparse.Namespace, job: ScanJob) -> tuple[ScanJob, str]:
    if job.protocol != "zigbee":
        return job, ""
    follow_channel = _zigbee_follow_channel(args)
    if follow_channel is None:
        return job, ""
    device_id = _command_value(job.command, "--device-id", args.hop_device_id)
    followed = ScanJob(
        name=f"{job.name}:follow{follow_channel}",
        protocol=job.protocol,
        dwell_s=job.dwell_s,
        command=[
            _bin("zigbee_802154"),
            "listen",
            "--device-id",
            device_id,
            "--channel",
            str(follow_channel),
            "--json",
            "--max-frames",
            "0",
            "--sample-rate-sps",
            str(args.zigbee_follow_sample_rate_sps),
            "--lna-gain-db",
            str(args.zigbee_lna_gain_db),
            "--vga-gain-db",
            str(args.zigbee_vga_gain_db),
            "--no-amp-enable",
            "--no-debug-bursts",
            "--live-decode-workers",
            str(args.zigbee_live_decode_workers),
            "--live-decode-queue",
            str(args.zigbee_live_decode_queue),
        ],
    )
    return followed, str(follow_channel)


def _priority_protocol(args: argparse.Namespace) -> str:
    return "zigbee" if _zigbee_follow_channel(args) is not None else ""


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

    def cycle_jobs(name: str, jobs: list[ScanJob]) -> None:
        cycle_index = 0
        idle_notice = False
        while jobs and not supervisor.stop_requested.is_set():
            job = jobs[cycle_index % len(jobs)]
            cycle_index += 1
            enabled_protocols = _enabled_protocols(args)
            priority_protocol = _priority_protocol(args)
            if priority_protocol and job.protocol != priority_protocol:
                time.sleep(0.15)
                continue
            if job.protocol not in enabled_protocols:
                if not enabled_protocols and not idle_notice:
                    print(f"[rf-sentinel] hop group={name} paused; no protocols enabled", flush=True)
                    idle_notice = True
                time.sleep(0.15)
                continue
            idle_notice = False
            active_job, follow_marker = _materialize_job(args, job)
            print(
                f"[rf-sentinel] hop group={name} job={active_job.name} dwell_s={active_job.dwell_s:.1f}: {_format_command(active_job.command)}",
                flush=True,
            )
            proc = supervisor.start(active_job)
            deadline = time.time() + max(1.0, float(active_job.dwell_s))
            proc_exited = False
            while time.time() < deadline and not supervisor.stop_requested.is_set():
                if proc.poll() is not None:
                    if follow_marker:
                        break
                    if not proc_exited:
                        proc_exited = True
                        print(f"[rf-sentinel] job exited early job={active_job.name}; holding slot", flush=True)
                if job.protocol not in _enabled_protocols(args):
                    print(f"[rf-sentinel] stopping disabled job={active_job.name}", flush=True)
                    break
                if _priority_protocol(args) and job.protocol != _priority_protocol(args):
                    print(f"[rf-sentinel] stopping deprioritized job={active_job.name}", flush=True)
                    break
                if job.protocol == "zigbee" and str(_zigbee_follow_channel(args) or "") != follow_marker:
                    print(f"[rf-sentinel] retuning zigbee job={active_job.name}", flush=True)
                    break
                time.sleep(0.1)
            supervisor.stop(active_job.name)
            if args.once and cycle_index >= len(jobs):
                break

    try:
        if not cycled:
            while not supervisor.stop_requested.is_set():
                time.sleep(0.25)
            return 0

        if bool(args.sweep_both_radios):
            groups: dict[str, list[ScanJob]] = {}
            for job in cycled:
                group = job.name.split(":", 1)[0]
                groups.setdefault(group, []).append(job)
            threads = [
                threading.Thread(target=cycle_jobs, args=(group, jobs), daemon=True)
                for group, jobs in groups.items()
            ]
            for thread in threads:
                thread.start()
            while any(thread.is_alive() for thread in threads) and not supervisor.stop_requested.is_set():
                time.sleep(0.25)
            return 0

        cycle_index = 0
        idle_notice = False
        while not supervisor.stop_requested.is_set():
            job = cycled[cycle_index % len(cycled)]
            cycle_index += 1
            enabled_protocols = _enabled_protocols(args)
            priority_protocol = _priority_protocol(args)
            if priority_protocol and job.protocol != priority_protocol:
                time.sleep(0.15)
                continue
            if job.protocol not in enabled_protocols:
                if not enabled_protocols and not idle_notice:
                    print("[rf-sentinel] hop paused; no protocols enabled", flush=True)
                    idle_notice = True
                time.sleep(0.15)
                continue
            idle_notice = False
            active_job, follow_marker = _materialize_job(args, job)
            print(
                f"[rf-sentinel] hop job={active_job.name} dwell_s={active_job.dwell_s:.1f}: {_format_command(active_job.command)}",
                flush=True,
            )
            proc = supervisor.start(active_job)
            deadline = time.time() + max(1.0, float(active_job.dwell_s))
            proc_exited = False
            while time.time() < deadline and not supervisor.stop_requested.is_set():
                if proc.poll() is not None:
                    if follow_marker:
                        break
                    if not proc_exited:
                        proc_exited = True
                        print(f"[rf-sentinel] job exited early job={active_job.name}; holding slot", flush=True)
                if job.protocol not in _enabled_protocols(args):
                    print(f"[rf-sentinel] stopping disabled job={active_job.name}", flush=True)
                    break
                if _priority_protocol(args) and job.protocol != _priority_protocol(args):
                    print(f"[rf-sentinel] stopping deprioritized job={active_job.name}", flush=True)
                    break
                if job.protocol == "zigbee" and str(_zigbee_follow_channel(args) or "") != follow_marker:
                    print(f"[rf-sentinel] retuning zigbee job={active_job.name}", flush=True)
                    break
                time.sleep(0.1)
            supervisor.stop(active_job.name)
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
    parser.add_argument("--radio-a-device-id", default="", help="first SDR for --sweep-both-radios; defaults to --btc-device-id")
    parser.add_argument("--radio-b-device-id", default="", help="second SDR for --sweep-both-radios; defaults to --hop-device-id")
    parser.add_argument("--sweep-both-radios", action="store_true", help="time-slice enabled protocol scanners on both SDRs")
    parser.add_argument("--btc-center-mhz", type=float, default=DEFAULT_BTC_CENTER_MHZ)
    parser.add_argument("--btc-bandwidth-mhz", type=int, default=DEFAULT_BTC_BANDWIDTH_MHZ)
    parser.add_argument("--radio-a-btc-bandwidth-mhz", type=int, default=DEFAULT_BTC_BANDWIDTH_MHZ)
    parser.add_argument("--radio-b-btc-bandwidth-mhz", type=int, default=20)
    parser.add_argument("--btc-slice-s", type=float, default=DEFAULT_JOB_DWELL_S)
    parser.add_argument("--btc-seconds", type=float, default=0.5)
    parser.add_argument("--btc-lna-gain-db", type=float, default=40.0)
    parser.add_argument("--btc-vga-gain-db", type=float, default=40.0)
    parser.add_argument("--btc-amp-gain-db", type=float, default=0.0)

    parser.add_argument("--ble-slice-s", type=float, default=DEFAULT_JOB_DWELL_S)
    parser.add_argument("--ble-dwell-s", type=float, default=DEFAULT_BLE_DWELL_S)
    parser.add_argument("--ble-lna-gain-db", type=int, default=40)
    parser.add_argument("--ble-vga-gain-db", type=int, default=40)

    parser.add_argument("--zigbee-slice-s", type=float, default=DEFAULT_ZIGBEE_SLICE_S)
    parser.add_argument("--zigbee-discovery-mode", choices=("auto", "fft", "iq"), default="auto")
    parser.add_argument("--zigbee-sample-rate-sps", type=int, default=0, help="0 means use the hop SDR max rate")
    parser.add_argument("--zigbee-follow-sample-rate-sps", type=int, default=DEFAULT_ZIGBEE_FOLLOW_SAMPLE_RATE_SPS)
    parser.add_argument("--zigbee-discovery-sweep-s", type=float, default=DEFAULT_ZIGBEE_DISCOVERY_SWEEP_S)
    parser.add_argument("--zigbee-active-dwell-s", type=float, default=DEFAULT_ZIGBEE_ACTIVE_DWELL_S)
    parser.add_argument("--zigbee-rescan-interval-s", type=float, default=45.0)
    parser.add_argument("--zigbee-activity-ttl-s", type=float, default=90.0)
    parser.add_argument("--zigbee-max-active-decode-channels", type=int, default=1)
    parser.add_argument("--zigbee-fft-active-threshold-db", type=float, default=3.0)
    parser.add_argument("--zigbee-fft-shape-threshold-db", type=float, default=2.0)
    parser.add_argument("--zigbee-fft-min-power-db", type=float, default=-95.0)
    parser.add_argument("--zigbee-fft-top-channels", type=int, default=4)
    parser.add_argument("--zigbee-follow-energy-only", action="store_true")
    parser.add_argument("--zigbee-lna-gain-db", type=int, default=16)
    parser.add_argument("--zigbee-vga-gain-db", type=int, default=32)
    parser.add_argument("--zigbee-live-decode-workers", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    parser.add_argument("--zigbee-live-decode-queue", type=int, default=32)

    parser.add_argument("--tpms-slice-s", type=float, default=DEFAULT_JOB_DWELL_S)
    parser.add_argument("--tpms-lna-gain-db", type=int, default=16)
    parser.add_argument("--tpms-vga-gain-db", type=int, default=20)

    parser.add_argument("--no-btc", action="store_true")
    parser.add_argument("--no-ble", action="store_true")
    parser.add_argument("--no-zigbee", action="store_true")
    parser.add_argument("--no-tpms", action="store_true")
    parser.add_argument("--control-file", default="", help="optional JSON file with a live protocols list")
    parser.add_argument("--once", action="store_true", help="run one BLE/Zigbee/TPMS cycle and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
