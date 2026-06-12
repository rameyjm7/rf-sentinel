from __future__ import annotations

import threading
from collections import deque
from typing import Any, Callable

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, RichLog, Static


class TpmsMonitorApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
    }

    #summary {
        height: 4;
        padding: 0 1;
        content-align: left middle;
        background: $surface;
    }

    #body {
        height: 1fr;
    }

    DataTable {
        height: 1fr;
    }

    .panel-title {
        height: 1;
        padding: 0 1;
        content-align: left middle;
        background: $boost;
        color: $text;
        text-style: bold;
    }

    #events {
        height: 12;
    }
    """

    BINDINGS = [("a", "toggle_all_bins", "All Bins"), ("q", "quit", "Quit")]

    def __init__(
        self,
        *,
        start_capture: Callable[[], int],
        event_queue: deque[dict[str, Any]],
        queue_lock: threading.Lock,
        stop_event: threading.Event,
    ) -> None:
        super().__init__()
        self._start_capture = start_capture
        self._event_queue = event_queue
        self._queue_lock = queue_lock
        self._stop_event = stop_event
        self._thread: threading.Thread | None = None
        self._status: dict[str, Any] = {
            "elapsed_s": 0.0,
            "chunks": 0,
            "bursts": 0,
            "decoded": 0,
            "candidates": 0,
            "rejected": 0,
        }
        self._bins: list[dict[str, Any]] = []
        self._families: list[dict[str, Any]] = []
        self._packets: deque[dict[str, Any]] = deque(maxlen=12)
        self._show_all_bins = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Starting capture…", id="summary")
        with Horizontal(id="body"):
            with Vertical():
                yield Static("Packets", classes="panel-title")
                yield DataTable(id="packets")
                yield Static("Events", classes="panel-title")
                yield RichLog(id="events", highlight=True, markup=True)
            with Vertical():
                yield Static("Active Bins", classes="panel-title")
                yield DataTable(id="bins")
                yield Static("Candidate Families", classes="panel-title")
                yield DataTable(id="families")
        yield Footer()

    def on_mount(self) -> None:
        packets = self.query_one("#packets", DataTable)
        packets.add_columns("Pkt", "Freq", "Bits", "Check", "Repeat", "Conf", "Hex")
        bins = self.query_one("#bins", DataTable)
        bins.add_columns("Focus", "Freq", "Packets", "Cand", "Rej", "Check", "Bits")
        families = self.query_one("#families", DataTable)
        families.add_columns("Count", "Freq", "Bits", "Strat", "Unit", "Check", "Prefix")
        self.set_interval(0.25, self._drain_events)
        self._thread = threading.Thread(target=self._start_capture, daemon=True)
        self._thread.start()

    def on_unmount(self) -> None:
        self._stop_event.set()

    def action_toggle_all_bins(self) -> None:
        self._show_all_bins = not self._show_all_bins
        log = self.query_one("#events", RichLog)
        mode = "showing all bins" if self._show_all_bins else "hiding empty bins"
        log.write(Text.from_markup(f"[cyan]{mode}[/]"))
        self._refresh_views()

    def _drain_events(self) -> None:
        batch: list[dict[str, Any]] = []
        with self._queue_lock:
            while self._event_queue:
                batch.append(self._event_queue.popleft())
        if not batch:
            return
        for event in batch:
            self._handle_event(event)
        self._refresh_views()

    def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "status":
            self._status.update(event)
        elif event_type == "wideband_bins":
            self._bins = list(event.get("bins", []))
        elif event_type == "family_summary":
            self._families = list(event.get("families", []))
        elif event_type == "packet":
            self._packets.appendleft(event)
            self._status["decoded"] = event.get("packet_index", self._status.get("decoded", 0))
        elif event_type in {"startup", "retune", "error", "candidate", "reject", "focus"}:
            self._log_event(event)

    def _log_event(self, event: dict[str, Any]) -> None:
        log = self.query_one("#events", RichLog)
        event_type = str(event.get("type", "?")).upper()
        if event_type == "CANDIDATE":
            message = Text.from_markup(
                f"[yellow]{event_type}[/] "
                f"freq={event.get('frequency_hz', 0)/1e6:.3f}MHz "
                f"bits={event.get('bits_len')} repeat={event.get('repeat_count')}/{event.get('min_repeats')} "
                f"check={event.get('checksum_hint')}"
            )
        elif event_type == "REJECT":
            message = Text.from_markup(
                f"[red]{event_type}[/] "
                f"freq={event.get('frequency_hz', 0)/1e6:.3f}MHz "
                f"dur={event.get('burst_ms')}ms reason={event.get('reject_reason')}"
            )
        elif event_type == "ERROR":
            message = Text.from_markup(f"[bold red]{event.get('message', 'Unknown error')}[/]")
        elif event_type == "FOCUS":
            message = Text.from_markup(f"[bold green]{event.get('message', 'Focus')}[/]")
        else:
            message = Text(str(event.get("message", event_type)))
        log.write(message)

    def _refresh_views(self) -> None:
        summary = self.query_one("#summary", Static)
        summary.update(
            " | ".join(
                [
                    f"elapsed {float(self._status.get('elapsed_s', 0.0)):.1f}s",
                    f"band {self._status.get('band_name', '-')}",
                    f"chunks {int(self._status.get('chunks', 0))}",
                    f"bursts {int(self._status.get('bursts', 0))}",
                    f"packets {int(self._status.get('decoded', 0))}",
                    f"candidates {int(self._status.get('candidates', 0))}",
                    f"rejected {int(self._status.get('rejected', 0))}",
                    f"bins {'all' if self._show_all_bins else 'active'}",
                ]
            )
        )

        packets = self.query_one("#packets", DataTable)
        packets.clear(columns=False)
        for packet in self._packets:
            packets.add_row(
                str(packet.get("packet_index", "")),
                f"{float(packet.get('frequency_hz', 0))/1e6:.3f}",
                str(packet.get("bits_len", "")),
                str(packet.get("checksum_hint", "-")),
                str(packet.get("repeat_count", "")),
                f"{float(packet.get('confidence', 0.0)):.2f}",
                str(packet.get("hex", ""))[:22],
            )

        bins = self.query_one("#bins", DataTable)
        bins.clear(columns=False)
        visible_bins = self._bins
        if not self._show_all_bins:
            visible_bins = [
                bin_state
                for bin_state in self._bins
                if int(bin_state.get("packets", 0))
                or int(bin_state.get("candidates", 0))
                or int(bin_state.get("rejected", 0))
                or bool(bin_state.get("focused", False))
            ]
        for bin_state in visible_bins:
            bins.add_row(
                "*" if bool(bin_state.get("focused", False)) else "",
                f"{float(bin_state.get('frequency_hz', 0))/1e6:.3f}",
                str(bin_state.get("packets", "")),
                str(bin_state.get("candidates", "")),
                str(bin_state.get("rejected", "")),
                str(bin_state.get("checksum_hint", "-") or "-"),
                str(bin_state.get("bits_len", "") or ""),
            )

        families = self.query_one("#families", DataTable)
        families.clear(columns=False)
        for family in self._families:
            families.add_row(
                str(family.get("count", "")),
                f"{float(family.get('frequency_hz', 0))/1e6:.3f}",
                str(family.get("bit_length", "")),
                str(family.get("strategy", "")),
                str(family.get("unit_samples", "")),
                str(family.get("checksum_hint", "-")),
                str(family.get("prefix", "")),
            )
