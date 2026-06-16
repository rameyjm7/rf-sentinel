from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class AMMetrics:
    power_dbfs: float
    carrier_dbfs: float
    local_noise_dbfs: float
    carrier_snr_db: float
    audio_dbfs: float
    modulation_pct: float
    carrier_rms: float
    audio_rms: float
    samples: int


def dbfs_from_rms(value: float, *, full_scale: float = 32768.0, floor: float = -160.0) -> float:
    if value <= 0.0 or not math.isfinite(value):
        return floor
    return max(float(floor), 20.0 * math.log10(float(value) / float(full_scale)))


def channel_grid(start_khz: int, stop_khz: int, step_khz: int) -> list[int]:
    start_hz = int(start_khz) * 1000
    stop_hz = int(stop_khz) * 1000
    step_hz = int(step_khz) * 1000
    if stop_hz < start_hz or step_hz <= 0:
        return []
    return list(range(start_hz, stop_hz + 1, step_hz))


def cs16_to_complex(samples: np.ndarray) -> np.ndarray:
    values = np.asarray(samples)
    if values.size < 2:
        return np.empty(0, dtype=np.complex64)
    usable = values.size - (values.size % 2)
    interleaved = values[:usable].astype(np.float32, copy=False)
    return (interleaved[0::2] + 1j * interleaved[1::2]).astype(np.complex64, copy=False)


def i8_to_complex(samples: bytes | np.ndarray) -> np.ndarray:
    values = np.frombuffer(samples, dtype=np.int8) if isinstance(samples, (bytes, bytearray, memoryview)) else np.asarray(samples, dtype=np.int8)
    if values.size < 2:
        return np.empty(0, dtype=np.complex64)
    usable = values.size - (values.size % 2)
    interleaved = values[:usable].astype(np.float32, copy=False) * 256.0
    return (interleaved[0::2] + 1j * interleaved[1::2]).astype(np.complex64, copy=False)


def measure_am_channel(
    iq: np.ndarray,
    sample_rate_sps: int,
    *,
    carrier_offset_hz: float = 0.0,
    carrier_width_hz: float = 350.0,
    audio_low_hz: float = 80.0,
    audio_high_hz: float = 5000.0,
) -> AMMetrics:
    samples = np.asarray(iq, dtype=np.complex64)
    if samples.size == 0:
        return AMMetrics(-160.0, -160.0, -160.0, 0.0, -160.0, 0.0, 0.0, 0.0, 0)

    magnitude = np.abs(samples).astype(np.float32, copy=False)
    carrier_rms = float(np.sqrt(np.mean(np.abs(samples) ** 2)))
    power_dbfs = dbfs_from_rms(carrier_rms)
    carrier_dbfs, local_noise_dbfs = _carrier_power_dbfs(
        samples,
        int(sample_rate_sps),
        float(carrier_offset_hz),
        float(carrier_width_hz),
    )
    carrier_snr_db = carrier_dbfs - local_noise_dbfs
    envelope_mean = float(np.mean(magnitude))
    envelope_ac = magnitude - envelope_mean
    audio_rms = _audio_band_rms(envelope_ac, int(sample_rate_sps), audio_low_hz, audio_high_hz)
    audio_dbfs = dbfs_from_rms(audio_rms)
    modulation_pct = 0.0
    if envelope_mean > 1e-9:
        modulation_pct = float(np.clip((audio_rms / envelope_mean) * math.sqrt(2.0) * 100.0, 0.0, 250.0))
    return AMMetrics(
        power_dbfs=round(power_dbfs, 1),
        carrier_dbfs=round(carrier_dbfs, 1),
        local_noise_dbfs=round(local_noise_dbfs, 1),
        carrier_snr_db=round(carrier_snr_db, 1),
        audio_dbfs=round(audio_dbfs, 1),
        modulation_pct=round(modulation_pct, 1),
        carrier_rms=carrier_rms,
        audio_rms=audio_rms,
        samples=int(samples.size),
    )


def _carrier_power_dbfs(iq: np.ndarray, sample_rate_sps: int, offset_hz: float, width_hz: float) -> tuple[float, float]:
    if iq.size < 1024 or sample_rate_sps <= 0:
        return -160.0, -160.0
    nfft = min(131072, 1 << int(np.floor(np.log2(iq.size))))
    if nfft < 1024:
        return -160.0, -160.0
    work = iq[-nfft:].astype(np.complex64, copy=False)
    work = work - np.mean(work)
    window = np.hanning(nfft).astype(np.float32)
    coherent_gain = max(1e-9, float(np.sum(window)) / float(nfft))
    spectrum = np.fft.fftshift(np.fft.fft(work * window))
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, d=1.0 / float(sample_rate_sps)))
    amplitude = np.abs(spectrum) / float(nfft) / coherent_gain
    carrier_mask = np.abs(freqs - float(offset_hz)) <= max(1.0, float(width_hz) / 2.0)
    if not np.any(carrier_mask):
        return -160.0, -160.0
    guard_hz = max(float(width_hz) * 4.0, 1200.0)
    noise_span_hz = max(float(width_hz) * 18.0, 9000.0)
    distance = np.abs(freqs - float(offset_hz))
    noise_mask = (distance >= guard_hz) & (distance <= noise_span_hz)
    carrier_amp = float(np.max(amplitude[carrier_mask]))
    noise_amp = float(np.median(amplitude[noise_mask])) if np.any(noise_mask) else 0.0
    return dbfs_from_rms(carrier_amp), dbfs_from_rms(noise_amp)


def _audio_band_rms(envelope_ac: np.ndarray, sample_rate_sps: int, low_hz: float, high_hz: float) -> float:
    if envelope_ac.size < 64 or sample_rate_sps <= 0:
        return 0.0
    nfft = min(65536, 1 << int(np.floor(np.log2(envelope_ac.size))))
    if nfft < 64:
        return 0.0
    work = envelope_ac[-nfft:].astype(np.float32, copy=False)
    work = work - float(np.mean(work))
    window = np.hanning(nfft).astype(np.float32)
    spectrum = np.fft.rfft(work * window)
    freqs = np.fft.rfftfreq(nfft, d=1.0 / float(sample_rate_sps))
    mask = (freqs >= float(low_hz)) & (freqs <= float(high_hz))
    if not np.any(mask):
        return 0.0
    filtered = np.zeros_like(spectrum)
    filtered[mask] = spectrum[mask]
    audio = np.fft.irfft(filtered, n=nfft)
    return float(np.sqrt(np.mean(audio * audio)))
