from __future__ import annotations

import numpy as np

from am_broadcast.cli import BAND_PRESETS, _resolve_scan_plan
from am_broadcast.dsp import channel_grid, cs16_to_complex, measure_am_channel


def test_channel_grid_uses_inclusive_khz_range() -> None:
    assert channel_grid(530, 560, 10) == [530_000, 540_000, 550_000, 560_000]


def test_band_presets_cover_vlf_lf_mf() -> None:
    assert BAND_PRESETS["vlf"].start_khz == 3
    assert BAND_PRESETS["lf"].start_khz == 30
    assert BAND_PRESETS["mf"].stop_khz == 3000
    assert BAND_PRESETS["1khz-1mhz"].start_khz == 1
    assert BAND_PRESETS["1khz-1mhz"].stop_khz == 1000


def test_cs16_to_complex_reads_interleaved_iq() -> None:
    raw = np.array([100, -50, 25, 75], dtype=np.int16)
    iq = cs16_to_complex(raw)
    assert iq.dtype == np.complex64
    assert np.allclose(iq, np.array([100 - 50j, 25 + 75j], dtype=np.complex64))


def test_measure_am_channel_detects_modulated_envelope() -> None:
    sample_rate = 250_000
    t = np.arange(sample_rate // 5, dtype=np.float32) / float(sample_rate)
    envelope = 9000.0 * (1.0 + 0.45 * np.sin(2.0 * np.pi * 1000.0 * t))
    carrier = envelope.astype(np.complex64)
    quiet = np.full_like(carrier, 9000.0 + 0j)

    modulated = measure_am_channel(carrier, sample_rate)
    unmodulated = measure_am_channel(quiet, sample_rate)

    assert modulated.samples == carrier.size
    assert modulated.power_dbfs > -20.0
    assert modulated.modulation_pct > 25.0
    assert modulated.audio_dbfs > unmodulated.audio_dbfs + 20.0


def test_measure_am_channel_scores_offset_carrier() -> None:
    sample_rate = 250_000
    offset_hz = -25_000.0
    t = np.arange(sample_rate // 5, dtype=np.float32) / float(sample_rate)
    carrier = 8000.0 * np.exp(2j * np.pi * offset_hz * t)
    noise = np.random.default_rng(1).normal(0.0, 80.0, carrier.size) + 1j * np.random.default_rng(2).normal(0.0, 80.0, carrier.size)

    metrics = measure_am_channel((carrier + noise).astype(np.complex64), sample_rate, carrier_offset_hz=offset_hz)

    assert metrics.carrier_dbfs > -20.0
    assert metrics.carrier_snr_db > 25.0
