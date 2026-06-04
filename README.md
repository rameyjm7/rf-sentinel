# Bluetooth Explorer

Bluetooth Explorer is a sibling app to AetherCast that uses `sdr-gateway` IQ streams to explore Bluetooth activity.

It currently supports:

- BLE advertising-channel scanning on channels 37, 38, and 39.
- BT Classic single-channel scanning across channels 0 through 78.
- Classic LAP extraction from the access code.
- UAP candidate brute forcing from the packet header using the HEC/whitening procedure from the included `research/btsniffer` code.
- Per-LAP candidate pruning across repeated packets using the 625 us slot clock relationship.
- LAP identity tracking that displays `UAP XXX` until the UAP candidate set resolves.
- Optional browser-controlled channel hopping with a configurable dwell time.
- 60 MHz BT Classic bank capture on bladeRF that splits the stream into Classic channel lanes and decodes them together.
- Separate BTC and BTLE SDR selection, so BTC can use `bladerf:0` while BTLE uses `hackrf:0`.
- A 79-channel Classic activity chart with one vertical bar per channel.

## Project Layout

- `backend/app.py`: Flask API, gateway stream control, BLE detector, BT Classic LAP/UAP tracker.
- `frontend/index.html`: browser UI for SDR controls, RF health, discoveries, and UAP candidates.
- `research/`: the referenced paper and corresponding `btsniffer` code.

## Requirements

- Running `sdr-gateway` instance (`http://127.0.0.1:8080` default).
- Python 3.10+.
- An SDR device visible in `sdr-gateway /devices`.

## Setup

```bash
cd /home/jake/workspace/SDR/BluetoothExplorer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

If `sdr-gateway` auth is enabled:

```bash
export SDR_GATEWAY_API_TOKEN="<your-token>"
```

Optional base URL override:

```bash
export SDR_GATEWAY_BASE_URL="http://127.0.0.1:8080"
```

## Run

```bash
cd /home/jake/workspace/SDR/BluetoothExplorer
source .venv/bin/activate
python3 backend/app.py
```

Open:

- `http://127.0.0.1:5050`

The UI defaults to BTC enabled. For combined scanning, enable both `BTC` and `BTLE`; BTC defaults to a bladeRF device at `60` MHz and BTLE defaults to a HackRF device. The single gain slider drives both LNA and VGA gain values. Set the dwell seconds and press the Start/Stop button to rotate BTC banks while BTLE cycles advertising channels 37/38/39.

## Notes

- The BT Classic implementation is based on `research/btsniffer/full-band/sources/frame-processing/btdecoder.cpp`.
- The paper's strongest setup captures many channels concurrently. This app uses one `sdr-gateway` stream at a time, so Classic resolution depends heavily on catching repeated packets for the same LAP on the tuned channel.
- A resolved UAP still gives `NAP:UAP:LAP` only as `??:UAP:LAP`; discovering the NAP requires additional protocol evidence beyond this header brute-force step.
