#!/usr/bin/env python3
import argparse
import csv
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


def parse_kv(path):
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(errors='replace').splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            out[k.strip()] = v.strip()
    return out


def load_cf32(path):
    raw = np.memmap(path, dtype='<f4', mode='r')
    if raw.size % 2:
        raw = raw[:-1]
    return raw.reshape((-1, 2))


def detect_events(iq, start_sample, end_sample, sample_rate, center_hz, bins, nfft, hop, threshold_db, merge_us, max_events, chunk_frames):
    total = end_sample - start_sample
    frames = 1 + max(0, (total - nfft) // hop)
    if frames <= 0:
        return []

    # First pass: estimate per-bin median from decimated frames to avoid holding full matrix.
    sample_every = max(1, frames // 20000)
    med_samples = []
    window = np.hanning(nfft).astype(np.float32)
    bin_edges = np.linspace(0, nfft, bins + 1).round().astype(int)

    def calc_bin_power(starts):
        need_start = int(starts[0])
        need_end = int(starts[-1] + nfft)
        pair = iq[start_sample + need_start:start_sample + need_end]
        comp = pair[:, 0].astype(np.float32) + 1j * pair[:, 1].astype(np.float32)
        framed = np.lib.stride_tricks.as_strided(
            comp,
            shape=(len(starts), nfft),
            strides=(hop * comp.strides[0], comp.strides[0]),
            writeable=False,
        )
        spec = np.fft.fftshift(np.fft.fft(framed * window, axis=1), axes=1)
        power = (spec.real * spec.real + spec.imag * spec.imag).astype(np.float32)
        bp = np.empty((len(starts), bins), dtype=np.float32)
        for b in range(bins):
            lo, hi = bin_edges[b], bin_edges[b + 1]
            if hi <= lo:
                hi = lo + 1
            bp[:, b] = power[:, lo:hi].mean(axis=1)
        return bp

    dec_frames = np.arange(0, frames, sample_every, dtype=np.int64)
    for off in range(0, len(dec_frames), chunk_frames):
        sub = dec_frames[off:off + chunk_frames] * hop
        med_samples.append(calc_bin_power(sub))
    med = np.median(np.vstack(med_samples), axis=0)
    med = np.maximum(med, 1e-20)

    # Second pass: keep local maxima above threshold.
    candidates = []
    merge_samples = int(sample_rate * merge_us / 1e6)
    low_edge = center_hz - sample_rate / 2.0
    bin_hz = sample_rate / bins

    for first in range(0, frames, chunk_frames):
        count = min(chunk_frames, frames - first)
        frame_nums = first + np.arange(count, dtype=np.int64)
        starts = frame_nums * hop
        bp = calc_bin_power(starts)
        scores = 10.0 * np.log10(np.maximum(bp, 1e-20) / med[None, :])
        hot = np.argwhere(scores >= threshold_db)
        for row, ch in hot:
            abs_sample = int(start_sample + starts[row])
            score = float(scores[row, ch])
            power = float(bp[row, ch])
            freq_hz = low_edge + (int(ch) + 0.5) * bin_hz
            candidates.append((score, abs_sample, int(ch), freq_hz, power))

    candidates.sort(reverse=True)
    events = []
    for score, abs_sample, ch, freq_hz, power in candidates:
        if any(ev['channel'] == ch and abs(ev['sample'] - abs_sample) <= merge_samples for ev in events):
            continue
        events.append({
            'rank': len(events) + 1,
            'sample': abs_sample,
            'time_s': abs_sample / sample_rate,
            'channel': ch,
            'freq_mhz': freq_hz / 1e6,
            'score_db': score,
            'power': power,
        })
        if len(events) >= max_events:
            break
    return events


def write_snippet(iq, event, out_dir, sample_rate, pre_us, post_us, source_center_hz, source_sample_rate):
    pre = int(round(sample_rate * pre_us / 1e6))
    post = int(round(sample_rate * post_us / 1e6))
    start = max(0, event['sample'] - pre)
    end = min(iq.shape[0], event['sample'] + post)
    data = np.asarray(iq[start:end], dtype='<f4')
    name = f"burst_{event['rank']:04d}_t{event['time_s']:.6f}_ch{event['channel']:02d}_{event['freq_mhz']:.3f}MHz.cf32"
    path = out_dir / name
    data.tofile(path)
    meta = {
        'format': 'cf32_le',
        'sample_type': 'complex_float32_interleaved_iq',
        'source_center_hz': source_center_hz,
        'source_sample_rate_hz': source_sample_rate,
        'snippet_sample_rate_hz': source_sample_rate,
        'event_sample': event['sample'],
        'snippet_start_sample': start,
        'snippet_end_sample': end,
        'event_offset_samples': event['sample'] - start,
        'event_time_s': event['time_s'],
        'channel': event['channel'],
        'freq_mhz': event['freq_mhz'],
        'score_db': event['score_db'],
    }
    path.with_suffix(path.suffix + '.meta').write_text(json.dumps(meta, indent=2) + '\n')
    return path, start, end


def main():
    ap = argparse.ArgumentParser(description='Extract short IQ snippets around high-energy l2ping capture bursts.')
    ap.add_argument('capture_dir')
    ap.add_argument('--out', default='', help='output dir; default capture_dir/bursts')
    ap.add_argument('--threshold-db', type=float, default=48.0)
    ap.add_argument('--max-events', type=int, default=80)
    ap.add_argument('--margin-sec', type=float, default=0.25)
    ap.add_argument('--pre-us', type=float, default=800.0)
    ap.add_argument('--post-us', type=float, default=1200.0)
    ap.add_argument('--merge-us', type=float, default=500.0)
    ap.add_argument('--nfft', type=int, default=4096)
    ap.add_argument('--hop', type=int, default=2048)
    ap.add_argument('--chunk-frames', type=int, default=1024)
    ap.add_argument('--max-sec', type=float, default=0.0, help='quick-test limit on analyzed window')
    args = ap.parse_args()

    cap = Path(args.capture_dir)
    iq_path = cap / 'capture.cf32'
    meta = json.loads((cap / 'capture.cf32.meta').read_text())
    exp = parse_kv(cap / 'experiment.txt')
    sample_rate = float(meta['sample_rate_hz'])
    center_hz = float(meta['center_hz'])
    bins = int(meta.get('bins') or round(sample_rate / 1e6))
    start_ts = float(meta['start_unix_sec']) + float(meta.get('start_unix_usec', 0)) / 1e6
    total_samples = iq_path.stat().st_size // 8
    total_sec = total_samples / sample_rate

    l2_start = parse_iso(exp['l2ping_start_utc']) if 'l2ping_start_utc' in exp else start_ts
    l2_end = parse_iso(exp['l2ping_end_utc']) if 'l2ping_end_utc' in exp else start_ts + total_sec
    rel_start = max(0.0, l2_start - start_ts - args.margin_sec)
    rel_end = min(total_sec, l2_end - start_ts + args.margin_sec)
    if args.max_sec > 0:
        rel_end = min(rel_end, rel_start + args.max_sec)
    start_sample = int(rel_start * sample_rate)
    end_sample = int(rel_end * sample_rate)
    start_sample = max(0, (start_sample // args.hop) * args.hop)

    out_dir = Path(args.out) if args.out else cap / 'bursts'
    out_dir.mkdir(parents=True, exist_ok=True)
    iq = load_cf32(iq_path)

    print(f'capture={cap}')
    print(f'out={out_dir}')
    print(f'analyze_sec={start_sample/sample_rate:.6f}..{end_sample/sample_rate:.6f}')
    print(f'threshold_db={args.threshold_db} max_events={args.max_events}')
    events = detect_events(
        iq, start_sample, end_sample, sample_rate, center_hz, bins,
        args.nfft, args.hop, args.threshold_db, args.merge_us, args.max_events, args.chunk_frames
    )
    print(f'events={len(events)}')

    manifest = out_dir / 'manifest.csv'
    with manifest.open('w', newline='') as f:
        fieldnames = ['rank', 'path', 'sample', 'time_s', 'channel', 'freq_mhz', 'score_db', 'power', 'snippet_start_sample', 'snippet_end_sample']
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for ev in events:
            path, snip_start, snip_end = write_snippet(iq, ev, out_dir, sample_rate, args.pre_us, args.post_us, center_hz, sample_rate)
            row = dict(ev)
            row['path'] = str(path)
            row['snippet_start_sample'] = snip_start
            row['snippet_end_sample'] = snip_end
            writer.writerow(row)
            print(f"{ev['rank']:03d} t={ev['time_s']:.6f}s ch={ev['channel']:02d} freq={ev['freq_mhz']:.3f}MHz score={ev['score_db']:.1f} file={path.name}")
    print(f'manifest={manifest}')


if __name__ == '__main__':
    main()
