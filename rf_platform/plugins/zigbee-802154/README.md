# Zigbee / IEEE 802.15.4

CLI-first Zigbee / IEEE 802.15.4 stack scaffold.

This starts with the receive path:

- streams IQ from `sdr-gateway`
- tunes to a 2.4 GHz 802.15.4 channel
- detects bursts with an energy gate
- performs a first-pass DSSS chip despread / SFD search
- emits candidate PHY frames with confidence and payload bytes

This is an RX prototype, not a full production demodulator yet. The current decoder is intentionally simple:

- assumes channelized baseband close to the 2 Mchip/s PHY
- uses hard decisions on alternating quadrature chip slices
- validates frames from preamble + `0xA7` SFD + length byte

`killerbee` now lives under `resources/killerbee`.

## Layout

- `src/zigbee_802154/`: receiver package
- `tests/`: decoder and detector tests
- `resources/killerbee/`: reference tooling and parsers
- `config/config.txt`: optional local defaults

## Setup

```bash
cd /home/jake/workspace/SDR/RF_Sentinel/rf_platform/plugins/zigbee-802154
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

If your gateway needs auth:

```bash
export SDR_GATEWAY_API_TOKEN="<token>"
```

If your gateway is remote:

```bash
export SDR_GATEWAY_BASE_URL="http://127.0.0.1:8080"
```

## Usage

List visible SDRs:

```bash
zigbee-802154 devices
```

Listen on channel 11:

```bash
zigbee-802154 listen --channel 11
```

The default HackRF receive profile matches the tested XBee settings:

```bash
zigbee_802154 listen --device-id hackrf:0 --channel 25 --debug-bursts
```

That expands to an 8 Msps channelized receive path with LNA 16 dB, VGA 32 dB,
amp disabled, 6 MHz baseband filter, 0.2 ms pre-roll, 0.8 ms decode prefilter,
`-27 dBFS` minimum burst peak, `0/-25k/+25k` carrier search, waveform
correlation floor `0.18`, and a 4-worker / 32-slot live decode queue.

Listen on channel 20 and stop after 5 decoded frames:

```bash
zigbee-802154 listen --channel 20 --max-frames 5
```

Emit JSON lines:

```bash
zigbee-802154 listen --channel 15 --json
```

Capture raw IQ for offline analysis:

```bash
zigbee-802154 capture --device-id hackrf:0 --channel 26 --seconds 5 --output captures/xbee_ch26.cs8
```

Decode a captured file:

```bash
zigbee-802154 decode-file --input captures/xbee_ch26.cs8 --channel 26 --debug-bursts
```

Sweep the full 2.4 GHz IEEE 802.15.4 band using the SDR's widest sample rate:

```bash
zigbee_802154 wideband-listen --device-id hackrf:0 --debug-bursts
```

Force a specific wideband rate:

```bash
zigbee-802154 wideband-listen --sample-rate-sps 20000000
zigbee-802154 wideband-listen --sample-rate-sps 60000000
```

Wideband mode defaults to adaptive scan. It briefly checks each window,
records active channels from burst energy, then spends longer dwell time on windows with
activity. Active dwell recenters the SDR on the selected active channel by
default, so channel 25 is decoded at baseband instead of through a 2.5 MHz
offset channelizer. Per-channel extraction uses a windowed-sinc low-pass before
decimation so the decoder sees a cleaner channelized waveform. It periodically rescans so newly active channels can be
picked up. Use `--no-center-active-channel` to keep midpoint window centers,
`--no-adaptive-scan` to park on the anchor channel window, `--scan-all-windows`
to continuously rotate through every window, and `--decode-all-channels` to
decode every channel in each active window. A 20 MHz SDR covers the full band in
four windows. A 60 MHz SDR covers it in two overlapping full-width windows:
channels 11-22 and 15-26, so the tail of the band is not treated as a tiny
leftover window.
By default, adaptive active dwell follows channels/windows that have decoded
frames and falls back to the anchor channel, so energy-only ghosts do not steal
dwell time. Use `--follow-energy-only` when you intentionally want to chase raw
burst energy before frames decode.

If you need slower discovery for very sparse transmitters, add
`--discovery-dwell-s 3`.

## Next

- improve timing recovery and carrier tracking
- add PCAP export
- add MAC header parsing on top of raw PSDU extraction
- add TX path built from the same symbol/chip pipeline
