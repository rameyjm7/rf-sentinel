from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .decoder import Burst, BurstDetector, IEEE802154Decoder, IEEE802154Frame, channel_to_center_freq
from .dsp import iq_i8_to_complex

try:
    from . import _cdecode
except ImportError:
    _cdecode = None


CHANNEL_OCCUPIED_BW_HZ = 2_000_000
CHANNEL_FILTER_CUTOFF_HZ = 1_650_000
CHANNEL_FILTER_TAPS = 129
NATIVE_CHANNELIZER_TAPS = 9
FIRST_CHANNEL = 11
LAST_CHANNEL = 26
DEFAULT_CHANNEL_RATE_SPS = 4_000_000


@dataclass(frozen=True)
class WidebandWindowPlan:
    index: int
    center_freq_hz: int
    sample_rate_sps: int
    channels: tuple[int, ...]


@dataclass(frozen=True)
class WidebandChannelPlan:
    channel: int
    center_freq_hz: int
    freq_offset_hz: float
    output_sample_rate_sps: int
    decimation: int


@dataclass(frozen=True)
class WidebandDetectorConfig:
    pre_roll_ms: float = 0.2
    open_factor: float = 6.0
    close_factor: float = 3.0
    min_burst_ms: float = 0.05
    max_burst_ms: float = 5.0


class WidebandChannelRuntime:
    def __init__(
        self,
        plan: WidebandChannelPlan,
        *,
        detector_config: WidebandDetectorConfig | None = None,
        decoder: IEEE802154Decoder | None = None,
    ) -> None:
        self.plan = plan
        detector_config = detector_config or WidebandDetectorConfig()
        self.detector = BurstDetector(
            sample_rate_sps=plan.output_sample_rate_sps,
            center_freq_hz=plan.center_freq_hz,
            stream_id=f"ch-{plan.channel}",
            pre_roll_ms=float(detector_config.pre_roll_ms),
            open_factor=float(detector_config.open_factor),
            close_factor=float(detector_config.close_factor),
            min_burst_ms=float(detector_config.min_burst_ms),
            max_burst_ms=float(detector_config.max_burst_ms),
        )
        self.decoder = decoder or IEEE802154Decoder()
        self.sample_cursor = 0
        self._filter_taps_by_rate: dict[int, np.ndarray] = {}

    def downconvert_and_decimate(self, iq: np.ndarray, input_sample_rate_sps: int) -> np.ndarray:
        if iq.size == 0:
            return np.empty(0, dtype=np.complex64)
        if _cdecode is not None and hasattr(_cdecode, "channelize_boxcar"):
            sample_cursor = int(self.sample_cursor)
            self.sample_cursor += int(iq.size)
            raw = _cdecode.channelize_boxcar(
                np.ascontiguousarray(iq, dtype=np.complex64),
                float(self.plan.freq_offset_hz),
                float(input_sample_rate_sps),
                sample_cursor,
                int(self.plan.decimation),
                int(NATIVE_CHANNELIZER_TAPS),
            )
            if raw is not None:
                return np.frombuffer(raw, dtype=np.complex64)
        sample_positions = np.arange(iq.size, dtype=np.float32) + float(self.sample_cursor)
        phase = (-2.0j * np.pi * float(self.plan.freq_offset_hz) / float(input_sample_rate_sps)) * sample_positions
        osc = np.exp(phase).astype(np.complex64, copy=False)
        mixed = iq * osc
        self.sample_cursor += int(iq.size)
        filtered = self._lowpass_channel(mixed, input_sample_rate_sps=input_sample_rate_sps)
        return filtered[:: self.plan.decimation].astype(np.complex64, copy=False)

    def _lowpass_channel(self, iq: np.ndarray, input_sample_rate_sps: int) -> np.ndarray:
        sample_rate_sps = int(input_sample_rate_sps)
        taps = self._filter_taps_by_rate.get(sample_rate_sps)
        if taps is None:
            cutoff_hz = min(CHANNEL_FILTER_CUTOFF_HZ, 0.45 * float(self.plan.output_sample_rate_sps))
            taps = design_lowpass_taps(
                sample_rate_sps=sample_rate_sps,
                cutoff_hz=cutoff_hz,
                num_taps=CHANNEL_FILTER_TAPS,
            )
            self._filter_taps_by_rate[sample_rate_sps] = taps
        return np.convolve(iq.astype(np.complex64, copy=False), taps, mode="same").astype(np.complex64, copy=False)


def design_lowpass_taps(sample_rate_sps: int, cutoff_hz: float, num_taps: int = CHANNEL_FILTER_TAPS) -> np.ndarray:
    sample_rate_sps = int(sample_rate_sps)
    num_taps = max(3, int(num_taps))
    if num_taps % 2 == 0:
        num_taps += 1
    cutoff = max(1.0, min(float(cutoff_hz), 0.49 * float(sample_rate_sps)))
    normalized_cutoff = cutoff / float(sample_rate_sps)
    midpoint = (num_taps - 1) / 2.0
    positions = np.arange(num_taps, dtype=np.float32) - np.float32(midpoint)
    taps = (2.0 * normalized_cutoff) * np.sinc((2.0 * normalized_cutoff) * positions)
    taps *= np.hamming(num_taps).astype(np.float32)
    tap_sum = float(np.sum(taps))
    if abs(tap_sum) > 1e-12:
        taps /= tap_sum
    return taps.astype(np.float32, copy=False)


def all_802154_channels() -> list[int]:
    return list(range(FIRST_CHANNEL, LAST_CHANNEL + 1))


def build_wideband_window_plans(sample_rate_sps: int) -> list[WidebandWindowPlan]:
    sample_rate_sps = int(sample_rate_sps)
    if sample_rate_sps < DEFAULT_CHANNEL_RATE_SPS:
        raise ValueError("sample_rate_sps must be at least 4 MHz for the current wideband receiver")

    channels = all_802154_channels()
    max_channels = _max_channels_for_span(sample_rate_sps)
    if max_channels >= len(channels):
        return [_window_plan_from_channels(0, channels, sample_rate_sps)]

    starts: list[int] = []
    start = 0
    last_start = max(0, len(channels) - max_channels)
    while start < len(channels):
        if start + max_channels >= len(channels):
            start = last_start
        if starts and start <= starts[-1]:
            break
        starts.append(start)
        start += max_channels

    return [
        _window_plan_from_channels(index, channels[start : start + max_channels], sample_rate_sps)
        for index, start in enumerate(starts)
    ]


def _max_channels_for_span(sample_rate_sps: int) -> int:
    centers = [channel_to_center_freq(channel) for channel in all_802154_channels()]
    max_channels = 1
    for count in range(1, len(centers) + 1):
        span_hz = ((count - 1) * 5_000_000) + CHANNEL_OCCUPIED_BW_HZ
        if span_hz <= int(sample_rate_sps):
            max_channels = count
    return max(1, max_channels)


def _window_plan_from_channels(index: int, channels: list[int], sample_rate_sps: int) -> WidebandWindowPlan:
    centers = [channel_to_center_freq(channel) for channel in channels]
    center_freq_hz = int(round((min(centers) + max(centers)) / 2.0))
    return WidebandWindowPlan(
        index=index,
        center_freq_hz=center_freq_hz,
        sample_rate_sps=int(sample_rate_sps),
        channels=tuple(channels),
    )


def build_channel_plans(window: WidebandWindowPlan, channel_rate_sps: int = DEFAULT_CHANNEL_RATE_SPS) -> list[WidebandChannelPlan]:
    sample_rate_sps = int(window.sample_rate_sps)
    channel_rate_sps = int(channel_rate_sps)
    decimation = max(1, int(round(sample_rate_sps / float(channel_rate_sps))))
    output_sample_rate_sps = max(1, int(round(sample_rate_sps / decimation)))
    plans: list[WidebandChannelPlan] = []
    for channel in window.channels:
        center_freq_hz = channel_to_center_freq(channel)
        plans.append(
            WidebandChannelPlan(
                channel=channel,
                center_freq_hz=center_freq_hz,
                freq_offset_hz=float(center_freq_hz - window.center_freq_hz),
                output_sample_rate_sps=output_sample_rate_sps,
                decimation=decimation,
            )
        )
    return plans


def create_runtimes(
    window: WidebandWindowPlan,
    channel_rate_sps: int = DEFAULT_CHANNEL_RATE_SPS,
    detector_config: WidebandDetectorConfig | None = None,
    decoder_factory=None,
) -> list[WidebandChannelRuntime]:
    runtimes: list[WidebandChannelRuntime] = []
    for plan in build_channel_plans(window, channel_rate_sps=channel_rate_sps):
        decoder = decoder_factory() if decoder_factory is not None else None
        runtimes.append(
            WidebandChannelRuntime(
                plan,
                detector_config=detector_config,
                decoder=decoder,
            )
        )
    return runtimes


def process_wideband_chunk(
    *,
    raw_chunk: bytes,
    input_sample_rate_sps: int,
    runtimes: list[WidebandChannelRuntime],
) -> list[tuple[WidebandChannelRuntime, Burst, IEEE802154Frame | None]]:
    return [
        (runtime, burst, runtime.decoder.decode(burst))
        for runtime, burst in detect_wideband_bursts(
            raw_chunk=raw_chunk,
            input_sample_rate_sps=input_sample_rate_sps,
            runtimes=runtimes,
        )
    ]


def detect_wideband_bursts(
    *,
    raw_chunk: bytes,
    input_sample_rate_sps: int,
    runtimes: list[WidebandChannelRuntime],
) -> list[tuple[WidebandChannelRuntime, Burst]]:
    iq = iq_i8_to_complex(raw_chunk)
    if iq.size == 0:
        return []
    out: list[tuple[WidebandChannelRuntime, Burst]] = []
    for runtime in runtimes:
        decimated = runtime.downconvert_and_decimate(iq, input_sample_rate_sps=input_sample_rate_sps)
        for burst in runtime.detector.ingest_iq(decimated):
            out.append((runtime, burst))
    return out


def flush_wideband_runtimes(runtimes: list[WidebandChannelRuntime]) -> list[tuple[WidebandChannelRuntime, Burst, IEEE802154Frame | None]]:
    return [
        (runtime, burst, runtime.decoder.decode(burst))
        for runtime, burst in flush_wideband_bursts(runtimes)
    ]


def flush_wideband_bursts(runtimes: list[WidebandChannelRuntime]) -> list[tuple[WidebandChannelRuntime, Burst]]:
    out: list[tuple[WidebandChannelRuntime, Burst]] = []
    for runtime in runtimes:
        for burst in runtime.detector.flush():
            out.append((runtime, burst))
    return out
