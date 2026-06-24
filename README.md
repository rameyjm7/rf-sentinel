# RF Sentinel

RF Sentinel is a passive multi-protocol RF intelligence platform. The current app is the first live dashboard and capture front end; it already uses `sdr-gateway` IQ streams for BLE and Bluetooth Classic discovery, and now hosts protocol plugins for additional RF families.

## License

RF Sentinel itself is proprietary commercial software licensed under the
`RF Sentinel Commercial License`; see [`LICENSE`](LICENSE).

Some plugins are distributed under separate open-source licenses. In particular,
`rf_platform/plugins/bluetooth-classic` is a GPLv3 submodule based on
[`bsnet/btsniffer`](https://github.com/bsnet/btsniffer). See
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

Product sentence:

> A multi-protocol RF intelligence platform that passively discovers and tracks nearby wireless devices across Bluetooth, WiFi, TPMS, Zigbee/802.15.4, drone/UAS, and SDR-observed signals, with optional authorized test/effects modules for defense and lab environments.

The core product stays passive: RF discovery, protocol intelligence, entity resolution, pattern-of-life analytics, dashboards, alerts, and reports. Active replay/simulation/effects work belongs in a separate authorized lab module with explicit controls.

## Current Capabilities

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
- Zigbee / IEEE 802.15.4 receiver plugin under `rf_platform/plugins/zigbee-802154`.
- Sub-GHz / TPMS receiver plugin under `rf_platform/plugins/subghz-stack`.
- Multi-protocol scanner CLI that runs BTC on one SDR while time-slicing BLE, Zigbee, and TPMS on another SDR.

## Project Layout

- `ui/backend/app.py`: Flask API, gateway stream control, BLE detector, BT Classic LAP/UAP tracker.
- `ui/frontend/index.html`: browser UI for SDR controls, RF health, discoveries, and UAP candidates.
- `rf_platform/`: shared normalized event and entity primitives for the broader platform.
- `rf_platform/plugins/bluetooth-classic/`: Bluetooth Classic sniffer plugin.
- `rf_platform/plugins/bluetooth-lowenergy/`: BLE advertising receiver plugin.
- `rf_platform/plugins/zigbee-802154/`: Zigbee / IEEE 802.15.4 receiver plugin.
- `rf_platform/plugins/subghz-stack/`: Sub-GHz / TPMS receiver plugin.
- `docs/`: product strategy, architecture, and milestone roadmap.
- `research/`: the referenced paper and corresponding `btsniffer` code.

## Platform Roadmap

The milestone plan lives in:

- `docs/product_strategy.md`
- `docs/architecture.md`
- `docs/milestones.md`
- `/home/jake/workspace/SDR/RF_Intelligence_Platform_Milestone_Plan.docx`

Near-term build order:

1. Normalize all observations into `rf_platform.RFEvent`.
2. Add a SQLite event store and replay/export mode.
3. Feed BLE and Bluetooth Classic detections into the event store.
4. Feed Zigbee / 802.15.4 plugin frames into the event store.
5. Add WiFi monitor-mode ingestion.
6. Add TPMS ingestion.
7. Build entity resolution and pattern-of-life dashboard views.

## Requirements

- Running `sdr-gateway` instance (`http://127.0.0.1:8080` default).
- Python 3.10+.
- An SDR device visible in `sdr-gateway /devices`.

## Setup

```bash
cd /home/jake/workspace/SDR/RF_Sentinel
./install.sh
```

`install.sh` creates/updates the Python venv and rebuilds the native Bluetooth
Classic sniffer plugin for the current machine architecture. This matters when
deploying between `x86_64` and `aarch64`; copied binaries are not portable.

At runtime, `ui/backend/app.py` also checks the Bluetooth Classic sniffer binary
before launch. If it is missing, stale, points at an old CMake source directory,
or has the wrong architecture, it will rebuild automatically unless
`BTC_SNIFFER_AUTO_BUILD=0` is set.

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
cd /home/jake/workspace/SDR/RF_Sentinel
source .venv/bin/activate
python3 ui/backend/app.py
```

Open:

- `http://127.0.0.1:5050`

The UI defaults to BTC enabled. For combined scanning, enable both `BTC` and `BTLE`; BTC defaults to a bladeRF device at `60` MHz and BTLE defaults to a HackRF device. The single gain slider drives both LNA and VGA gain values. Set the dwell seconds and press the Start/Stop button to rotate BTC banks while BTLE cycles advertising channels 37/38/39.

Run the multi-protocol CLI scanner:

```bash
rf_sentinel_scan
```

Default scanner layout:

- `bladerf:0` runs Bluetooth Classic continuously at `2442 MHz` / `60 MHz`.
- `hackrf:0` time-slices BLE, Zigbee/802.15.4, and TPMS.
- BLE uses the gateway-managed HackRF IQ sweep.
- Zigbee defaults to the known-good XBee channel 25 settings.
- TPMS auto-hops known `315 MHz` and `433.92 MHz` bands.

Example with explicit radios and shorter slices:

```bash
rf_sentinel_scan \
  --btc-device-id bladerf:0 \
  --hop-device-id hackrf:0 \
  --ble-slice-s 15 \
  --zigbee-slice-s 15 \
  --tpms-slice-s 15
```

## Notes

- The BT Classic implementation is based on `research/btsniffer/full-band/sources/frame-processing/btdecoder.cpp`.
- The paper's strongest setup captures many channels concurrently. This app uses one `sdr-gateway` stream at a time, so Classic resolution depends heavily on catching repeated packets for the same LAP on the tuned channel.
- A resolved UAP still gives `NAP:UAP:LAP` only as `??:UAP:LAP`; discovering the NAP requires additional protocol evidence beyond this header brute-force step.
