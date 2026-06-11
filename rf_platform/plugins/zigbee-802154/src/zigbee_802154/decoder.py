from __future__ import annotations

from dataclasses import dataclass, field
import json
import time

import numpy as np

from .dsp import iq_i8_to_complex, moving_average

try:
    from . import _cdecode
except ImportError:
    _cdecode = None


CHIP_RATE_SPS = 2_000_000
SYMBOL_CHIPS = 32
PREAMBLE_SYMBOLS = [0] * 8
SFD_SYMBOL_PATTERNS = (
    (0x7, 0xA),
    (0xA, 0x7),
)
MAX_PHY_PSDU = 127

_BASE_CHIP_SEQUENCE = "11011001110000110101001000101110"
_UPPER_BASE_CHIP_SEQUENCE = "10001100100101100000011101111011"


def _rotate_right(text: str, amount: int) -> str:
    amount %= len(text)
    if amount == 0:
        return text
    return text[-amount:] + text[:-amount]


def _build_symbol_table(base_sequence: str, upper_base_sequence: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for symbol in range(8):
        out[symbol] = _rotate_right(base_sequence, symbol * 4)
    for symbol in range(8, 16):
        out[symbol] = _rotate_right(upper_base_sequence, (symbol - 8) * 4)
    return out


_PRIMARY_SYMBOL_TABLE = _build_symbol_table(_BASE_CHIP_SEQUENCE, _UPPER_BASE_CHIP_SEQUENCE)
_REVERSED_SYMBOL_TABLE = {symbol: chips[::-1] for symbol, chips in _PRIMARY_SYMBOL_TABLE.items()}
_INVERTED_SYMBOL_TABLE = {symbol: "".join("1" if bit == "0" else "0" for bit in chips) for symbol, chips in _PRIMARY_SYMBOL_TABLE.items()}
_REVERSED_INVERTED_SYMBOL_TABLE = {
    symbol: "".join("1" if bit == "0" else "0" for bit in chips[::-1]) for symbol, chips in _PRIMARY_SYMBOL_TABLE.items()
}
SYMBOL_TABLES: tuple[tuple[str, dict[int, str]], ...] = (
    ("primary", _PRIMARY_SYMBOL_TABLE),
    ("reversed", _REVERSED_SYMBOL_TABLE),
    ("inverted", _INVERTED_SYMBOL_TABLE),
    ("reversed_inverted", _REVERSED_INVERTED_SYMBOL_TABLE),
)


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


@dataclass(frozen=True)
class SymbolDecision:
    symbol: int
    hamming_distance: int
    chips: str


@dataclass
class IEEE802154DecodeDiagnostics:
    chip_slicer: str = "alternating_mean"
    frequency_offset_hz: float = 0.0
    symbol_table_name: str = "primary"
    phase_index: int = 0
    phase_degrees: float = 0.0
    chip_offset: int = 0
    symbol_phase: int = 0
    symbols_available: int = 0
    candidates_checked: int = 0
    sfd_hits: int = 0
    best_pattern_mismatches: int = len(PREAMBLE_SYMBOLS) + len(SFD_SYMBOL_PATTERNS[0])
    best_pattern_error_sum: int = 1_000_000
    best_length: int | None = None
    best_payload_preview_hex: str = ""
    best_total_error_sum: int | None = None
    best_symbol_distance: int = SYMBOL_CHIPS + 1
    crc_ok: bool = False
    pattern_correlation: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "chip_slicer": self.chip_slicer,
            "frequency_offset_hz": round(self.frequency_offset_hz, 1),
            "phase_index": self.phase_index,
            "phase_degrees": round(self.phase_degrees, 3),
            "symbol_table_name": self.symbol_table_name,
            "chip_offset": self.chip_offset,
            "symbol_phase": self.symbol_phase,
            "symbols_available": self.symbols_available,
            "candidates_checked": self.candidates_checked,
            "sfd_hits": self.sfd_hits,
            "best_pattern_mismatches": self.best_pattern_mismatches,
            "best_pattern_error_sum": self.best_pattern_error_sum,
            "best_length": self.best_length,
            "best_payload_preview_hex": self.best_payload_preview_hex,
            "best_total_error_sum": self.best_total_error_sum,
            "best_symbol_distance": self.best_symbol_distance,
            "crc_ok": self.crc_ok,
            "pattern_correlation": round(self.pattern_correlation, 4),
        }


@dataclass
class IEEE802154MacFields:
    frame_type: str
    frame_version: int
    sequence_number: int | None
    ack_request: bool
    frame_pending: bool
    security_enabled: bool
    pan_id_compression: bool
    destination_pan_id: int | None
    destination_address_mode: str
    destination_address: str | None
    source_pan_id: int | None
    source_address_mode: str
    source_address: str | None
    payload_hex: str
    fcs_hex: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "frame_type": self.frame_type,
            "frame_version": self.frame_version,
            "sequence_number": self.sequence_number,
            "ack_request": self.ack_request,
            "frame_pending": self.frame_pending,
            "security_enabled": self.security_enabled,
            "pan_id_compression": self.pan_id_compression,
            "destination_pan_id": self.destination_pan_id,
            "destination_address_mode": self.destination_address_mode,
            "destination_address": self.destination_address,
            "source_pan_id": self.source_pan_id,
            "source_address_mode": self.source_address_mode,
            "source_address": self.source_address,
            "payload_hex": self.payload_hex,
            "fcs_hex": self.fcs_hex,
        }


@dataclass
class IEEE802154Frame:
    stream_id: str
    center_freq_hz: int
    sample_rate_sps: int
    channel: int | None
    phy_length: int
    psdu: bytes
    confidence: float
    chip_offset: int
    chip_samples: int
    symbol_errors: list[int] = field(default_factory=list)
    mac: IEEE802154MacFields | None = None
    timestamp: float = field(default_factory=time.time)

    @property
    def hex(self) -> str:
        return self.psdu.hex()

    def to_json(self) -> str:
        payload = {
            "protocol": "ieee802154",
            "stream_id": self.stream_id,
            "center_freq_hz": self.center_freq_hz,
            "sample_rate_sps": self.sample_rate_sps,
            "channel": self.channel,
            "phy_length": self.phy_length,
            "psdu_hex": self.psdu.hex(),
            "confidence": round(self.confidence, 4),
            "chip_offset": self.chip_offset,
            "chip_samples": self.chip_samples,
            "symbol_errors": self.symbol_errors,
            "timestamp": round(self.timestamp, 6),
        }
        if self.mac is not None:
            payload["mac"] = self.mac.to_dict()
        return json.dumps(payload, sort_keys=True)


class BurstDetector:
    def __init__(
        self,
        sample_rate_sps: int,
        center_freq_hz: int,
        stream_id: str,
        pre_roll_ms: float = 0.2,
        open_factor: float = 3.5,
        close_factor: float = 1.8,
        min_burst_ms: float = 0.3,
        max_burst_ms: float = 25.0,
    ) -> None:
        self.sample_rate_sps = int(sample_rate_sps)
        self.center_freq_hz = int(center_freq_hz)
        self.stream_id = stream_id
        self.pre_roll_samples = max(16, int(self.sample_rate_sps * (pre_roll_ms / 1000.0)))
        self.open_factor = float(open_factor)
        self.close_factor = float(close_factor)
        self.min_burst_samples = max(128, int(self.sample_rate_sps * (min_burst_ms / 1000.0)))
        self.max_burst_samples = max(self.min_burst_samples * 2, int(self.sample_rate_sps * (max_burst_ms / 1000.0)))
        self.noise_floor = 0.01
        self._pre_roll = np.empty(0, dtype=np.complex64)
        self._burst_parts: list[np.ndarray] = []
        self._burst_start_time = 0.0
        self._peak = 0.0
        self._average_energy = 0.0
        self._in_burst = False

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
        envelope = moving_average(np.abs(iq).astype(np.float32, copy=False), window=max(3, self.sample_rate_sps // 1_000_000))
        quiet = float(np.percentile(envelope, 20.0))
        self.noise_floor = (self.noise_floor * 0.98) + (quiet * 0.02)
        open_threshold = max(0.02, self.noise_floor * self.open_factor)
        close_threshold = max(0.015, self.noise_floor * self.close_factor)
        active = envelope > open_threshold
        active_close = envelope > close_threshold
        silent_needed = max(16, self.sample_rate_sps // 50_000)
        segment_start = 0
        index = 0

        while index < iq.size:
            if not self._in_burst:
                active_indices = np.flatnonzero(active[index:])
                if active_indices.size == 0:
                    break
                start_index = index + int(active_indices[0])
                current_prefix_start = max(0, start_index - self.pre_roll_samples)
                current_prefix = iq[current_prefix_start:start_index].astype(np.complex64, copy=False)
                prior_needed = self.pre_roll_samples - current_prefix.size
                if prior_needed > 0:
                    prior_prefix = self._pre_roll[-prior_needed:]
                    prefix = np.concatenate((prior_prefix, current_prefix)).astype(np.complex64, copy=False)
                else:
                    prefix = current_prefix
                self._burst_parts = [prefix]
                self._burst_start_time = now + (start_index / self.sample_rate_sps) - (len(prefix) / self.sample_rate_sps)
                self._peak = 0.0
                self._average_energy = 0.0
                self._in_burst = True
                segment_start = start_index
                index = start_index

            if self._in_burst:
                close_after = None
                silent_run = 0
                cursor = index
                while cursor < iq.size:
                    if active_close[cursor]:
                        silent_run = 0
                    else:
                        silent_run += 1
                        if silent_run >= silent_needed:
                            close_after = cursor - silent_run + 1
                            break
                    cursor += 1

                chunk_end = close_after if close_after is not None else iq.size
                if chunk_end > segment_start:
                    segment = iq[segment_start:chunk_end].astype(np.complex64, copy=False)
                    segment_env = envelope[segment_start:chunk_end]
                    self._burst_parts.append(segment)
                    if segment_env.size:
                        self._peak = max(self._peak, float(np.max(segment_env)))
                        segment_mean = float(np.mean(segment_env))
                        if self._average_energy <= 0.0:
                            self._average_energy = segment_mean
                        else:
                            self._average_energy = (self._average_energy * 0.9) + (segment_mean * 0.1)

                total_samples = sum(part.size for part in self._burst_parts)
                if total_samples >= self.max_burst_samples:
                    emitted.extend(self.flush())
                    if close_after is None:
                        break
                    index = close_after + silent_needed
                    segment_start = index
                    continue

                if close_after is None:
                    break

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
                            ended_at=now + (close_after / self.sample_rate_sps),
                        )
                    )
                self._in_burst = False
                self._burst_parts = []
                self._peak = 0.0
                self._average_energy = 0.0
                index = close_after + silent_needed
                segment_start = index

        self._pre_roll = np.concatenate((self._pre_roll, iq.astype(np.complex64, copy=False)))
        if self._pre_roll.size > self.pre_roll_samples:
            self._pre_roll = self._pre_roll[-self.pre_roll_samples :]
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


class IEEE802154Decoder:
    def __init__(
        self,
        symbol_error_limit: int = 10,
        phase_search_steps: int = 8,
        max_chip_hamming_distance: int = 12,
        pattern_chip_error_limit: int = 120,
        start_search_symbols: int = 24,
        frequency_search_hz: tuple[int, ...] = (0, -100_000, -50_000, 50_000, 100_000),
        waveform_pattern_corr_min: float = 0.35,
    ) -> None:
        self.symbol_error_limit = int(symbol_error_limit)
        self.phase_search_steps = max(1, int(phase_search_steps))
        self.max_chip_hamming_distance = max(0, int(max_chip_hamming_distance))
        self.pattern_chip_error_limit = max(0, int(pattern_chip_error_limit))
        self.start_search_symbols = max(4, int(start_search_symbols))
        self.frequency_search_hz = tuple(int(entry) for entry in frequency_search_hz) or (0,)
        self.waveform_pattern_corr_min = float(waveform_pattern_corr_min)
        self.last_diagnostics = IEEE802154DecodeDiagnostics()

    def decode(self, burst: Burst) -> IEEE802154Frame | None:
        chip_samples = max(1, int(round(burst.sample_rate_sps / CHIP_RATE_SPS)))
        best: IEEE802154Frame | None = None
        best_diag = IEEE802154DecodeDiagnostics()
        iq = self._prepare_iq(burst.iq)
        for frequency_offset_hz in self.frequency_search_hz:
            corrected = self._correct_frequency_offset(iq, burst.sample_rate_sps, float(frequency_offset_hz))
            for phase_index in range(1):
                phase = 0.0
                rotated = self._rotate_iq(corrected, phase)
                for symbol_table_name, symbol_table in SYMBOL_TABLES:
                    for sfd_symbols in SFD_SYMBOL_PATTERNS:
                        start_samples = self._find_pattern_start_samples(
                            iq=rotated,
                            chip_samples=chip_samples,
                            symbol_table=symbol_table,
                            sfd_symbols=sfd_symbols,
                        )
                        for sample_start in start_samples:
                            refined_iq, refined_cfo_hz, refined_sample_start = self._refine_pattern_frequency(
                                iq=rotated,
                                sample_rate_sps=burst.sample_rate_sps,
                                coarse_frequency_offset_hz=float(frequency_offset_hz),
                                sample_start=sample_start,
                                chip_samples=chip_samples,
                                symbol_table=symbol_table,
                                sfd_symbols=sfd_symbols,
                            )
                            frame, diagnostics = self._decode_waveform_stream(
                                iq=refined_iq,
                                burst=burst,
                                chip_samples=chip_samples,
                                sample_start=refined_sample_start,
                                phase_index=phase_index,
                                phase=phase,
                                frequency_offset_hz=refined_cfo_hz,
                                symbol_table_name=symbol_table_name,
                                symbol_table=symbol_table,
                                sfd_symbols=sfd_symbols,
                            )
                            if self._diagnostics_better(diagnostics, best_diag):
                                best_diag = diagnostics
                            if frame is not None and (best is None or frame.confidence > best.confidence):
                                best = frame
                                best_diag = diagnostics
                                if diagnostics.crc_ok:
                                    self.last_diagnostics = diagnostics
                                    return frame
                            chip_offset = max(0, int(refined_sample_start % chip_samples))
                            candidate_chip_start = max(0, int(refined_sample_start // chip_samples))
                            for chip_slicer_name, chips in self._slice_chip_variants(refined_iq, chip_samples=chip_samples, offset=chip_offset):
                                frame, diagnostics = self._decode_chip_stream(
                                    chips=chips,
                                    burst=burst,
                                    chip_samples=chip_samples,
                                    chip_offset=chip_offset,
                                    phase_index=phase_index,
                                    phase=phase,
                                    chip_slicer_name=chip_slicer_name,
                                    frequency_offset_hz=refined_cfo_hz,
                                    symbol_table_name=symbol_table_name,
                                    symbol_table=symbol_table,
                                    sfd_symbols=sfd_symbols,
                                    candidate_chip_starts=[candidate_chip_start],
                                )
                                if self._diagnostics_better(diagnostics, best_diag):
                                    best_diag = diagnostics
                                if frame is None:
                                    continue
                                if best is None or frame.confidence > best.confidence:
                                    best = frame
                                    best_diag = diagnostics
                                    if diagnostics.crc_ok:
                                        self.last_diagnostics = diagnostics
                                        return frame
        self.last_diagnostics = best_diag
        return best

    def _refine_pattern_frequency(
        self,
        iq: np.ndarray,
        sample_rate_sps: int,
        coarse_frequency_offset_hz: float,
        sample_start: int,
        chip_samples: int,
        symbol_table: dict[int, str],
        sfd_symbols: tuple[int, int],
    ) -> tuple[np.ndarray, float, int]:
        pattern_ref = synthesize_pattern_iq(
            PREAMBLE_SYMBOLS + list(sfd_symbols),
            chip_samples=chip_samples,
            symbol_table=symbol_table,
            amplitude=1.0,
            half_sine=True,
        )
        if pattern_ref.size == 0 or sample_start < 0 or sample_start + pattern_ref.size > iq.size:
            return iq, coarse_frequency_offset_hz, sample_start
        best_score = float("-inf")
        best_iq = iq
        best_cfo_hz = float(coarse_frequency_offset_hz)
        best_start = int(sample_start)
        for residual_hz in (-25_000.0, -12_500.0, 0.0, 12_500.0, 25_000.0):
            candidate_cfo_hz = float(coarse_frequency_offset_hz) + residual_hz
            if abs(residual_hz) <= 1e-9:
                candidate_iq = iq
            else:
                candidate_iq = self._correct_frequency_offset(iq, sample_rate_sps, residual_hz)
            starts = self._find_pattern_start_samples_from_reference(candidate_iq, pattern_ref, sample_start, chip_samples)
            for candidate_start in starts:
                if candidate_start < 0 or candidate_start + pattern_ref.size > candidate_iq.size:
                    continue
                obs = candidate_iq[candidate_start : candidate_start + pattern_ref.size]
                correlation = np.vdot(pattern_ref, obs)
                score = float(
                    np.abs(correlation)
                    / ((float(np.linalg.norm(pattern_ref)) + 1e-9) * (float(np.linalg.norm(obs)) + 1e-9))
                )
                if score > best_score:
                    best_score = score
                    best_iq = candidate_iq
                    best_cfo_hz = candidate_cfo_hz
                    best_start = int(candidate_start)
        preamble_cfo_hz = self._estimate_preamble_cfo(
            iq=best_iq,
            sample_rate_sps=sample_rate_sps,
            sample_start=best_start,
            chip_samples=chip_samples,
            symbol_table=symbol_table,
        )
        if abs(preamble_cfo_hz) >= 1.0:
            refined_iq = self._correct_frequency_offset(best_iq, sample_rate_sps, preamble_cfo_hz)
            refined_starts = self._find_pattern_start_samples_from_reference(
                refined_iq,
                pattern_ref,
                best_start,
                chip_samples,
            )
            refined_start = refined_starts[0] if refined_starts else best_start
            if refined_start >= 0 and refined_start + pattern_ref.size <= refined_iq.size:
                obs = refined_iq[refined_start : refined_start + pattern_ref.size]
                score = float(
                    np.abs(np.vdot(pattern_ref, obs))
                    / ((float(np.linalg.norm(pattern_ref)) + 1e-9) * (float(np.linalg.norm(obs)) + 1e-9))
                )
                if score >= best_score:
                    best_iq = refined_iq
                    best_cfo_hz += preamble_cfo_hz
                    best_start = int(refined_start)
        return best_iq, best_cfo_hz, best_start

    def _estimate_preamble_cfo(
        self,
        iq: np.ndarray,
        sample_rate_sps: int,
        sample_start: int,
        chip_samples: int,
        symbol_table: dict[int, str],
    ) -> float:
        symbol_ref = synthesize_pattern_iq(
            [0],
            chip_samples=chip_samples,
            symbol_table=symbol_table,
            amplitude=1.0,
            half_sine=True,
        )
        symbol_stride = SYMBOL_CHIPS * chip_samples
        phases: list[float] = []
        times: list[float] = []
        for symbol_index in range(len(PREAMBLE_SYMBOLS)):
            lo = int(sample_start) + (symbol_index * symbol_stride)
            hi = lo + symbol_ref.size
            if lo < 0 or hi > iq.size:
                break
            correlation = np.vdot(symbol_ref, iq[lo:hi])
            if abs(correlation) <= 1e-9:
                continue
            phases.append(float(np.angle(correlation)))
            times.append(float(symbol_index * symbol_stride) / float(sample_rate_sps))
        if len(phases) < 4:
            return 0.0
        unwrapped = np.unwrap(np.asarray(phases, dtype=np.float64))
        slope, _intercept = np.polyfit(np.asarray(times, dtype=np.float64), unwrapped, 1)
        estimate_hz = float(slope / (2.0 * np.pi))
        return max(-100_000.0, min(100_000.0, estimate_hz))

    def _find_pattern_start_samples_from_reference(
        self,
        iq: np.ndarray,
        pattern_ref: np.ndarray,
        around_sample: int,
        chip_samples: int,
    ) -> list[int]:
        if pattern_ref.size == 0 or iq.size < pattern_ref.size:
            return [max(0, int(around_sample))]
        window = max(chip_samples * 8, chip_samples)
        search_lo = max(0, int(around_sample) - window)
        search_hi = min(iq.size, int(around_sample) + pattern_ref.size + window)
        search = iq[search_lo:search_hi]
        if search.size < pattern_ref.size:
            return [max(0, int(around_sample))]
        scores = np.abs(np.convolve(search, np.conj(pattern_ref[::-1]), mode="valid"))
        if scores.size == 0:
            return [max(0, int(around_sample))]
        best = int(np.argmax(scores))
        return [search_lo + best]

    def _find_pattern_start_samples(
        self,
        iq: np.ndarray,
        chip_samples: int,
        symbol_table: dict[int, str],
        sfd_symbols: tuple[int, int],
    ) -> list[int]:
        if iq.size == 0:
            return [0]
        ref = synthesize_pattern_iq(
            symbols=PREAMBLE_SYMBOLS + list(sfd_symbols),
            chip_samples=chip_samples,
            symbol_table=symbol_table,
            amplitude=1.0,
        )
        if ref.size == 0:
            return [0]
        max_search_samples = max(chip_samples, self.start_search_symbols * SYMBOL_CHIPS * chip_samples)
        search_len = min(iq.size, ref.size + max_search_samples)
        if search_len < ref.size:
            return [0]
        search = iq[:search_len]
        scores = np.abs(np.convolve(search, np.conj(ref[::-1]), mode="valid"))
        if scores.size == 0:
            return [0]
        if scores.size == 1:
            return [0]
        best = int(np.argmax(scores))
        starts = [best]
        masked = scores.copy()
        lo = max(0, best - chip_samples)
        hi = min(masked.size, best + chip_samples + 1)
        masked[lo:hi] = 0.0
        second = int(np.argmax(masked))
        if masked[second] > 0.85 * scores[best]:
            starts.append(second)
        return starts

    def _diagnostics_better(self, current: IEEE802154DecodeDiagnostics, previous: IEEE802154DecodeDiagnostics) -> bool:
        current_key = (
            current.pattern_correlation,
            current.sfd_hits,
            -current.best_pattern_mismatches,
            -(current.best_pattern_error_sum),
            -(current.best_total_error_sum if current.best_total_error_sum is not None else 1_000_000),
            -current.best_symbol_distance,
            current.symbols_available,
        )
        previous_key = (
            previous.pattern_correlation,
            previous.sfd_hits,
            -previous.best_pattern_mismatches,
            -(previous.best_pattern_error_sum),
            -(previous.best_total_error_sum if previous.best_total_error_sum is not None else 1_000_000),
            -previous.best_symbol_distance,
            previous.symbols_available,
        )
        return current_key > previous_key

    def _prepare_iq(self, iq: np.ndarray) -> np.ndarray:
        if iq.size == 0:
            return iq
        centered = iq.astype(np.complex64, copy=False) - np.complex64(np.mean(iq))
        power = float(np.sqrt(np.mean(np.abs(centered) ** 2)))
        if power <= 1e-9:
            return centered
        return (centered / power).astype(np.complex64, copy=False)

    def _rotate_iq(self, iq: np.ndarray, phase: float) -> np.ndarray:
        if abs(phase) <= 1e-12:
            return iq
        return (iq * np.complex64(np.exp(-1j * phase))).astype(np.complex64, copy=False)

    def _correct_frequency_offset(self, iq: np.ndarray, sample_rate_sps: int, frequency_offset_hz: float) -> np.ndarray:
        if abs(frequency_offset_hz) <= 1e-9 or iq.size == 0:
            return iq
        sample_index = np.arange(iq.size, dtype=np.float32)
        rotation = np.exp(
            np.complex64(-1j)
            * np.float32(2.0 * np.pi * float(frequency_offset_hz) / float(sample_rate_sps))
            * sample_index
        )
        return (iq * rotation.astype(np.complex64, copy=False)).astype(np.complex64, copy=False)

    def _slice_chips(self, iq: np.ndarray, chip_samples: int, offset: int) -> list[float]:
        cursor = max(0, int(offset))
        if chip_samples <= 0 or cursor >= iq.size:
            return []
        n_chips = (iq.size - cursor) // chip_samples
        if n_chips <= 0:
            return []
        windows = iq[cursor : cursor + (n_chips * chip_samples)].reshape(n_chips, chip_samples)
        real_means = np.mean(windows.real, axis=1)
        imag_means = np.mean(windows.imag, axis=1)
        use_real = (np.arange(n_chips) % 2) == 0
        return np.where(use_real, real_means, imag_means).astype(np.float32, copy=False).tolist()

    def _slice_chips_oqpsk(self, iq: np.ndarray, chip_samples: int, offset: int, center_sample: bool) -> list[float]:
        chips: list[float] = []
        half_chip = max(1, chip_samples // 2)
        cursor = int(offset)
        while True:
            if center_sample:
                i_index = cursor + max(0, chip_samples // 2)
                if i_index >= iq.size:
                    break
                chips.append(float(iq[i_index].real))
                q_index = cursor + half_chip + max(0, chip_samples // 2)
                if q_index >= iq.size:
                    break
                chips.append(float(iq[q_index].imag))
            else:
                if cursor + chip_samples > iq.size:
                    break
                i_window = iq[cursor : cursor + chip_samples]
                chips.append(float(np.mean(i_window.real)))
                q_start = cursor + half_chip
                if q_start + chip_samples > iq.size:
                    break
                q_window = iq[q_start : q_start + chip_samples]
                chips.append(float(np.mean(q_window.imag)))
            cursor += chip_samples
        return chips

    def _slice_chip_variants(self, iq: np.ndarray, chip_samples: int, offset: int) -> list[tuple[str, list[float]]]:
        return [
            ("alternating_mean", self._slice_chips(iq, chip_samples=chip_samples, offset=offset)),
            ("msk_discriminator", self._slice_chips_msk(iq, chip_samples=chip_samples, offset=offset)),
        ]

    def _slice_chips_msk(self, iq: np.ndarray, chip_samples: int, offset: int) -> list[float]:
        if iq.size < 2:
            return []
        shifted = iq[1:] * np.conj(iq[:-1])
        discriminator = np.imag(shifted).astype(np.float32, copy=False)
        cursor = max(0, int(offset))
        if chip_samples <= 0 or cursor >= discriminator.size:
            return []
        n_chips = (discriminator.size - cursor) // chip_samples
        if n_chips <= 0:
            return []
        windows = discriminator[cursor : cursor + (n_chips * chip_samples)].reshape(n_chips, chip_samples)
        return np.mean(windows, axis=1).astype(np.float32, copy=False).tolist()

    def _decode_chip_stream(
        self,
        chips: list[float],
        burst: Burst,
        chip_samples: int,
        chip_offset: int,
        phase_index: int,
        phase: float,
        chip_slicer_name: str,
        frequency_offset_hz: float,
        symbol_table_name: str,
        symbol_table: dict[int, str],
        sfd_symbols: tuple[int, int],
        candidate_chip_starts: list[int],
    ) -> tuple[IEEE802154Frame | None, IEEE802154DecodeDiagnostics]:
        best_diag = IEEE802154DecodeDiagnostics(
            chip_slicer=chip_slicer_name,
            frequency_offset_hz=frequency_offset_hz,
            symbol_table_name=symbol_table_name,
            phase_index=phase_index,
            phase_degrees=float(np.degrees(phase)),
            chip_offset=chip_offset,
        )
        pattern = PREAMBLE_SYMBOLS + list(sfd_symbols)
        pattern_chip_blocks = [symbol_table[symbol] for symbol in pattern]
        pattern_chip_string = "".join(pattern_chip_blocks)
        pattern_chip_length = len(pattern_chip_string)
        for start in candidate_chip_starts:
            if start < 0 or start + pattern_chip_length + (2 * SYMBOL_CHIPS) > len(chips):
                continue
            best_diag.candidates_checked += 1
            chip_window = chips[start : start + pattern_chip_length]
            chip_string = "".join("1" if value >= 0.0 else "0" for value in chip_window)
            pattern_error_sum = sum(1 for left, right in zip(chip_string, pattern_chip_string) if left != right)
            pattern_mismatches = 0
            for block_index, expected_block in enumerate(pattern_chip_blocks):
                lo = block_index * SYMBOL_CHIPS
                hi = lo + SYMBOL_CHIPS
                if chip_string[lo:hi] != expected_block:
                    pattern_mismatches += 1
            best_diag.symbols_available = max(best_diag.symbols_available, (len(chips) - start) // SYMBOL_CHIPS)
            if (
                pattern_mismatches < best_diag.best_pattern_mismatches
                or (
                    pattern_mismatches == best_diag.best_pattern_mismatches
                    and pattern_error_sum < best_diag.best_pattern_error_sum
                )
            ):
                best_diag.best_pattern_mismatches = pattern_mismatches
                best_diag.best_pattern_error_sum = pattern_error_sum
                best_diag.symbol_phase = start % SYMBOL_CHIPS
            if pattern_error_sum > self.pattern_chip_error_limit:
                continue
            best_diag.sfd_hits += 1

            decision_start = start + pattern_chip_length
            length_symbols: list[SymbolDecision] = []
            for block_index in range(2):
                lo = decision_start + (block_index * SYMBOL_CHIPS)
                hi = lo + SYMBOL_CHIPS
                symbol = self._nearest_symbol(chips[lo:hi], symbol_table=symbol_table)
                if symbol is None:
                    length_symbols = []
                    break
                if symbol.hamming_distance < best_diag.best_symbol_distance:
                    best_diag.best_symbol_distance = symbol.hamming_distance
                length_symbols.append(symbol)
            if len(length_symbols) != 2:
                continue

            length_candidates = (
                nibbles_to_bytes([length_symbols[0].symbol, length_symbols[1].symbol])[0],
                nibbles_to_bytes([length_symbols[1].symbol, length_symbols[0].symbol])[0],
            )
            for length_byte in length_candidates:
                best_diag.best_length = length_byte
                if length_byte > MAX_PHY_PSDU:
                    continue

                psdu_symbol_count = length_byte * 2
                payload_symbols: list[SymbolDecision] = []
                payload_start = decision_start + (2 * SYMBOL_CHIPS)
                payload_end = payload_start + (psdu_symbol_count * SYMBOL_CHIPS)
                if payload_end > len(chips):
                    continue

                for block_index in range(psdu_symbol_count):
                    lo = payload_start + (block_index * SYMBOL_CHIPS)
                    hi = lo + SYMBOL_CHIPS
                    symbol = self._nearest_symbol(chips[lo:hi], symbol_table=symbol_table)
                    if symbol is None:
                        payload_symbols = []
                        break
                    if symbol.hamming_distance < best_diag.best_symbol_distance:
                        best_diag.best_symbol_distance = symbol.hamming_distance
                    payload_symbols.append(symbol)
                if len(payload_symbols) != psdu_symbol_count:
                    continue

                symbol_errors = [entry.hamming_distance for entry in length_symbols + payload_symbols]
                best_diag.best_total_error_sum = sum(symbol_errors)
                average_symbol_error = float(sum(symbol_errors)) / float(len(symbol_errors))
                if average_symbol_error > float(self.symbol_error_limit):
                    preview_nibbles = [entry.symbol for entry in payload_symbols[: min(8, len(payload_symbols))]]
                    if preview_nibbles and len(preview_nibbles) % 2 == 0:
                        best_diag.best_payload_preview_hex = nibbles_to_bytes(preview_nibbles).hex()
                    continue

                psdu = nibbles_to_bytes([entry.symbol for entry in payload_symbols])
                best_diag.best_payload_preview_hex = psdu[: min(8, len(psdu))].hex()
                best_diag.crc_ok = ieee802154_fcs_ok(psdu)
                if length_byte < 5 or (not best_diag.crc_ok and sum(symbol_errors) != 0):
                    continue
                confidence = max(0.0, 1.0 - (sum(symbol_errors) / float(len(symbol_errors) * SYMBOL_CHIPS)))
                return (
                    IEEE802154Frame(
                        stream_id=burst.stream_id,
                        center_freq_hz=burst.center_freq_hz,
                        sample_rate_sps=burst.sample_rate_sps,
                        channel=channel_from_center_freq(burst.center_freq_hz),
                        phy_length=length_byte,
                        psdu=psdu,
                        confidence=confidence,
                        chip_offset=chip_offset + ((start % SYMBOL_CHIPS) * chip_samples),
                        chip_samples=chip_samples,
                        symbol_errors=symbol_errors,
                        mac=parse_mac_fields(psdu),
                        timestamp=burst.ended_at,
                    ),
                    best_diag,
                )
        return None, best_diag

    def _decode_waveform_stream(
        self,
        iq: np.ndarray,
        burst: Burst,
        chip_samples: int,
        sample_start: int,
        phase_index: int,
        phase: float,
        frequency_offset_hz: float,
        symbol_table_name: str,
        symbol_table: dict[int, str],
        sfd_symbols: tuple[int, int],
    ) -> tuple[IEEE802154Frame | None, IEEE802154DecodeDiagnostics]:
        best_diag = IEEE802154DecodeDiagnostics(
            chip_slicer="waveform_match",
            frequency_offset_hz=frequency_offset_hz,
            symbol_table_name=symbol_table_name,
            phase_index=phase_index,
            phase_degrees=float(np.degrees(phase)),
            chip_offset=max(0, int(sample_start % chip_samples)),
            symbol_phase=0,
        )
        pattern_symbols = PREAMBLE_SYMBOLS + list(sfd_symbols)
        pattern_ref = synthesize_pattern_iq(pattern_symbols, chip_samples=chip_samples, symbol_table=symbol_table, amplitude=1.0, half_sine=True)
        if pattern_ref.size == 0:
            return None, best_diag
        pattern_end = int(sample_start) + (len(pattern_symbols) * SYMBOL_CHIPS * chip_samples)
        symbol_samples = SYMBOL_CHIPS * chip_samples
        if sample_start < 0 or sample_start + pattern_ref.size > iq.size or pattern_end + (2 * symbol_samples) + chip_samples > iq.size:
            return None, best_diag
        pattern_obs = iq[int(sample_start) : int(sample_start) + pattern_ref.size]
        pattern_dot = np.vdot(pattern_ref, pattern_obs)
        pattern_corr = float(
            np.abs(pattern_dot)
            / ((float(np.linalg.norm(pattern_ref)) + 1e-9) * (float(np.linalg.norm(pattern_obs)) + 1e-9))
        )
        phase_correction = float(np.angle(pattern_dot))
        aligned_iq = self._rotate_iq(iq, phase_correction)
        best_diag.phase_degrees = float(np.degrees(phase_correction))
        best_diag.candidates_checked = 1
        best_diag.pattern_correlation = pattern_corr
        best_diag.sfd_hits = 1 if pattern_corr >= self.waveform_pattern_corr_min else 0
        best_diag.best_pattern_mismatches = 0 if pattern_corr >= self.waveform_pattern_corr_min else len(pattern_symbols)
        best_diag.best_pattern_error_sum = int(round((1.0 - max(-1.0, min(1.0, pattern_corr))) * 1000.0))
        best_diag.symbols_available = max(0, (iq.size - sample_start) // symbol_samples)
        if pattern_corr < self.waveform_pattern_corr_min:
            return None, best_diag
        matched_chips = self._slice_chips_half_sine(
            aligned_iq,
            chip_samples=chip_samples,
            sample_start=int(sample_start),
        )
        matched_frame, matched_diag = self._decode_chip_stream(
            chips=matched_chips,
            burst=burst,
            chip_samples=chip_samples,
            chip_offset=max(0, int(sample_start % chip_samples)),
            phase_index=phase_index,
            phase=phase_correction,
            chip_slicer_name="half_sine_matched",
            frequency_offset_hz=frequency_offset_hz,
            symbol_table_name=symbol_table_name,
            symbol_table=symbol_table,
            sfd_symbols=sfd_symbols,
            candidate_chip_starts=[0],
        )
        matched_diag.pattern_correlation = pattern_corr
        matched_diag.phase_degrees = float(np.degrees(phase_correction))
        if matched_frame is not None:
            return matched_frame, matched_diag
        if self._diagnostics_better(matched_diag, best_diag):
            best_diag = matched_diag
        for decision_adjust in range(-chip_samples, chip_samples + 1):
            decision_start = pattern_end + decision_adjust
            if decision_start < 0 or decision_start + (2 * symbol_samples) > iq.size:
                continue
            length_symbols: list[SymbolDecision] = []
            for block_index in range(2):
                lo = decision_start + (block_index * symbol_samples)
                hi = lo + symbol_samples + chip_samples
                symbol = self._nearest_symbol_iq(
                    aligned_iq[lo:hi],
                    chip_samples=chip_samples,
                    symbol_table_name=symbol_table_name,
                    symbol_table=symbol_table,
                )
                if symbol is None:
                    length_symbols = []
                    break
                length_symbols.append(symbol)
            if len(length_symbols) != 2:
                continue

            length_candidates = (
                nibbles_to_bytes([length_symbols[0].symbol, length_symbols[1].symbol])[0],
                nibbles_to_bytes([length_symbols[1].symbol, length_symbols[0].symbol])[0],
            )
            for length_byte in length_candidates:
                best_diag.best_length = length_byte
                if length_byte > MAX_PHY_PSDU:
                    continue
                psdu_symbol_count = length_byte * 2
                payload_symbols: list[SymbolDecision] = []
                payload_start = decision_start + (2 * symbol_samples)
                payload_end = payload_start + (psdu_symbol_count * symbol_samples) + chip_samples
                if payload_end > iq.size:
                    continue
                for block_index in range(psdu_symbol_count):
                    lo = payload_start + (block_index * symbol_samples)
                    hi = lo + symbol_samples + chip_samples
                    symbol = self._nearest_symbol_iq(
                        aligned_iq[lo:hi],
                        chip_samples=chip_samples,
                        symbol_table_name=symbol_table_name,
                        symbol_table=symbol_table,
                    )
                    if symbol is None:
                        payload_symbols = []
                        break
                    payload_symbols.append(symbol)
                if len(payload_symbols) != psdu_symbol_count:
                    continue

                symbol_errors = [entry.hamming_distance for entry in length_symbols + payload_symbols]
                best_diag.best_total_error_sum = sum(symbol_errors)
                psdu = nibbles_to_bytes([entry.symbol for entry in payload_symbols])
                best_diag.best_payload_preview_hex = psdu[: min(8, len(psdu))].hex()
                best_diag.crc_ok = ieee802154_fcs_ok(psdu)
                if length_byte < 5 or (not best_diag.crc_ok and sum(symbol_errors) != 0):
                    continue
                confidence = max(0.0, 1.0 - (sum(symbol_errors) / float(max(1, len(symbol_errors) * SYMBOL_CHIPS))))
                return (
                    IEEE802154Frame(
                        stream_id=burst.stream_id,
                        center_freq_hz=burst.center_freq_hz,
                        sample_rate_sps=burst.sample_rate_sps,
                        channel=channel_from_center_freq(burst.center_freq_hz),
                        phy_length=length_byte,
                        psdu=psdu,
                        confidence=confidence,
                        chip_offset=max(0, int(sample_start)),
                        chip_samples=chip_samples,
                        symbol_errors=symbol_errors,
                        mac=parse_mac_fields(psdu),
                        timestamp=burst.ended_at,
                    ),
                    best_diag,
                )
        return None, best_diag

    def _slice_chips_half_sine(
        self,
        iq: np.ndarray,
        chip_samples: int,
        sample_start: int,
    ) -> list[float]:
        pulse_samples = 2 * chip_samples
        if chip_samples <= 0 or sample_start < 0 or sample_start + pulse_samples > iq.size:
            return []
        pulse = np.sin(
            np.pi * ((np.arange(pulse_samples, dtype=np.float32) + 0.5) / float(pulse_samples))
        ).astype(np.float32)
        chips: list[float] = []
        chip_index = 0
        cursor = int(sample_start)
        while cursor + pulse_samples <= iq.size:
            window = iq[cursor : cursor + pulse_samples]
            branch = window.real if chip_index % 2 == 0 else window.imag
            chips.append(float(np.dot(branch, pulse)))
            chip_index += 1
            cursor += chip_samples
        return chips

    def _nearest_symbol_iq(
        self,
        iq: np.ndarray,
        chip_samples: int,
        symbol_table_name: str,
        symbol_table: dict[int, str],
    ) -> SymbolDecision | None:
        expected_samples = (SYMBOL_CHIPS + 1) * chip_samples
        if iq.size != expected_samples:
            return None
        if _cdecode is not None:
            accelerated = _cdecode.nearest_symbol_iq(np.ascontiguousarray(iq, dtype=np.complex64), int(chip_samples), symbol_table_name)
            if accelerated is not None:
                symbol, pseudo_distance, chip_string = accelerated
                return SymbolDecision(symbol=int(symbol), hamming_distance=int(pseudo_distance), chips=str(chip_string))
        best_symbol = -1
        best_score = float("-inf")
        best_chip_string = ""
        iq_norm = float(np.linalg.norm(iq)) + 1e-9
        for symbol, reference in symbol_table.items():
            ref_iq = synthesize_pattern_iq([symbol], chip_samples=chip_samples, symbol_table=symbol_table, amplitude=1.0, half_sine=True)
            ref_norm = float(np.linalg.norm(ref_iq)) + 1e-9
            score = float(np.real(np.vdot(ref_iq, iq)) / (ref_norm * iq_norm))
            if score > best_score:
                best_score = score
                best_symbol = int(symbol)
                best_chip_string = reference
        if best_symbol < 0:
            return None
        quality = max(-1.0, min(1.0, best_score))
        pseudo_distance = int(round((1.0 - max(0.0, quality)) * float(SYMBOL_CHIPS)))
        return SymbolDecision(symbol=best_symbol, hamming_distance=pseudo_distance, chips=best_chip_string)

    def _nearest_symbol(self, chips: list[float], symbol_table: dict[int, str]) -> SymbolDecision | None:
        if len(chips) != SYMBOL_CHIPS:
            return None
        chip_string = "".join("1" if value >= 0.0 else "0" for value in chips)
        for exact_symbol, exact_chips in symbol_table.items():
            if chip_string == exact_chips:
                return SymbolDecision(symbol=exact_symbol, hamming_distance=0, chips=chip_string)

        best_symbol = -1
        best_distance = SYMBOL_CHIPS + 1
        best_correlation = float("-inf")
        for symbol, reference in symbol_table.items():
            distance = sum(1 for left, right in zip(chip_string, reference) if left != right)
            reference_signs = [1.0 if bit == "1" else -1.0 for bit in reference]
            correlation = sum(float(sample) * ref for sample, ref in zip(chips, reference_signs))
            if distance < best_distance or (distance == best_distance and correlation > best_correlation):
                best_symbol = symbol
                best_distance = distance
                best_correlation = correlation
        if best_symbol < 0 or best_distance > self.max_chip_hamming_distance:
            return None
        return SymbolDecision(symbol=best_symbol, hamming_distance=best_distance, chips=chip_string)


def channel_to_center_freq(channel: int) -> int:
    if not 11 <= int(channel) <= 26:
        raise ValueError("802.15.4 2.4 GHz channels must be in the range 11-26")
    return 2_405_000_000 + ((int(channel) - 11) * 5_000_000)


def channel_from_center_freq(center_freq_hz: int) -> int | None:
    offset = int(center_freq_hz) - 2_405_000_000
    if offset % 5_000_000:
        return None
    channel = 11 + (offset // 5_000_000)
    if 11 <= channel <= 26:
        return channel
    return None


def bytes_to_nibbles(payload: bytes) -> list[int]:
    nibbles: list[int] = []
    for value in payload:
        nibbles.append(value & 0x0F)
        nibbles.append((value >> 4) & 0x0F)
    return nibbles


def nibbles_to_bytes(nibbles: list[int]) -> bytes:
    if len(nibbles) % 2:
        raise ValueError("Nibble list must contain an even number of entries")
    out = bytearray()
    for index in range(0, len(nibbles), 2):
        low = int(nibbles[index]) & 0x0F
        high = int(nibbles[index + 1]) & 0x0F
        out.append(low | (high << 4))
    return bytes(out)


def build_phy_nibbles(psdu: bytes) -> list[int]:
    phr = bytes([len(psdu)])
    return PREAMBLE_SYMBOLS + list(SFD_SYMBOL_PATTERNS[0]) + bytes_to_nibbles(phr) + bytes_to_nibbles(psdu)


def phy_nibbles_to_chips(nibbles: list[int]) -> list[int]:
    chips: list[int] = []
    for nibble in nibbles:
        chips.extend(1 if bit == "1" else 0 for bit in _PRIMARY_SYMBOL_TABLE[int(nibble) & 0x0F])
    return chips


def synthesize_pattern_iq(
    symbols: list[int],
    chip_samples: int,
    symbol_table: dict[int, str],
    amplitude: float = 1.0,
    half_sine: bool = True,
) -> np.ndarray:
    chip_values: list[int] = []
    for symbol in symbols:
        chip_values.extend(1 if bit == "1" else 0 for bit in symbol_table[int(symbol) & 0x0F])
    if not chip_values or chip_samples <= 0:
        return np.empty(0, dtype=np.complex64)
    pulse_samples = 2 * chip_samples
    pulse = np.ones(pulse_samples, dtype=np.float32)
    if half_sine:
        pulse = np.sin(
            np.pi * ((np.arange(pulse_samples, dtype=np.float32) + 0.5) / float(pulse_samples))
        ).astype(np.float32)
    body = np.zeros((len(chip_values) + 1) * chip_samples, dtype=np.complex64)
    for index, chip in enumerate(chip_values):
        value = (float(amplitude) if chip else -float(amplitude)) * pulse
        start = index * chip_samples
        end = start + pulse_samples
        if index % 2 == 0:
            body[start:end] += value.astype(np.complex64)
        else:
            body[start:end] += (1j * value).astype(np.complex64)
    return body


def synthesize_iq_for_psdu(
    psdu: bytes,
    sample_rate_sps: int = 4_000_000,
    amplitude: float = 0.9,
    noise_std: float = 0.0,
    lead_samples: int = 128,
    tail_samples: int = 128,
) -> np.ndarray:
    chip_samples = max(1, int(round(sample_rate_sps / CHIP_RATE_SPS)))
    body = synthesize_pattern_iq(
        symbols=build_phy_nibbles(psdu),
        chip_samples=chip_samples,
        symbol_table=_PRIMARY_SYMBOL_TABLE,
        amplitude=amplitude,
        half_sine=True,
    )
    if noise_std > 0.0:
        noise = (np.random.normal(0.0, noise_std, body.size) + 1j * np.random.normal(0.0, noise_std, body.size)).astype(np.complex64)
        body = body + noise
    return np.concatenate(
        [
            np.zeros(lead_samples, dtype=np.complex64),
            body,
            np.zeros(tail_samples, dtype=np.complex64),
        ]
    )


def parse_mac_fields(psdu: bytes) -> IEEE802154MacFields | None:
    if len(psdu) < 3:
        return None

    fcf = int.from_bytes(psdu[0:2], byteorder="little", signed=False)
    frame_type = _frame_type_name(fcf & 0x0007)
    security_enabled = bool(fcf & 0x0008)
    frame_pending = bool(fcf & 0x0010)
    ack_request = bool(fcf & 0x0020)
    pan_id_compression = bool(fcf & 0x0040)
    destination_mode_raw = (fcf >> 10) & 0x3
    frame_version = (fcf >> 12) & 0x3
    source_mode_raw = (fcf >> 14) & 0x3

    cursor = 3
    destination_pan_id: int | None = None
    destination_address: str | None = None
    source_pan_id: int | None = None
    source_address: str | None = None

    if destination_mode_raw != 0:
        destination_pan_id, cursor = _read_pan_id(psdu, cursor)
        destination_address, cursor = _read_address(psdu, cursor, destination_mode_raw)
    if source_mode_raw != 0:
        if pan_id_compression and destination_pan_id is not None:
            source_pan_id = destination_pan_id
        else:
            source_pan_id, cursor = _read_pan_id(psdu, cursor)
        source_address, cursor = _read_address(psdu, cursor, source_mode_raw)

    fcs_hex = None
    payload_end = len(psdu)
    if len(psdu) >= cursor + 2:
        fcs_hex = psdu[-2:].hex()
        payload_end = len(psdu) - 2
    if payload_end < cursor:
        payload_end = cursor
    payload_hex = psdu[cursor:payload_end].hex()

    return IEEE802154MacFields(
        frame_type=frame_type,
        frame_version=frame_version,
        sequence_number=psdu[2],
        ack_request=ack_request,
        frame_pending=frame_pending,
        security_enabled=security_enabled,
        pan_id_compression=pan_id_compression,
        destination_pan_id=destination_pan_id,
        destination_address_mode=_address_mode_name(destination_mode_raw),
        destination_address=destination_address,
        source_pan_id=source_pan_id,
        source_address_mode=_address_mode_name(source_mode_raw),
        source_address=source_address,
        payload_hex=payload_hex,
        fcs_hex=fcs_hex,
    )


def ieee802154_fcs_ok(psdu: bytes) -> bool:
    if len(psdu) < 3:
        return False
    payload = psdu[:-2]
    observed_fcs = int.from_bytes(psdu[-2:], byteorder="little", signed=False)
    return crc16_kermit(payload) == observed_fcs


def crc16_kermit(data: bytes) -> int:
    crc = 0x0000
    for value in data:
        crc ^= int(value) & 0xFF
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0x8408
            else:
                crc >>= 1
    return crc & 0xFFFF


def _frame_type_name(value: int) -> str:
    return {
        0: "beacon",
        1: "data",
        2: "ack",
        3: "mac-command",
    }.get(int(value), f"reserved-{int(value)}")


def _address_mode_name(value: int) -> str:
    return {
        0: "none",
        2: "short",
        3: "extended",
    }.get(int(value), f"reserved-{int(value)}")


def _read_pan_id(data: bytes, cursor: int) -> tuple[int | None, int]:
    if cursor + 2 > len(data):
        return None, len(data)
    return int.from_bytes(data[cursor : cursor + 2], byteorder="little", signed=False), cursor + 2


def _read_address(data: bytes, cursor: int, mode: int) -> tuple[str | None, int]:
    if mode == 2:
        if cursor + 2 > len(data):
            return None, len(data)
        return f"0x{int.from_bytes(data[cursor:cursor + 2], byteorder='little', signed=False):04x}", cursor + 2
    if mode == 3:
        if cursor + 8 > len(data):
            return None, len(data)
        return f"0x{int.from_bytes(data[cursor:cursor + 8], byteorder='little', signed=False):016x}", cursor + 8
    return None, cursor
