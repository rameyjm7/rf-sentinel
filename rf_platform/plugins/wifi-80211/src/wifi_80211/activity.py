from __future__ import annotations

from dataclasses import dataclass
import json
import math
import time
from pathlib import Path
from typing import Iterable

import numpy as np


@dataclass(frozen=True)
class ActivityEvent:
    timestamp: float
    sample_index: int
    duration_samples: int
    score: float
    power_dbfs: float
    likely_offset_hz: float | None

    def as_dict(self) -> dict[str, float | int | None]:
        return {
            "timestamp": self.timestamp,
            "sample_index": self.sample_index,
            "duration_samples": self.duration_samples,
            "score": self.score,
            "power_dbfs": self.power_dbfs,
            "likely_offset_hz": self.likely_offset_hz,
        }


def wifi_stf_metric(samples: np.ndarray, sample_rate: float, window_us: float = 3.2) -> tuple[np.ndarray, np.ndarray]:
    period = max(1, int(round(float(sample_rate) * 0.8e-6)))
    window = max(period * 2, int(round(float(sample_rate) * window_us * 1e-6)))
    if len(samples) < window + period + 1:
        return np.array([], dtype=np.float32), np.array([], dtype=np.float32)

    delayed = samples[period:]
    aligned = samples[:-period]
    prod = aligned * np.conj(delayed)
    energy = np.abs(delayed) ** 2
    kernel = np.ones(window, dtype=np.float32)
    corr = np.abs(np.convolve(prod, kernel, mode="valid")) ** 2
    pwr = np.convolve(energy, kernel, mode="valid")
    metric = corr / np.maximum(pwr * pwr, 1e-18)
    return metric.astype(np.float32), (pwr / window).astype(np.float32)


def detect_events(
    samples: np.ndarray,
    sample_rate: float,
    *,
    start_index: int = 0,
    threshold: float = 0.55,
    min_gap_us: float = 20.0,
    min_duration_us: float = 2.0,
    center_freq: float | None = None,
) -> list[ActivityEvent]:
    metric, power = wifi_stf_metric(samples, sample_rate)
    if metric.size == 0:
        return []
    active = metric >= float(threshold)
    if not np.any(active):
        return []

    min_gap = int(round(float(sample_rate) * float(min_gap_us) * 1e-6))
    min_duration = int(round(float(sample_rate) * float(min_duration_us) * 1e-6))
    events: list[ActivityEvent] = []
    starts: list[int] = []
    stops: list[int] = []
    in_run = False
    for idx, value in enumerate(active):
        if value and not in_run:
            starts.append(idx)
            in_run = True
        elif not value and in_run:
            stops.append(idx)
            in_run = False
    if in_run:
        stops.append(len(active) - 1)

    merged: list[tuple[int, int]] = []
    for start, stop in zip(starts, stops):
        if not merged or start - merged[-1][1] > min_gap:
            merged.append((start, stop))
        else:
            merged[-1] = (merged[-1][0], stop)

    for start, stop in merged:
        if stop - start < min_duration:
            continue
        sl = slice(start, stop)
        peak_local = int(np.argmax(metric[sl])) + start
        power_dbfs = 10 * math.log10(max(float(power[peak_local]), 1e-18))
        likely_offset = strongest_20mhz_offset(samples[start:stop], sample_rate) if center_freq is not None else None
        events.append(
            ActivityEvent(
                timestamp=time.time(),
                sample_index=start_index + peak_local,
                duration_samples=stop - start,
                score=float(metric[peak_local]),
                power_dbfs=power_dbfs,
                likely_offset_hz=likely_offset,
            )
        )
    return events


def strongest_20mhz_offset(samples: np.ndarray, sample_rate: float) -> float | None:
    if samples.size < 1024:
        return None
    nfft = min(8192, 1 << int(math.floor(math.log2(samples.size))))
    spectrum = np.fft.fftshift(np.fft.fft(samples[:nfft] * np.hanning(nfft)))
    power = np.abs(spectrum) ** 2
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / float(sample_rate)))
    offsets = [-20e6, 0.0, 20e6] if float(sample_rate) >= 50e6 else [0.0]
    best_offset = None
    best_power = -1.0
    for offset in offsets:
        mask = np.abs(freqs - offset) <= 10e6
        band_power = float(np.mean(power[mask])) if np.any(mask) else -1.0
        if band_power > best_power:
            best_power = band_power
            best_offset = offset
    return best_offset


def write_jsonl(path: Path, events: Iterable[ActivityEvent]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event.as_dict(), sort_keys=True) + "\n")
