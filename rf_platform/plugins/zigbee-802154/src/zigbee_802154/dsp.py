from __future__ import annotations

import numpy as np


def iq_i8_to_complex(raw: bytes) -> np.ndarray:
    if not raw:
        return np.empty(0, dtype=np.complex64)
    data = np.frombuffer(raw, dtype=np.int8)
    if data.size < 2:
        return np.empty(0, dtype=np.complex64)
    if data.size % 2:
        data = data[:-1]
    i = data[0::2].astype(np.float32) / 128.0
    q = data[1::2].astype(np.float32) / 128.0
    return (i + (1j * q)).astype(np.complex64)


def moving_average(values: np.ndarray, window: int) -> np.ndarray:
    if values.size == 0:
        return values.astype(np.float32)
    window = max(1, int(window))
    if window == 1:
        return values.astype(np.float32, copy=False)
    kernel = np.ones(window, dtype=np.float32) / float(window)
    return np.convolve(values.astype(np.float32, copy=False), kernel, mode="same")
