from __future__ import annotations

from dataclasses import asdict
import os
import time
from typing import Any

import numpy as np

from .activity import detect_events


WIFI_24GHZ_CHANNELS_HZ = {
    channel: 2_412_000_000 + ((channel - 1) * 5_000_000)
    for channel in range(1, 14)
}
WIFI_24GHZ_CHANNELS_HZ[14] = 2_484_000_000


def cs8_to_complex(raw_i8: bytes) -> np.ndarray:
    raw = np.frombuffer(raw_i8, dtype=np.int8)
    if raw.size < 2:
        return np.empty(0, dtype=np.complex64)
    raw = raw[: raw.size - (raw.size % 2)].astype(np.float32)
    return ((raw[0::2] + 1j * raw[1::2]) / 128.0).astype(np.complex64, copy=False)


def channel_from_frequency_hz(freq_hz: float | int | None) -> int | None:
    if freq_hz is None:
        return None
    freq = float(freq_hz)
    best_channel = None
    best_delta = 1e99
    for channel, center in WIFI_24GHZ_CHANNELS_HZ.items():
        delta = abs(freq - center)
        if delta < best_delta:
            best_channel = channel
            best_delta = delta
    return best_channel if best_delta <= 12_000_000 else None


class WiFiActivityDemodulator:
    protocol = "wifi"

    def __init__(self, *, threshold: float | None = None, min_interval_s: float | None = None) -> None:
        self.threshold = float(
            threshold
            if threshold is not None
            else os.getenv("RF_SENTINEL_WIFI_ACTIVITY_THRESHOLD", "0.55") or "0.55"
        )
        self.min_interval_s = float(
            min_interval_s
            if min_interval_s is not None
            else os.getenv("RF_SENTINEL_WIFI_DECODE_INTERVAL_MS", "250") or "250"
        ) / 1000.0
        self._next_process_at_by_key: dict[tuple[int, int], float] = {}
        self._sample_cursor_by_key: dict[tuple[int, int], int] = {}

    def process_chunk(
        self,
        *,
        raw_i8: bytes,
        center_freq_hz: int,
        sample_rate_sps: int,
        source: str = "",
        source_window: str = "",
        source_device_id: str = "",
    ) -> list[dict[str, Any]]:
        key = (int(center_freq_hz), int(sample_rate_sps))
        now = time.monotonic()
        if now < self._next_process_at_by_key.get(key, 0.0):
            return []
        self._next_process_at_by_key[key] = now + self.min_interval_s

        samples = cs8_to_complex(raw_i8)
        if samples.size < 1024:
            return []
        start_index = self._sample_cursor_by_key.get(key, 0)
        self._sample_cursor_by_key[key] = start_index + int(samples.size)

        out: list[dict[str, Any]] = []
        for event in detect_events(
            samples,
            float(sample_rate_sps),
            start_index=start_index,
            threshold=self.threshold,
            center_freq=float(center_freq_hz),
        ):
            likely_offset = event.likely_offset_hz
            likely_center = (
                int(center_freq_hz)
                if likely_offset is None and int(sample_rate_sps) <= 25_000_000
                else None if likely_offset is None
                else int(round(float(center_freq_hz) + float(likely_offset)))
            )
            channel = channel_from_frequency_hz(likely_center)
            item = asdict(event)
            item.update(
                {
                    "protocol": "wifi",
                    "kind": "wifi_activity",
                    "source": source or "iq_tap",
                    "source_window": source_window,
                    "source_device_id": source_device_id,
                    "center_freq_hz": int(center_freq_hz),
                    "sample_rate_sps": int(sample_rate_sps),
                    "likely_center_freq_hz": likely_center,
                    "channel": channel,
                    "confidence": max(0.0, min(1.0, float(event.score))),
                    "seen_at": float(event.timestamp),
                }
            )
            out.append(item)
        return out
