from __future__ import annotations

import numpy as np

from tpms_stack.decoder import Burst, TpmsDecoder


def _synth_ook_burst(bits: str, unit_samples: int = 200, sync_units: int = 4) -> Burst:
    segments: list[np.ndarray] = [np.ones(sync_units * unit_samples, dtype=np.complex64)]
    for bit in bits:
        high_units = 1 if bit == "0" else 2
        segments.append(np.zeros(unit_samples, dtype=np.complex64))
        segments.append(np.ones(high_units * unit_samples, dtype=np.complex64))
    iq = np.concatenate(segments)
    return Burst(
        stream_id="test",
        sample_rate_sps=2_000_000,
        center_freq_hz=315_000_000,
        iq=iq,
        peak=1.0,
        average=0.5,
        started_at=0.0,
        ended_at=1.0,
    )


def test_tpms_decoder_reads_width_encoded_bits() -> None:
    decoder = TpmsDecoder()
    payload = "10110011100011110000111100110011"
    result = decoder.decode(_synth_ook_burst(payload))
    assert result is not None
    assert result.bits.startswith(payload[:16])
    assert result.candidate.strategy in {"pulse", "gap"}
