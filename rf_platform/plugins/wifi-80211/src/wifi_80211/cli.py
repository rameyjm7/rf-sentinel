from __future__ import annotations

import argparse
import json
from pathlib import Path

from .demodulator import WiFiActivityDemodulator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wifi_80211", description="RF Sentinel WiFi / 802.11 SDR plugin")
    sub = parser.add_subparsers(dest="command")
    activity = sub.add_parser("activity", help="detect 802.11 OFDM activity from interleaved cs8 IQ")
    activity.add_argument("--input", type=Path, required=True)
    activity.add_argument("--sample-rate", type=float, required=True)
    activity.add_argument("--center-freq", type=float, required=True)
    activity.add_argument("--threshold", type=float, default=0.55)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "activity":
        build_parser().print_help()
        return 2
    raw = args.input.read_bytes()
    demod = WiFiActivityDemodulator(threshold=args.threshold, min_interval_s=0.0)
    for event in demod.process_chunk(
        raw_i8=raw,
        center_freq_hz=int(args.center_freq),
        sample_rate_sps=int(args.sample_rate),
        source="file",
        source_window=args.input.name,
    ):
        print(json.dumps(event, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
