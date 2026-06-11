#!/usr/bin/env python3
"""Rapidly test NAP candidates with one process and timestamped probe windows."""

import argparse
import csv
import json
import random
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path


DEFAULT_OUI_CSV = Path("/usr/share/ieee-data/oui.csv")
L2CAP_ECHO_REQ = 0x08
L2CAP_ECHO_RSP = 0x09
L2CAP_COMMAND_REJ = 0x01


def clean_hex(value, digits, name):
    clean = "".join(ch for ch in value.upper() if ch in "0123456789ABCDEF")
    if len(clean) != digits:
        raise ValueError(f"{name} must contain exactly {digits} hex digits")
    return clean


def bdaddr(nap, uap, lap):
    value = f"{nap}{uap}{lap}"
    return ":".join(value[index : index + 2] for index in range(0, 12, 2))


def emit(event, **fields):
    payload = {
        "time_ns": time.time_ns(),
        "monotonic_ns": time.monotonic_ns(),
        "type": event,
        **fields,
    }
    print(json.dumps(payload, separators=(",", ":")), flush=True)


def local_controller_address(interface):
    address_path = Path("/sys/class/bluetooth") / interface / "address"
    if address_path.exists():
        return address_path.read_text(encoding="ascii").strip().upper()

    try:
        result = subprocess.run(
            ["hcitool", "dev"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"Bluetooth controller not found: {interface}") from exc

    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == interface:
            return fields[1].upper()
    raise RuntimeError(f"Bluetooth controller not found: {interface}")


def oui_candidates(uap, lap, oui_csv):
    candidates = []
    with oui_csv.open(newline="", encoding="utf-8", errors="replace") as handle:
        for row in csv.DictReader(handle):
            assignment = (row.get("Assignment") or "").strip().upper()
            if len(assignment) != 6 or not assignment.endswith(uap):
                continue
            candidates.append(
                {
                    "nap": assignment[:4],
                    "address": bdaddr(assignment[:4], uap, lap),
                    "organization": (row.get("Organization Name") or "").strip(),
                    "source": "oui",
                }
            )
    return candidates


def all_candidates(uap, lap):
    return [
        {
            "nap": f"{nap:04X}",
            "address": bdaddr(f"{nap:04X}", uap, lap),
            "organization": "",
            "source": "full",
        }
        for nap in range(0x10000)
    ]


def deduplicate(candidates):
    seen = set()
    result = []
    for candidate in candidates:
        if candidate["nap"] in seen:
            continue
        seen.add(candidate["nap"])
        result.append(candidate)
    return result


def probe(candidate, local_address, connect_timeout, echo_timeout, payload, ident):
    started = time.monotonic()
    connected = False
    try:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_L2CAP)
    except OSError as exc:
        return False, f"socket_error:{exc.errno}:{exc.strerror}", None, (time.monotonic() - started) * 1000.0
    try:
        sock.settimeout(connect_timeout)
        sock.bind((local_address, 0))
        sock.connect((candidate["address"], 0))
        connected = True
        connected_ms = (time.monotonic() - started) * 1000.0

        request = struct.pack("<BBH", L2CAP_ECHO_REQ, ident, len(payload)) + payload
        sock.settimeout(echo_timeout)
        sock.sendall(request)

        while True:
            response = sock.recv(65535)
            if len(response) < 4:
                continue
            code, response_ident, length = struct.unpack_from("<BBH", response)
            if response_ident != ident:
                continue
            if code == L2CAP_COMMAND_REJ:
                return False, "command_rejected", connected_ms, (time.monotonic() - started) * 1000.0
            if code == L2CAP_ECHO_RSP:
                valid = response[4 : 4 + length] == payload
                return valid, "echo_response" if valid else "payload_mismatch", connected_ms, (time.monotonic() - started) * 1000.0
    except socket.timeout:
        phase = "echo_timeout" if connected else "connect_timeout"
        return False, phase, None, (time.monotonic() - started) * 1000.0
    except OSError as exc:
        return False, f"oserror:{exc.errno}:{exc.strerror}", None, (time.monotonic() - started) * 1000.0
    finally:
        sock.close()


def main():
    parser = argparse.ArgumentParser(
        description="Batch-probe Bluetooth Classic NAP candidates while emitting SDR correlation timestamps."
    )
    parser.add_argument("--uap", required=True)
    parser.add_argument("--lap", required=True)
    parser.add_argument("--interface", default="hci0")
    parser.add_argument("--nap", action="append", default=[], help="priority NAP; repeatable")
    parser.add_argument("--full-scan", action="store_true")
    parser.add_argument("--oui-csv", default=str(DEFAULT_OUI_CSV))
    parser.add_argument("--connect-timeout", type=float, default=0.10)
    parser.add_argument("--echo-timeout", type=float, default=0.10)
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--randomize", action="store_true")
    parser.add_argument("--continue-after-hit", action="store_true")
    args = parser.parse_args()

    try:
        uap = clean_hex(args.uap, 2, "UAP")
        lap = clean_hex(args.lap, 6, "LAP")
        local_address = local_controller_address(args.interface)
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    priority = []
    for value in args.nap:
        nap = clean_hex(value, 4, "NAP")
        priority.append(
            {
                "nap": nap,
                "address": bdaddr(nap, uap, lap),
                "organization": "",
                "source": "priority",
            }
        )

    if args.full_scan:
        candidates = all_candidates(uap, lap)
    else:
        oui_csv = Path(args.oui_csv)
        if not oui_csv.exists():
            print(f"error: OUI CSV not found: {oui_csv}", file=sys.stderr)
            return 2
        candidates = oui_candidates(uap, lap, oui_csv)

    if args.randomize:
        random.shuffle(candidates)
    candidates = deduplicate(priority + candidates)
    if args.limit > 0:
        candidates = candidates[: args.limit]

    emit(
        "nap_probe_started",
        interface=args.interface,
        local_address=local_address,
        uap=uap,
        lap=lap,
        candidates=len(candidates),
        connect_timeout=args.connect_timeout,
        echo_timeout=args.echo_timeout,
    )

    payload = b"RF-Sentinel-NAP"
    hits = 0
    for index, candidate in enumerate(candidates):
        ident = 1 + (index % 254)
        emit("nap_probe_candidate", index=index, total=len(candidates), **candidate)
        ok, result, connected_ms, elapsed_ms = probe(
            candidate,
            local_address,
            args.connect_timeout,
            args.echo_timeout,
            payload,
            ident,
        )
        emit(
            "nap_probe_result",
            index=index,
            total=len(candidates),
            success=ok,
            result=result,
            connected_ms=connected_ms,
            elapsed_ms=round(elapsed_ms, 3),
            **candidate,
        )
        if ok:
            hits += 1
            if not args.continue_after_hit:
                break
        if args.delay > 0:
            time.sleep(args.delay)

    emit("nap_probe_finished", tested=index + 1 if candidates else 0, hits=hits)
    return 0 if hits else 1


if __name__ == "__main__":
    raise SystemExit(main())
