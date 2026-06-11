from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urlencode

import requests


def _load_config_defaults() -> dict[str, str]:
    config_path = Path.cwd() / "config" / "config.txt"
    if not config_path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            values[key] = value
    return values


def _setting(name: str, explicit: str | None = None, default: str = "") -> str:
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    env_value = os.getenv(name, "").strip()
    if env_value:
        return env_value
    return _load_config_defaults().get(name, default).strip()


@dataclass(frozen=True)
class GatewayDevice:
    id: str
    driver: str
    label: str
    serial: str | None
    freq_min_hz: int
    freq_max_hz: int
    max_sample_rate_sps: int
    notes: str | None
    occupied: bool


@dataclass(frozen=True)
class StreamHandle:
    stream_id: str
    device_id: str
    center_freq_hz: int
    sample_rate_sps: int


@dataclass(frozen=True)
class ActiveStream:
    stream_id: str
    device_id: str
    center_freq_hz: int
    sample_rate_sps: int
    status: str


@dataclass(frozen=True)
class StreamConfig:
    device_id: str
    center_freq_hz: int
    sample_rate_sps: int
    lna_gain_db: int = 16
    vga_gain_db: int = 20
    amp_enable: bool = False
    baseband_filter_hz: int | None = None
    duration_seconds: int | None = None
    num_samples: int | None = None


class GatewayClient:
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = _setting("SDR_GATEWAY_BASE_URL", explicit=base_url, default="http://127.0.0.1:8080").rstrip("/")
        self.token = _setting("SDR_GATEWAY_API_TOKEN", explicit=token)
        self._session = requests.Session()

    def headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}

    def list_devices(self) -> list[GatewayDevice]:
        response = self._session.get(f"{self.base_url}/devices", headers=self.headers(), timeout=8)
        response.raise_for_status()
        devices: list[GatewayDevice] = []
        for item in response.json():
            devices.append(
                GatewayDevice(
                    id=str(item.get("id", "")),
                    driver=str(item.get("driver", "")),
                    label=str(item.get("label", "")),
                    serial=item.get("serial"),
                    freq_min_hz=int(item.get("freq_min_hz", 0)),
                    freq_max_hz=int(item.get("freq_max_hz", 0)),
                    max_sample_rate_sps=int(item.get("max_sample_rate_sps", 0)),
                    notes=item.get("notes"),
                    occupied=bool(item.get("occupied", False)),
                )
            )
        return devices

    def resolve_default_device_id(self) -> str:
        devices = self.list_devices()
        for device in devices:
            if device.driver.lower() == "hackrf":
                return device.id
        if devices:
            return devices[0].id
        raise RuntimeError("No SDR devices are visible from sdr-gateway")

    def start_stream(self, config: StreamConfig) -> StreamHandle:
        payload: dict[str, object] = {
            "device_id": config.device_id,
            "center_freq_hz": int(config.center_freq_hz),
            "sample_rate_sps": int(config.sample_rate_sps),
            "lna_gain_db": int(config.lna_gain_db),
            "vga_gain_db": int(config.vga_gain_db),
            "amp_enable": bool(config.amp_enable),
        }
        if config.baseband_filter_hz is not None:
            payload["baseband_filter_hz"] = int(config.baseband_filter_hz)
        if config.duration_seconds is not None:
            payload["duration_seconds"] = int(config.duration_seconds)
        if config.num_samples is not None:
            payload["num_samples"] = int(config.num_samples)

        response = self._session.post(f"{self.base_url}/streams/start", headers=self.headers(), json=payload, timeout=12)
        if response.status_code == 409:
            self.stop_streams_for_device(config.device_id)
            response = self._session.post(f"{self.base_url}/streams/start", headers=self.headers(), json=payload, timeout=12)
        response.raise_for_status()
        body = response.json()
        return StreamHandle(
            stream_id=str(body["stream_id"]),
            device_id=config.device_id,
            center_freq_hz=config.center_freq_hz,
            sample_rate_sps=config.sample_rate_sps,
        )

    def list_streams(self) -> list[ActiveStream]:
        response = self._session.get(f"{self.base_url}/streams", headers=self.headers(), timeout=8)
        response.raise_for_status()
        streams: list[ActiveStream] = []
        for item in response.json():
            config = item.get("config", {}) or {}
            streams.append(
                ActiveStream(
                    stream_id=str(item.get("stream_id", "")),
                    device_id=str(config.get("device_id", "")),
                    center_freq_hz=int(config.get("center_freq_hz", 0)),
                    sample_rate_sps=int(config.get("sample_rate_sps", 0)),
                    status=str(item.get("status", "")),
                )
            )
        return streams

    def stop_streams_for_device(self, device_id: str) -> int:
        stopped = 0
        for stream in self.list_streams():
            if stream.device_id != device_id or not stream.stream_id:
                continue
            try:
                self.stop_stream(stream.stream_id)
                stopped += 1
            except Exception:
                continue
        return stopped

    def stop_stream(self, stream_id: str) -> None:
        if not stream_id:
            return
        response = self._session.post(f"{self.base_url}/streams/{stream_id}/stop", headers=self.headers(), timeout=2)
        response.raise_for_status()

    def retune_stream(self, stream_id: str, config: StreamConfig) -> StreamHandle:
        payload: dict[str, object] = {
            "device_id": config.device_id,
            "center_freq_hz": int(config.center_freq_hz),
            "sample_rate_sps": int(config.sample_rate_sps),
            "lna_gain_db": int(config.lna_gain_db),
            "vga_gain_db": int(config.vga_gain_db),
            "amp_enable": bool(config.amp_enable),
        }
        if config.baseband_filter_hz is not None:
            payload["baseband_filter_hz"] = int(config.baseband_filter_hz)
        response = self._session.post(
            f"{self.base_url}/streams/{stream_id}/retune",
            headers=self.headers(),
            json=payload,
            timeout=12,
        )
        if response.status_code == 404:
            self.stop_stream(stream_id)
            return self.start_stream(config)
        response.raise_for_status()
        body = response.json()
        current = body.get("config", {})
        return StreamHandle(
            stream_id=str(body["stream_id"]),
            device_id=str(current.get("device_id", config.device_id)),
            center_freq_hz=int(current.get("center_freq_hz", config.center_freq_hz)),
            sample_rate_sps=int(current.get("sample_rate_sps", config.sample_rate_sps)),
        )

    def ws_url(self, stream_id: str, keep_stream: bool = True) -> str:
        if self.base_url.startswith("https://"):
            ws_base = "wss://" + self.base_url[len("https://") :]
        else:
            ws_base = "ws://" + self.base_url[len("http://") :]
        params = {"keep": "1" if keep_stream else "0"}
        if self.token:
            params["token"] = self.token
        return f"{ws_base}/ws/iq/{stream_id}?{urlencode(params)}"

    def iter_iq_chunks(self, stream_id: str, keep_stream: bool = True) -> Iterator[bytes]:
        import websocket
        from websocket import WebSocketConnectionClosedException

        headers = []
        if self.token:
            headers.append(f"Authorization: Bearer {self.token}")
        ws = websocket.create_connection(self.ws_url(stream_id, keep_stream=keep_stream), header=headers or None, timeout=10)
        try:
            while True:
                try:
                    message = ws.recv()
                except WebSocketConnectionClosedException:
                    break
                if isinstance(message, bytes):
                    if message:
                        yield message
                    continue
                if isinstance(message, str) and message:
                    try:
                        payload = json.loads(message)
                    except json.JSONDecodeError:
                        continue
                    chunk = payload.get("data")
                    if isinstance(chunk, str):
                        yield chunk.encode("latin1")
        finally:
            try:
                ws.close()
            except Exception:
                pass
