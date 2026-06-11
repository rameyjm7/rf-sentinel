#!/usr/bin/env python3
import argparse
import csv
import json
import math
from pathlib import Path

import numpy as np

P = 0x83848D96BBCC54FC
GP = 0o157464165547
G = (GP << 1) ^ GP


def length(word):
    return word.bit_length()


def compute_remainder(input_word, divisor):
    dl = length(divisor)
    il = length(input_word)
    if dl + il > 63:
        return input_word
    input_word <<= dl
    while length(input_word) >= dl:
        input_word ^= divisor << (length(input_word) - dl)
    return input_word


def access_word_for_lap(lap):
    barker = 0x13 if (lap & 0x800000) else 0x2C
    x = (barker << 24) | lap
    xtilde = (P >> 34) ^ x
    ctilde = compute_remainder(xtilde, G)
    stilde = ctilde | (xtilde << 34)
    return stilde ^ P


def extract_byte(bits, start):
    value = 0
    for b in range(8):
        value |= int(bits[start + b]) << b
    return value


def is_valid_preamble(bits, k):
    p1 = int(bits[k]) + int(bits[k + 2])
    p2 = int(bits[k + 1]) + int(bits[k + 3])
    return (p1 == 2 and p2 == 0) or (p1 == 0 and p2 == 2)


def detect_access_words(bits, max_hits=20):
    hits = []
    if len(bits) < 80:
        return hits
    for i in range(0, len(bits) - 80):
        if not is_valid_preamble(bits, i):
            continue
        barker = extract_byte(bits, i + 62) & 0x3F
        if barker not in (0x13, 0x2C):
            continue
        lap = (extract_byte(bits, i + 54) << 16) | (extract_byte(bits, i + 46) << 8) | extract_byte(bits, i + 38)
        code = (
            (extract_byte(bits, i + 4) << 0) |
            (extract_byte(bits, i + 12) << 8) |
            (extract_byte(bits, i + 20) << 16) |
            (extract_byte(bits, i + 28) << 24) |
            (extract_byte(bits, i + 36) << 32)
        ) & 0x3FFFFFFFF
        aw = (barker << 58) | (lap << 34) | code
        aw_expected = access_word_for_lap(lap)
        if aw == aw_expected:
            hits.append({'bit_index': i, 'lap': lap, 'barker': barker, 'aw': aw})
            if len(hits) >= max_hits:
                break
    return hits


def load_cf32(path):
    raw = np.fromfile(path, dtype='<f4')
    if raw.size % 2:
        raw = raw[:-1]
    return raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)


def lowpass_fir(num_taps, cutoff_hz, sample_rate):
    n = np.arange(num_taps, dtype=np.float64) - (num_taps - 1) / 2.0
    h = 2.0 * cutoff_hz / sample_rate * np.sinc(2.0 * cutoff_hz * n / sample_rate)
    h *= np.hamming(num_taps)
    h /= np.sum(h)
    return h.astype(np.float32)


def filter_decimate(x, decim, phase, taps):
    if len(x) < len(taps) + decim:
        return np.empty(0, dtype=np.complex64)
    y = np.convolve(x, taps, mode='same')
    return y[phase::decim].astype(np.complex64)


def moving_average_decimate(x, decim, phase):
    usable = ((len(x) - phase) // decim) * decim
    if usable <= 0:
        return np.empty(0, dtype=np.complex64)
    framed = x[phase:phase + usable].reshape((-1, decim))
    return framed.mean(axis=1).astype(np.complex64)


def diff_bits(y):
    if len(y) < 2:
        return np.empty(0, dtype=np.uint8), 0.0
    prod = np.conj(y[:-1]) * y[1:]
    metric = prod.imag
    bits = (metric > 0).astype(np.uint8)
    quality = float(np.mean(np.abs(metric)) / (np.mean(np.abs(y[:-1]) * np.abs(y[1:])) + 1e-12))
    return bits, quality


def analyze_snippet(row, source_center_hz, sample_rate, symbol_rate, phases, invert, freq_offsets_hz, filter_taps):
    path = Path(row['path'])
    x = load_cf32(path)
    event_freq_hz = float(row['freq_mhz']) * 1e6
    base_freq_offset = event_freq_hz - source_center_hz
    n = np.arange(len(x), dtype=np.float64)
    decim = int(round(sample_rate / symbol_rate))
    phase_list = range(decim) if phases <= 0 else np.linspace(0, decim - 1, phases).round().astype(int)

    best = None
    hits_out = []
    for extra_hz in freq_offsets_hz:
        freq_offset = base_freq_offset + extra_hz
        mixer = np.exp(-2j * np.pi * freq_offset * n / sample_rate).astype(np.complex64)
        bb = x * mixer
        for phase in phase_list:
            y = filter_decimate(bb, decim, int(phase), filter_taps)
            bits, quality = diff_bits(y)
            for polarity in ([0, 1] if invert else [0]):
                test_bits = bits ^ polarity
                hits = detect_access_words(test_bits)
                if hits:
                    for hit in hits:
                        hit = dict(hit)
                        hit['phase'] = int(phase)
                        hit['polarity'] = polarity
                        hit['quality'] = quality
                        hit['bit_time_us'] = hit['bit_index']
                        hit['freq_adjust_hz'] = float(extra_hz)
                        hits_out.append(hit)
            if best is None or quality > best['quality']:
                best = {'phase': int(phase), 'quality': quality, 'symbols': len(y), 'freq_adjust_hz': float(extra_hz)}
    return best, hits_out


def main():
    ap = argparse.ArgumentParser(description='Demod extracted l2ping burst snippets and detect Bluetooth access words/LAPs.')
    ap.add_argument('burst_dir', help='directory containing manifest.csv and burst snippets')
    ap.add_argument('--capture-meta', default='', help='capture.cf32.meta path; inferred from burst meta if omitted')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--phases', type=int, default=0, help='timing phases to try; 0 means all 60')
    ap.add_argument('--symbol-rate', type=float, default=1_000_000.0)
    ap.add_argument('--invert', action='store_true', default=True, help='try inverted discriminator polarity too')
    ap.add_argument('--only-lap', default='', help='filter printed hits to this LAP hex, e.g. FF33BE')
    ap.add_argument('--freq-search-khz', type=float, default=300.0, help='search +/- this frequency offset around event bin center')
    ap.add_argument('--freq-steps', type=int, default=7, help='number of frequency offsets to try')
    ap.add_argument('--filter-taps', type=int, default=241, help='lowpass FIR taps before decimation')
    ap.add_argument('--cutoff-khz', type=float, default=650.0, help='lowpass cutoff before decimation')
    args = ap.parse_args()

    burst_dir = Path(args.burst_dir)
    rows = list(csv.DictReader((burst_dir / 'manifest.csv').open()))
    if args.limit > 0:
        rows = rows[:args.limit]

    if args.capture_meta:
        meta = json.loads(Path(args.capture_meta).read_text())
    else:
        first_meta = Path(rows[0]['path'] + '.meta')
        meta = json.loads(first_meta.read_text())
        meta = {
            'center_hz': meta['source_center_hz'],
            'sample_rate_hz': meta['source_sample_rate_hz'],
        }
    source_center_hz = float(meta['center_hz'])
    sample_rate = float(meta['sample_rate_hz'])
    only_lap = int(args.only_lap, 16) if args.only_lap else None

    freq_offsets_hz = np.linspace(-args.freq_search_khz * 1e3, args.freq_search_khz * 1e3, args.freq_steps)
    filter_taps = lowpass_fir(args.filter_taps, args.cutoff_khz * 1e3, sample_rate)

    print(f'burst_dir={burst_dir}')
    print(f'rows={len(rows)} sample_rate={sample_rate:.0f} center_mhz={source_center_hz/1e6:.3f} phases={args.phases or int(round(sample_rate/args.symbol_rate))} freq_offsets={len(freq_offsets_hz)} cutoff_khz={args.cutoff_khz}')

    total_hits = 0
    lap_counts = {}
    for idx, row in enumerate(rows, 1):
        best, hits = analyze_snippet(row, source_center_hz, sample_rate, args.symbol_rate, args.phases, args.invert, freq_offsets_hz, filter_taps)
        printable = []
        for h in hits:
            if only_lap is not None and h['lap'] != only_lap:
                continue
            printable.append(h)
            lap_counts[h['lap']] = lap_counts.get(h['lap'], 0) + 1
        if printable:
            total_hits += len(printable)
            print(f"burst rank={row['rank']} file={Path(row['path']).name} event_t={float(row['time_s']):.6f}s freq={float(row['freq_mhz']):.3f}MHz best_phase={best['phase']} best_df={best['freq_adjust_hz']:.0f}Hz q={best['quality']:.3f}")
            for h in printable:
                print(f"  hit lap={h['lap']:06X} bit={h['bit_index']} phase={h['phase']} polarity={h['polarity']} df={h.get('freq_adjust_hz', 0):.0f}Hz q={h['quality']:.3f}")
    print(f'total_hits={total_hits}')
    if lap_counts:
        print('lap_counts:')
        for lap, count in sorted(lap_counts.items(), key=lambda kv: kv[1], reverse=True):
            print(f'  {lap:06X} {count}')


if __name__ == '__main__':
    main()
