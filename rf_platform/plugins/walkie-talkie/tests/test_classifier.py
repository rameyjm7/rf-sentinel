from __future__ import annotations

import numpy as np

from walkie_talkie.dsp import classify_walkie_signal


def _synth_nbfm_voice_like(sample_rate_sps: int = 1_000_000, duration_s: float = 1.0) -> np.ndarray:
    t = np.arange(int(sample_rate_sps * duration_s), dtype=np.float32) / float(sample_rate_sps)
    voice = (
        0.65 * np.sin(2.0 * np.pi * 440.0 * t)
        + 0.35 * np.sin(2.0 * np.pi * 920.0 * t)
        + 0.20 * np.sin(2.0 * np.pi * 1_650.0 * t)
    ).astype(np.float32)
    voice *= (0.55 + 0.45 * np.sin(2.0 * np.pi * 2.7 * t)).astype(np.float32)
    phase = np.cumsum((2.0 * np.pi * 2_500.0 / float(sample_rate_sps)) * voice).astype(np.float32)
    return np.exp(1j * phase).astype(np.complex64)


def test_voice_like_signal_classifies_as_nbfm_audio_family() -> None:
    iq = _synth_nbfm_voice_like()
    result, audio = classify_walkie_signal(iq, 1_000_000)
    assert result.label in {"narrowband_fm_voice", "narrowband_fm_audio"}
    assert result.modulation == "NBFM"
    assert audio.size > 100


def test_empty_signal_classifies_as_no_signal() -> None:
    iq = np.zeros(4096, dtype=np.complex64)
    result, _ = classify_walkie_signal(iq, 1_000_000)
    assert result.label == "no_signal"
