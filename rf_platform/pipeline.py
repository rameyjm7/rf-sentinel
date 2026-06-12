from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote

import requests
import websocket


@dataclass(frozen=True)
class ScanWindow:
    name: str
    center_freq_hz: int
    sample_rate_sps: int
    dwell_s: float
    lna_gain_db: int = 16
    vga_gain_db: int = 20
    amp_enable: bool = False
    baseband_filter_hz: int | None = None

    def contains(self, freq_hz: int, guard_hz: int = 0) -> bool:
        half = max(0, int(self.sample_rate_sps // 2) - int(guard_hz))
        return abs(int(freq_hz) - int(self.center_freq_hz)) <= half


@dataclass(frozen=True)
class IQChunk:
    source: str
    device_id: str
    window: ScanWindow
    raw_i8: bytes
    seen_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class DemodDecision:
    protocol: str
    reason: str
    priority: int = 0


@dataclass(frozen=True)
class DemodResult:
    protocol: str
    event: dict[str, Any]


class Demodulator(Protocol):
    protocol: str

    def process(self, chunk: IQChunk) -> list[DemodResult]: ...


class ProtocolDecisionEngine:
    BLE_ADV_HZ = (2_402_000_000, 2_426_000_000, 2_480_000_000)
    ZIGBEE_HZ = tuple(2_405_000_000 + (5_000_000 * i) for i in range(16))
    TPMS_HZ = (315_000_000, 433_920_000)
    BTC_LOW_HZ = 2_402_000_000
    BTC_HIGH_HZ = 2_480_000_000

    def decide(self, chunk: IQChunk) -> list[DemodDecision]:
        decisions: list[DemodDecision] = []
        window = chunk.window
        if any(window.contains(freq, guard_hz=1_200_000) for freq in self.BLE_ADV_HZ):
            decisions.append(DemodDecision("ble", "BLE advertising channel overlaps receive window", 90))
        if any(window.contains(freq, guard_hz=1_500_000) for freq in self.ZIGBEE_HZ):
            decisions.append(DemodDecision("zigbee", "802.15.4 channel overlaps receive window", 80))
        if any(window.contains(freq, guard_hz=100_000) for freq in self.TPMS_HZ):
            decisions.append(DemodDecision("tpms", "TPMS known band overlaps receive window", 70))
        low = int(window.center_freq_hz - (window.sample_rate_sps / 2))
        high = int(window.center_freq_hz + (window.sample_rate_sps / 2))
        if high >= self.BTC_LOW_HZ and low <= self.BTC_HIGH_HZ:
            decisions.append(DemodDecision("bluetooth_classic", "Bluetooth Classic band overlaps receive window", 60))
        decisions.sort(key=lambda item: item.priority, reverse=True)
        return decisions


class BLEWidebandDemodulator:
    protocol = "ble"

    def __init__(self) -> None:
        self._detectors: dict[tuple[int, int], Any] = {}

    def process(self, chunk: IQChunk) -> list[DemodResult]:
        try:
            from bluetooth_lowenergy.detector import WideBLEAdvertisingDetector
        except Exception:
            return []

        key = (int(chunk.window.center_freq_hz), int(chunk.window.sample_rate_sps))
        detector = self._detectors.get(key)
        if detector is None:
            detector = WideBLEAdvertisingDetector(
                sample_rate_sps=chunk.window.sample_rate_sps,
                center_freq_hz=chunk.window.center_freq_hz,
            )
            self._detectors[key] = detector
        _, events = detector.process_iq_i8(chunk.raw_i8)
        results: list[DemodResult] = []
        for event in events:
            if event.get("kind") != "ble_adv":
                continue
            event = dict(event)
            event.setdefault("source_window", chunk.window.name)
            event.setdefault("source_device_id", chunk.device_id)
            results.append(DemodResult(protocol=self.protocol, event=event))
        return results


class PlaceholderDemodulator:
    def __init__(self, protocol: str) -> None:
        self.protocol = protocol

    def process(self, chunk: IQChunk) -> list[DemodResult]:
        return []


class DemodWorker(threading.Thread):
    def __init__(
        self,
        iq_queue: "queue.Queue[IQChunk]",
        output_queue: "queue.Queue[DemodResult]",
        *,
        decision_engine: ProtocolDecisionEngine | None = None,
        demodulators: dict[str, Demodulator] | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        super().__init__(daemon=True)
        self.iq_queue = iq_queue
        self.output_queue = output_queue
        self.decision_engine = decision_engine or ProtocolDecisionEngine()
        self.demodulators = demodulators or {
            "ble": BLEWidebandDemodulator(),
            "zigbee": PlaceholderDemodulator("zigbee"),
            "tpms": PlaceholderDemodulator("tpms"),
            "bluetooth_classic": PlaceholderDemodulator("bluetooth_classic"),
        }
        self.stop_event = stop_event or threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                chunk = self.iq_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                for decision in self.decision_engine.decide(chunk):
                    demodulator = self.demodulators.get(decision.protocol)
                    if demodulator is None:
                        continue
                    for result in demodulator.process(chunk):
                        self.output_queue.put(result)
            finally:
                self.iq_queue.task_done()


class GatewaySweepReceiver(threading.Thread):
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        device_id: str,
        windows: list[ScanWindow],
        iq_queue: "queue.Queue[IQChunk]",
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True)
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.device_id = device_id
        self.windows = windows
        self.iq_queue = iq_queue
        self.stop_event = stop_event
        self._stream_id: str | None = None

    def run(self) -> None:
        if not self.windows:
            return
        try:
            self._run()
        finally:
            self._stop_stream()

    def _run(self) -> None:
        current = self.windows[0]
        self._stream_id = self._start_stream(current)
        ws = websocket.create_connection(self._ws_url(self._stream_id), timeout=5)
        try:
            window_started = time.time()
            index = 0
            while not self.stop_event.is_set():
                now = time.time()
                if now - window_started >= max(0.05, current.dwell_s):
                    index = (index + 1) % len(self.windows)
                    current = self.windows[index]
                    self._retune_stream(self._stream_id, current)
                    window_started = now
                data = ws.recv()
                if isinstance(data, str):
                    data = data.encode("latin1")
                if not data:
                    continue
                chunk = IQChunk(
                    source="sdr-gateway",
                    device_id=self.device_id,
                    window=current,
                    raw_i8=bytes(data),
                )
                try:
                    self.iq_queue.put(chunk, timeout=0.5)
                except queue.Full:
                    # Backpressure: drop oldest work rather than blocking receive.
                    with contextlib_suppress_queue_empty(self.iq_queue):
                        self.iq_queue.get_nowait()
                        self.iq_queue.task_done()
                    self.iq_queue.put(chunk, timeout=0.1)
        finally:
            ws.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _start_stream(self, window: ScanWindow) -> str:
        payload = self._payload(window)
        response = requests.post(f"{self.base_url}/streams/start", json=payload, headers=self._headers(), timeout=12)
        if response.status_code == 409:
            self._stop_streams_for_device()
            response = requests.post(f"{self.base_url}/streams/start", json=payload, headers=self._headers(), timeout=12)
        response.raise_for_status()
        return str(response.json()["stream_id"])

    def _retune_stream(self, stream_id: str, window: ScanWindow) -> None:
        response = requests.post(
            f"{self.base_url}/streams/{stream_id}/retune",
            json=self._payload(window),
            headers=self._headers(),
            timeout=12,
        )
        response.raise_for_status()

    def _payload(self, window: ScanWindow) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "device_id": self.device_id,
            "center_freq_hz": int(window.center_freq_hz),
            "sample_rate_sps": int(window.sample_rate_sps),
            "lna_gain_db": int(window.lna_gain_db),
            "vga_gain_db": int(window.vga_gain_db),
            "amp_enable": bool(window.amp_enable),
            "duration_seconds": None,
            "num_samples": None,
        }
        if window.baseband_filter_hz is not None:
            payload["baseband_filter_hz"] = int(window.baseband_filter_hz)
        return payload

    def _ws_url(self, stream_id: str) -> str:
        if self.base_url.startswith("https://"):
            base = "wss://" + self.base_url[len("https://") :]
        else:
            base = "ws://" + self.base_url.removeprefix("http://")
        suffix = "?keep=1"
        if self.token:
            suffix += f"&token={quote(self.token)}"
        return f"{base}/ws/iq/{stream_id}{suffix}"

    def _stop_streams_for_device(self) -> None:
        try:
            response = requests.get(f"{self.base_url}/streams", headers=self._headers(), timeout=8)
            response.raise_for_status()
            for item in response.json():
                config = item.get("config") or {}
                if str(config.get("device_id") or "") == self.device_id:
                    stream_id = item.get("stream_id")
                    if stream_id:
                        requests.post(f"{self.base_url}/streams/{stream_id}/stop", headers=self._headers(), timeout=5)
        except requests.RequestException:
            return

    def _stop_stream(self) -> None:
        if not self._stream_id:
            return
        try:
            requests.post(f"{self.base_url}/streams/{self._stream_id}/stop", headers=self._headers(), timeout=5)
        except requests.RequestException:
            pass


class contextlib_suppress_queue_empty:
    def __init__(self, _queue: "queue.Queue[IQChunk]") -> None:
        self.queue = _queue

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return exc_type is queue.Empty


def default_windows() -> list[ScanWindow]:
    return [
        ScanWindow("ble-zigbee-low", 2_414_000_000, 20_000_000, 1.0, lna_gain_db=40, vga_gain_db=40, baseband_filter_hz=20_000_000),
        ScanWindow("ble-zigbee-mid", 2_426_000_000, 20_000_000, 1.0, lna_gain_db=40, vga_gain_db=40, baseband_filter_hz=20_000_000),
        ScanWindow("ble-zigbee-high", 2_475_000_000, 20_000_000, 1.0, lna_gain_db=40, vga_gain_db=40, baseband_filter_hz=20_000_000),
        ScanWindow("tpms-315", 315_000_000, 2_000_000, 1.0, lna_gain_db=16, vga_gain_db=20, baseband_filter_hz=2_000_000),
        ScanWindow("tpms-433", 433_920_000, 2_000_000, 1.0, lna_gain_db=16, vga_gain_db=20, baseband_filter_hz=2_000_000),
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rf_sentinel_pipeline", description="Threaded receive/decision/demod RF pipeline")
    parser.add_argument("--base-url", default=os.getenv("SDR_GATEWAY_BASE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--token", default=os.getenv("SDR_GATEWAY_API_TOKEN", ""))
    parser.add_argument("--device-id", default="hackrf:0")
    parser.add_argument("--queue-size", type=int, default=64)
    parser.add_argument("--max-events", type=int, default=0)
    parser.add_argument("--duration-s", type=float, default=0.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    stop_event = threading.Event()
    iq_queue: queue.Queue[IQChunk] = queue.Queue(maxsize=max(1, int(args.queue_size)))
    output_queue: queue.Queue[DemodResult] = queue.Queue()

    def stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    receiver = GatewaySweepReceiver(
        base_url=args.base_url,
        token=args.token,
        device_id=args.device_id,
        windows=default_windows(),
        iq_queue=iq_queue,
        stop_event=stop_event,
    )
    worker = DemodWorker(iq_queue, output_queue, stop_event=stop_event)
    receiver.start()
    worker.start()

    started = time.time()
    event_count = 0
    try:
        while not stop_event.is_set():
            if args.duration_s and time.time() - started >= float(args.duration_s):
                stop_event.set()
                break
            try:
                result = output_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            print(json.dumps({"protocol": result.protocol, "event": result.event}, sort_keys=True), flush=True)
            event_count += 1
            if args.max_events and event_count >= int(args.max_events):
                stop_event.set()
                break
    finally:
        stop_event.set()
        receiver.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
