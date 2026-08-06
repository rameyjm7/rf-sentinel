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
DEFAULT_JOB_DWELL_S = 30.0
DEFAULT_ZIGBEE_SLICE_S = 30.0
DEFAULT_ZIGBEE_DISCOVERY_SWEEP_S = 2.0
DEFAULT_ZIGBEE_ACTIVE_DWELL_S = 1.0
DEFAULT_ZIGBEE_FOLLOW_SAMPLE_RATE_SPS = 8_000_000
DEFAULT_WIFI_INTERFACE = "wlan0"
DEFAULT_FM_DEVICE = "sdrplay:0"
DEFAULT_FM_SAMPLE_RATE_SPS = 2_400_000
DEFAULT_FM_INTERVAL_S = 60.0
DEFAULT_LFMF_INTERVAL_S = 120.0
DEFAULT_CELLULAR_CENTER_FREQ_HZ = 751_000_000
DEFAULT_CELLULAR_SAMPLE_RATE_SPS = 20_000_000
DEFAULT_CELLULAR_SLICE_S = 12.0
DEFAULT_WALKIE_CENTER_FREQ_HZ = 462_500_000
DEFAULT_WALKIE_SAMPLE_RATE_SPS = 1_000_000
DEFAULT_IQ_DIR = Path(os.getenv("RF_SENTINEL_IQ_DIR", "/var/log/rf_sentinel/iq"))
DEFAULT_BLUETOOTH_IQ_CAPTURE = DEFAULT_IQ_DIR / "bluetooth_combined.cs8"
DEFAULT_BTC_PAGE_SCAN_INTERVAL_S = 60.0
DEFAULT_BTC_PAGE_SCAN_ACTIVE_S = 12.0


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
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            job.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
            env=env,
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

    def is_running(self, name: str) -> bool:
        with self._lock:
            proc = self._processes.get(name)
        return bool(proc is not None and proc.poll() is None)

    def names(self) -> list[str]:
        with self._lock:
            return list(self._processes)

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
    candidates = [
        candidate,
        Path(__file__).resolve().parents[2] / ".venv" / "bin" / name,
    ]
    for item in candidates:
        if item.exists():
            return str(item)
    found = shutil.which(name)
    if found:
        return found
    if name == "wifi_scanner":
        gateway_candidate = Path(__file__).resolve().parents[2] / "sdr-gateway" / ".venv" / "bin" / name
        if gateway_candidate.exists():
            return str(gateway_candidate)
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

    def bluetooth_combined_job(name: str, device_id: str, bandwidth_mhz: int) -> ScanJob:
        command = [
            _bin("bluetooth_scanner"),
            "--device-id",
            device_id,
            "--center-mhz",
            f"{args.btc_center_mhz:.3f}",
            "--bandwidth-mhz",
            str(int(bandwidth_mhz)),
            "--lna-gain-db",
            str(args.btc_lna_gain_db),
            "--vga-gain-db",
            str(args.btc_vga_gain_db),
            "--amp-gain-db",
            str(args.btc_amp_gain_db),
            "--json",
            "--metrics",
            "--rf-input-mode",
            args.rf_input_mode,
        ]
        iq_source = os.getenv("SDR_BACKEND", "gateway").strip().lower()
        if iq_source == "rfiq":
            command.extend(["--iq-source", "rfiq"])
            rfiq_socket = os.getenv("SDR_RFIQ_SOCKET", "/tmp/rfiq0.sock")
            rfiq_control_socket = os.getenv("SDR_RFIQ_CONTROL_SOCKET", "/tmp/rfiq0-control.sock")
            command.extend(["--rfiq-socket", rfiq_socket, "--rfiq-control-socket", rfiq_control_socket])
        if args.iq_capture_path:
            command.extend(["--iq-capture-path", args.iq_capture_path])
        if args.iq_playback_path:
            command.extend(["--iq-playback-path", args.iq_playback_path])
        if args.iq_capture_max_bytes:
            command.extend(["--iq-capture-max-bytes", str(args.iq_capture_max_bytes)])
        if args.btc_log_passive_fhs_bdaddr:
            command.append("--log-passive-fhs-bdaddr")
        if args.btc_band_hop:
            command.extend(["--band-hop", "--band-hop-dwell-s", str(args.btc_band_hop_dwell_s)])
        if args.btc_expected_bdaddr:
            command.extend(["--expected-bdaddr", args.btc_expected_bdaddr])
        if args.no_page_detection:
            command.append("--no-page-detection")
        return ScanJob(
            name=name,
            protocol="btle+btc",
            continuous=True,
            dwell_s=0.0,
            command=command,
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
                "--require-fcs",
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

    def walkie_job(name: str, device_id: str) -> ScanJob:
        return ScanJob(
            name=name,
            protocol="walkie",
            dwell_s=args.walkie_slice_s,
            command=[
                _bin("walkie_talkie_scanner"),
                "scan",
                "--device-id",
                device_id,
                "--center-freq-hz",
                str(args.walkie_center_freq_hz),
                "--sample-rate-sps",
                str(args.walkie_sample_rate_sps),
                "--baseband-filter-hz",
                str(args.walkie_baseband_filter_hz),
                "--scan-window-s",
                str(args.walkie_slice_s),
                "--lna-gain-db",
                str(args.walkie_lna_gain_db),
                "--vga-gain-db",
                str(args.walkie_vga_gain_db),
                "--json",
            ],
        )

    def wifi_job(name: str) -> ScanJob:
        command = [
            _bin("wifi_scanner"),
            "--interface",
            args.wifi_interface,
            "--backend",
            "gateway",
            "--command",
            args.wifi_command,
            "--channels",
            args.wifi_channels,
            "--hop-interval-s",
            str(args.wifi_hop_interval_s),
            "--event-limit",
            str(args.wifi_event_limit),
            "--json",
            "--replace-existing",
        ]
        if args.wifi_active_scan:
            command.extend(["--active-scan", "--active-scan-interval-s", str(args.wifi_active_scan_interval_s)])
        if not args.wifi_set_monitor:
            command.append("--no-set-monitor")
        if not args.wifi_set_channel:
            command.append("--no-set-channel")
        return ScanJob(name=name, protocol="wifi", dwell_s=args.wifi_slice_s, command=command)

    def fm_job(name: str, device_id: str) -> ScanJob:
        is_hackrf = str(device_id or "").strip().lower().startswith("hackrf:")
        is_bladerf = str(device_id or "").strip().lower().startswith("bladerf:")
        is_rtlsdr = str(device_id or "").strip().lower().startswith("rtlsdr:")
        discovery_mode = "wideband" if is_bladerf else str(args.fm_discovery_mode)
        if is_bladerf or is_hackrf:
            sample_rate_sps = max(int(args.fm_sample_rate_sps), 20_000_000)
        elif is_rtlsdr:
            sample_rate_sps = min(int(args.fm_sample_rate_sps), DEFAULT_FM_SAMPLE_RATE_SPS)
        else:
            sample_rate_sps = int(args.fm_sample_rate_sps)
        sweep_prominence_db = 0.0 if is_hackrf else float(args.fm_sweep_prominence_db)
        station_merge_hz = 0 if is_hackrf else int(args.fm_station_merge_hz)
        active_threshold_db = 4.0 if is_hackrf else float(args.fm_active_threshold_db)
        min_power_dbfs = -90.0 if is_hackrf else float(args.fm_min_power_dbfs)
        max_stations = max(int(args.fm_max_stations), 50) if is_hackrf else int(args.fm_max_stations)
        command = [
            _bin("fm_broadcast"),
            "scan",
            "--device-id",
            device_id,
            "--json",
            "--tuner-offset-hz",
            str(args.fm_tuner_offset_hz),
            "--discovery-mode",
            discovery_mode,
            "--sample-rate-sps",
            str(sample_rate_sps),
            "--sweep-bin-width-hz",
            str(args.fm_sweep_bin_width_hz),
            "--sweep-prominence-db",
            str(sweep_prominence_db),
            "--station-merge-hz",
            str(station_merge_hz),
            "--discovery-dwell-s",
            str(args.fm_discovery_dwell_s),
            "--decode-dwell-s",
            str(args.fm_decode_dwell_s),
            "--active-threshold-db",
            str(active_threshold_db),
            "--min-power-dbfs",
            str(min_power_dbfs),
            "--max-stations",
            str(max_stations),
            "--lna-gain-db",
            str(args.fm_lna_gain_db),
            "--vga-gain-db",
            str(args.fm_vga_gain_db),
        ]
        if args.fm_debug:
            command.append("--debug")
        if args.fm_skip_decode:
            command.append("--skip-decode")
        return ScanJob(
            name=name,
            protocol="fm",
            dwell_s=args.fm_slice_s,
            command=command,
        )

    def lfmf_job(name: str, device_id: str) -> ScanJob:
        command = [
            _bin("lowfreq-scan"),
            "scan",
            "--device-id",
            device_id,
            "--band",
            args.lfmf_band,
            "--step-khz",
            str(args.lfmf_step_khz),
            "--sample-rate-sps",
            str(args.lfmf_sample_rate_sps),
            "--bandwidth-hz",
            str(args.lfmf_bandwidth_hz),
            "--dwell-s",
            str(args.lfmf_dwell_s),
            "--active-threshold-db",
            str(args.lfmf_active_threshold_db),
            "--top",
            str(args.lfmf_top_signals),
            "--jsonl",
            "--yes",
            "--replace-existing",
        ]
        if args.lfmf_wideband:
            command.extend(
                [
                    "--wideband",
                    "--wideband-center-khz",
                    str(args.lfmf_wideband_center_khz),
                    "--wideband-sample-rate-sps",
                    str(args.lfmf_wideband_sample_rate_sps),
                    "--wideband-bandwidth-hz",
                    str(args.lfmf_wideband_bandwidth_hz),
                    "--active-only",
                    "--confirm",
                ]
            )
        if args.lfmf_serial:
            command.extend(["--serial", args.lfmf_serial])
        return ScanJob(name=name, protocol="lfmf", dwell_s=args.lfmf_slice_s, command=command)

    if not args.no_wifi:
        # WiFi is not SDR-backed, so start it first instead of making it wait
        # behind long RF dwell jobs.
        cycled.append(wifi_job("wifi"))

    def cellular_job(name: str, device_id: str) -> ScanJob:
        return ScanJob(
            name=name,
            protocol="cellular",
            dwell_s=args.cellular_slice_s,
            command=[
                _bin("cellular_scanner"),
                "scan",
                "--device-id",
                device_id,
                "--center-freq-hz",
                str(args.cellular_center_freq_hz),
                "--target-freq-hz",
                str(args.cellular_target_freq_hz),
                "--sample-rate-sps",
                str(args.cellular_sample_rate_sps),
                "--bandwidth-hz",
                str(args.cellular_bandwidth_hz),
                "--dwell-s",
                str(args.cellular_dwell_s),
                "--lna-gain-db",
                str(args.cellular_lna_gain_db),
                "--vga-gain-db",
                str(args.cellular_vga_gain_db),
                "--active-threshold-db",
                str(args.cellular_active_threshold_db),
                "--candidate-threshold-db",
                str(args.cellular_candidate_threshold_db),
                "--target-threshold-db",
                str(args.cellular_target_threshold_db),
                "--top",
                str(args.cellular_top),
                "--replace-existing",
                "--jsonl",
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
            if prefix == "radio_a" and not args.no_btc:
                cycled.append(btc_job(f"{prefix}:btc", device_id, btc_bandwidth_mhz, btc_lna, btc_vga))
            if prefix == "radio_a" and not args.no_ble:
                cycled.append(ble_job(f"{prefix}:ble", device_id))
            if prefix == "radio_b" and not args.no_zigbee:
                cycled.append(zigbee_job(f"{prefix}:zigbee", device_id))
            if prefix == "radio_b" and not args.no_tpms:
                cycled.append(tpms_job(f"{prefix}:tpms", device_id))
            if not args.no_lfmf and _is_sdrplay_device_id(device_id):
                cycled.append(lfmf_job(f"{prefix}:lfmf", device_id))
            if prefix == "radio_b" and not args.no_cellular:
                cycled.append(cellular_job(f"{prefix}:cellular", device_id))
        if not args.no_fm:
            fm_device_id = _pick_fm_device_id(args)
            if fm_device_id:
                cycled.append(fm_job("radio_b:fm", fm_device_id))
            else:
                print("[rf-sentinel] FM disabled; no allowed FM-capable SDR", flush=True)
        return continuous, cycled

    combined_bluetooth = not args.no_btc and not args.no_ble

    if combined_bluetooth:
        continuous.append(bluetooth_combined_job("btc", args.btc_device_id, args.btc_bandwidth_mhz))
    elif not args.no_btc:
        job = btc_job("btc", args.btc_device_id, args.btc_bandwidth_mhz, args.btc_lna_gain_db, args.btc_vga_gain_db)
        continuous.append(ScanJob(name=job.name, protocol=job.protocol, continuous=True, dwell_s=0.0, command=job.command))

    if not args.no_ble and not combined_bluetooth:
        cycled.append(ble_job("ble", args.hop_device_id))

    if not args.no_zigbee:
        cycled.append(zigbee_job("zigbee", args.hop_device_id))

    if not args.no_tpms:
        cycled.append(tpms_job("tpms", args.hop_device_id))

    if not args.no_walkie:
        walkie_device_id = str(args.walkie_device_id or args.hop_device_id).strip() or args.hop_device_id
        cycled.append(walkie_job("walkie", walkie_device_id))

    if not args.no_fm:
        fm_device_id = _pick_fm_device_id(args)
        if fm_device_id:
            cycled.append(fm_job("fm", fm_device_id))
        else:
            print("[rf-sentinel] FM disabled; no allowed FM-capable SDR", flush=True)

    if not args.no_lfmf:
        lfmf_device_id = _pick_lfmf_device_id(args)
        if lfmf_device_id:
            cycled.append(lfmf_job("lfmf", lfmf_device_id))
        else:
            print("[rf-sentinel] VLF/LF/MF disabled; no allowed SDRplay RSP2 device", flush=True)

    if not args.no_cellular:
        cycled.append(cellular_job("cellular", args.hop_device_id))

    return continuous, cycled


def _configured_protocols(args: argparse.Namespace) -> set[str]:
    protocols = {"btc", "ble", "zigbee", "tpms", "walkie", "wifi", "fm", "lfmf", "cellular"}
    if args.no_btc:
        protocols.discard("btc")
    if args.no_ble:
        protocols.discard("ble")
    if args.no_zigbee:
        protocols.discard("zigbee")
    if args.no_tpms:
        protocols.discard("tpms")
    if args.no_walkie:
        protocols.discard("walkie")
    if args.no_wifi:
        protocols.discard("wifi")
    if args.no_fm:
        protocols.discard("fm")
    if args.no_lfmf:
        protocols.discard("lfmf")
    if args.no_cellular:
        protocols.discard("cellular")
    return protocols


def _is_sdrplay_device_id(device_id: str) -> bool:
    text = str(device_id or "").strip().lower()
    return text.startswith("sdrplay:")


def _is_rtlsdr_device_id(device_id: str) -> bool:
    text = str(device_id or "").strip().lower()
    return text.startswith("rtlsdr:")


def _pick_fm_device_id(args: argparse.Namespace) -> str:
    configured = _control_protocol_device(args, "fm")
    if configured:
        return configured
    preferred = str(getattr(args, "fm_device_id", "") or "").strip()
    allowed = [str(item).strip() for item in getattr(args, "allowed_device_id", []) if str(item).strip()]
    if allowed:
        if preferred and preferred in allowed:
            return preferred
        for device_id in allowed:
            if _is_sdrplay_device_id(device_id):
                return device_id
        for device_id in allowed:
            if _is_rtlsdr_device_id(device_id):
                return device_id
        return ""
    for device_id in (
        preferred,
        getattr(args, "lfmf_device_id", ""),
        getattr(args, "hop_device_id", ""),
        getattr(args, "radio_b_device_id", ""),
        getattr(args, "btc_device_id", ""),
    ):
        if str(device_id or "").strip():
            return str(device_id).strip()
    return ""


def _pick_lfmf_device_id(args: argparse.Namespace) -> str:
    allowed = [str(item).strip() for item in getattr(args, "allowed_device_id", []) if str(item).strip()]
    for device_id in allowed:
        if _is_sdrplay_device_id(device_id):
            return device_id
    for device_id in (getattr(args, "lfmf_device_id", ""), getattr(args, "hop_device_id", ""), getattr(args, "radio_b_device_id", ""), getattr(args, "btc_device_id", "")):
        if _is_sdrplay_device_id(str(device_id)):
            return str(device_id)
    return ""


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


def _control_protocol_device(args: argparse.Namespace, protocol: str) -> str:
    payload = _control_payload(args)
    devices = payload.get("protocol_devices")
    if not isinstance(devices, dict):
        return ""
    protocol_key = str(protocol or "").strip().lower()
    selected = str(devices.get(protocol_key) or "").strip()
    if selected:
        return selected
    if protocol_key == "walkie":
        return str(devices.get("tpms") or "").strip()
    return ""


def _control_wifi_channels(args: argparse.Namespace) -> str:
    payload = _control_payload(args)
    channels = payload.get("wifi_channels")
    if not isinstance(channels, list):
        return ""
    clean: list[str] = []
    for channel in channels:
        try:
            channel_int = int(channel)
        except (TypeError, ValueError):
            continue
        if channel_int > 0 and str(channel_int) not in clean:
            clean.append(str(channel_int))
    return ",".join(clean)


def _replace_command_value(command: list[str], flag: str, value: str) -> list[str]:
    updated = list(command)
    try:
        updated[updated.index(flag) + 1] = value
    except (ValueError, IndexError):
        updated.extend([flag, value])
    return updated


def _fm_apply_device_defaults(command: list[str], device_id: str) -> list[str]:
    updated = list(command)
    lowered = str(device_id or "").lower()
    if lowered.startswith("bladerf:"):
        updated = _replace_command_value(updated, "--discovery-mode", "wideband")
        updated = _replace_command_value(updated, "--sample-rate-sps", "20000000")
    elif lowered.startswith("hackrf:"):
        updated = _replace_command_value(updated, "--discovery-mode", "sweep")
        updated = _replace_command_value(updated, "--sample-rate-sps", "20000000")
        updated = _replace_command_value(updated, "--sweep-prominence-db", "0.0")
        updated = _replace_command_value(updated, "--station-merge-hz", "0")
        updated = _replace_command_value(updated, "--active-threshold-db", "4.0")
        updated = _replace_command_value(updated, "--min-power-dbfs", "-90.0")
        updated = _replace_command_value(updated, "--max-stations", "50")
    elif lowered.startswith("rtlsdr:"):
        updated = _replace_command_value(updated, "--sample-rate-sps", str(DEFAULT_FM_SAMPLE_RATE_SPS))
    return updated


def _apply_control_overrides(args: argparse.Namespace, job: ScanJob) -> ScanJob:
    command = list(job.command)
    changed = False
    if job.protocol in {"zigbee", "tpms", "walkie", "fm", "cellular"}:
        device_id = _control_protocol_device(args, job.protocol)
        if device_id:
            command = _replace_command_value(command, "--device-id", device_id)
            if job.protocol == "fm":
                command = _fm_apply_device_defaults(command, device_id)
            changed = True
    if job.protocol == "wifi":
        channels = _control_wifi_channels(args)
        if channels:
            command = _replace_command_value(command, "--channels", channels)
            changed = True
    if not changed:
        return job
    return ScanJob(name=job.name, protocol=job.protocol, command=command, dwell_s=job.dwell_s, continuous=job.continuous)


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


def _zigbee_follow_device_id(args: argparse.Namespace) -> str:
    payload = _control_payload(args)
    follow = payload.get("follow")
    if not isinstance(follow, dict):
        return ""
    zigbee = follow.get("zigbee")
    if not isinstance(zigbee, dict):
        return ""
    return str(zigbee.get("device_id") or "").strip()


def _command_value(command: list[str], flag: str, default: str = "") -> str:
    try:
        return command[command.index(flag) + 1]
    except (ValueError, IndexError):
        return default


def _follow_device_id(args: argparse.Namespace) -> str:
    return _zigbee_follow_device_id(args) or str(args.radio_b_device_id or args.hop_device_id or "").strip()


def _job_device_id(args: argparse.Namespace, job: ScanJob) -> str:
    return _command_value(job.command, "--device-id", args.hop_device_id).strip()


def _is_follow_device_job(args: argparse.Namespace, job: ScanJob) -> bool:
    follow_device = _follow_device_id(args)
    return bool(follow_device) and _job_device_id(args, job) == follow_device


def _zigbee_follow_job(args: argparse.Namespace, name: str, device_id: str, follow_channel: int, dwell_s: float) -> ScanJob:
    return ScanJob(
        name=name,
        protocol="zigbee",
        dwell_s=dwell_s,
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
            "--require-fcs",
            "--live-decode-workers",
            str(args.zigbee_live_decode_workers),
            "--live-decode-queue",
            str(args.zigbee_live_decode_queue),
        ],
    )


def _materialize_job(args: argparse.Namespace, job: ScanJob) -> tuple[ScanJob, str]:
    if job.protocol != "zigbee":
        return job, ""
    follow_channel = _zigbee_follow_channel(args)
    if follow_channel is None:
        return job, ""
    follow_device_id = _zigbee_follow_device_id(args)
    if follow_device_id:
        return job, ""
    elif _is_follow_device_job(args, job):
        device_id = _command_value(job.command, "--device-id", args.hop_device_id)
    else:
        return job, ""
    followed = _zigbee_follow_job(
        name=f"{job.name}:follow{follow_channel}",
        device_id=device_id,
        follow_channel=follow_channel,
        dwell_s=job.dwell_s,
    )
    return followed, str(follow_channel)


def _priority_protocol(args: argparse.Namespace, job: ScanJob) -> str:
    if "zigbee" not in _enabled_protocols(args):
        return ""
    return "zigbee" if _zigbee_follow_channel(args) is not None and _is_follow_device_job(args, job) else ""


def _job_interval_s(args: argparse.Namespace, job: ScanJob) -> float:
    if job.protocol == "fm":
        return max(1.0, float(args.fm_interval_s))
    if job.protocol == "lfmf":
        return max(1.0, float(args.lfmf_interval_s))
    if job.protocol == "cellular":
        return max(1.0, float(args.cellular_interval_s))
    return 0.0


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

    page_scan_thread: threading.Thread | None = None
    if args.btc_periodic_page_scan or args.control_file:
        page_scan_thread = threading.Thread(target=_periodic_btc_page_scan_loop, args=(args, supervisor.stop_requested), daemon=True)
        page_scan_thread.start()

    for job in continuous:
        print(f"[rf-sentinel] starting continuous {job.name}: {_format_command(job.command)}", flush=True)
        supervisor.start(job)

    def sync_zigbee_follow_sidecar(group_name: str) -> None:
        if group_name not in {"main", "radio_b"}:
            return
        prefix = f"{group_name}:zigbee-follow:"
        follow_channel = _zigbee_follow_channel(args)
        follow_device = _zigbee_follow_device_id(args)
        enabled = "zigbee" in _enabled_protocols(args)
        wanted_name = ""
        if enabled and follow_channel is not None and follow_device:
            safe_device = follow_device.replace(":", "_").replace("/", "_")
            wanted_name = f"{prefix}{safe_device}:ch{follow_channel}"
        for name in supervisor.names():
            if name.startswith(prefix) and name != wanted_name:
                print(f"[rf-sentinel] stopping zigbee follow sidecar job={name}", flush=True)
                supervisor.stop(name)
        if not wanted_name or supervisor.is_running(wanted_name):
            return
        job = _zigbee_follow_job(
            args,
            name=wanted_name,
            device_id=follow_device,
            follow_channel=int(follow_channel),
            dwell_s=86_400.0,
        )
        print(
            f"[rf-sentinel] sidecar group={group_name} job={job.name} dwell_s=continuous: {_format_command(job.command)}",
            flush=True,
        )
        supervisor.start(job)

    def cycle_jobs(name: str, jobs: list[ScanJob]) -> None:
        cycle_index = 0
        idle_notice = False
        next_run_at: dict[str, float] = {}
        while jobs and not supervisor.stop_requested.is_set():
            sync_zigbee_follow_sidecar(name)
            job = jobs[cycle_index % len(jobs)]
            cycle_index += 1
            job = _apply_control_overrides(args, job)
            enabled_protocols = _enabled_protocols(args)
            priority_protocol = _priority_protocol(args, job)
            if priority_protocol and job.protocol != priority_protocol:
                time.sleep(0.15)
                continue
            if job.protocol not in enabled_protocols:
                if not enabled_protocols and not idle_notice:
                    print(f"[rf-sentinel] hop group={name} paused; no protocols enabled", flush=True)
                    idle_notice = True
                time.sleep(0.15)
                continue
            interval_s = _job_interval_s(args, job)
            if interval_s > 0.0 and time.time() < next_run_at.get(job.name, 0.0):
                time.sleep(0.05)
                continue
            idle_notice = False
            active_job, follow_marker = _materialize_job(args, job)
            print(
                f"[rf-sentinel] hop group={name} job={active_job.name} dwell_s={active_job.dwell_s:.1f}: {_format_command(active_job.command)}",
                flush=True,
            )
            if interval_s > 0.0:
                next_run_at[job.name] = time.time() + interval_s
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
                priority_protocol = _priority_protocol(args, job)
                if priority_protocol and job.protocol != priority_protocol:
                    print(f"[rf-sentinel] stopping deprioritized job={active_job.name}", flush=True)
                    break
                if follow_marker and job.protocol == "zigbee" and str(_zigbee_follow_channel(args) or "") != follow_marker:
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
        next_run_at: dict[str, float] = {}
        while not supervisor.stop_requested.is_set():
            sync_zigbee_follow_sidecar("main")
            job = cycled[cycle_index % len(cycled)]
            cycle_index += 1
            job = _apply_control_overrides(args, job)
            enabled_protocols = _enabled_protocols(args)
            priority_protocol = _priority_protocol(args, job)
            if priority_protocol and job.protocol != priority_protocol:
                time.sleep(0.15)
                continue
            if job.protocol not in enabled_protocols:
                if not enabled_protocols and not idle_notice:
                    print("[rf-sentinel] hop paused; no protocols enabled", flush=True)
                    idle_notice = True
                time.sleep(0.15)
                continue
            interval_s = _job_interval_s(args, job)
            if interval_s > 0.0 and time.time() < next_run_at.get(job.name, 0.0):
                time.sleep(0.05)
                continue
            idle_notice = False
            active_job, follow_marker = _materialize_job(args, job)
            print(
                f"[rf-sentinel] hop job={active_job.name} dwell_s={active_job.dwell_s:.1f}: {_format_command(active_job.command)}",
                flush=True,
            )
            if interval_s > 0.0:
                next_run_at[job.name] = time.time() + interval_s
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
                priority_protocol = _priority_protocol(args, job)
                if priority_protocol and job.protocol != priority_protocol:
                    print(f"[rf-sentinel] stopping deprioritized job={active_job.name}", flush=True)
                    break
                if follow_marker and job.protocol == "zigbee" and str(_zigbee_follow_channel(args) or "") != follow_marker:
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


def _control_bluetooth_classic(args: argparse.Namespace) -> dict[str, bool]:
    payload = _control_payload(args)
    value = payload.get("bluetooth_classic")
    value = value if isinstance(value, dict) else {}
    return {
        "log_passive_fhs_bdaddr": bool(value.get("log_passive_fhs_bdaddr", args.btc_log_passive_fhs_bdaddr)),
        "periodic_page_scan": bool(value.get("periodic_page_scan", args.btc_periodic_page_scan)),
    }


def _start_btc_page_scan_process() -> subprocess.Popen[str] | None:
    if shutil.which("bluetoothctl") is None:
        print("[rf-sentinel] btc periodic page scan unavailable; bluetoothctl not found", flush=True)
        return None
    commands = "\n".join(["power on", "scan bredr on"]) + "\n"
    try:
        proc = subprocess.Popen(
            ["bluetoothctl"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        if proc.stdin:
            proc.stdin.write(commands)
            proc.stdin.flush()
        return proc
    except OSError as exc:
        print(f"[rf-sentinel] btc periodic page scan failed: {exc}", flush=True)
        return None


def _stop_btc_page_scan_process(proc: subprocess.Popen[str] | None) -> None:
    if proc is None:
        return
    try:
        if proc.stdin and proc.poll() is None:
            proc.stdin.write("scan off\nexit\n")
            proc.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    _terminate_process_group(proc, timeout_s=2.0)


def _periodic_btc_page_scan_loop(args: argparse.Namespace, stop: threading.Event) -> None:
    active_s = max(1.0, float(args.btc_page_scan_active_s))
    interval_s = max(active_s, float(args.btc_page_scan_interval_s))
    while not stop.is_set():
        if not _control_bluetooth_classic(args).get("periodic_page_scan"):
            stop.wait(0.5)
            continue
        print(f"[rf-sentinel] btc periodic page scan active_s={active_s:.1f}", flush=True)
        proc = _start_btc_page_scan_process()
        stop.wait(active_s)
        _stop_btc_page_scan_process(proc)
        stop.wait(max(0.5, interval_s - active_s))


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
    parser.add_argument("--no-page-detection", action="store_true", help="legacy Bluetooth mode: suppress page/inquiry access-code events")
    parser.add_argument("--btc-log-passive-fhs-bdaddr", action="store_true", help="forward passive FHS BD_ADDR events from the Bluetooth Classic decoder")
    parser.add_argument("--btc-periodic-page-scan", action="store_true", help="periodically run a local BR/EDR inquiry scan while BTC/BLE decoding")
    parser.add_argument("--btc-page-scan-interval-s", type=float, default=DEFAULT_BTC_PAGE_SCAN_INTERVAL_S)
    parser.add_argument("--btc-page-scan-active-s", type=float, default=DEFAULT_BTC_PAGE_SCAN_ACTIVE_S)
    parser.add_argument("--btc-band-hop", action="store_true", help="retune the shared BTC/BLE SDR stream across overlapping 2.4 GHz windows")
    parser.add_argument("--btc-band-hop-dwell-s", type=float, default=10.0)
    parser.add_argument("--btc-expected-bdaddr", default=os.getenv("BTC_TARGET_MAC", ""), help="optional BD_ADDR used by the BTC decoder to verify passive FHS events")
    parser.add_argument(
        "--rf-input-mode",
        choices=("live", "capture", "playback"),
        default=os.getenv("RF_SENTINEL_RF_INPUT_MODE", "live"),
        help="global RF input mode for compatible scanners",
    )
    parser.add_argument(
        "--iq-capture-path",
        "--rf-capture-path",
        dest="iq_capture_path",
        default=os.getenv("RF_SENTINEL_IQ_CAPTURE_PATH", str(DEFAULT_BLUETOOTH_IQ_CAPTURE)),
        help="IQ recording path used when --rf-input-mode=capture",
    )
    parser.add_argument(
        "--iq-playback-path",
        "--rf-playback-path",
        dest="iq_playback_path",
        default=os.getenv("RF_SENTINEL_IQ_PLAYBACK_PATH", ""),
        help="IQ recording path used when --rf-input-mode=playback",
    )
    parser.add_argument("--iq-capture-max-bytes", type=int, default=int(os.getenv("RF_SENTINEL_IQ_CAPTURE_MAX_BYTES", "0") or 0))

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

    parser.add_argument("--walkie-device-id", default="", help="Preferred SDR for walkie-talkie detection")
    parser.add_argument("--walkie-slice-s", type=float, default=DEFAULT_JOB_DWELL_S)
    parser.add_argument("--walkie-center-freq-hz", type=int, default=DEFAULT_WALKIE_CENTER_FREQ_HZ)
    parser.add_argument("--walkie-sample-rate-sps", type=int, default=DEFAULT_WALKIE_SAMPLE_RATE_SPS)
    parser.add_argument("--walkie-baseband-filter-hz", type=int, default=250_000)
    parser.add_argument("--walkie-lna-gain-db", type=int, default=24)
    parser.add_argument("--walkie-vga-gain-db", type=int, default=28)

    parser.add_argument("--wifi-slice-s", type=float, default=DEFAULT_JOB_DWELL_S)
    parser.add_argument("--wifi-interface", default=os.getenv("RF_SENTINEL_WIFI_INTERFACE", DEFAULT_WIFI_INTERFACE))
    parser.add_argument("--wifi-command", choices=("scapy", "tcpdump", "tshark"), default="scapy")
    parser.add_argument("--wifi-channels", default="1,6,11")
    parser.add_argument("--wifi-hop-interval-s", type=float, default=1.0)
    parser.add_argument("--wifi-event-limit", type=int, default=500)
    parser.add_argument("--wifi-active-scan", action="store_true")
    parser.add_argument("--wifi-active-scan-interval-s", type=float, default=60.0)
    parser.add_argument("--wifi-set-monitor", action="store_true", default=True)
    parser.add_argument("--wifi-set-channel", action="store_true", default=True)

    parser.add_argument("--fm-slice-s", type=float, default=DEFAULT_JOB_DWELL_S)
    parser.add_argument("--fm-interval-s", type=float, default=DEFAULT_FM_INTERVAL_S)
    parser.add_argument("--fm-device-id", default=DEFAULT_FM_DEVICE, help="Preferred SDR for FM broadcast discovery")
    parser.add_argument("--fm-tuner-offset-hz", type=int, default=0)
    parser.add_argument("--fm-discovery-mode", choices=("auto", "wideband", "sweep", "iq"), default="sweep")
    parser.add_argument("--fm-sample-rate-sps", type=int, default=DEFAULT_FM_SAMPLE_RATE_SPS)
    parser.add_argument("--fm-sweep-bin-width-hz", type=int, default=100_000)
    parser.add_argument("--fm-sweep-prominence-db", type=float, default=2.0)
    parser.add_argument("--fm-station-merge-hz", type=int, default=300_000)
    parser.add_argument("--fm-discovery-dwell-s", type=float, default=3.0)
    parser.add_argument("--fm-decode-dwell-s", type=float, default=1.0)
    parser.add_argument("--fm-active-threshold-db", type=float, default=10.0)
    parser.add_argument("--fm-min-power-dbfs", type=float, default=-102.0)
    parser.add_argument("--fm-max-stations", type=int, default=24)
    parser.add_argument("--fm-lna-gain-db", type=int, default=32)
    parser.add_argument("--fm-vga-gain-db", type=int, default=32)
    parser.add_argument("--fm-debug", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--fm-skip-decode",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="emit FM sweep candidates without the slower retuned pilot/RDS quality pass",
    )

    parser.add_argument("--lfmf-device-id", default="sdrplay:0", help="SDRplay device reserved for VLF/LF/MF scans")
    parser.add_argument("--lfmf-serial", default=os.getenv("SDRPLAY_SERIAL", ""))
    parser.add_argument("--lfmf-band", choices=("vlf", "lf", "mf", "am", "1khz-1mhz", "vlf-lf-mf"), default="1khz-1mhz")
    parser.add_argument("--lfmf-slice-s", type=float, default=90.0)
    parser.add_argument("--lfmf-interval-s", type=float, default=DEFAULT_LFMF_INTERVAL_S)
    parser.add_argument("--lfmf-sample-rate-sps", type=int, default=1_000_000)
    parser.add_argument("--lfmf-bandwidth-hz", type=int, default=1_000_000)
    parser.add_argument("--lfmf-dwell-s", type=float, default=0.35)
    parser.add_argument("--lfmf-active-threshold-db", type=float, default=6.0)
    parser.add_argument("--lfmf-top-signals", type=int, default=20)
    parser.add_argument("--lfmf-step-khz", type=int, default=5)
    parser.add_argument("--lfmf-wideband", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--lfmf-wideband-center-khz", type=int, default=501)
    parser.add_argument("--lfmf-wideband-sample-rate-sps", type=int, default=1_000_000)
    parser.add_argument("--lfmf-wideband-bandwidth-hz", type=int, default=1_000_000)

    parser.add_argument("--cellular-slice-s", type=float, default=DEFAULT_CELLULAR_SLICE_S)
    parser.add_argument("--cellular-interval-s", type=float, default=30.0)
    parser.add_argument("--cellular-center-freq-hz", type=int, default=DEFAULT_CELLULAR_CENTER_FREQ_HZ)
    parser.add_argument("--cellular-target-freq-hz", type=int, default=DEFAULT_CELLULAR_CENTER_FREQ_HZ)
    parser.add_argument("--cellular-sample-rate-sps", type=int, default=DEFAULT_CELLULAR_SAMPLE_RATE_SPS)
    parser.add_argument("--cellular-bandwidth-hz", type=int, default=DEFAULT_CELLULAR_SAMPLE_RATE_SPS)
    parser.add_argument("--cellular-dwell-s", type=float, default=0.35)
    parser.add_argument("--cellular-active-threshold-db", type=float, default=5.0)
    parser.add_argument("--cellular-candidate-threshold-db", type=float, default=1.5)
    parser.add_argument("--cellular-target-threshold-db", type=float, default=1.5)
    parser.add_argument("--cellular-top", type=int, default=8)
    parser.add_argument("--cellular-lna-gain-db", type=int, default=32)
    parser.add_argument("--cellular-vga-gain-db", type=int, default=40)

    parser.add_argument("--no-btc", action="store_true")
    parser.add_argument("--no-ble", action="store_true")
    parser.add_argument("--no-zigbee", action="store_true")
    parser.add_argument("--no-tpms", action="store_true")
    parser.add_argument("--no-walkie", action="store_true")
    parser.add_argument("--no-wifi", action="store_true")
    parser.add_argument("--no-fm", action="store_true")
    parser.add_argument("--no-lfmf", action="store_true")
    parser.add_argument("--no-cellular", action="store_true")
    parser.add_argument(
        "--allowed-device-id",
        action="append",
        default=[],
        help="SDR device allowed by the UI; repeatable and currently used for compatibility with UI launches",
    )
    parser.add_argument("--control-file", default="", help="optional JSON file with a live protocols list")
    parser.add_argument("--once", action="store_true", help="run one BLE/Zigbee/TPMS/Walkie cycle and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
