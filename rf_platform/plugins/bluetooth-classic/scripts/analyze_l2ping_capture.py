#!/usr/bin/env python3
import argparse
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


def parse_iso(value):
    value = value.strip().replace(',', '.')
    if value.endswith('Z'):
        value = value[:-1] + '+00:00'
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_experiment(path):
    data = {}
    if not path.exists():
        return data
    for line in path.read_text(errors='replace').splitlines():
        if '=' not in line:
            continue
        k, v = line.split('=', 1)
        data[k.strip()] = v.strip()
    return data


def parse_l2ping(path):
    rtts = []
    if not path.exists():
        return rtts
    for line in path.read_text(errors='replace').splitlines():
        m = re.search(r'time ([0-9.]+)ms', line)
        if m:
            rtts.append(float(m.group(1)))
    return rtts


def load_cf32_memmap(path):
    raw = np.memmap(path, dtype='<f4', mode='r')
    if raw.size % 2:
        raw = raw[:-1]
    return raw.reshape((-1, 2))


def frame_bin_powers(samples_iq, sample_rate, nfft, hop, bins, chunk_frames):
    # Returns frame start sample numbers and 1 MHz-ish bin powers.
    total_samples = samples_iq.shape[0]
    frames = 1 + max(0, (total_samples - nfft) // hop)
    if frames <= 0:
        return np.empty(0, dtype=np.int64), np.empty((0, bins), dtype=np.float32)

    window = np.hanning(nfft).astype(np.float32)
    bin_edges = np.linspace(0, nfft, bins + 1).round().astype(int)
    out = []
    starts_out = []

    for first_frame in range(0, frames, chunk_frames):
        count = min(chunk_frames, frames - first_frame)
        starts = (first_frame + np.arange(count, dtype=np.int64)) * hop
        need_start = int(starts[0])
        need_end = int(starts[-1] + nfft)
        chunk_pair = samples_iq[need_start:need_end]
        chunk_complex = chunk_pair[:, 0].astype(np.float32) + 1j * chunk_pair[:, 1].astype(np.float32)

        framed = np.lib.stride_tricks.as_strided(
            chunk_complex,
            shape=(count, nfft),
            strides=(hop * chunk_complex.strides[0], chunk_complex.strides[0]),
            writeable=False,
        )
        spec = np.fft.fftshift(np.fft.fft(framed * window, axis=1), axes=1)
        power = (spec.real * spec.real + spec.imag * spec.imag).astype(np.float32)
        bin_power = np.empty((count, bins), dtype=np.float32)
        for b in range(bins):
            lo, hi = bin_edges[b], bin_edges[b + 1]
            if hi <= lo:
                hi = lo + 1
            bin_power[:, b] = power[:, lo:hi].mean(axis=1)
        starts_out.append(starts)
        out.append(bin_power)

    return np.concatenate(starts_out), np.vstack(out)


def summarize_events(starts, powers, sample_rate, center_hz, bins, top_n, merge_us, window_start_sample):
    if len(starts) == 0:
        return []
    med = np.median(powers, axis=0)
    med = np.maximum(med, 1e-20)
    score_db = 10.0 * np.log10(np.maximum(powers, 1e-20) / med[None, :])

    flat = score_db.ravel()
    top_count = min(max(top_n * 20, top_n), flat.size)
    idx = np.argpartition(flat, -top_count)[-top_count:]
    frame_idx, chan_idx = np.unravel_index(idx, score_db.shape)
    order = np.argsort(flat[idx])[::-1]

    events = []
    merge_samples = int(sample_rate * merge_us / 1_000_000.0)
    bin_hz = sample_rate / bins
    low_edge = center_hz - sample_rate / 2.0

    for oi in order:
        fi = int(frame_idx[oi])
        ch = int(chan_idx[oi])
        abs_sample = int(starts[fi] + window_start_sample)
        score = float(score_db[fi, ch])
        raw_power = float(powers[fi, ch])
        # Merge same channel events that are very close in time.
        duplicate = False
        for ev in events:
            if ev['channel'] == ch and abs(ev['sample'] - abs_sample) <= merge_samples:
                duplicate = True
                break
        if duplicate:
            continue
        freq_hz = low_edge + (ch + 0.5) * bin_hz
        events.append({
            'sample': abs_sample,
            'time_s': abs_sample / sample_rate,
            'rel_window_s': starts[fi] / sample_rate,
            'channel': ch,
            'freq_mhz': freq_hz / 1e6,
            'score_db': score,
            'power': raw_power,
        })
        if len(events) >= top_n:
            break
    return events


def main():
    ap = argparse.ArgumentParser(description='Analyze a btcsniffer l2ping RF capture for bursty 1 MHz channel events.')
    ap.add_argument('capture_dir', help='capture directory, e.g. captures/20260604T210440Z')
    ap.add_argument('--margin-sec', type=float, default=0.25, help='extra seconds around l2ping window to analyze')
    ap.add_argument('--nfft', type=int, default=4096, help='FFT size')
    ap.add_argument('--hop', type=int, default=2048, help='FFT hop samples')
    ap.add_argument('--top', type=int, default=80, help='top merged events to print')
    ap.add_argument('--merge-us', type=float, default=180.0, help='merge same-channel events closer than this')
    ap.add_argument('--chunk-frames', type=int, default=2048, help='FFT frames per processing chunk')
    ap.add_argument('--max-sec', type=float, default=0.0, help='limit analyzed duration for quick tests; 0 disables')
    args = ap.parse_args()

    cap = Path(args.capture_dir)
    meta_path = cap / 'capture.cf32.meta'
    iq_path = cap / 'capture.cf32'
    exp_path = cap / 'experiment.txt'
    l2_path = cap / 'l2ping.log'
    if not meta_path.exists() or not iq_path.exists():
        raise SystemExit('missing capture.cf32 or capture.cf32.meta')

    meta = json.loads(meta_path.read_text())
    exp = parse_experiment(exp_path)
    sample_rate = float(meta['sample_rate_hz'])
    center_hz = float(meta['center_hz'])
    bins = int(meta.get('bins') or round(sample_rate / 1e6))
    start_ts = float(meta['start_unix_sec']) + float(meta.get('start_unix_usec', 0)) / 1e6

    total_samples = iq_path.stat().st_size // 8
    total_sec = total_samples / sample_rate

    if 'l2ping_start_utc' in exp and 'l2ping_end_utc' in exp:
        l2_start = parse_iso(exp['l2ping_start_utc'])
        l2_end = parse_iso(exp['l2ping_end_utc'])
    else:
        l2_start = start_ts
        l2_end = start_ts + total_sec

    rel_start = max(0.0, l2_start - start_ts - args.margin_sec)
    rel_end = min(total_sec, l2_end - start_ts + args.margin_sec)
    if args.max_sec > 0:
        rel_end = min(rel_end, rel_start + args.max_sec)
    start_sample = int(rel_start * sample_rate)
    end_sample = int(rel_end * sample_rate)
    # Align to hop for cleaner reporting.
    start_sample = max(0, (start_sample // args.hop) * args.hop)
    end_sample = min(total_samples, end_sample)
    if end_sample - start_sample < args.nfft:
        raise SystemExit('selected analysis window is too small')

    rtts = parse_l2ping(l2_path)
    print(f'capture={cap}')
    print(f'format={meta.get("format")} sample_rate={sample_rate:.0f} center_mhz={center_hz/1e6:.3f} bins={bins}')
    print(f'total_samples={total_samples} total_sec={total_sec:.6f}')
    print(f'capture_start_unix={start_ts:.6f}')
    print(f'l2ping_window_rel_sec={l2_start-start_ts:.6f}..{l2_end-start_ts:.6f} replies={len(rtts)}')
    if rtts:
        print(f'l2ping_rtt_ms min={min(rtts):.2f} avg={sum(rtts)/len(rtts):.2f} max={max(rtts):.2f}')
    print(f'analyze_rel_sec={start_sample/sample_rate:.6f}..{end_sample/sample_rate:.6f} samples={end_sample-start_sample}')
    print(f'nfft={args.nfft} hop={args.hop} frame_us={args.nfft/sample_rate*1e6:.2f} hop_us={args.hop/sample_rate*1e6:.2f}')

    mm = load_cf32_memmap(iq_path)
    window_samples = mm[start_sample:end_sample]
    starts, powers = frame_bin_powers(window_samples, sample_rate, args.nfft, args.hop, bins, args.chunk_frames)
    print(f'frames={len(starts)} power_matrix={powers.shape[0]}x{powers.shape[1]}')

    # Channel occupancy summary.
    med = np.median(powers, axis=0)
    med = np.maximum(med, 1e-20)
    score_db = 10.0 * np.log10(np.maximum(powers, 1e-20) / med[None, :])
    occ = (score_db > 10.0).sum(axis=0)
    top_ch = np.argsort(occ)[::-1][:12]
    bin_hz = sample_rate / bins
    low_edge = center_hz - sample_rate / 2.0
    print('top_occupancy_channels score_gt_10dB:')
    for ch in top_ch:
        freq_mhz = (low_edge + (int(ch) + 0.5) * bin_hz) / 1e6
        print(f'  ch={int(ch):02d} freq_mhz={freq_mhz:.3f} frames={int(occ[ch])}')

    events = summarize_events(starts, powers, sample_rate, center_hz, bins, args.top, args.merge_us, start_sample)
    print('top_events:')
    for i, ev in enumerate(events, 1):
        print(
            f'{i:03d} abs_t={ev["time_s"]:.6f}s '
            f'win_t={ev["rel_window_s"]:.6f}s '
            f'ch={ev["channel"]:02d} freq_mhz={ev["freq_mhz"]:.3f} '
            f'score_db={ev["score_db"]:.1f} power={ev["power"]:.3e}'
        )


if __name__ == '__main__':
    main()
