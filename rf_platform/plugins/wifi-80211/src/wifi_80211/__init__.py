"""RF Sentinel WiFi / IEEE 802.11 SDR plugin."""

from .activity import ActivityEvent, detect_events, wifi_stf_metric
from .demodulator import WiFiActivityDemodulator, channel_from_frequency_hz
from .mac80211 import MacFrameInfo, RadiotapPcapWriter, parse_mac_frame
from .pyshark_io import normalize_frame_event, read_jsonl_events, read_pcap_events

__all__ = [
    "ActivityEvent",
    "MacFrameInfo",
    "RadiotapPcapWriter",
    "WiFiActivityDemodulator",
    "channel_from_frequency_hz",
    "detect_events",
    "parse_mac_frame",
    "normalize_frame_event",
    "read_jsonl_events",
    "read_pcap_events",
    "wifi_stf_metric",
]
