# RF Sentinel Docker image

Containerizes the RF Sentinel dashboard (`ui/backend/app.py`) plus all
protocol plugins, so it can be deployed to a station (e.g. PASSIVE-SHIELD
station1, `10.139.1.160`) without depending on whatever's installed
natively on that host's Python.

The image runs the project's own `install.sh` at build time (creates
`.venv`, `pip install -e` for the root package and every
`rf_platform/plugins/*`, compiles the Bluetooth Classic C++ plugin via
CMake) rather than hand-rolling a subset — this is a full parity build,
not a stripped-down one.

## Build

From the repo root (context matters — Dockerfile references paths
relative to root):

```
docker build -f docker/Dockerfile -t rf-sentinel:latest .
```

## Deploy (actual production config: `rfiq`)

This is what's actually running on station1 and dev-desktop today —
shares the radio with sdr-shark and the shared `bt-detector` sidecar
through `rfiq_daemon` (see `rf-iq-gateway`) instead of opening SoapySDR
directly:

```
docker run -d \
  --name rf-sentinel \
  --restart unless-stopped \
  --network host \
  -v /tmp:/tmp \
  -e RF_SENTINEL_HOST=0.0.0.0 \
  -e RF_SENTINEL_PORT=5050 \
  -e RF_SENTINEL_TEXTUAL_CONSOLE=0 \
  -e SDR_BACKEND=rfiq \
  -e SDR_RFIQ_SOCKET=/tmp/rfiq0.sock \
  -e SDR_RFIQ_CONTROL_SOCKET=/tmp/rfiq0-control.sock \
  -e SDR_GATEWAY_BASE_URL=http://127.0.0.1:8080 \
  rf-sentinel:latest
```

`SDR_GATEWAY_BASE_URL` is still set out of habit/parity with the
original design, but `sdr-gateway` isn't actually running on any host
today - see "Known gap" below for the one piece that still can't fully
do without it.

## Deploy (older: direct SoapySDR via `sdr-gateway`)

The original design point, before `rfiq_daemon` existed - the dashboard
talks to a separate `sdr-gateway` HTTP service (not part of this repo)
for device access instead of a direct SoapySDR caller. Not what's
actually deployed anywhere currently; kept here for reference in case
`sdr-gateway` comes back into the picture:

```
docker run -d \
  --name rf-sentinel \
  --restart unless-stopped \
  --network host \
  -v /dev/bus/usb:/dev/bus/usb \
  --device-cgroup-rule="c 189:* rmw" \
  --group-add plugdev \
  -v rf-sentinel-data:/opt/rf-sentinel/ui/backend/data \
  -e SDR_GATEWAY_BASE_URL=http://127.0.0.1:8080 \
  rf-sentinel:latest
```

`--network host` is what lets it reach `sdr-gateway` at
`127.0.0.1:8080`. The Bluetooth Classic C++ binary is the exception in
either mode — it does open SoapySDR directly (see below), not through
either the gateway or `rfiq_daemon`.

## Known gap: standalone BLE-only scans still need `sdr-gateway`

**Fixed 2026-08-09:** the device picker (`_available_devices()` /
`/api/devices`) used to unconditionally call `_fetch_gateway_devices()`
against `SDR_GATEWAY_BASE_URL`, so it always showed "no SDRs are
available from sdr-gateway" in the `rfiq` deploy mode above. It now has
an `_rfiq_available_devices()` fallback: when `SDR_BACKEND=rfiq`, it
synthesizes a device entry as long as `SDR_RFIQ_SOCKET` actually exists
on disk (i.e. `rfiq_daemon` is running), instead of ever touching the
gateway. `SDR_RFIQ_FREQ_MIN_HZ`/`_MAX_HZ`/`SDR_RFIQ_MAX_SAMPLE_RATE_SPS`
override the advertised tuning range/rate if the deployed radio isn't a
bladeRF 2.0 micro (the default assumption) - a bladeRF1 x40, for
example, has a much narrower real tuning range and device pickers that
filter by frequency need to know that.

**Still open:** starting a scan from the UI works for the combined
Bluetooth Classic+LE mode (`bluetooth_scanner` binary, which already
has real `rfiq_daemon` socket support - same one the shared
`bt-detector` sidecar uses), but standalone BLE-only scans
(`ble_scanner`'s `iq-sweep` subcommand) do not - confirmed via
`ble_scanner iq-sweep --help`, it has no `--iq-source`/`--rfiq-socket`
flags at all. That's a compiled-binary gap (would need changes to the
`ble_scanner` C/C++ source and a rebuild), not something fixable in
`ui/backend/app.py` or `rf_platform/scanner.py` alone.

## Why `docker/soapy-build/`

Same finding as `passive-shield/station1-docker/`, reused here because
the Bluetooth Classic plugin (`rf_platform/plugins/bluetooth-classic`,
`CMakeLists.txt`) links directly against SoapySDR + a source-built
`libbladeRF.so.2`:

- apt has no SoapySDR dev package for this arch/repo; headers are
  custom-built on-device at `~/soapy/SoapySDR`.
- apt's `libbladerf2` (0.2021.10-2, Dec 2021) cannot properly drive
  station1's radios after their firmware/FPGA update (confirmed
  separately: `readStream()` capped at ~508 samples/call instead of
  65536, chronic RX FIFO overflow, zero real detections despite USB3
  SuperSpeed enumerating fine). The known-good build must overwrite the
  apt copy **at its own path**
  (`/usr/lib/aarch64-linux-gnu/libbladeRF.so.2`) — `ld.so.cache` kept
  preferring that directory over `/usr/local/lib` regardless of a
  higher-priority copy being placed there too.

## Container-specific build gotchas already worked through

- `python3-dev` is required (`Python.h`) — `zigbee-802154` has a C
  extension (`_cdecode.c`).
- `libboost-program-options-dev`, `libboost-system-dev`,
  `libboost-thread-dev`, `libfftw3-dev` — Bluetooth Classic's CMake
  build needs Boost ≥1.46 and `fftw3f`.
- `tshark` — `wifi-80211` depends on `pyshark`, which shells out to it.
  `wireshark-common`'s postinst prompts (via debconf) about non-root
  packet capture; preseeded to decline non-interactively since we run
  as root.
- Plain `pip install .` against the root `pyproject.toml` silently
  built an unnamed `UNKNOWN-0.0.0` package with no `console_scripts`
  (some metadata-parsing issue with the pip/setuptools version here,
  possibly the PEP 639 `license = "..."` string) — running the real
  `install.sh` (which creates a proper venv with an upgraded pip first)
  avoided this; a hand-written entry point isn't needed with the full
  build.

## Verified working

Deployed to station1 and run with all protocols enabled
(`mode: sentinel`) — confirmed real decoded output end-to-end (FM
broadcast stations with plausible pilot-tone/RDS metrics; both
BladeRFs actively in use, one dedicated to BTC/BLE, the other
hopping Zigbee/TPMS/FM).
