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

## Deploy

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
`127.0.0.1:8080` — the dashboard is an HTTP client of that service for
device access, not a direct SoapySDR caller, so USB passthrough here is
a fallback rather than the primary path. The Bluetooth Classic C++
binary is the exception (see below) — it does open SoapySDR directly.

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
