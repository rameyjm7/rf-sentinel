# Bluetooth Low Energy

Standalone BLE advertising receiver plugin for RF Sentinel.

The first CLI focuses on passive advertising-channel reception through
`sdr-gateway`.

```bash
ble_scanner scan --device-id hackrf:0
ble_scanner scan --device-id hackrf:0 --summary-interval-s 2 --top 20
ble_scanner band-scan --replace-existing
ble_scanner listen --device-id hackrf:0 --channel 37
ble_scanner listen --device-id hackrf:0 --channel 38 --json
ble_scanner listen --device-id hackrf:0 --hop
ble_scanner wideband-listen --device-id bladerf:0 --center-freq-hz 2414000000 --sample-rate-sps 60000000
```

BLE advertising channels:

- `37`: 2402 MHz
- `38`: 2426 MHz
- `39`: 2480 MHz

## Wideband Notes

The original UI's main BTLE behavior was a narrow 2 MHz stream that hopped
advertising channels every 0.25 seconds while preserving discoveries. Use this
to match it:

```bash
ble_scanner scan --device-id hackrf:0
```

`wideband-listen` is a separate mode: one wide IQ stream is mixed into BLE
advertising lanes, decimated to 2 Msps per lane, and each lane is passed to the
same BLE advertising decoder. It only sees the advertising channels that fit
inside the tuned bandwidth.

## Text Output

`ble_scanner scan` defaults to grouped text summaries, similar to the RF
Sentinel UI cards:

- manufacturer group
- device count and detection count
- best RSSI, last-seen age, observed channels
- per-device MAC, local name or inferred identity, UUID16, manufacturer ID,
  manufacturer-data prefix, and inferred type/detail

Use `--events` for one decoded-packet line per advertisement, or `--json` for
machine-readable events.

## Two-SDR Band Coverage

For BLE advertising coverage with both available radios:

```bash
ble_scanner band-scan --replace-existing
```

Defaults:

- `bladerf:0` at `2430 MHz / 61.44 MHz`, channelized for BLE adv `37` and `38`
- `hackrf:0` at `2471 MHz / 20 MHz`, channelized for BLE adv `39`

This intentionally overlaps the windows a little. A mathematically exact
`2432 MHz / 60 MHz` lower window puts BLE channel 37 directly on the lower edge,
where real SDR filter rolloff can make it disappear.

Both streams feed one text reporter, so manufacturer/device summaries are
merged across the two radios.

A 60 MHz stream centered at 2414 MHz covers BLE advertising channels 37 and 38.
It does not cover channel 39 at 2480 MHz because that channel is 66 MHz above
center. To cover all BLE advertising channels with the current radios, use:

- bladeRF at 2442 MHz / 60 MHz for channels 37 and 38
- HackRF at 2480 MHz / 20 MHz for channel 39

Example second process for channel 39:

```bash
ble_scanner listen --device-id hackrf:0 --channel 39 --sample-rate-sps 20000000 --baseband-filter-hz 20000000
```
