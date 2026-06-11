#!/usr/bin/env python3
import argparse
import csv
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path


DEFAULT_OUI_CSV = Path("/usr/share/ieee-data/oui.csv")


def clean_hex(value, digits, name):
    clean = "".join(ch for ch in value.upper() if ch in "0123456789ABCDEF")
    if len(clean) != digits:
        raise ValueError(f"{name} must contain exactly {digits} hex digits")
    return clean


def bdaddr_from_parts(nap, uap, lap):
    full = f"{nap}{uap}{lap}"
    return ":".join(full[i : i + 2] for i in range(0, 12, 2))


def candidate_from_nap(nap, uap, lap, organization):
    return {
        "nap": nap,
        "bdaddr": bdaddr_from_parts(nap, uap, lap),
        "organization": organization,
    }


def load_oui_candidates(uap, lap, oui_csv):
    candidates = []
    with oui_csv.open(newline="", encoding="utf-8", errors="replace") as handle:
        for row in csv.DictReader(handle):
            assignment = (row.get("Assignment") or "").strip().upper()
            if len(assignment) != 6 or not assignment.endswith(uap):
                continue
            nap = assignment[:4]
            candidates.append(candidate_from_nap(nap, uap, lap, row.get("Organization Name", "").strip()))
    return sorted(candidates, key=lambda item: item["bdaddr"])


def load_full_nap_candidates(uap, lap):
    return [
        candidate_from_nap(f"{nap:04X}", uap, lap, "full-scan")
        for nap in range(0x10000)
    ]


def unique_candidates(candidates):
    seen = set()
    out = []
    for candidate in candidates:
        if candidate["nap"] in seen:
            continue
        seen.add(candidate["nap"])
        out.append(candidate)
    return out


def probe_l2ping(candidate, interface, count, timeout, process_timeout, flood, verify):
    cmd = ["l2ping"]
    if interface:
        cmd += ["-i", interface]
    if flood:
        cmd += ["-f"]
    if verify:
        cmd += ["-v"]
    cmd += ["-c", str(count), "-t", str(timeout), candidate["bdaddr"]]

    started = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=process_timeout,
        )
        elapsed = time.monotonic() - started
        output = result.stdout.strip().replace("\n", " | ")
        ok = result.returncode == 0 or f"from {candidate['bdaddr']}" in output
        return ok, elapsed, output
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        output = (exc.stdout or "").strip()
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        output = output.replace("\n", " | ")
        if output:
            output = f"{output} | l2ping timed out"
        else:
            output = "l2ping timed out"
        return False, elapsed, output


def decoy_candidates(found, uap, lap, count):
    if count <= 0:
        return []
    rng = random.Random(0xB17ECAFE)
    decoys = []
    used = {found["nap"]}
    while len(decoys) < count and len(used) < 0x10000:
        nap = f"{rng.randrange(0x10000):04X}"
        if nap in used:
            continue
        used.add(nap)
        decoys.append(candidate_from_nap(nap, uap, lap, "decoy-nap-check"))
    return decoys


def main():
    parser = argparse.ArgumentParser(
        description="Complete a resolved Bluetooth Classic UAP:LAP by probing NAP candidates with BlueZ l2ping."
    )
    parser.add_argument("--uap", required=True, help="Resolved UAP byte, e.g. CA")
    parser.add_argument("--lap", required=True, help="Resolved LAP, e.g. FF33BE")
    parser.add_argument("--oui-csv", default=str(DEFAULT_OUI_CSV), help="IEEE oui.csv path")
    parser.add_argument("--full-scan", action="store_true", help="try all 65536 NAPs instead of OUI-filtered candidates")
    parser.add_argument("--nap", action="append", default=[], help="specific NAP to try first, e.g. C2D1; repeatable")
    parser.add_argument("--nap-prefix", default="", help="filter candidates by NAP prefix, e.g. C2 or C2D1")
    parser.add_argument("--randomize", action="store_true", help="shuffle candidate order after any --nap entries")
    parser.add_argument("--probe", action="store_true", help="actively run l2ping against candidates")
    parser.add_argument("--interface", default="", help="Bluetooth HCI device, e.g. hci0")
    parser.add_argument("--count", type=int, default=1, help="l2ping echo count per candidate")
    parser.add_argument("--timeout", type=int, default=1, help="l2ping timeout in seconds")
    parser.add_argument("--process-timeout", type=float, default=1.25, help="maximum wall time per l2ping process")
    parser.add_argument("--delay", type=float, default=0.0, help="delay between active probes")
    parser.add_argument("--flood", action="store_true", help="pass -f to l2ping")
    parser.add_argument("--verify", action="store_true", help="pass -v to l2ping")
    parser.add_argument("--decoy-check", type=int, default=0, help="after a hit, probe this many wrong NAPs to test whether NAP is a real discriminator")
    parser.add_argument("--limit", type=int, default=0, help="limit candidate count for testing")
    args = parser.parse_args()

    if args.probe and shutil.which("l2ping") is None:
        print("error: l2ping not found", file=sys.stderr)
        return 2

    try:
        uap = clean_hex(args.uap, 2, "UAP")
        lap = clean_hex(args.lap, 6, "LAP")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try_first = []
    for nap_arg in args.nap:
        try_first.append(candidate_from_nap(clean_hex(nap_arg, 4, "NAP"), uap, lap, "manual-priority"))

    if args.full_scan:
        candidates = load_full_nap_candidates(uap, lap)
    else:
        oui_csv = Path(args.oui_csv)
        if not oui_csv.exists():
            print(f"error: OUI CSV not found: {oui_csv}", file=sys.stderr)
            return 2
        candidates = load_oui_candidates(uap, lap, oui_csv)

    nap_prefix = "".join(ch for ch in args.nap_prefix.upper() if ch in "0123456789ABCDEF")
    if args.nap_prefix and not nap_prefix:
        print("error: --nap-prefix must contain hex digits", file=sys.stderr)
        return 2
    if nap_prefix:
        candidates = [candidate for candidate in candidates if candidate["nap"].startswith(nap_prefix)]

    if args.randomize:
        random.shuffle(candidates)

    candidates = unique_candidates(try_first + candidates)

    if args.limit > 0:
        candidates = candidates[: args.limit]

    mode = "active probe" if args.probe else "candidate list"
    print(f"{mode}: UAP:LAP {uap}:{lap}, candidates={len(candidates)}")

    for idx, candidate in enumerate(candidates, start=1):
        prefix = f"{idx:05d}/{len(candidates):05d} {candidate['bdaddr']} NAP={candidate['nap']}"
        if not args.probe:
            print(f"{prefix} {candidate['organization']}")
            continue

        ok, elapsed, output = probe_l2ping(
            candidate,
            args.interface,
            args.count,
            args.timeout,
            args.process_timeout,
            args.flood,
            args.verify,
        )
        status = "FOUND" if ok else "miss"
        print(f"{prefix} {status} {elapsed:.2f}s {candidate['organization']} {output}")
        if ok:
            for decoy in decoy_candidates(candidate, uap, lap, args.decoy_check):
                decoy_ok, decoy_elapsed, decoy_output = probe_l2ping(
                    decoy,
                    args.interface,
                    args.count,
                    args.timeout,
                    args.process_timeout,
                    args.flood,
                    args.verify,
                )
                decoy_status = "ALSO-RESPONDED" if decoy_ok else "decoy-miss"
                print(f"decoy {decoy['bdaddr']} {decoy_status} {decoy_elapsed:.2f}s {decoy_output}")
            return 0
        if args.delay > 0:
            time.sleep(args.delay)

    return 1 if args.probe else 0


if __name__ == "__main__":
    raise SystemExit(main())
