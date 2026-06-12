from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from .decoder import Burst, BurstDetector, DecodeResult, ProtocolDecoder, build_decoder
from .dsp import iq_i8_to_complex, moving_average


@dataclass(frozen=True)
class WidebandBinPlan:
    center_freq_hz: int
    freq_offset_hz: float
    output_sample_rate_sps: int
    decimation: int


@dataclass
class WidebandBinStats:
    center_freq_hz: int
    candidate_count: int = 0
    packet_count: int = 0
    rejected_count: int = 0
    last_checksum_hint: str | None = None
    last_bits_len: int | None = None


class WidebandBinRuntime:
    def __init__(self, plan: WidebandBinPlan, protocol: str = "tpms") -> None:
        self.plan = plan
        self.protocol = str(protocol).strip().lower()
        self.detector = BurstDetector(
            sample_rate_sps=plan.output_sample_rate_sps,
            center_freq_hz=plan.center_freq_hz,
            stream_id=f"bin-{plan.center_freq_hz}",
            min_burst_ms=1.0,
            max_burst_ms=120.0,
        )
        self.decoder: ProtocolDecoder = build_decoder(self.protocol)
        self.stats = WidebandBinStats(center_freq_hz=plan.center_freq_hz)
        self.sample_cursor = 0

    def downconvert_and_decimate(self, iq: np.ndarray, input_sample_rate_sps: int) -> np.ndarray:
        if iq.size == 0:
            return np.empty(0, dtype=np.complex64)
        sample_positions = np.arange(iq.size, dtype=np.float32) + float(self.sample_cursor)
        phase = (-2.0j * np.pi * float(self.plan.freq_offset_hz) / float(input_sample_rate_sps)) * sample_positions
        osc = np.exp(phase).astype(np.complex64, copy=False)
        mixed = iq * osc
        self.sample_cursor += int(iq.size)
        window = max(1, int(self.plan.decimation * 2))
        real = moving_average(mixed.real.astype(np.float32, copy=False), window=window)
        imag = moving_average(mixed.imag.astype(np.float32, copy=False), window=window)
        filtered = (real + (1j * imag)).astype(np.complex64, copy=False)
        return filtered[:: self.plan.decimation].astype(np.complex64, copy=False)


def build_bin_plans(
    *,
    center_freq_hz: int,
    sample_rate_sps: int,
    band_start_hz: int,
    band_end_hz: int,
    bin_width_hz: int,
    channel_rate_sps: int,
) -> list[WidebandBinPlan]:
    if band_end_hz <= band_start_hz:
        raise ValueError("band_end_hz must be greater than band_start_hz")
    if bin_width_hz <= 0:
        raise ValueError("bin_width_hz must be > 0")
    if channel_rate_sps <= 0:
        raise ValueError("channel_rate_sps must be > 0")
    covered_span_hz = band_end_hz - band_start_hz
    if covered_span_hz > sample_rate_sps:
        raise ValueError("Requested band span is wider than the SDR sample rate")
    nyquist_edge_hz = sample_rate_sps / 2.0
    if abs(float(band_start_hz - center_freq_hz)) > nyquist_edge_hz or abs(float(band_end_hz - center_freq_hz)) > nyquist_edge_hz:
        raise ValueError("Requested band is not fully contained inside the chosen center frequency / sample rate window")
    decimation = max(1, int(round(sample_rate_sps / float(channel_rate_sps))))
    output_sample_rate_sps = max(1, int(round(sample_rate_sps / decimation)))
    centers: list[int] = []
    current = int(band_start_hz + (bin_width_hz // 2))
    while current < band_end_hz:
        centers.append(current)
        current += int(bin_width_hz)
    if not centers:
        centers = [int((band_start_hz + band_end_hz) // 2)]
    plans: list[WidebandBinPlan] = []
    for bin_center_hz in centers:
        plans.append(
            WidebandBinPlan(
                center_freq_hz=int(bin_center_hz),
                freq_offset_hz=float(bin_center_hz - center_freq_hz),
                output_sample_rate_sps=output_sample_rate_sps,
                decimation=decimation,
            )
        )
    return plans


def process_wideband_chunk(
    *,
    raw_chunk: bytes,
    input_sample_rate_sps: int,
    runtimes: list[WidebandBinRuntime],
) -> list[tuple[WidebandBinRuntime, Burst, DecodeResult | None]]:
    iq = iq_i8_to_complex(raw_chunk)
    if iq.size == 0:
        return []
    out: list[tuple[WidebandBinRuntime, Burst, DecodeResult | None]] = []
    for runtime in runtimes:
        decimated = runtime.downconvert_and_decimate(iq, input_sample_rate_sps=input_sample_rate_sps)
        bursts = runtime.detector.ingest_iq(decimated)
        for burst in bursts:
            result = runtime.decoder.decode(burst)
            out.append((runtime, burst, result))
    return out


def flush_wideband_runtimes(runtimes: list[WidebandBinRuntime]) -> list[tuple[WidebandBinRuntime, Burst, DecodeResult | None]]:
    out: list[tuple[WidebandBinRuntime, Burst, DecodeResult | None]] = []
    for runtime in runtimes:
        for burst in runtime.detector.flush():
            result = runtime.decoder.decode(burst)
            out.append((runtime, burst, result))
    return out
