from __future__ import annotations

import numpy as np

from ..decoder import Burst, DecodeCandidate, DecodeResult, ProtocolDecoder
from ..dsp import collapse_short_runs, estimate_unit_from_lengths, moving_average, quantize_units, run_length_encode
from .tpms_families import parse_tpms_family


class TpmsDecoder(ProtocolDecoder):
    protocol_name = "tpms"

    def __init__(self) -> None:
        super().__init__()
        self.min_burst_ms = 2.0
        self.max_burst_ms = 120.0
        self.max_segment_burst_ms = 20.0
        self.min_symbol_samples = 8
        self.max_symbol_samples = 128
        self.min_bits = 32
        self.max_bits = 128
        self.preferred_min_bits = 48
        self.preferred_max_bits = 96

    def decode(self, burst: Burst) -> DecodeResult | None:
        if burst.iq.size == 0:
            self.last_debug = None
            return None
        burst_ms = burst.duration_seconds * 1000.0
        if burst_ms < self.min_burst_ms or burst_ms > self.max_burst_ms:
            self.last_debug = self._build_debug_info(burst, 0.0, [], [], f"burst_ms_out_of_range:{burst_ms:.2f}")
            return None
        envelope = moving_average(np.abs(burst.iq).astype(np.float32, copy=False), window=max(5, burst.sample_rate_sps // 400_000))
        if envelope.size < 32:
            self.last_debug = self._build_debug_info(burst, 0.0, [], [], "envelope_too_short")
            return None
        threshold = self._adaptive_threshold(envelope)
        raw_runs = run_length_encode(envelope > threshold)
        collapsed_runs = collapse_short_runs(raw_runs, min_run_samples=max(4, burst.sample_rate_sps // 250_000))
        self.last_debug = self._build_debug_info(burst, threshold, raw_runs, collapsed_runs, reject_reason="")
        raw_runs = collapsed_runs
        if len(raw_runs) < 4:
            self.last_debug.reject_reason = "too_few_runs"
            return None
        candidate = self._decode_segment_or_runs(raw_runs, burst.sample_rate_sps)
        if candidate is None:
            if self.last_debug is not None:
                self.last_debug.reject_reason = "no_candidate_after_run_decode"
            return None
        if not self._candidate_looks_tpms_like(candidate):
            if self.last_debug is not None:
                self.last_debug.reject_reason = "candidate_not_tpms_like"
            return None
        family_match = parse_tpms_family(candidate.bits)
        confidence = max(0.0, min(1.0, 1.0 - candidate.score))
        if family_match is not None:
            confidence = max(confidence, min(0.99, confidence + 0.12))
        self.last_debug = None
        return DecodeResult(
            burst=burst,
            modulation="ook",
            confidence=confidence,
            candidate=candidate,
            raw_runs=raw_runs,
            protocol_variant=family_match.family if family_match is not None else None,
            decoded_fields=family_match.fields if family_match is not None else {},
        )

    def _adaptive_threshold(self, envelope: np.ndarray) -> float:
        low = float(np.percentile(envelope, 20.0))
        high = float(np.percentile(envelope, 92.0))
        if high <= low:
            return low * 1.5
        return low + ((high - low) * 0.4)

    def _decode_width_runs(self, runs: list[tuple[int, int]]) -> DecodeCandidate | None:
        if not runs:
            return None
        payload_runs = runs[:]
        while payload_runs and payload_runs[0][0] == 0 and payload_runs[0][1] > 1:
            payload_runs.pop(0)
        while payload_runs and payload_runs[-1][0] == 0 and payload_runs[-1][1] > 1:
            payload_runs.pop()
        if len(payload_runs) < 3:
            return None

        high_lengths = [length for value, length in payload_runs if value == 1]
        low_lengths = [length for value, length in payload_runs if value == 0]
        high_unit = estimate_unit_from_lengths(high_lengths)
        low_unit = estimate_unit_from_lengths(low_lengths)

        candidates: list[DecodeCandidate] = []
        for strategy, source_runs, unit_samples in (
            ("pulse", [length for value, length in payload_runs if value == 1], high_unit),
            ("gap", [length for value, length in payload_runs if value == 0], low_unit),
        ):
            if unit_samples <= 0 or len(source_runs) < 2:
                continue
            if unit_samples < self.min_symbol_samples or unit_samples > self.max_symbol_samples:
                continue
            bit_units = quantize_units(source_runs, unit_samples)
            filtered_units = [unit for unit in bit_units if unit <= 8]
            if len(filtered_units) < 2:
                continue
            short_unit = max(1, min(filtered_units))
            sync_limit = max(short_unit * 3, short_unit + 2)
            filtered_units = [unit for unit in filtered_units if unit <= sync_limit]
            if len(filtered_units) < 2:
                continue
            bits = "".join("1" if unit > short_unit else "0" for unit in filtered_units)
            if not bits or len(bits) < self.min_bits or len(bits) > self.max_bits:
                continue
            candidates.append(
                DecodeCandidate(
                    strategy=strategy,
                    bits=bits,
                    hex_string=self._bits_to_hex(bits),
                    unit_samples=float(unit_samples),
                    score=self._score_units(filtered_units, short_unit),
                    runs=filtered_units,
                )
            )
        if not candidates:
            return None
        candidates.sort(key=lambda candidate: (candidate.score, -len(candidate.bits)))
        return candidates[0]

    def _decode_segment_or_runs(self, runs: list[tuple[int, int]], sample_rate_sps: int) -> DecodeCandidate | None:
        direct = self._decode_width_runs(runs)
        if direct is not None:
            return direct
        inverted = self._decode_width_runs([(1 - value, length) for value, length in runs])
        if inverted is not None:
            return inverted
        segment_candidates: list[DecodeCandidate] = []
        for segment in self._split_runs_into_segments(runs, sample_rate_sps):
            candidate = self._decode_width_runs(segment)
            if candidate is None:
                candidate = self._decode_width_runs([(1 - value, length) for value, length in segment])
            if candidate is None:
                candidate = self._decode_manchester_runs(segment)
            if candidate is not None:
                segment_candidates.append(candidate)
        if not segment_candidates:
            return None
        segment_candidates.sort(key=lambda item: (item.score, -len(item.bits)))
        return segment_candidates[0]

    def _split_runs_into_segments(self, runs: list[tuple[int, int]], sample_rate_sps: int) -> list[list[tuple[int, int]]]:
        low_lengths = [length for value, length in runs if value == 0]
        if len(low_lengths) < 8:
            return []
        median_low = float(np.median(np.asarray(low_lengths, dtype=np.float32)))
        split_threshold = max(int(median_low * 10.0), int(sample_rate_sps * 0.0004))
        segments: list[list[tuple[int, int]]] = []
        current: list[tuple[int, int]] = []
        for value, length in runs:
            if value == 0 and length >= split_threshold:
                if current:
                    segments.append(current)
                    current = []
                continue
            current.append((value, length))
        if current:
            segments.append(current)
        filtered: list[list[tuple[int, int]]] = []
        for segment in segments:
            segment_samples = sum(length for _, length in segment)
            segment_ms = (segment_samples / float(sample_rate_sps)) * 1000.0
            if self.min_burst_ms <= segment_ms <= self.max_segment_burst_ms:
                filtered.append(segment)
        return filtered

    def _decode_manchester_runs(self, runs: list[tuple[int, int]]) -> DecodeCandidate | None:
        if len(runs) < 12:
            return None
        lengths = [length for _, length in runs]
        half_unit = estimate_unit_from_lengths(lengths)
        if half_unit < self.min_symbol_samples or half_unit > self.max_symbol_samples:
            return None
        expanded: list[int] = []
        for value, length in runs:
            repeat = max(1, int(round(length / half_unit)))
            if repeat > 6:
                return None
            expanded.extend([value] * repeat)
        if len(expanded) < self.min_bits * 2:
            return None
        candidates: list[DecodeCandidate] = []
        for zero_pattern, one_pattern, label in (([0, 1], [1, 0], "manchester-01"), ([1, 0], [0, 1], "manchester-10")):
            bits: list[str] = []
            invalid_pairs = 0
            for index in range(0, len(expanded) - 1, 2):
                pair = expanded[index : index + 2]
                if pair == zero_pattern:
                    bits.append("0")
                elif pair == one_pattern:
                    bits.append("1")
                else:
                    invalid_pairs += 1
            bit_string = "".join(bits)
            if not bit_string or len(bit_string) < self.min_bits or len(bit_string) > self.max_bits:
                continue
            score = invalid_pairs / max(1, len(bits))
            if score > 0.2:
                continue
            candidates.append(
                DecodeCandidate(
                    strategy=label,
                    bits=bit_string,
                    hex_string=self._bits_to_hex(bit_string),
                    unit_samples=float(half_unit),
                    score=float(score),
                    runs=[],
                )
            )
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item.score, -len(item.bits)))
        return candidates[0]

    def _candidate_looks_tpms_like(self, candidate: DecodeCandidate) -> bool:
        bit_length = len(candidate.bits)
        unit_samples = float(candidate.unit_samples)
        if bit_length < self.min_bits or bit_length > self.max_bits:
            return False
        if unit_samples < self.min_symbol_samples or unit_samples > self.max_symbol_samples:
            return False
        if candidate.strategy not in {"pulse", "gap", "manchester-01", "manchester-10"}:
            return False
        if not self._bit_balance_ok(candidate.bits):
            return False
        if self.preferred_min_bits <= bit_length <= self.preferred_max_bits:
            return True
        return candidate.score <= 0.18

    def _bit_balance_ok(self, bits: str) -> bool:
        ones = bits.count("1")
        ratio = ones / float(len(bits)) if bits else 0.0
        return 0.12 <= ratio <= 0.88
