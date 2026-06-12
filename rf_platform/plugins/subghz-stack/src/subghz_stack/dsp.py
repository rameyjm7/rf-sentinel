from __future__ import annotations

from collections import deque

import numpy as np


def iq_i8_to_complex(raw: bytes) -> np.ndarray:
    if not raw:
        return np.empty(0, dtype=np.complex64)
    if len(raw) % 2:
        raw = raw[:-1]
    if not raw:
        return np.empty(0, dtype=np.complex64)
    iq = np.frombuffer(raw, dtype=np.int8).astype(np.float32)
    if iq.size < 2:
        return np.empty(0, dtype=np.complex64)
    i = iq[0::2] / 128.0
    q = iq[1::2] / 128.0
    return (i + 1j * q).astype(np.complex64, copy=False)


def run_length_encode(mask: np.ndarray) -> list[tuple[int, int]]:
    if mask.size == 0:
        return []
    values = mask.astype(np.uint8, copy=False)
    changes = np.flatnonzero(np.diff(values)) + 1
    boundaries = np.concatenate(([0], changes, [values.size]))
    runs: list[tuple[int, int]] = []
    for start, end in zip(boundaries[:-1], boundaries[1:], strict=False):
        runs.append((int(values[start]), int(end - start)))
    return runs


def collapse_short_runs(runs: list[tuple[int, int]], min_run_samples: int) -> list[tuple[int, int]]:
    if min_run_samples <= 1 or len(runs) < 3:
        return runs
    merged = runs[:]
    changed = True
    while changed and len(merged) >= 3:
        changed = False
        output: list[tuple[int, int]] = []
        index = 0
        while index < len(merged):
            value, length = merged[index]
            if length < min_run_samples and 0 < index < len(merged) - 1:
                prev_value, prev_length = output[-1]
                next_value, next_length = merged[index + 1]
                if prev_value == next_value:
                    output[-1] = (prev_value, prev_length + length + next_length)
                    index += 2
                    changed = True
                    continue
            output.append((value, length))
            index += 1
        merged = output
    return merged


def moving_average(x: np.ndarray, window: int) -> np.ndarray:
    if x.size == 0:
        return x.astype(np.float32, copy=False)
    window = max(1, int(window))
    if window == 1 or x.size < window:
        return x.astype(np.float32, copy=False)
    kernel = np.ones(window, dtype=np.float32) / float(window)
    return np.convolve(x.astype(np.float32, copy=False), kernel, mode="same").astype(np.float32)


def rms(x: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    data = x.astype(np.float32, copy=False)
    return float(np.sqrt(np.mean(data * data)))


def quantize_units(lengths: list[int], unit_samples: float) -> list[int]:
    if unit_samples <= 0:
        return []
    units: list[int] = []
    for length in lengths:
        units.append(max(1, int(round(length / unit_samples))))
    return units


def estimate_unit_from_lengths(lengths: list[int]) -> float:
    if not lengths:
        return 0.0
    sorted_lengths = np.asarray(sorted(lengths), dtype=np.float32)
    lower = sorted_lengths[: max(1, int(np.ceil(sorted_lengths.size * 0.4)))]
    value = float(np.percentile(lower, 25.0))
    return max(1.0, value)


class RollingBuffer:
    def __init__(self, maxlen: int) -> None:
        self.maxlen = max(1, int(maxlen))
        self._buf = deque[complex]()

    def extend(self, values: np.ndarray) -> None:
        for value in values:
            self._buf.append(complex(value))
        while len(self._buf) > self.maxlen:
            self._buf.popleft()

    def array(self) -> np.ndarray:
        if not self._buf:
            return np.empty(0, dtype=np.complex64)
        return np.asarray(list(self._buf), dtype=np.complex64)
