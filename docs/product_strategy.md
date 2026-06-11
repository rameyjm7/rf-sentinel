# RF Intelligence Platform Product Strategy

## Product Sentence

A multi-protocol RF intelligence platform that passively discovers and tracks nearby wireless devices across Bluetooth, WiFi, TPMS, Zigbee/802.15.4, drone/UAS, and SDR-observed signals, with optional authorized test/effects modules for defense and lab environments.

## Core Positioning

Lead with passive RF discovery, protocol intelligence, and pattern-of-life analytics. This keeps the product useful for commercial security, field surveys, defense awareness, lab validation, and research without making sensitive active capabilities the center of the story.

## Core Platform: Passive RF Intelligence

- Bluetooth Classic LAP/UAP discovery and partial identity tracking.
- BLE advertisement discovery and metadata extraction.
- WiFi monitor-mode observation of probes, beacons, APs, clients, and RSSI.
- TPMS detection and recurring sensor ID tracking.
- Zigbee / IEEE 802.15.4 packet visibility.
- SDR-based protocol classification for known and unknown bursts.
- Unified entity resolution across protocol-specific identifiers.
- Pattern-of-life timelines, recurring presence, co-occurrence, and alerts.
- Dashboards, exports, reports, and replayable sessions.

## Optional Defense/Test Modules

These should remain separate, explicitly enabled, permissioned, and audit-logged.

- Authorized signal replay in controlled environments.
- Synthetic RF generation and emitter simulation.
- Spoof/resilience testing.
- Adversarial ML robustness testing.
- Deception / countermeasure research hooks.
- Hardware allowlists and lab-mode banners.

## First Proof Of Concept

1. Passive BLE + Bluetooth Classic discovery.
2. WiFi monitor-mode ingestion.
3. TPMS decoder integration.
4. Zigbee / IEEE 802.15.4 ingestion.
5. Unified entity database.
6. Pattern-of-life dashboard.
7. SDR ML classifier for unknown/protocol-level RF bursts.

## Later Expansion

1. Authorized replay/simulation module.
2. Spoof-detection and spoof-resilience testing.
3. Multi-node geolocation / movement tracking.
4. Report generation for field operations, security audits, and lab validation.
