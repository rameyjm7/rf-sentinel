#!/usr/bin/env python3
"""MQTT publish/subscribe bridge for RF-Sentinel.

Publishes this unit's scan status (incl. GPS) and detections onto this
host's local MQTT broker, and subscribes to the same detections topic
plus a command topic - so a standalone mock-data publisher can feed this
backend's normal state/UI through the exact same ingestion path real
captures use, with no frontend changes.

Reuses the per-host Mosquitto broker already deployed for PASSIVE-SHIELD
(container `passive-shield-mqtt-broker`, 127.0.0.1:1883, anonymous) -
that's "the MQTT broker for each unit"; this module doesn't stand up a
new one.

Deliberately thin and callback-driven (unlike AirScope's bridge, which
calls straight into WifiScanManager) so it never needs to import from
app.py - app.py imports this module, so the reverse would be circular.
app.py wires ``status_provider``/``on_command``/``on_detection_message``
to its own real functions (``gps_status()``, ``start_scan()``/
``_stop_scan()`` via ``app.test_request_context()``, ``_append_detections()``).

Topics (unit defaults to socket.gethostname()):
    rf-sentinel/<unit>/status
    rf-sentinel/<unit>/detections
    rf-sentinel/<unit>/command
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover - optional at import time
    mqtt = None  # type: ignore[assignment]

STATUS_INTERVAL_SEC = 2.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


class MqttBridge:
    def __init__(
        self,
        *,
        status_provider: Callable[[], dict[str, Any]],
        on_command: Callable[[dict[str, Any]], None],
        on_detection_message: Callable[[dict[str, Any]], None],
    ) -> None:
        self.status_provider = status_provider
        self.on_command = on_command
        self.on_detection_message = on_detection_message
        self.unit = os.getenv("RF_SENTINEL_MQTT_UNIT") or socket.gethostname()
        self.host = os.getenv("RF_SENTINEL_MQTT_HOST", "127.0.0.1")
        self.port = int(os.getenv("RF_SENTINEL_MQTT_PORT", "1883"))
        self.enabled = _env_bool("RF_SENTINEL_MQTT_ENABLED", True) and mqtt is not None
        self.topic_status = f"rf-sentinel/{self.unit}/status"
        self.topic_detections = f"rf-sentinel/{self.unit}/detections"
        self.topic_command = f"rf-sentinel/{self.unit}/command"
        self.topic_command_response = f"rf-sentinel/{self.unit}/command/response"
        self._client: Any = None
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        if not self.enabled:
            logging.info(
                "mqtt_bridge: disabled (%s)",
                "paho-mqtt not installed" if mqtt is None else "RF_SENTINEL_MQTT_ENABLED=false",
            )
            return
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
            client_id=f"rf-sentinel-{self.unit}-{os.getpid()}",
            clean_session=True,
        )
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        try:
            client.connect_async(self.host, self.port, keepalive=30)
            client.loop_start()
        except Exception as exc:  # pragma: no cover - best effort
            logging.warning("mqtt_bridge: connect to %s:%s failed: %s", self.host, self.port, exc)
            return
        self._client = client
        threading.Thread(target=self._status_loop, daemon=True).start()
        logging.info("mqtt_bridge: starting, unit=%s broker=%s:%s", self.unit, self.host, self.port)

    def _on_connect(self, client: Any, _userdata: Any, _flags: Any, rc: int) -> None:
        if rc == 0:
            client.subscribe(self.topic_detections, qos=0)
            client.subscribe(self.topic_command, qos=0)
            logging.info("mqtt_bridge: connected, subscribed to %s and %s", self.topic_detections, self.topic_command)
        else:
            logging.warning("mqtt_bridge: connect rc=%s", rc)

    def _on_message(self, _client: Any, _userdata: Any, msg: Any) -> None:
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        try:
            if msg.topic == self.topic_detections:
                self.on_detection_message(payload)
            elif msg.topic == self.topic_command:
                self.on_command(payload)
        except Exception:
            logging.exception("mqtt_bridge: failed handling message on %s", msg.topic)

    # ---- publish ----

    def _status_loop(self) -> None:
        while True:
            try:
                self.publish_status()
            except Exception:
                logging.exception("mqtt_bridge: status publish failed")
            time.sleep(STATUS_INTERVAL_SEC)

    def publish_status(self) -> None:
        if self._client is None:
            return
        payload = {
            "schema": "rf_sentinel.station_status.v1",
            "unit": self.unit,
            "time_utc": _utc_now(),
            **self.status_provider(),
        }
        self._publish(self.topic_status, payload)

    def publish_detection(self, event: dict[str, Any]) -> None:
        """One MQTT message per detection - RF-Sentinel's detections are
        already discrete events (not raw frames), unlike AirScope's, so
        no batching/throttling is needed here."""
        if self._client is None:
            return
        payload = {
            "schema": "rf_sentinel.detection.v1",
            "unit": self.unit,
            "time_utc": _utc_now(),
            "event": event,
        }
        self._publish(self.topic_detections, payload)

    def publish_command_response(
        self,
        command: dict[str, Any],
        *,
        ok: bool,
        error: str | None = None,
    ) -> None:
        response = {
            "schema": "rf_sentinel.command_response.v1",
            "unit": self.unit,
            "time_utc": _utc_now(),
            "command": command,
            "request_id": command.get("request_id"),
            "ok": ok,
            "error": error,
        }
        self._publish(self.topic_command_response, response)

    def _publish(self, topic: str, payload: dict[str, Any]) -> None:
        if self._client is None:
            return
        try:
            self._client.publish(topic, json.dumps(payload, default=str), qos=0, retain=False)
        except Exception:  # pragma: no cover - best effort, never block a caller
            logging.exception("mqtt_bridge: publish to %s failed", topic)
