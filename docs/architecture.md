# RF Sentinel Architecture

## Role Of This Repository

`RF_Sentinel` is the first product shell for the RF Intelligence Platform. The existing Flask backend and browser UI live under `ui/` as the first dashboard, while protocol-specific decoders and sniffers live under `rf_platform/plugins/`.

## Target Layout

```text
RF_Sentinel/
  ui/
    backend/               Existing Flask API and live gateway integration
    frontend/              Existing browser UI, evolving into platform dashboard
  rf_platform/             Shared schemas, entity resolution, analytics, storage
    plugins/
      bluetooth-classic/   Bluetooth Classic sniffer plugin
      zigbee-802154/       Zigbee / IEEE 802.15.4 receiver plugin
  docs/                    Product, architecture, and milestone docs
```

## Data Flow

```text
RF sensors / adapters / SDR streams
  -> capture or protocol plugin
  -> normalized RFEvent
  -> local event store
  -> entity resolver
  -> analytics and alerts
  -> API / dashboard / exports
```

## Core Concepts

### Observation

A raw or decoded event from a protocol source. Examples include BLE advertisement, BTC LAP hit, WiFi beacon, TPMS frame, Zigbee data frame, or unknown SDR burst.

### Entity

A tracked thing inferred from one or more observations. Entity identity may be complete, partial, or probabilistic depending on the protocol.

### Sensor

The collection node or hardware source that observed an event. Single-node support comes first; multi-node correlation comes later.

### Protocol Plugin

A protocol-specific collector or decoder that emits normalized events and optional raw metadata.

## Passive-Core Boundary

The core platform is passive. Any replay, synthetic signal generation, spoof/resilience testing, or active RF effects belong in a separate authorized lab module with explicit enablement, audit logs, and hardware allowlists.
