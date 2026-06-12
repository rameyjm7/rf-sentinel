from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TpmsFamilyMatch:
    family: str
    bit_offset: int
    bit_length: int
    fields: dict[str, object]


def parse_tpms_family(bits: str) -> TpmsFamilyMatch | None:
    for parser in (
        _parse_schrader_gg4,
        _parse_schrader_eg53ma4,
    ):
        match = parser(bits)
        if match is not None:
            return match
    return None


def _parse_schrader_gg4(bits: str) -> TpmsFamilyMatch | None:
    for offset, window in _iter_windows(bits, 64, extra_leading_bits=(0, 4)):
        payload = _bits_to_bytes(window)
        if payload is None or len(payload) != 8:
            continue
        if _crc8(payload[:7], poly=0x07, init=0xF0) != payload[7]:
            continue
        sensor_id = ((payload[1] & 0x0F) << 24) | (payload[2] << 16) | (payload[3] << 8) | payload[4]
        pressure_raw = int(payload[5])
        temperature_c = int(payload[6]) - 50
        flags = ((payload[0] & 0x0F) << 4) | (payload[1] >> 4)
        pressure_kpa = pressure_raw * 2.5
        if sensor_id == 0 or not (0.0 <= pressure_kpa <= 800.0) or not (-80 <= temperature_c <= 200):
            continue
        return TpmsFamilyMatch(
            family="schrader-gg4",
            bit_offset=offset,
            bit_length=64,
            fields={
                "model": "Schrader",
                "id": f"{sensor_id:07X}",
                "flags": f"{flags:02X}",
                "pressure_kpa": round(pressure_kpa, 1),
                "temperature_c": temperature_c,
                "integrity": "crc8_07_f0",
            },
        )
    return None


def _parse_schrader_eg53ma4(bits: str) -> TpmsFamilyMatch | None:
    for offset, window in _iter_windows(bits, 80, extra_leading_bits=(0, 40)):
        payload = _bits_to_bytes(window)
        if payload is None or len(payload) != 10:
            continue
        checksum = sum(payload[:9]) & 0xFF
        if checksum != payload[9]:
            continue
        sensor_id = (payload[4] << 16) | (payload[5] << 8) | payload[6]
        flags = int.from_bytes(payload[:4], byteorder="big", signed=False)
        pressure_kpa = payload[7] * 2.5
        temperature_f = int(payload[8])
        if sensor_id == 0 or not (0.0 <= pressure_kpa <= 800.0) or not (-80 <= temperature_f <= 300):
            continue
        return TpmsFamilyMatch(
            family="schrader-eg53ma4",
            bit_offset=offset,
            bit_length=80,
            fields={
                "model": "Schrader-EG53MA4",
                "id": f"{sensor_id:06X}",
                "flags": f"{flags:08X}",
                "pressure_kpa": round(pressure_kpa, 1),
                "temperature_f": temperature_f,
                "integrity": "sum8",
            },
        )
    return None


def _iter_windows(bits: str, width: int, extra_leading_bits: tuple[int, ...]) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    seen: set[tuple[int, int]] = set()
    for extra in extra_leading_bits:
        total = width + extra
        if len(bits) < total:
            continue
        for start in range(0, len(bits) - total + 1):
            offset = start + extra
            key = (offset, width)
            if key in seen:
                continue
            seen.add(key)
            out.append((offset, bits[offset : offset + width]))
    return out


def _bits_to_bytes(bits: str) -> bytes | None:
    if len(bits) % 8 != 0:
        return None
    out = bytearray()
    for offset in range(0, len(bits), 8):
        chunk = bits[offset : offset + 8]
        if any(bit not in {"0", "1"} for bit in chunk):
            return None
        out.append(int(chunk, 2))
    return bytes(out)


def _crc8(data: bytes, poly: int, init: int) -> int:
    crc = init & 0xFF
    for value in data:
        crc ^= value
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ poly) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc & 0xFF
