# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# The key-hint legend is ported from the sibling rxved and p2ked projects,
# which port it from s3ked, eosed and k2kremote:
#   Copyright (C) 2026  rxved / p2ked / s3ked / eosed / k2kremote
#   contributors                                     - GPL-2.0-or-later
#
# kwsed is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version.
#
# kwsed is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.

"""``kwsed`` -- a terminal browser for the Korg Wavestation SR.

Two panes: banks on the left, that bank's slots on the right.

Three rules, each about not lying to the user:

**Moving the cursor never sends anything.** Selecting a program changes what
the instrument plays. Selection is bound to Enter and nothing else, and a
test enforces it.

**Reading names makes no sound.** One bulk dump per bank, no selection. The
unit may refuse -- it sometimes answers "data format error" for reasons not
yet understood -- and the status line says *refused*, which is different
from silence and worth distinguishing.

**A program change is not a bank change.** The Wavestation's banks are chosen on the
unit, not by Bank Select, and a freshly powered Wavestation sits in Bank G whatever
a sequencer sends. So Enter sends a program change and says which bank it
assumed, rather than claiming to have selected something it cannot reach.
"""

from __future__ import annotations

import argparse
import sys
import threading
from typing import Dict, List, Optional

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Header, Input, Label, Static

from kws import banks
from kws import catalog as cat

__all__ = ["KwsedApp", "main", "build_parser", "KEY_HINTS", "wrap_blocks"]

_SEP = " · "

KEY_HINTS = (
    "↑↓ move", "tab pane", "⏎ select on the unit", "r read names",
    "/ search", "i device", "? help", "q quit",
)


def wrap_blocks(blocks, width: int, sep: str = _SEP) -> str:
    """Pack hints into lines no wider than ``width``.

    Ported from the siblings. Breaks happen only between blocks, so a hint
    is never split mid-label. The point is that the legend wraps instead of
    truncating: a footer that silently drops its last keys teaches the user
    those keys do not exist.
    """
    lines, current = [], ""
    for block in blocks:
        candidate = block if not current else current + sep + block
        if width and len(candidate) > width and current:
            lines.append(current)
            current = block
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\n".join(lines)


class KeyHints(Static):
    """The legend, wrapped to the window rather than truncated."""

    DEFAULT_CSS = "KeyHints { height: auto; background: $panel; padding: 0 1; }"

    def on_mount(self) -> None:
        self._render_hints()

    def on_resize(self) -> None:
        self._render_hints()

    def _render_hints(self) -> None:
        # NOT named _render: Widget._render is Textual's own, and shadowing
        # it makes every layout pass ask a None for its height.
        self.update(wrap_blocks(KEY_HINTS, max(self.size.width - 2, 20)))


class ReportScreen(ModalScreen[None]):
    """A scrollable block of text."""

    DEFAULT_CSS = """
    ReportScreen { align: center middle; }
    ReportScreen > Vertical {
        width: 84; height: 80%; border: thick $accent;
        background: $surface; padding: 1 2;
    }
    ReportScreen .body { height: 1fr; overflow-y: auto; }
    """

    BINDINGS = [Binding("escape", "close", "Close"),
                Binding("q", "close", "Close")]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[b]{self._title}[/b]")
            yield Static(self._body, classes="body")
            yield Static("[dim]esc / q to close[/dim]")

    def action_close(self) -> None:
        self.dismiss(None)


class PromptScreen(ModalScreen[Optional[str]]):
    """One line of text."""

    DEFAULT_CSS = """
    PromptScreen { align: center middle; }
    PromptScreen > Vertical {
        width: 70; height: auto; border: thick $accent;
        background: $surface; padding: 1 2;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, prompt: str, current: str = "") -> None:
        super().__init__()
        self._prompt = prompt
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._prompt)
            yield Input(value=self._current, id="value")

    def on_mount(self) -> None:
        self.query_one("#value", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class KwsedApp(App):
    """The browser."""

    CSS = """
    Screen { layers: base; }
    #panes { height: 1fr; }
    #bank-table { width: 30; border-right: solid $panel; }
    #slot-table { width: 1fr; }
    DataTable { height: 1fr; }
    #detail { height: 3; padding: 0 1; border-top: solid $panel; }
    #status { height: 1; background: $boost; color: $text; padding: 0 1; }
    """

    BINDINGS = [
        # Enter is NOT bound here: a focused DataTable consumes it for its
        # own row selection, so it arrives as RowSelected instead.
        Binding("r", "read_names", "Read names"),
        Binding("slash", "search", "Search"),
        Binding("i", "device_info", "Device"),
        Binding("question_mark", "help", "Help"),
        Binding("tab", "switch_pane", "Switch pane"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, device, *, catalog=None) -> None:
        super().__init__()
        self.device = device
        self.catalog = catalog if catalog is not None else cat.empty()
        self._lock = threading.RLock()
        #: The left pane lists bank-and-kind pairs, because a Wavestation
        #: bank holds 50 performances *and* 35 patches and they are
        #: different things. A pane listing banks alone would need a second
        #: control to say which kind, for no gain.
        self._pairs: List[tuple] = [
            (entry.number, kind)
            for entry in banks.BANKS
            for kind in (banks.Kind.PERFORMANCE, banks.Kind.PATCH)]
        self._current_pair = self._pairs[0]
        self._current: List[banks.Slot] = []
        self._busy = False
        self._filling = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="panes"):
            yield DataTable(id="bank-table", cursor_type="row")
            yield DataTable(id="slot-table", cursor_type="row")
        yield Static("", id="detail")
        yield Static("", id="status")
        yield KeyHints()

    def on_mount(self) -> None:
        self.title = "kwsed"
        table = self.query_one("#bank-table", DataTable)
        for label, key in (("bank", "bank"), ("kind", "kind"), ("n", "n"),
                           ("", "rw")):
            table.add_column(label, key=key)
        for number, kind in self._pairs:
            entry = banks.bank(number)
            table.add_row(entry.label, kind[:4],
                          str(banks.count_for(kind)),
                          "" if entry.writable else "ro",
                          key=f"{number}:{kind}")

        slots = self.query_one("#slot-table", DataTable)
        for label, key in (("#", "num"), ("name", "name")):
            slots.add_column(label, key=key)
        self._fill(self._current_pair)
        table.focus()
        self.notify_status(
            f"{len(self.catalog)} names from the catalog" if self.catalog
            else "no catalog -- numbers only. Build one with "
                 "tools/extract_catalog.py, or press r to read from the unit."
        )

    # --- filling ------------------------------------------------------------

    def _fill(self, pair) -> None:
        number, kind = pair
        entry = banks.bank(number)
        table = self.query_one("#slot-table", DataTable)
        self._current_pair = pair
        self._current = banks.slots(number, kind)
        self._filling = True
        try:
            table.clear()
            for item in self._current:
                name = self.catalog.display_name(entry.id, kind, item.number)
                if self.catalog.differs(entry.id, kind, item.number):
                    shown = f"[b]{name}[/b] [dim]*[/dim]"
                elif self.catalog.is_live(entry.id, kind, item.number):
                    shown = f"[b]{name}[/b]"
                else:
                    shown = name
                table.add_row(f"{item.number:02d}", shown, key=item.key)
        finally:
            self._filling = False
        self._detail(0)

    def _detail(self, row: int) -> None:
        if not self._current or row >= len(self._current):
            self.query_one("#detail", Static).update("")
            return
        item = self._current[row]
        name = self.catalog.display_name(item.bank.id, item.kind, item.number)
        note = "" if item.bank.writable else "   [dim]ROM — read only[/dim]"
        self.query_one("#detail", Static).update(
            f"[b]{item.bank.label} {item.kind} {item.number:02d}[/b]  "
            f"{name}{note}\n"
            f"Bank Select [b]{item.bank.number}[/b]   Program Change "
            f"[b]{item.number}[/b]   [dim]which bank a Bank Select actually "
            f"reaches is not established[/dim]"
        )

    def notify_status(self, message: str, *, refused: bool = False) -> None:
        prefix = "[b]refused[/b] · " if refused else ""
        self.query_one("#status", Static).update(prefix + message)

    # --- navigation ---------------------------------------------------------

    def on_data_table_row_highlighted(self, event) -> None:
        if self._filling:
            return
        if event.data_table.id == "bank-table":
            row = event.cursor_row
            if 0 <= row < len(self._pairs):
                self._fill(self._pairs[row])
        elif event.data_table.id == "slot-table":
            self._detail(event.cursor_row)

    def on_data_table_row_selected(self, event) -> None:
        """Enter on a slot. It arrives here, not as a binding."""
        if event.data_table.id == "slot-table":
            self.action_select()

    def action_switch_pane(self) -> None:
        left = self.query_one("#bank-table", DataTable)
        right = self.query_one("#slot-table", DataTable)
        (right if left.has_focus else left).focus()

    # --- operations ---------------------------------------------------------

    def action_select(self) -> None:
        table = self.query_one("#slot-table", DataTable)
        row = table.cursor_row
        if not 0 <= row < len(self._current):
            return
        if self._busy:
            self.notify_status("busy", refused=True)
            return
        self._select_worker(self._current[row])

    @work(thread=True)
    def _select_worker(self, entry: banks.Slot) -> None:
        """**MIDI only.**"""
        try:
            with self._lock:
                self.device.select(entry)
        except Exception as exc:
            self.call_from_thread(
                self.notify_status, f"select: {exc}", refused=True)
            return
        self.call_from_thread(
            self.notify_status,
            f"bank select {entry.bank.number} + program change "
            f"{entry.number} sent for {entry} — which bank a Bank Select "
            f"actually reaches is not established")

    def action_read_names(self) -> None:
        if self._busy:
            self.notify_status("busy", refused=True)
            return
        self._read_worker(self._current_pair[0])

    @work(thread=True)
    def _read_worker(self, bank_id: int) -> None:
        """**MIDI only** -- the catalog is updated on the main thread."""
        self._busy = True
        try:
            self.call_from_thread(
                self.notify_status,
                f"asking for bank {bank_id} in one message (silent)...")
            with self._lock:
                kind, names = self.device.read_bank_names(bank_id)
        except Exception as exc:
            self.call_from_thread(
                self.notify_status, f"read: {exc}", refused=True)
            return
        finally:
            self._busy = False
        self.call_from_thread(self._adopt, bank_id, kind, names)

    def _adopt(self, bank_id: int, kind: str, names: Dict[int, str]) -> None:
        entry = banks.bank(bank_id)
        for number, name in names.items():
            self.catalog.set_live_name(entry.id, kind, number, name)
        differing = sum(1 for n in names
                        if self.catalog.differs(entry.id, kind, n))
        if self._current_pair == (entry.number, kind):
            self._fill(self._current_pair)
        self.notify_status(
            f"read {len(names)} {kind} names from {entry.label}"
            + (f"; {differing} differ from the catalog" if differing else "")
            + "  (the unit chose the kind, not the request)")

    def action_search(self) -> None:
        def done(text) -> None:
            if not text:
                return
            hits = self.catalog.search(text)
            if not hits:
                self.notify_status(f"nothing matching {text!r}")
                return
            lines = [f"{len(hits)} match(es) for {text!r}", ""]
            for bank_id, kind, number, name in hits[:400]:
                lines.append(f"{bank_id:<6} {kind:<11} {number:02d}  {name}")
            if len(hits) > 400:
                lines.append(f"... and {len(hits) - 400} more")
            self.push_screen(ReportScreen("Search", "\n".join(lines)))

        self.push_screen(PromptScreen("Search names:"), done)

    def action_device_info(self) -> None:
        self._info_worker()

    @work(thread=True)
    def _info_worker(self) -> None:
        """**MIDI only** -- the report is built on the main thread."""
        try:
            with self._lock:
                fields = self.device.identify()
        except Exception as exc:
            self.call_from_thread(
                self.notify_status, f"device: {exc}", refused=True)
            return
        self.call_from_thread(self._show_info, fields)

    def _show_info(self, fields) -> None:
        lines = [f"connection      {self.device.description}", ""]
        if fields:
            lines.append(f"manufacturer    {fields['manufacturer']:#04x}"
                         + ("  (Korg)" if fields["is_korg"] else ""))
            lines.append(f"family          {fields['family']}"
                         + ("  (X5 family)" if fields["is_x5_family"] else ""))
            lines.append(f"global channel  {fields['channel'] + 1}")
            lines.append("")
            lines.append("The global channel comes from the unit. Every")
            lines.append("System Exclusive request is answered only on it.")
        else:
            lines.append("nothing answered an Identity Request")
        lines.append("")
        lines.append(f"catalog         {len(self.catalog)} names"
                     + (f" from {self.catalog.source}"
                        if self.catalog.source else ""))
        self.push_screen(ReportScreen("Device", "\n".join(lines)))

    def action_help(self) -> None:
        self.push_screen(ReportScreen("kwsed", _HELP))


_HELP = """\
Two panes. Left: the banks. Right: that bank's slots.

  Bank A   100 programs, A00-A99. Yours; a soundset overwrites these.
  Bank G   136 General MIDI programs, G001-G136. Read-only.
  Combi    100 combinations, C00-C99, each layering up to eight programs.

  #    what the unit's display shows
  PC   the program change on the wire

**Those are different numbers on Bank G.** Korg numbers it from 1 and a
program change counts from 0, so G001 goes out as program change 0. They are
equal on Bank A and the combinations, which is exactly why conflating them
survives casual testing. kwsed keeps them apart everywhere.

  ⏎   send a program change — it will sound
  r   read this bank's names from the unit — SILENT, nothing is selected
  /   search names
  i   device identity and the unit's global channel

**A program change is not a bank change.** The Wavestation's banks are chosen on the
unit, not over MIDI, and a freshly powered Wavestation sits in Bank G whatever a
sequencer sends. So "my sequencer only plays GM sounds" is the expected
behaviour of an Wavestation nobody has switched to Bank A -- not a fault. kwsed
sends the program change and says which bank it assumed.

Moving the cursor sends nothing at all.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kwsed", description="Browse a Korg Wavestation SR's sounds.")
    parser.add_argument("--port")
    parser.add_argument("--recv-port")
    parser.add_argument("--demo", action="store_true",
                        help="built-in demo device; opens no MIDI ports")
    parser.add_argument("--midi-channel", type=int, default=None,
                        help="the unit's global channel, 1-16")
    parser.add_argument("--catalog", default=None)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.midi_channel is not None and not 1 <= args.midi_channel <= 16:
        sys.exit("error: --midi-channel is 1-16")

    if args.demo:
        from kwsed.demo import DemoDevice

        device = DemoDevice()
    else:
        from kws import bridge as b

        b.install_clean_exit()
        try:
            if args.port:
                device = b.WavestationBridge.open(
                    args.port, recv_port=args.recv_port,
                    channel=(args.midi_channel - 1) if args.midi_channel
                    else 0)
            else:
                device = b.WavestationBridge.autodetect(
                    on_try=lambda n: print(f"  probing {n}...",
                                           file=sys.stderr))
        except Exception as exc:
            sys.exit(f"error: {exc}")

    app = KwsedApp(device, catalog=cat.load(args.catalog))
    try:
        app.run()
    finally:
        device.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
