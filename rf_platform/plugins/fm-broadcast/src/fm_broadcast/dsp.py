from __future__ import annotations

import math

import numpy as np


def dbfs(value: float, floor: float = -120.0) -> float:
    if value <= 0 or not math.isfinite(value):
        return floor
    return max(floor, 20.0 * math.log10(value))


def iq_i8_to_complex(raw: bytes) -> np.ndarray:
    if len(raw) < 4:
        return np.empty(0, dtype=np.complex64)
    usable = len(raw) - (len(raw) % 2)
    values = np.frombuffer(raw[:usable], dtype=np.int8).astype(np.float32)
    i = values[0::2] / 128.0
    q = values[1::2] / 128.0
    return (i + 1j * q).astype(np.complex64, copy=False)


def station_grid(start_hz: int, stop_hz: int, step_hz: int = 200_000, anchor_hz: int = 87_700_000) -> list[int]:
    start_hz = int(start_hz)
    stop_hz = int(stop_hz)
    step_hz = int(step_hz)
    anchor_hz = int(anchor_hz)
    if stop_hz < start_hz or step_hz <= 0:
        return []
    offset = (start_hz - anchor_hz) % step_hz
    first = start_hz if offset == 0 else start_hz + (step_hz - offset)
    return list(range(first, stop_hz + 1, step_hz))


def window_plan(start_hz: int, stop_hz: int, sample_rate_sps: int, usable_fraction: float = 0.72) -> list[int]:
    usable_hz = max(200_000, int(sample_rate_sps * usable_fraction))
    centers: list[int] = []
    current = int(start_hz + usable_hz // 2)
    max_center = int(stop_hz - usable_hz // 2)
    if current > max_center:
        return [int((start_hz + stop_hz) // 2)]
    while current <= max_center:
        centers.append(current)
        current += usable_hz
    if not centers or (max_center - centers[-1]) > usable_hz * 0.35:
        centers.append(max_center)
    return list(dict.fromkeys(centers))


def band_power_dbfs(iq: np.ndarray, sample_rate_sps: int, offset_hz: float, width_hz: float = 160_000.0) -> float:
    if iq.size < 2048:
        return -120.0
    nfft = min(65536, 1 << int(np.floor(np.log2(iq.size))))
    if nfft < 2048:
        return -120.0
    samples = iq[-nfft:].astype(np.complex64, copy=False)
    samples = samples - np.mean(samples)
    window = np.hanning(nfft).astype(np.float32)
    spectrum = np.fft.fftshift(np.fft.fft(samples * window))
    power = (np.abs(spectrum) / max(1, nfft / 2.0)) ** 2
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, d=1.0 / float(sample_rate_sps)))
    mask = np.abs(freqs - float(offset_hz)) <= float(width_hz) / 2.0
    if not np.any(mask):
        return -120.0
    return 10.0 * math.log10(float(np.mean(power[mask])) + 1e-18)


class FmQualityDemod:
    """AetherCast-derived FM discriminator for station-quality scoring."""

    def __init__(self, sample_rate_sps: int) -> None:
        self.sample_rate_sps = int(sample_rate_sps)
        self.decim = max(1, int(round(self.sample_rate_sps / 240_000.0)))
        self.demod_rate = self.sample_rate_sps / float(self.decim)
        self.prev = np.complex64(1.0 + 0j)
        self.audio_rms = 0.0
        self.pilot_db = -120.0
        self.rds_subcarrier_db = -120.0
        self._metric_buf = np.empty(0, dtype=np.float32)

    def process(self, raw: bytes) -> None:
        iq = iq_i8_to_complex(raw)
        self.process_iq(iq)

    def process_iq(self, iq: np.ndarray) -> None:
        if iq.size < self.decim * 8:
            return
        z = iq[:: self.decim]
        if z.size < 8:
            return
        previous = np.empty_like(z)
        previous[0] = self.prev
        previous[1:] = z[:-1]
        self.prev = z[-1]
        demod = np.angle(z * np.conj(previous)).astype(np.float32)
        if demod.size < 128:
            return
        demod = demod - float(np.mean(demod))
        self.audio_rms = float((self.audio_rms * 0.8) + (np.sqrt(np.mean(demod * demod)) * 0.2))
        self._metric_buf = np.concatenate((self._metric_buf, demod))
        max_metric_samples = int(max(self.demod_rate * 1.5, 8192))
        if self._metric_buf.size > max_metric_samples:
            self._metric_buf = self._metric_buf[-max_metric_samples:]
        self._update_metrics()

    def _update_metrics(self) -> None:
        if self._metric_buf.size < 4096:
            return
        n = min(32768, self._metric_buf.size)
        samples = self._metric_buf[-n:]
        windowed = samples * np.hanning(samples.size).astype(np.float32)
        spectrum = np.abs(np.fft.rfft(windowed)) ** 2
        freqs = np.fft.rfftfreq(samples.size, d=1.0 / float(self.demod_rate))
        noise = float(np.median(spectrum)) + 1e-12

        def band_db(center_hz: float, width_hz: float) -> float:
            mask = np.abs(freqs - center_hz) <= (width_hz / 2.0)
            if not np.any(mask):
                return -120.0
            power = float(np.mean(spectrum[mask]))
            return 10.0 * math.log10((power + 1e-12) / noise)

        self.pilot_db = band_db(19_000.0, 900.0)
        self.rds_subcarrier_db = band_db(57_000.0, 3_500.0)
