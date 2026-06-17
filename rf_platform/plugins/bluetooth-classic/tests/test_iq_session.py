from __future__ import annotations

import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from bluetooth_classic.cli import (
    _build_parser,
    _iq_metadata,
    _iter_iq_playback_chunks,
    _resolve_iq_playback_path,
    _write_iq_metadata,
)


def _args(**overrides: object) -> Namespace:
    values: dict[str, object] = {
        "device_id": "bladerf:0",
        "driver": "bladerf",
        "center_mhz": 2442.0,
        "bandwidth_mhz": 60,
        "iq_capture_path": None,
        "iq_playback_path": None,
    }
    values.update(overrides)
    return Namespace(**values)


class IQSessionTests(unittest.TestCase):
    def test_combined_parser_accepts_iq_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            capture_path = Path(tmpdir) / "session.cs8"
            parser = _build_parser()

            args = parser.parse_args(
                [
                    "combined",
                    "--rf-input-mode",
                    "capture",
                    "--iq-capture-path",
                    str(capture_path),
                    "--iq-capture-max-bytes",
                    "4096",
                ]
            )

            self.assertEqual(args.command, "combined")
            self.assertEqual(args.rf_input_mode, "capture")
            self.assertEqual(args.iq_capture_path, capture_path)
            self.assertEqual(args.iq_capture_max_bytes, 4096)

    def test_playback_path_falls_back_to_capture_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            capture_path = Path(tmpdir) / "bluetooth.cs8"

            self.assertEqual(_resolve_iq_playback_path(_args(iq_capture_path=capture_path)), capture_path)

    def test_iq_metadata_records_radio_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            capture_path = Path(tmpdir) / "bluetooth.cs8"
            args = _args(iq_capture_path=capture_path)

            metadata_path = _write_iq_metadata(args, mode="capture", path=capture_path, bytes_written=1234)
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            expected = _iq_metadata(args, mode="capture", path=capture_path, bytes_written=1234)

            self.assertIsInstance(payload.pop("created_at"), float)
            expected.pop("created_at")
            self.assertEqual(payload, expected)
            self.assertEqual(payload["format"], "cs8")
            self.assertEqual(payload["sample_layout"], "interleaved_i8_iq")
            self.assertEqual(payload["center_freq_hz"], 2_442_000_000)
            self.assertEqual(payload["sample_rate_sps"], 60_000_000)
            self.assertEqual(payload["bytes_written"], 1234)
            self.assertEqual(payload["protocols"], ["bluetooth_classic", "bluetooth_lowenergy"])

    def test_iter_iq_playback_chunks_preserves_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            recording = Path(tmpdir) / "session.cs8"
            recording.write_bytes(b"abcdefghi")

            chunks = list(_iter_iq_playback_chunks(recording, chunk_bytes=4))

            self.assertEqual(chunks, [b"abcd", b"efgh", b"i"])
            self.assertEqual(b"".join(chunks), recording.read_bytes())


if __name__ == "__main__":
    unittest.main()
