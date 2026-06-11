#!/usr/bin/env python3
import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

P = 0x83848D96BBCC54FC
GP = 0o157464165547
G = (GP << 1) ^ GP


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
    if not path.exists(): return out
    for line in path.read_text(errors='replace').splitlines():
        if '=' in line:
            k,v=line.split('=',1); out[k.strip()]=v.strip()
    return out


def compute_remainder(input_word, divisor):
    dl = divisor.bit_length(); il = input_word.bit_length()
    if dl + il > 63: return input_word
    input_word <<= dl
    while input_word.bit_length() >= dl:
        input_word ^= divisor << (input_word.bit_length() - dl)
    return input_word


def access_word_for_lap(lap):
    barker = 0x13 if (lap & 0x800000) else 0x2C
    x = (barker << 24) | lap
    xtilde = (P >> 34) ^ x
    ctilde = compute_remainder(xtilde, G)
    stilde = ctilde | (xtilde << 34)
    return stilde ^ P


def extract_byte(bits, start):
    v=0
    for b in range(8): v |= int(bits[start+b]) << b
    return v


def is_valid_preamble(bits, k):
    p1 = int(bits[k]) + int(bits[k+2])
    p2 = int(bits[k+1]) + int(bits[k+3])
    return (p1 == 2 and p2 == 0) or (p1 == 0 and p2 == 2)


def scan_bits(bits, base_us, channel, only_lap=None, max_hits=100):
    hits=[]
    n=len(bits)
    for i in range(0, n-80):
        if not is_valid_preamble(bits, i): continue
        barker = extract_byte(bits, i+62) & 0x3F
        if barker not in (0x13, 0x2C): continue
        lap = (extract_byte(bits, i+54) << 16) | (extract_byte(bits, i+46) << 8) | extract_byte(bits, i+38)
        if only_lap is not None and lap != only_lap: continue
        code = ((extract_byte(bits, i+4) << 0) | (extract_byte(bits, i+12) << 8) |
                (extract_byte(bits, i+20) << 16) | (extract_byte(bits, i+28) << 24) |
                (extract_byte(bits, i+36) << 32)) & 0x3FFFFFFFF
        aw = (barker << 58) | (lap << 34) | code
        if aw == access_word_for_lap(lap):
            hits.append({'time_us': base_us + i, 'channel': channel, 'lap': lap, 'bit_index': i})
            if len(hits) >= max_hits: break
    return hits


def load_cf32_memmap(path):
    raw = np.memmap(path, dtype='<f4', mode='r')
    if raw.size % 2: raw = raw[:-1]
    return raw.reshape((-1,2))


def main():
    ap=argparse.ArgumentParser(description='Offline replay btsniffer FFT channelizer and scan access words.')
    ap.add_argument('capture_dir')
    ap.add_argument('--only-lap', default='', help='hex LAP to filter, e.g. FF33BE')
    ap.add_argument('--margin-sec', type=float, default=0.25)
    ap.add_argument('--chunk-us', type=int, default=200000)
    ap.add_argument('--max-sec', type=float, default=0.0)
    ap.add_argument('--max-hits', type=int, default=200)
    ap.add_argument('--channels', default='', help='comma channels to scan; default all')
    args=ap.parse_args()
    cap=Path(args.capture_dir)
    meta=json.loads((cap/'capture.cf32.meta').read_text())
    exp=parse_kv(cap/'experiment.txt')
    iq_path=cap/'capture.cf32'
    sample_rate=int(float(meta['sample_rate_hz']))
    bins=int(meta.get('bins') or round(sample_rate/1e6))
    center=float(meta['center_hz'])
    start_ts=float(meta['start_unix_sec'])+float(meta.get('start_unix_usec',0))/1e6
    total_samples=iq_path.stat().st_size//8
    total_us=total_samples//bins
    if 'l2ping_start_utc' in exp:
        rel_start=parse_iso(exp['l2ping_start_utc'])-start_ts-args.margin_sec
        rel_end=parse_iso(exp['l2ping_end_utc'])-start_ts+args.margin_sec
    else:
        rel_start=0; rel_end=total_us/1e6
    if args.max_sec>0: rel_end=min(rel_end, rel_start+args.max_sec)
    start_us=max(0, int(rel_start*1e6)); end_us=min(total_us, int(rel_end*1e6))
    only_lap=int(args.only_lap,16) if args.only_lap else None
    if args.channels:
        channels=[int(x) for x in args.channels.split(',') if x.strip()]
    else:
        channels=list(range(bins))
    raw=load_cf32_memmap(iq_path)
    hits=[]
    print(f'capture={cap} bins={bins} sample_rate={sample_rate} center_mhz={center/1e6:.3f}')
    print(f'scan_us={start_us}..{end_us} channels={len(channels)} only_lap={args.only_lap or "any"}')
    for chunk_start_us in range(start_us, end_us, args.chunk_us):
        chunk_end_us=min(end_us, chunk_start_us+args.chunk_us)
        raw_start=chunk_start_us*bins; raw_end=chunk_end_us*bins
        pair=raw[raw_start:raw_end]
        comp=pair[:,0].astype(np.float32)+1j*pair[:,1].astype(np.float32)
        syms=(len(comp)//bins)
        if syms <= 100: continue
        mat=comp[:syms*bins].reshape((syms,bins))
        spec=np.fft.fft(mat, axis=1)
        for ch in channels:
            fft_bin=(ch + bins//2) % bins
            y=spec[:,fft_bin]
            metric=(y[:-1].real*y[1:].imag - y[:-1].imag*y[1:].real)
            bits=(metric>0).astype(np.uint8)
            for polarity in (0,1):
                test=bits ^ polarity
                new=scan_bits(test, chunk_start_us, ch, only_lap=only_lap, max_hits=args.max_hits-len(hits))
                for h in new:
                    h['polarity']=polarity
                    hits.append(h)
                    print(f"hit time_us={h['time_us']} time_s={h['time_us']/1e6:.6f} ch={h['channel']} lap={h['lap']:06X} polarity={polarity}")
                    if len(hits)>=args.max_hits:
                        print(f'total_hits={len(hits)}')
                        return
        print(f'progress {chunk_end_us/1e6:.3f}s hits={len(hits)}')
    print(f'total_hits={len(hits)}')

if __name__=='__main__':
    main()
