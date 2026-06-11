from __future__ import annotations

import numpy as np

from zigbee_802154.decoder import (
    Burst,
    BurstDetector,
    IEEE802154Decoder,
    channel_to_center_freq,
    parse_mac_fields,
    synthesize_iq_for_psdu,
)
from zigbee_802154.wideband import WidebandWindowPlan, build_channel_plans, build_wideband_window_plans, create_runtimes


def test_decoder_recovers_synthetic_psdu() -> None:
    sample_rate_sps = 4_000_000
    center_freq_hz = channel_to_center_freq(15)
    psdu = bytes.fromhex("4188013412cdabdeadbeef")
    iq = synthesize_iq_for_psdu(psdu, sample_rate_sps=sample_rate_sps)
    burst = Burst(
        stream_id="test",
        sample_rate_sps=sample_rate_sps,
        center_freq_hz=center_freq_hz,
        iq=iq,
        peak=float(np.max(np.abs(iq))),
        average=float(np.mean(np.abs(iq))),
        started_at=0.0,
        ended_at=1.0,
    )
    frame = IEEE802154Decoder().decode(burst)
    assert frame is not None
    assert frame.channel == 15
    assert frame.phy_length == len(psdu)
    assert frame.psdu == psdu
    assert frame.confidence > 0.85


def test_burst_detector_emits_candidate_for_synthetic_signal() -> None:
    sample_rate_sps = 4_000_000
    center_freq_hz = channel_to_center_freq(11)
    psdu = bytes.fromhex("010203040506")
    iq = synthesize_iq_for_psdu(psdu, sample_rate_sps=sample_rate_sps, lead_samples=512, tail_samples=512)
    detector = BurstDetector(
        sample_rate_sps=sample_rate_sps,
        center_freq_hz=center_freq_hz,
        stream_id="stream-test",
        min_burst_ms=0.05,
    )
    bursts = detector.ingest_iq(iq[: len(iq) // 2], timestamp=0.5)
    bursts.extend(detector.ingest_iq(iq[len(iq) // 2 :], timestamp=1.0))
    bursts.extend(detector.flush())
    assert bursts
    frame = IEEE802154Decoder().decode(bursts[0])
    assert frame is not None
    assert frame.psdu == psdu


def test_burst_detector_preserves_pre_roll_from_current_chunk() -> None:
    sample_rate_sps = 4_000_000
    pre_roll_samples = 800
    lead_samples = 1200
    signal_samples = 2000
    iq = np.concatenate(
        (
            np.zeros(lead_samples, dtype=np.complex64),
            np.full(signal_samples, 0.5 + 0.5j, dtype=np.complex64),
            np.zeros(512, dtype=np.complex64),
        )
    )
    detector = BurstDetector(
        sample_rate_sps=sample_rate_sps,
        center_freq_hz=channel_to_center_freq(25),
        stream_id="stream-test",
        pre_roll_ms=0.2,
        min_burst_ms=0.05,
    )

    bursts = detector.ingest_iq(iq, timestamp=0.0)

    assert len(bursts) == 1
    assert np.count_nonzero(bursts[0].iq[:pre_roll_samples]) == 0
    assert bursts[0].iq.size >= pre_roll_samples + signal_samples


def test_parse_mac_fields_for_short_address_data_frame() -> None:
    psdu = bytes.fromhex(
        "418801"
        "3412"
        "cdab"
        "efbe"
        "44332211"
        "a1b2"
    )
    mac = parse_mac_fields(psdu)
    assert mac is not None
    assert mac.frame_type == "data"
    assert mac.sequence_number == 1
    assert mac.destination_pan_id == 0x1234
    assert mac.destination_address_mode == "short"
    assert mac.destination_address == "0xabcd"
    assert mac.source_pan_id == 0x1234
    assert mac.source_address_mode == "short"
    assert mac.source_address == "0xbeef"
    assert mac.payload_hex == "44332211"
    assert mac.fcs_hex == "a1b2"


def test_wideband_window_plan_for_20mhz_radio() -> None:
    plans = build_wideband_window_plans(20_000_000)
    assert [tuple(plan.channels) for plan in plans] == [
        (11, 12, 13, 14),
        (15, 16, 17, 18),
        (19, 20, 21, 22),
        (23, 24, 25, 26),
    ]


def test_wideband_window_plan_for_60mhz_radio() -> None:
    plans = build_wideband_window_plans(60_000_000)
    assert [tuple(plan.channels) for plan in plans] == [
        (11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22),
        (15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26),
    ]


def test_focused_wideband_window_places_anchor_channel_at_dc() -> None:
    base_plan = build_wideband_window_plans(20_000_000)[3]
    focused = WidebandWindowPlan(
        index=base_plan.index,
        center_freq_hz=channel_to_center_freq(25),
        sample_rate_sps=base_plan.sample_rate_sps,
        channels=base_plan.channels,
    )
    channel_plans = build_channel_plans(focused, channel_rate_sps=8_000_000)
    offsets = {plan.channel: plan.freq_offset_hz for plan in channel_plans}
    assert offsets[25] == 0.0
    assert offsets[26] == 5_000_000.0


def test_wideband_channelizer_preserves_centered_802154_frame() -> None:
    sample_rate_sps = 20_000_000
    psdu = bytes.fromhex("418801323300000000bf006f6e650d9fef")
    channel_iq = synthesize_iq_for_psdu(
        psdu,
        sample_rate_sps=10_000_000,
        lead_samples=1024,
        tail_samples=1024,
    )
    wideband_iq = np.repeat(channel_iq, 2).astype(np.complex64)
    focused = WidebandWindowPlan(
        index=3,
        center_freq_hz=channel_to_center_freq(25),
        sample_rate_sps=sample_rate_sps,
        channels=(23, 24, 25, 26),
    )
    runtime = [entry for entry in create_runtimes(focused, channel_rate_sps=8_000_000) if entry.plan.channel == 25][0]
    decimated = runtime.downconvert_and_decimate(wideband_iq, input_sample_rate_sps=sample_rate_sps)
    burst = Burst(
        stream_id="wideband-test",
        sample_rate_sps=runtime.plan.output_sample_rate_sps,
        center_freq_hz=channel_to_center_freq(25),
        iq=decimated,
        peak=float(np.max(np.abs(decimated))),
        average=float(np.mean(np.abs(decimated))),
        started_at=0.0,
        ended_at=1.0,
    )
    frame = IEEE802154Decoder(frequency_search_hz=(0, -25_000, 25_000), waveform_pattern_corr_min=0.18).decode(burst)
    assert frame is not None
    assert frame.psdu == psdu
