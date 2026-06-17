from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Iterator

import requests
import websocket
from websocket import WebSocketConnectionClosedException


def gateway_base(base_url: str | None = None) -> str:
    return (base_url or os.getenv("SDR_GATEWAY_BASE_URL", "http://127.0.0.1:8080")).rstrip("/")


def gateway_token(token: str | None = None) -> str:
    explicit = (token or "").strip()
    if explicit:
        return explicit
    return (os.getenv("SDR_GATEWAY_API_TOKEN", "") or "").strip()


@dataclass(frozen=True)
class StreamHandle:
    stream_id: str
    device_id: str
    center_freq_hz: int
    sample_rate_sps: int


class GatewayClient:
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = gateway_base(base_url)
        self.token = gateway_token(token)
        self.session = requests.Session()

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def list_devices(self) -> list[dict]:
        response = self.session.get(f"{self.base_url}/devices", headers=self.headers(), timeout=8)
        response.raise_for_status()
        body = response.json()
        return body if isinstance(body, list) else []

    def resolve_default_device_id(self) -> str:
        devices = self.list_devices()
        for preferred in ("hackrf", "bladerf", "rtlsdr", "sdrplay"):
            for device in devices:
                if str(device.get("driver") or "").lower() == preferred and not bool(device.get("occupied")):
                    return str(device.get("id"))
        for device in devices:
            if not bool(device.get("occupied")):
                return str(device.get("id"))
        if devices:
            return str(devices[0].get("id"))
        raise RuntimeError("No SDR devices are visible from sdr-gateway")

    def stop_streams_for_device(self, device_id: str) -> None:
        try:
            response = self.session.get(f"{self.base_url}/streams", headers=self.headers(), timeout=5)
            response.raise_for_status()
            streams = response.json()
        except requests.RequestException:
            return
        if not isinstance(streams, list):
            return
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            config = stream.get("config") if isinstance(stream.get("config"), dict) else {}
            if str(config.get("device_id") or "") != device_id:
                continue
            stream_id = str(stream.get("stream_id") or "")
            if stream_id:
                self.stop_stream(stream_id)

    def start_stream(
        self,
        *,
        device_id: str,
        center_freq_hz: int,
        sample_rate_sps: int,
        lna_gain_db: int,
        vga_gain_db: int,
        amp_enable: bool,
        baseband_filter_hz: int,
        replace_existing: bool = True,
    ) -> StreamHandle:
        payload = {
            "device_id": device_id,
            "center_freq_hz": int(center_freq_hz),
            "sample_rate_sps": int(sample_rate_sps),
            "lna_gain_db": int(lna_gain_db),
            "vga_gain_db": int(vga_gain_db),
            "amp_enable": bool(amp_enable),
            "baseband_filter_hz": int(baseband_filter_hz),
            "duration_seconds": None,
            "num_samples": None,
        }
        response = self.session.post(f"{self.base_url}/streams/start", headers=self.headers(), json=payload, timeout=12)
        if response.status_code == 409 and replace_existing:
            self.stop_streams_for_device(device_id)
            time.sleep(0.2)
            response = self.session.post(f"{self.base_url}/streams/start", headers=self.headers(), json=payload, timeout=12)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            try:
                detail = response.json().get("detail")
            except (ValueError, AttributeError):
                detail = response.text.strip()
            raise RuntimeError(f"sdr-gateway stream start failed: HTTP {response.status_code}: {detail}") from exc
        body = response.json()
        return StreamHandle(
            stream_id=str(body["stream_id"]),
            device_id=device_id,
            center_freq_hz=int(body.get("center_freq_hz") or center_freq_hz),
            sample_rate_sps=int(body.get("sample_rate_sps") or sample_rate_sps),
        )

    def stop_stream(self, stream_id: str) -> None:
        try:
            self.session.post(f"{self.base_url}/streams/{stream_id}/stop", headers=self.headers(), timeout=5)
        except requests.RequestException:
            pass

    def ws_url(self, stream_id: str, keep_stream: bool = True) -> str:
        base = self.base_url
        if base.startswith("https://"):
            ws_base = "wss://" + base[len("https://") :]
        else:
            ws_base = "ws://" + base[len("http://") :]
        query = f"?keep={1 if keep_stream else 0}"
        if self.token:
            query += f"&token={self.token}"
        return f"{ws_base}/ws/iq/{stream_id}{query}"

    def iter_iq_chunks(
        self,
        stream_id: str,
        *,
        keep_stream: bool = True,
        deadline_monotonic: float | None = None,
    ) -> Iterator[bytes]:
        headers = [f"Authorization: Bearer {self.token}"] if self.token else None
        ws = websocket.create_connection(self.ws_url(stream_id, keep_stream=keep_stream), header=headers, timeout=10)
        ws.settimeout(0.2)
        try:
            while True:
                if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                    return
                try:
                    chunk = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except WebSocketConnectionClosedException:
                    return
                if isinstance(chunk, bytes) and chunk:
                    yield chunk
                elif isinstance(chunk, str) and chunk:
                    yield chunk.encode("latin1")
        finally:
            ws.close()
