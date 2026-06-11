#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/capture_l2ping_rf.sh --addr BDADDR [options]
  scripts/capture_l2ping_rf.sh --manual [options]

Records raw wideband CF32 IQ from btsniffer while an l2ping/page attempt happens.
The output directory contains:
  capture.cf32          raw interleaved complex float32 IQ
  capture.cf32.meta     center/rate/format metadata from btsniffer
  btsniffer.log         sniffer event log
  results.txt           sniffer results
  l2ping.log            l2ping stdout/stderr, if --addr was used
  experiment.txt        command/timing notes

Options:
  --addr BDADDR         run l2ping against this address during recording
  --manual              do not run l2ping; you trigger it yourself
  --driver NAME         SoapySDR driver, default bladerf
  --freq-mhz MHz        center frequency, default 2452MHz
  --bandwidth-mhz MHz   capture bandwidth/bins, default 60MHz
  --seconds SEC         btsniffer processing buffer seconds, default 2
  --duration SEC        total recording duration, default 20
  --settle SEC          delay before l2ping starts, default 3
  --interface HCI       l2ping interface, default hci0
  --ping-seconds SEC    l2ping flood duration, default duration-settle-2
  --out DIR             output directory, default captures/YYYYmmddTHHMMSSZ
  --btsniffer PATH      btsniffer binary, default ./build-codex/btsniffer then ./build/btsniffer
USAGE
}

DRIVER=bladerf
FREQ_MHZ=2452MHz
BANDWIDTH_MHZ=60MHz
SECONDS=2
DURATION=20
SETTLE=3
INTERFACE=hci0
PING_SECONDS=""
OUT=""
BTSNIFFER=""
ADDR=""
MANUAL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --addr) ADDR="$2"; shift 2 ;;
    --manual) MANUAL=1; shift ;;
    --driver) DRIVER="$2"; shift 2 ;;
    --freq-mhz) FREQ_MHZ="$2"; shift 2 ;;
    --bandwidth-mhz) BANDWIDTH_MHZ="$2"; shift 2 ;;
    --seconds) SECONDS="$2"; shift 2 ;;
    --duration) DURATION="$2"; shift 2 ;;
    --settle) SETTLE="$2"; shift 2 ;;
    --interface) INTERFACE="$2"; shift 2 ;;
    --ping-seconds) PING_SECONDS="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --btsniffer) BTSNIFFER="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$ADDR" && "$MANUAL" -eq 0 ]]; then
  echo "error: pass --addr BDADDR or --manual" >&2
  usage >&2
  exit 2
fi
if [[ -n "$ADDR" && "$MANUAL" -eq 1 ]]; then
  echo "error: use either --addr or --manual, not both" >&2
  exit 2
fi

if [[ -z "$BTSNIFFER" ]]; then
  if [[ -x ./build-codex/btsniffer ]]; then
    BTSNIFFER=./build-codex/btsniffer
  elif [[ -x ./build/btsniffer ]]; then
    BTSNIFFER=./build/btsniffer
  else
    echo "error: could not find btsniffer binary" >&2
    exit 2
  fi
fi

if [[ -z "$OUT" ]]; then
  OUT="captures/$(date -u +%Y%m%dT%H%M%SZ)"
fi
mkdir -p "$OUT"
IQ="$OUT/capture.cf32"
LOG="$OUT/btsniffer.log"
PING_LOG="$OUT/l2ping.log"
EXP="$OUT/experiment.txt"

if [[ -z "$PING_SECONDS" ]]; then
  PING_SECONDS=$(( DURATION - SETTLE - 2 ))
  if [[ "$PING_SECONDS" -lt 1 ]]; then PING_SECONDS=1; fi
fi

{
  echo "start_utc=$(date -u --iso-8601=ns)"
  echo "cwd=$(pwd)"
  echo "btsniffer=$BTSNIFFER"
  echo "driver=$DRIVER"
  echo "freq_mhz=$FREQ_MHZ"
  echo "bandwidth_mhz=$BANDWIDTH_MHZ"
  echo "seconds=$SECONDS"
  echo "duration=$DURATION"
  echo "settle=$SETTLE"
  echo "manual=$MANUAL"
  echo "addr=$ADDR"
  echo "interface=$INTERFACE"
  echo "ping_seconds=$PING_SECONDS"
  echo "iq=$IQ"
  echo "log=$LOG"
} > "$EXP"

"$BTSNIFFER" \
  --driver "$DRIVER" \
  --freq-mhz "$FREQ_MHZ" \
  --bandwidth-mhz "$BANDWIDTH_MHZ" \
  --seconds "$SECONDS" \
  --fifo "$IQ" \
  --log "$LOG" \
  --record-only \
  > "$OUT/btsniffer.stdout" 2>&1 &
SNIFFER_PID=$!

cleanup() {
  if kill -0 "$SNIFFER_PID" 2>/dev/null; then
    kill -INT "$SNIFFER_PID" 2>/dev/null || true
    wait "$SNIFFER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

for _ in $(seq 1 200); do
  if [[ -f "$LOG" ]] && grep -q "streaming started" "$LOG"; then
    break
  fi
  if ! kill -0 "$SNIFFER_PID" 2>/dev/null; then
    echo "error: btsniffer exited before streaming started" >&2
    cat "$OUT/btsniffer.stdout" >&2 || true
    exit 1
  fi
  sleep 0.1
done
if ! grep -q "streaming started" "$LOG" 2>/dev/null; then
  echo "error: timed out waiting for btsniffer streaming started" >&2
  cat "$OUT/btsniffer.stdout" >&2 || true
  exit 1
fi
echo "streaming_ready_utc=$(date -u --iso-8601=ns)" >> "$EXP"

sleep "$SETTLE"
if [[ "$MANUAL" -eq 1 ]]; then
  echo "manual_ping_window_start_utc=$(date -u --iso-8601=ns)" >> "$EXP"
  echo "Manual mode: run your l2ping now. Recording for $DURATION seconds total. Output: $OUT"
else
  echo "l2ping_start_utc=$(date -u --iso-8601=ns)" >> "$EXP"
  timeout --signal=INT "$PING_SECONDS" l2ping -i "$INTERFACE" -f "$ADDR" > "$PING_LOG" 2>&1 || true
  echo "l2ping_end_utc=$(date -u --iso-8601=ns)" >> "$EXP"
fi

sleep_time=$(( DURATION - SETTLE ))
if [[ "$MANUAL" -eq 0 ]]; then
  sleep_time=2
fi
if [[ "$sleep_time" -gt 0 ]]; then sleep "$sleep_time"; fi

cleanup
trap - EXIT

if [[ -f results.txt ]]; then cp results.txt "$OUT/results.txt"; fi
{
  echo "end_utc=$(date -u --iso-8601=ns)"
  [[ -f "$IQ" ]] && echo "iq_bytes=$(stat -c%s "$IQ")"
  [[ -f "$IQ.meta" ]] && echo "meta=$IQ.meta"
} >> "$EXP"

echo "capture complete: $OUT"
