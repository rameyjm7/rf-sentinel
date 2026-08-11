#!/usr/bin/env python3
"""Publish synthetic RF-Sentinel detections onto a unit's MQTT broker.

Lets you demo the real RF-Sentinel dashboard - live, interactive - without
any radio hardware. Stop the real scan on the target unit (or just don't
start it) and run this pointed at that same unit's hostname; the
backend's own MqttBridge subscriber ingests these messages through the
exact same _append_detections() path a live capture uses, so the UI
updates as if it were real. (The backend only ingests MQTT detections
while it isn't running a real scan itself - see mqtt_bridge wiring in
ui/backend/app.py - so stop any real scan first.)

Reuses the same anonymized manufacturer-style identities already used in
RF-Sentinel's own sanitized docs/media/rf-sentinel-full-scan.jpg
screenshot ("Apple, Inc.", "Microsoft", masked MACs) so a live demo
visually matches the real product's existing public material - none of
this is derived from any real capture.

Usage:
    python3 mqtt_mock_publisher.py --unit passive-shield-01
    python3 mqtt_mock_publisher.py --mqtt-host 10.139.1.160 --unit passive-shield-01
"""

from __future__ import annotations

import argparse
import json
import random
import socket
import time
from datetime import datetime, timezone

try:
    import paho.mqtt.client as mqtt
except ImportError as exc:  # pragma: no cover
    raise SystemExit("mqtt_mock_publisher.py requires paho-mqtt (pip install paho-mqtt)") from exc

# (address, name, manufacturer company name, uuid16_names)
SYNTHETIC_BLE_DEVICES = [
    ("aa:bb:cc:10:50:91", "", "Apple, Inc.", ["Continuity"]),
    ("aa:bb:cc:10:50:92", "", "Apple, Inc.", ["Apple manufacturer frame 0908"]),
    ("aa:bb:cc:10:50:93", "", "Apple, Inc.", ["Continuity"]),
    ("aa:bb:cc:20:60:01", "", "Microsoft", ["Microsoft manufacturer frame 0109"]),
    ("aa:bb:cc:30:70:11", "Field Beacon 01", "", []),
]

SYNTHETIC_CLASSIC = [
    # (nap, uap, lap)
    ("XXXX", "CA", "FF33BE"),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def jitter_rssi(base: float) -> float:
    return round(max(-100.0, min(-20.0, base + random.uniform(-4, 4))), 1)


def build_ble_event(now: float) -> dict:
    address, name, company_name, uuid16_names = random.choice(SYNTHETIC_BLE_DEVICES)
    manufacturer = {"company_name": company_name, "name": company_name} if company_name else None
    return {
        "kind": "ble_adv",
        "seen_at": now,
        "address": address,
        "address_type": random.choice(["random", "public"]),
        "name": name,
        "uuid16": [],
        "uuid16_names": uuid16_names,
        "manufacturer": manufacturer,
        "appearance": None,
        "rssi_dbfs": jitter_rssi(-45),
        "channel": random.choice([37, 38, 39]),
        "center_freq_hz": 2_402_000_000,
    }


def build_classic_event(now: float) -> dict:
    nap, uap, lap = random.choice(SYNTHETIC_CLASSIC)
    return {
        "kind": "classic_lap",
        "seen_at": now,
        "lap": lap,
        "uap": uap,
        "nap": nap,
        "type": "lap_seen",
        "status": "active piconet",
        "rssi_dbfs": jitter_rssi(-34),
        "channel": 65,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mqtt-host", default="127.0.0.1")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--unit", default=socket.gethostname(), help="must match the target backend's own hostname/unit")
    parser.add_argument("--interval", type=float, default=1.5)
    args = parser.parse_args()

    topic_detections = f"rf-sentinel/{args.unit}/detections"

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION1, client_id=f"rf-sentinel-mock-{args.unit}")
    client.connect(args.mqtt_host, args.mqtt_port, keepalive=30)
    client.loop_start()

    print(f"mqtt_mock_publisher: publishing to {args.mqtt_host}:{args.mqtt_port} as unit={args.unit} (Ctrl-C to stop)", flush=True)

    try:
        while True:
            now = time.time()
            event = build_classic_event(now) if random.random() < 0.15 else build_ble_event(now)
            payload = {
                "schema": "rf_sentinel.detection.v1",
                "unit": args.unit,
                "time_utc": utc_now(),
                "event": event,
            }
            client.publish(topic_detections, json.dumps(payload), qos=0)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
