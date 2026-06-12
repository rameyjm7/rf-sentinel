from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json
import time

import numpy as np

from .dsp import iq_i8_to_complex, moving_average


@dataclass
class Burst:
    stream_id: str
    sample_rate_sps: int
    center_freq_hz: int
    iq: np.ndarray
    peak: float
    average: float
    started_at: float
    ended_at: float

    @property
    def duration_seconds(self) -> float:
        if self.sample_rate_sps <= 0:
            return 0.0
        return float(self.iq.size / float(self.sample_rate_sps))


@dataclass
class DecodeCandidate:
    strategy: str
    bits: str
    hex_string: str
    unit_samples: float
    score: float
    runs: list[int] = field(default_factory=list)


@dataclass
class DecodeResult:
    burst: Burst
    modulation: str
    confidence: float
    candidate: DecodeCandidate
    raw_runs: list[tuple[int, int]]
    protocol_variant: str | None = None
    decoded_fields: dict[str, object] = field(default_factory=dict)

    @property
    def bits(self) -> str:
        return self.candidate.bits

    @property
    def hex_string(self) -> str:
        return self.candidate.hex_string


@dataclass
class BurstDebugInfo:
    burst_ms: float
    peak: float
    average: float
    envelope_threshold: float
    raw_run_count: int
    collapsed_run_count: int
    min_high_samples: int
    median_high_samples: float
    max_high_samples: int
    min_low_samples: int
    median_low_samples: float
    max_low_samples: int
    reject_reason: str


class BurstDetector:
    def __init__(
        self,
        sample_rate_sps: int,
        center_freq_hz: int,
        stream_id: str,
        pre_roll_ms: float = 4.0,
        open_factor: float = 4.0,
        close_factor: float = 2.0,
        min_burst_ms: float = 1.0,
        max_burst_ms: float = 120.0,
    ) -> None:
        self.sample_rate_sps = int(sample_rate_sps)
        self.center_freq_hz = int(center_freq_hz)
        self.stream_id = stream_id
        self.pre_roll_samples = max(32, int(self.sample_rate_sps * (pre_roll_ms / 1000.0)))
        self.open_factor = float(open_factor)
        self.close_factor = float(close_factor)
        self.min_burst_samples = max(64, int(self.sample_rate_sps * (min_burst_ms / 1000.0)))
        self.max_burst_samples = max(self.min_burst_samples * 2, int(self.sample_rate_sps * (max_burst_ms / 1000.0)))
        self.noise_floor = 0.01
        self._pre_roll = np.empty(0, dtype=np.complex64)
        self._in_burst = False
        self._burst_start_time = 0.0
        self._burst_parts: list[np.ndarray] = []
        self._peak = 0.0
        self._average_energy = 0.0
        self._total_samples = 0

    def ingest(self, raw: bytes, timestamp: float | None = None) -> list[Burst]:
        iq = iq_i8_to_complex(raw)
        if iq.size == 0:
            return []
        return self.ingest_iq(iq, timestamp=timestamp)

    def ingest_iq(self, iq: np.ndarray, timestamp: float | None = None) -> list[Burst]:
        if iq.size == 0:
            return []
        now = timestamp if timestamp is not None else time.time()
        emitted: list[Burst] = []
        envelope = moving_average(np.abs(iq).astype(np.float32, copy=False), window=max(3, self.sample_rate_sps // 500_000))
        if envelope.size == 0:
            return []

        quiet = float(np.percentile(envelope, 20.0))
        self.noise_floor = (self.noise_floor * 0.97) + (quiet * 0.03)
        open_threshold = max(0.02, self.noise_floor * self.open_factor)
        close_threshold = max(0.015, self.noise_floor * self.close_factor)
        active = envelope > open_threshold
        active_close = envelope > close_threshold

        if not self._in_burst:
            if active.any():
                start_index = int(np.flatnonzero(active)[0])
                prefix = self._pre_roll[-self.pre_roll_samples :]
                self._burst_parts = [prefix, iq[start_index:].astype(np.complex64, copy=False)]
                self._peak = float(np.max(envelope[start_index:]))
                self._average_energy = float(np.mean(envelope[start_index:]))
                self._burst_start_time = now - (len(prefix) / self.sample_rate_sps)
                self._in_burst = True
        else:
            self._burst_parts.append(iq.astype(np.complex64, copy=False))
            self._peak = max(self._peak, float(np.max(envelope)))
            self._average_energy = (self._average_energy * 0.9) + (float(np.mean(envelope)) * 0.1)
            total_samples = sum(part.size for part in self._burst_parts)
            if total_samples >= self.max_burst_samples:
                emitted.extend(self.flush())
                self._pre_roll = iq[-self.pre_roll_samples :].astype(np.complex64, copy=False)
                return emitted
            last_active = int(np.flatnonzero(active_close)[-1]) if active_close.any() else -1
            silent_tail = envelope.size - 1 - last_active if last_active >= 0 else envelope.size
            if silent_tail > max(32, self.sample_rate_sps // 2000):
                burst_iq = np.concatenate(self._burst_parts).astype(np.complex64, copy=False)
                if burst_iq.size >= self.min_burst_samples:
                    emitted.append(
                        Burst(
                            stream_id=self.stream_id,
                            sample_rate_sps=self.sample_rate_sps,
                            center_freq_hz=self.center_freq_hz,
                            iq=burst_iq,
                            peak=self._peak,
                            average=self._average_energy,
                            started_at=self._burst_start_time,
                            ended_at=now,
                        )
                    )
                self._in_burst = False
                self._burst_parts = []
                self._peak = 0.0
                self._average_energy = 0.0
        self._pre_roll = np.concatenate((self._pre_roll, iq.astype(np.complex64, copy=False)))
        if self._pre_roll.size > self.pre_roll_samples:
            self._pre_roll = self._pre_roll[-self.pre_roll_samples :]
        self._total_samples += int(iq.size)
        return emitted

    def flush(self) -> list[Burst]:
        if not self._in_burst or not self._burst_parts:
            return []
        burst_iq = np.concatenate(self._burst_parts).astype(np.complex64, copy=False)
        self._in_burst = False
        self._burst_parts = []
        if burst_iq.size < self.min_burst_samples:
            return []
        return [
            Burst(
                stream_id=self.stream_id,
                sample_rate_sps=self.sample_rate_sps,
                center_freq_hz=self.center_freq_hz,
                iq=burst_iq,
                peak=self._peak,
                average=self._average_energy,
                started_at=self._burst_start_time,
                ended_at=time.time(),
            )
        ]


class ProtocolDecoder:
    protocol_name = "subghz"

    def __init__(self) -> None:
        self.last_debug: BurstDebugInfo | None = None

    def decode(self, burst: Burst) -> DecodeResult | None:
        raise NotImplementedError

    def debug_info(self) -> BurstDebugInfo | None:
        return self.last_debug

    def decode_to_json(self, result: DecodeResult) -> str:
        payload = {
            "protocol": self.protocol_name,
            "protocol_variant": result.protocol_variant,
            "stream_id": result.burst.stream_id,
            "center_freq_hz": result.burst.center_freq_hz,
            "sample_rate_sps": result.burst.sample_rate_sps,
            "modulation": result.modulation,
            "confidence": round(result.confidence, 4),
            "bits": result.bits,
            "hex": result.hex_string,
            "symbol_strategy": result.candidate.strategy,
            "symbol_unit_samples": round(result.candidate.unit_samples, 2),
            "symbol_runs": result.candidate.runs,
            "burst_samples": int(result.burst.iq.size),
            "burst_duration_ms": round(result.burst.duration_seconds * 1000.0, 3),
            "burst_peak": round(result.burst.peak, 4),
            "burst_average": round(result.burst.average, 4),
            "raw_runs": [[int(value), int(length)] for value, length in result.raw_runs],
        }
        if result.decoded_fields:
            payload["decoded_fields"] = result.decoded_fields
        return json.dumps(payload, sort_keys=True)

    def save_burst(self, result: DecodeResult, directory: Path) -> tuple[Path, Path]:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(result.burst.ended_at))
        stem = f"{self.protocol_name}_{int(result.burst.center_freq_hz)}_{stamp}"
        iq_path = directory / f"{stem}.iq"
        meta_path = directory / f"{stem}.json"
        interleaved = np.empty(result.burst.iq.size * 2, dtype=np.int8)
        interleaved[0::2] = np.clip(np.rint(result.burst.iq.real * 127.0), -128, 127).astype(np.int8)
        interleaved[1::2] = np.clip(np.rint(result.burst.iq.imag * 127.0), -128, 127).astype(np.int8)
        iq_path.write_bytes(interleaved.tobytes())
        meta_path.write_text(
            json.dumps(
                {
                    "protocol": self.protocol_name,
                    "protocol_variant": result.protocol_variant,
                    "stream_id": result.burst.stream_id,
                    "center_freq_hz": result.burst.center_freq_hz,
                    "sample_rate_sps": result.burst.sample_rate_sps,
                    "modulation": result.modulation,
                    "confidence": round(result.confidence, 4),
                    "bits": result.bits,
                    "hex": result.hex_string,
                    "symbol_strategy": result.candidate.strategy,
                    "symbol_unit_samples": round(result.candidate.unit_samples, 2),
                    "burst_samples": int(result.burst.iq.size),
                    "burst_duration_ms": round(result.burst.duration_seconds * 1000.0, 3),
                    "burst_peak": round(result.burst.peak, 4),
                    "burst_average": round(result.burst.average, 4),
                    "saved_iq_path": iq_path.name,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        if result.decoded_fields:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            payload["decoded_fields"] = result.decoded_fields
            meta_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return iq_path, meta_path

    def save_rejected_burst(self, burst: Burst, directory: Path, debug: BurstDebugInfo | None) -> tuple[Path, Path]:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(burst.ended_at))
        stem = f"rejected_{self.protocol_name}_{int(burst.center_freq_hz)}_{stamp}"
        iq_path = directory / f"{stem}.iq"
        meta_path = directory / f"{stem}.json"
        interleaved = np.empty(burst.iq.size * 2, dtype=np.int8)
        interleaved[0::2] = np.clip(np.rint(burst.iq.real * 127.0), -128, 127).astype(np.int8)
        interleaved[1::2] = np.clip(np.rint(burst.iq.imag * 127.0), -128, 127).astype(np.int8)
        iq_path.write_bytes(interleaved.tobytes())
        payload = {
            "protocol": self.protocol_name,
            "stream_id": burst.stream_id,
            "center_freq_hz": burst.center_freq_hz,
            "sample_rate_sps": burst.sample_rate_sps,
            "burst_samples": int(burst.iq.size),
            "burst_duration_ms": round(burst.duration_seconds * 1000.0, 3),
            "burst_peak": round(burst.peak, 4),
            "burst_average": round(burst.average, 4),
            "saved_iq_path": iq_path.name,
        }
        if debug is not None:
            payload.update(
                {
                    "reject_reason": debug.reject_reason,
                    "raw_run_count": debug.raw_run_count,
                    "collapsed_run_count": debug.collapsed_run_count,
                    "min_high_samples": debug.min_high_samples,
                    "median_high_samples": round(debug.median_high_samples, 2),
                    "max_high_samples": debug.max_high_samples,
                    "min_low_samples": debug.min_low_samples,
                    "median_low_samples": round(debug.median_low_samples, 2),
                    "max_low_samples": debug.max_low_samples,
                    "envelope_threshold": round(debug.envelope_threshold, 5),
                }
            )
        meta_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return iq_path, meta_path

    def _build_debug_info(
        self,
        burst: Burst,
        threshold: float,
        raw_runs: list[tuple[int, int]],
        collapsed_runs: list[tuple[int, int]],
        reject_reason: str,
    ) -> BurstDebugInfo:
        high_lengths = [length for value, length in collapsed_runs if value == 1]
        low_lengths = [length for value, length in collapsed_runs if value == 0]
        return BurstDebugInfo(
            burst_ms=burst.duration_seconds * 1000.0,
            peak=burst.peak,
            average=burst.average,
            envelope_threshold=threshold,
            raw_run_count=len(raw_runs),
            collapsed_run_count=len(collapsed_runs),
            min_high_samples=min(high_lengths) if high_lengths else 0,
            median_high_samples=float(np.median(np.asarray(high_lengths, dtype=np.float32))) if high_lengths else 0.0,
            max_high_samples=max(high_lengths) if high_lengths else 0,
            min_low_samples=min(low_lengths) if low_lengths else 0,
            median_low_samples=float(np.median(np.asarray(low_lengths, dtype=np.float32))) if low_lengths else 0.0,
            max_low_samples=max(low_lengths) if low_lengths else 0,
            reject_reason=reject_reason,
        )

    def _score_units(self, units: list[int], short_unit: int) -> float:
        if not units:
            return 1.0
        expected = np.asarray(units, dtype=np.float32)
        short = max(1.0, float(short_unit))
        ideal = np.where(expected > short, short * 2.0, short)
        return float(np.mean(np.abs(expected - ideal) / np.maximum(ideal, 1.0)))

    def _bits_to_hex(self, bits: str) -> str:
        if not bits:
            return ""
        pad = (-len(bits)) % 8
        if pad:
            bits = bits + ("0" * pad)
        out = bytearray()
        for offset in range(0, len(bits), 8):
            chunk = bits[offset : offset + 8]
            out.append(int(chunk, 2))
        return out.hex().upper()


def build_decoder(protocol: str) -> ProtocolDecoder:
    normalized = str(protocol).strip().lower()
    if normalized == "tpms":
        return TpmsDecoder()
    if normalized == "lora":
        return LoraDecoder()
    raise ValueError(f"Unsupported protocol: {protocol}")


from .protocols.lora import LoraDecoder  # noqa: E402
from .protocols.tpms import TpmsDecoder  # noqa: E402

__all__ = [
    "Burst",
    "DecodeCandidate",
    "DecodeResult",
    "BurstDebugInfo",
    "BurstDetector",
    "ProtocolDecoder",
    "TpmsDecoder",
    "LoraDecoder",
    "build_decoder",
]
