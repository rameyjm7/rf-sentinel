from __future__ import annotations

from dataclasses import dataclass
import struct
import time
from typing import BinaryIO


FRAME_TYPES = {
    0: "management",
    1: "control",
    2: "data",
    3: "extension",
}

MANAGEMENT_SUBTYPES = {
    0: "association-request",
    1: "association-response",
    2: "reassociation-request",
    3: "reassociation-response",
    4: "probe-request",
    5: "probe-response",
    8: "beacon",
    9: "atim",
    10: "disassociation",
    11: "authentication",
    12: "deauthentication",
    13: "action",
    14: "action-no-ack",
}

CONTROL_SUBTYPES = {
    7: "control-wrapper",
    8: "block-ack-request",
    9: "block-ack",
    10: "ps-poll",
    11: "rts",
    12: "cts",
    13: "ack",
    14: "cf-end",
    15: "cf-end-cf-ack",
}

DATA_SUBTYPES = {
    0: "data",
    1: "data-cf-ack",
    2: "data-cf-poll",
    3: "data-cf-ack-cf-poll",
    4: "null",
    5: "cf-ack",
    6: "cf-poll",
    7: "cf-ack-cf-poll",
    8: "qos-data",
    9: "qos-data-cf-ack",
    10: "qos-data-cf-poll",
    11: "qos-data-cf-ack-cf-poll",
    12: "qos-null",
    14: "qos-cf-poll",
    15: "qos-cf-ack-cf-poll",
}


def mac_addr(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw)


@dataclass(frozen=True)
class MacFrameInfo:
    frame_type: str
    subtype: str
    duration: int | None
    receiver: str | None
    transmitter: str | None
    destination: str | None
    source: str | None
    bssid: str | None
    sequence: int | None
    ssid: str | None
    length: int


def parse_mac_frame(frame: bytes) -> MacFrameInfo:
    if len(frame) < 2:
        raise ValueError("802.11 frame is too short")

    frame_control = int.from_bytes(frame[0:2], "little")
    type_id = (frame_control >> 2) & 0x3
    subtype_id = (frame_control >> 4) & 0xF
    to_ds = bool(frame_control & (1 << 8))
    from_ds = bool(frame_control & (1 << 9))
    frame_type = FRAME_TYPES.get(type_id, f"type-{type_id}")
    subtype = _subtype_name(type_id, subtype_id)
    duration = int.from_bytes(frame[2:4], "little") if len(frame) >= 4 else None

    addr1 = mac_addr(frame[4:10]) if len(frame) >= 10 else None
    addr2 = mac_addr(frame[10:16]) if len(frame) >= 16 else None
    addr3 = mac_addr(frame[16:22]) if len(frame) >= 22 else None
    addr4 = mac_addr(frame[24:30]) if len(frame) >= 30 and to_ds and from_ds else None
    sequence = int.from_bytes(frame[22:24], "little") >> 4 if len(frame) >= 24 else None

    receiver = addr1
    transmitter = addr2 if type_id != 1 or subtype_id in {8, 11} else None
    destination = None
    source = None
    bssid = None

    if type_id == 0:
        destination, source, bssid = addr1, addr2, addr3
    elif type_id == 1:
        if subtype_id in {11, 12, 13, 14, 15}:
            receiver = addr1
            transmitter = addr2
    elif type_id == 2:
        if not to_ds and not from_ds:
            destination, source, bssid = addr1, addr2, addr3
        elif to_ds and not from_ds:
            bssid, source, destination = addr1, addr2, addr3
        elif not to_ds and from_ds:
            destination, bssid, source = addr1, addr2, addr3
        else:
            receiver, transmitter, destination, source = addr1, addr2, addr3, addr4

    return MacFrameInfo(
        frame_type=frame_type,
        subtype=subtype,
        duration=duration,
        receiver=receiver,
        transmitter=transmitter,
        destination=destination,
        source=source,
        bssid=bssid,
        sequence=sequence,
        ssid=_ssid(frame, type_id, subtype_id),
        length=len(frame),
    )


def _subtype_name(type_id: int, subtype_id: int) -> str:
    if type_id == 0:
        return MANAGEMENT_SUBTYPES.get(subtype_id, f"management-{subtype_id}")
    if type_id == 1:
        return CONTROL_SUBTYPES.get(subtype_id, f"control-{subtype_id}")
    if type_id == 2:
        return DATA_SUBTYPES.get(subtype_id, f"data-{subtype_id}")
    return f"subtype-{subtype_id}"


def _ssid(frame: bytes, type_id: int, subtype_id: int) -> str | None:
    if type_id != 0 or subtype_id not in {0, 2, 4, 5, 8}:
        return None
    fixed_len = 12 if subtype_id in {0, 2, 5, 8} else 0
    pos = 24 + fixed_len
    while pos + 2 <= len(frame):
        tag = frame[pos]
        size = frame[pos + 1]
        pos += 2
        value = frame[pos : pos + size]
        if len(value) != size:
            return None
        if tag == 0:
            return value.decode("utf-8", "replace")
        pos += size
    return None


class RadiotapPcapWriter:
    """Writes 802.11 frames as DLT_IEEE802_11_RADIO PCAP records."""

    LINKTYPE_IEEE802_11_RADIOTAP = 127

    def __init__(self, fh: BinaryIO):
        self._fh = fh
        self._fh.write(
            struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, self.LINKTYPE_IEEE802_11_RADIOTAP)
        )

    def write(self, frame: bytes, *, ts: float | None = None) -> None:
        if ts is None:
            ts = time.time()
        sec = int(ts)
        usec = int((ts - sec) * 1_000_000)
        radiotap = b"\x00\x00\x08\x00\x00\x00\x00\x00"
        packet = radiotap + frame
        self._fh.write(struct.pack("<IIII", sec, usec, len(packet), len(packet)))
        self._fh.write(packet)
        self._fh.flush()
