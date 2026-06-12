from __future__ import annotations

import numpy as np

from ..decoder import Burst, BurstDebugInfo, DecodeCandidate, DecodeResult, ProtocolDecoder
from ..dsp import moving_average


class LoraDecoder(ProtocolDecoder):
    protocol_name = "lora"

    def __init__(self) -> None:
        super().__init__()
        self.min_burst_ms = 4.0
        self.max_burst_ms = 250.0
        self.min_samples = 256
        self.min_bandwidth_hz = 40_000.0
        self.min_chirp_strength = 0.68
        self.fingerprint_segments = 40

    def decode(self, burst: Burst) -> DecodeResult | None:
        if burst.iq.size < self.min_samples:
            self.last_debug = self._simple_debug(burst, "burst_too_short")
            return None
        burst_ms = burst.duration_seconds * 1000.0
        if burst_ms < self.min_burst_ms or burst_ms > self.max_burst_ms:
            self.last_debug = self._simple_debug(burst, f"burst_ms_out_of_range:{burst_ms:.2f}")
            return None

        inst_freq_hz = self._instantaneous_frequency_hz(burst.iq, burst.sample_rate_sps)
        if inst_freq_hz.size < 128:
            self.last_debug = self._simple_debug(burst, "inst_freq_too_short")
            return None
        smoothed = moving_average(inst_freq_hz.astype(np.float32, copy=False), window=max(5, burst.sample_rate_sps // 500_000))
        slope = np.diff(smoothed)
        if slope.size < 64:
            self.last_debug = self._simple_debug(burst, "slope_too_short")
            return None
        slope_abs = np.abs(slope)
        threshold = float(np.percentile(slope_abs, 60.0))
        significant = slope_abs > max(threshold, 1.0)
        if not np.any(significant):
            self.last_debug = self._simple_debug(burst, "no_significant_slope")
            return None

        positive_ratio = float(np.mean(slope[significant] > 0.0))
        negative_ratio = float(np.mean(slope[significant] < 0.0))
        chirp_strength = max(positive_ratio, negative_ratio)
        if chirp_strength < self.min_chirp_strength:
            self.last_debug = self._simple_debug(burst, f"chirp_strength_too_low:{chirp_strength:.2f}")
            return None

        bandwidth_hz = float(np.percentile(smoothed, 95.0) - np.percentile(smoothed, 5.0))
        if bandwidth_hz < self.min_bandwidth_hz:
            self.last_debug = self._simple_debug(burst, f"bandwidth_too_low:{bandwidth_hz:.0f}")
            return None

        direction = "up" if positive_ratio >= negative_ratio else "down"
        fingerprint_bits = self._fingerprint_bits(smoothed)
        candidate = DecodeCandidate(
            strategy=f"lora-{direction}",
            bits=fingerprint_bits,
            hex_string=self._bits_to_hex(fingerprint_bits),
            unit_samples=max(1.0, float(burst.iq.size) / max(1.0, burst_ms / 8.0)),
            score=max(0.0, 1.0 - min(1.0, chirp_strength)),
            runs=[],
        )
        confidence = max(0.0, min(1.0, 0.55 + (chirp_strength * 0.35) + min(0.10, bandwidth_hz / 1_000_000.0)))
        self.last_debug = None
        return DecodeResult(
            burst=burst,
            modulation="lora",
            confidence=confidence,
            candidate=candidate,
            raw_runs=[],
        )

    def _instantaneous_frequency_hz(self, iq: np.ndarray, sample_rate_sps: int) -> np.ndarray:
        if iq.size < 2:
            return np.empty(0, dtype=np.float32)
        phase_delta = np.angle(iq[1:] * np.conj(iq[:-1])).astype(np.float32, copy=False)
        return (phase_delta * float(sample_rate_sps) / (2.0 * np.pi)).astype(np.float32, copy=False)

    def _fingerprint_bits(self, series: np.ndarray) -> str:
        if series.size == 0:
            return "0" * 32
        normalized = series - float(np.mean(series))
        scale = float(np.max(np.abs(normalized))) or 1.0
        normalized = normalized / scale
        edges = np.linspace(0, normalized.size, num=self.fingerprint_segments + 1, dtype=int)
        nibble_bits: list[str] = []
        for start, end in zip(edges[:-1], edges[1:], strict=False):
            segment = normalized[start:end]
            mean_value = float(np.mean(segment)) if segment.size else 0.0
            quantized = int(np.clip(np.round((mean_value + 1.0) * 7.5), 0, 15))
            nibble_bits.append(f"{quantized:04b}")
        return "".join(nibble_bits)

    def _simple_debug(self, burst: Burst, reason: str) -> BurstDebugInfo:
        return BurstDebugInfo(
            burst_ms=burst.duration_seconds * 1000.0,
            peak=burst.peak,
            average=burst.average,
            envelope_threshold=0.0,
            raw_run_count=0,
            collapsed_run_count=0,
            min_high_samples=0,
            median_high_samples=0.0,
            max_high_samples=0,
            min_low_samples=0,
            median_low_samples=0.0,
            max_low_samples=0,
            reject_reason=reason,
        )
