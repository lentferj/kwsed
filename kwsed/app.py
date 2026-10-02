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
from kws import messages as m
from kws import catalog as cat

__all__ = ["KwsedApp", "main", "build_parser", "KEY_HINTS", "wrap_blocks"]

_SEP = " · "

#: What `F` steps through. The same three the sibling rxved, p2ked and
#: x5ded use, so the key means the same thing across these projects.
VIEW_ALL = "all"
VIEW_BANK_FAVOURITES = "bank-favourites"
VIEW_ALL_FAVOURITES = "all-favourites"
VIEW_CYCLE = (VIEW_ALL, VIEW_BANK_FAVOURITES, VIEW_ALL_FAVOURITES)
VIEW_LABELS = {
    VIEW_ALL: "all slots",
    VIEW_BANK_FAVOURITES: "favourites in this bank",
    VIEW_ALL_FAVOURITES: "every favourite, all banks",
}


def favorite_bank_id(bank_id: str, kind: str) -> str:
    """The key a favourite is filed under.

    The kind is part of it, in full. A bank holds 50 performances and 35
    patches, and both words begin with P -- the same collision that once
    merged them in ``Slot.key`` would merge a favourited performance with
    the patch of the same number.
    """
    return f"{bank_id}:{kind}"


KEY_HINTS = (
    "↑↓ move", "tab pane", "⏎ select on the unit", "r read names",
    "/ search", "[ ] channel", "c set channel", "f favourite",
    "F favourites view", "t tags", "n note", "m multi", "i device",
    "? help", "q quit",
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




def _bank_label(sysex_byte: int) -> str:
    """A part's bank number as a person reads it.

    The number in a Multiset is the instrument's own, which is not the
    position a person sees the banks in -- byte 3 is the Card, not ROM 4.
    """
    try:
        return banks.by_sysex(sysex_byte).label
    except LookupError:
        return f"bank {sysex_byte}"


class MultiScreen(ModalScreen[None]):
    """The 32 Multisets and what each assigns to the 16 MIDI channels.

    Read-only, and reading it makes no sound: one request, three replies,
    nothing selected.

    This is MIDI multi mode, not a performance's eight parts -- the two are
    both called "multi" on this instrument and are different objects. See
    :mod:`kws.multi`.
    """

    DEFAULT_CSS = """
    MultiScreen { align: center middle; }
    MultiScreen > Vertical {
        width: 92; height: 86%; border: thick $accent;
        background: $surface; padding: 1 2;
    }
    MultiScreen DataTable { height: 1fr; }
    """

    BINDINGS = [Binding("escape", "close", "Close"),
                Binding("q", "close", "Close")]

    def __init__(self, multisets, bank_label=None) -> None:
        super().__init__()
        self._multisets = list(multisets)
        self._bank_label = bank_label or (lambda n: str(n))

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("[b]Multi mode -- 32 Multisets[/b]")
            yield DataTable(id="multi-table", cursor_type="row")
            yield Static("", id="multi-detail")
            yield Static("[dim]arrows to move, esc / q to close[/dim]")

    def on_mount(self) -> None:
        table = self.query_one("#multi-table", DataTable)
        for label, key in (("#", "num"), ("name", "name"),
                           ("channels used", "used")):
            table.add_column(label, key=key)
        for entry in self._multisets:
            used = sum(1 for part in entry.parts if part.used)
            table.add_row(f"{entry.number:02d}", entry.name or "[dim]--[/dim]",
                          str(used), key=str(entry.number))
        self._detail(0)

    def on_data_table_row_highlighted(self, event) -> None:
        self._detail(event.cursor_row)

    def _detail(self, row: int) -> None:
        if not self._multisets or row is None or row >= len(self._multisets):
            return
        entry = self._multisets[row]
        lines = []
        for part in entry.parts:
            if not part.used:
                lines.append(f"ch {part.channel:2d}  --")
                continue
            lines.append(
                f"ch {part.channel:2d}  {self._bank_label(part.bank)} "
                f"perf {part.performance:02d}  level {part.level}")
        half = (len(lines) + 1) // 2
        rows = [f"{a:38s}{b}" for a, b in
                zip(lines[:half], lines[half:] + [""])]
        self.query_one("#multi-detail", Static).update("\n".join(rows))

    def action_close(self) -> None:
        self.dismiss(None)


class SendScreen(ModalScreen[Optional[dict]]):
    """Write a bank dump into the instrument. **Destructive.**

    The only screen in kwsed that can destroy anything. Writing a bank
    replaces all fifty performances or all thirty-five patches that were in
    it, and no power cycle brings them back. So this follows the sibling
    projects' rule for destructive operations -- eosed's Master menu,
    k2kremote's DELBANK, rxved's store -- and then adds a backup:

    1. The destination's **current names** are read and shown, because
       "Card" means nothing and the names it is about to replace mean
       everything.
    2. **Arm.** Nothing has been sent yet.
    3. **Fire**, a different key, only while armed.

    Three deliberate steps, none of them a single keystroke from the
    browser. Opening this screen sends nothing; arming sends nothing.

    Before a byte goes out the destination is read in full and saved to a
    file. If that read fails, nothing is written at all.
    """

    DEFAULT_CSS = """
    SendScreen { align: center middle; }
    SendScreen > Vertical {
        width: 80; height: auto; border: thick $error;
        background: $surface; padding: 1 2;
    }
    SendScreen .armed { color: $error; text-style: bold; }
    SendScreen .what { height: auto; }
    """

    BINDINGS = [
        Binding("escape", "close", "Cancel"),
        Binding("q", "close", "Cancel"),
        Binding("a", "arm", "Arm", show=False),
        # Deliberately not Enter, and deliberately not beside "a": the fire
        # key should not be the one a finger is already resting on.
        Binding("w", "fire", "Write", show=False),
    ]

    def __init__(self, *, path: str, kind: str, destination,
                 current: Optional[Dict[int, str]] = None,
                 on_send=None) -> None:
        super().__init__()
        self._path = path
        self._kind = kind
        self._destination = destination
        self._current = dict(current or {})
        self._on_send = on_send
        self._armed = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("[b]Write a bank into the instrument[/b]")
            yield Static(self._describe(), classes="what")
            yield Static("", id="send-state")
            yield Static(
                "[dim]a to arm, then w to write. esc cancels.[/dim]")

    def _describe(self) -> str:
        lines = [
            f"source      {self._path}",
            f"destination {self._destination.label}  "
            f"({'writable' if self._destination.writable else 'ROM'})",
            f"kind        {self._kind}",
        ]
        if self._current:
            shown = ", ".join(f"{n:03d} {name}" for n, name
                              in sorted(self._current.items())[:5])
            more = " ..." if len(self._current) > 5 else ""
            lines.append(f"replacing   {shown}{more}")
        else:
            lines.append("replacing   (names not read)")
        return "\n".join(lines)

    def on_mount(self) -> None:
        if not self._destination.writable:
            self.query_one("#send-state", Static).update(
                "[b]$error[/b] this bank is ROM and cannot be written")

    def action_arm(self) -> None:
        if not self._destination.writable:
            return
        self._armed = True
        self.query_one("#send-state", Static).update(
            f"[b]ARMED[/b] - w replaces {self._destination.label} "
            f"{self._kind}s. Nothing has been sent yet.")

    def action_fire(self) -> None:
        """Only while armed, and only once."""
        if not self._armed:
            self.query_one("#send-state", Static).update(
                "[dim]not armed - press a first[/dim]")
            return
        self._armed = False
        if self._on_send is not None:
            try:
                self._on_send(self._destination, self._kind)
            except Exception as exc:
                # A failed write must not dismiss: the screen is the only
                # feedback the user gets, and the bridge already reported
                # the backup's read-back mismatch. Leave it open.
                self.query_one("#send-state", Static).update(
                    f"[b]write failed[/b] {exc}")
                return
        self.dismiss({"destination": self._destination.number,
                      "kind": self._kind})

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
    /* Sized to its content. A fixed width does not truncate the end
       of a label -- the table scrolls sideways and clips the
       *start*, so a bank reads as nonsense rather than as narrow. */
    #bank-table {
        width: auto; min-width: 20; max-width: 52;
        border-right: solid $panel;
    }
    #slot-table { width: 1fr; }
    DataTable { height: 1fr; }
    #detail { height: 3; padding: 0 1; border-top: solid $panel; }
    #status { height: 1; background: $boost; color: $text; padding: 0 1; }
    """

    BINDINGS = [
        # Enter is NOT bound here: a focused DataTable consumes it for its
        # own row selection, so it arrives as RowSelected instead.
        Binding("r", "read_names", "Read names"),
        Binding("left_square_bracket", "channel_down", "Channel -"),
        Binding("right_square_bracket", "channel_up", "Channel +"),
        Binding("c", "pick_channel", "Set channel"),
        Binding("m", "multi", "Multi mode"),
        Binding("f", "toggle_favorite", "Favourite"),
        Binding("F", "cycle_favorites", "Favourites view"),
        Binding("t", "edit_tags", "Tags"),
        Binding("n", "edit_note", "Note"),
        Binding("slash", "search", "Search"),
        Binding("i", "device_info", "Device"),
        Binding("question_mark", "help", "Help"),
        Binding("tab", "switch_pane", "Switch pane"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, device, *, catalog=None, favorites=None,
                 config_path: Optional[str] = None) -> None:
        super().__init__()
        self.device = device
        #: Injected, never opened here, so a test cannot write into the
        #: user's real database.
        self.favorites = favorites
        #: Where the send channel is remembered. Injected and None
        #: in tests, so a test cannot drop a config into the
        #: working directory.
        self._config_path = config_path
        self.view_mode = VIEW_ALL
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
                           ("", "rw"), ("♥", "fav")):
            table.add_column(label, key=key)
        for number, kind in self._pairs:
            entry = banks.bank(number)
            marked = len(self._favorite_keys(entry.id, kind))
            table.add_row(entry.label, kind[:4],
                          str(banks.count_for(kind)),
                          "" if entry.writable else "ro",
                          # blank rather than "0": a column of zeroes is
                          # noise, and the eye wants the few that differ
                          str(marked) if marked else "",
                          key=f"{number}:{kind}")

        slots = self.query_one("#slot-table", DataTable)
        # The favourite marker leads the row: a gutter mark the eye runs
        # down, rather than a character glued to the name.
        for label, key in (("♥", "fav"), ("#", "num"), ("name", "name")):
            slots.add_column(label, key=key)
        self._fill(self._current_pair)
        table.focus()
        self.notify_status(
            f"{len(self.catalog)} names from the catalog" if self.catalog
            else "no catalog -- numbers only. Build one with "
                 "tools/extract_catalog.py, or press r to read from the unit."
        )

    # --- filling ------------------------------------------------------------

    def _favorite_keys(self, bank_id: str, kind: str) -> set:
        if self.favorites is None:
            return set()
        return self.favorites.keys_for_bank(favorite_bank_id(bank_id, kind))

    def _all_favourites(self):
        """Every favourite in every bank and kind, in listing order."""
        if self.favorites is None:
            return []
        wanted = {}
        for favourite in self.favorites.all(order="bank"):
            wanted.setdefault(favourite.bank_id, set()).add(favourite.number)
        out = []
        for number, kind in self._pairs:
            entry = banks.bank(number)
            numbers = wanted.get(favorite_bank_id(entry.id, kind))
            if not numbers:
                continue
            out.extend(s for s in banks.slots(number, kind)
                       if s.number in numbers)
        return out

    def _visible(self, pair):
        number, kind = pair
        if self.view_mode == VIEW_ALL_FAVOURITES:
            return self._all_favourites()
        entries = banks.slots(number, kind)
        if self.view_mode == VIEW_BANK_FAVOURITES:
            marked = self._favorite_keys(banks.bank(number).id, kind)
            entries = [e for e in entries if e.number in marked]
        return entries


    def _cursor_entry(self):
        """The row under the cursor, or None."""
        table = self.query_one("#slot-table", DataTable)
        row = table.cursor_row
        if not self._current or row is None or row >= len(self._current):
            return None
        return self._current[row]


    # --- the send channel ---------------------------------------------------

    def _channel(self) -> int:
        return getattr(self.device, "channel", 0)

    def _set_channel(self, channel: int) -> None:
        """Change the channel selections are sent on, and remember it."""
        channel = max(0, min(15, channel))
        try:
            self.device.channel = channel
        except Exception:
            self.notify_status("this device has no settable channel",
                               refused=True)
            return
        self._remember_channel(channel)
        self.notify_status(f"sending on MIDI channel {channel + 1}")

    def _remember_channel(self, channel: int) -> None:
        """Save it for next time. Only OSError is swallowed."""
        if getattr(self, "_config_path", None) is None:
            return
        from kws import config

        try:
            config.save_channel(channel, self._config_path)
        except OSError:
            pass

    def action_channel_up(self) -> None:
        self._set_channel(self._channel() + 1)

    def action_channel_down(self) -> None:
        self._set_channel(self._channel() - 1)

    def action_pick_channel(self) -> None:
        def apply(text):
            if not text:
                return
            try:
                value = int(text)
            except ValueError:
                self.notify_status(f"{text!r} is not a number", refused=True)
                return
            if not 1 <= value <= 16:
                self.notify_status("MIDI channels are 1-16", refused=True)
                return
            self._set_channel(value - 1)

        self.push_screen(
            PromptScreen("MIDI channel (1-16):", str(self._channel() + 1)),
            apply)

    def action_toggle_favorite(self) -> None:
        """Mark or unmark the slot under the cursor. Sends no MIDI."""
        if self.favorites is None:
            self.notify_status("no favourites database")
            return
        table = self.query_one("#slot-table", DataTable)
        row = table.cursor_row
        if not self._current or row is None or row >= len(self._current):
            return
        item = self._current[row]
        key = favorite_bank_id(item.bank.id, item.kind)
        name = self.catalog.display_name(item.bank.id, item.kind, item.number)
        # Favorites.toggle, not remove-then-add: un-favouriting keeps
        # the row so a rating, tags, a note and the original date
        # survive being toggled off and on. As a delete followed by a
        # fresh add this destroyed all four, and this is one keystroke.
        now = self.favorites.toggle(key, item.number, name=name)
        self.notify_status(
            f"{'favourited' if now else 'unfavourited'} {name or item}")
        self._refill_keeping_place()


    def _favourite_under_cursor(self):
        """The favourite row for the slot under the cursor, if there is one.

        Tags and notes hang off a favourite, so there is nothing to attach
        them to until `f` has been pressed. Saying so is better than
        silently creating one: the user asked to annotate, not to favourite.
        """
        entry = self._cursor_entry()
        if entry is None or self.favorites is None:
            return None, None
        bank_id = favorite_bank_id(entry.bank.id, entry.kind)
        number = entry.number
        return (bank_id, number), self.favorites.get(bank_id, number)

    def action_edit_tags(self) -> None:
        where, existing = self._favourite_under_cursor()
        if where is None:
            return
        if existing is None:
            self.notify_status("not a favourite yet -- press f first",
                               refused=True)
            return

        def apply(value):
            if value is None:
                return
            self.favorites.set_tags(where[0], where[1], value)
            self._refill_keeping_place()
            self.notify_status("tags set")

        self.push_screen(PromptScreen("Tags (comma separated):",
                                  existing.tags), apply)

    def action_edit_note(self) -> None:
        where, existing = self._favourite_under_cursor()
        if where is None:
            return
        if existing is None:
            self.notify_status("not a favourite yet -- press f first",
                               refused=True)
            return

        def apply(value):
            if value is None:
                return
            self.favorites.set_note(where[0], where[1], value)
            self._refill_keeping_place()
            self.notify_status("note set")

        self.push_screen(PromptScreen("Note:", existing.note), apply)

    def action_cycle_favorites(self) -> None:
        """Step: all slots -> this bank's favourites -> every favourite.

        Always exactly three steps, even when a view is empty.
        """
        if self.favorites is None:
            self.notify_status("no favourites database")
            return
        index = VIEW_CYCLE.index(self.view_mode)
        self.view_mode = VIEW_CYCLE[(index + 1) % len(VIEW_CYCLE)]
        self._refill_keeping_place()
        shown = len(self._current)
        if self.view_mode == VIEW_ALL:
            self.notify_status(VIEW_LABELS[self.view_mode])
        elif not shown:
            where = ("this bank" if self.view_mode == VIEW_BANK_FAVOURITES
                     else "any bank")
            self.notify_status(
                f"{VIEW_LABELS[self.view_mode]} - nothing favourited in "
                f"{where} yet")
        else:
            self.notify_status(f"{VIEW_LABELS[self.view_mode]} - {shown}")

    def _refill_keeping_place(self) -> None:
        table = self.query_one("#slot-table", DataTable)
        row = table.cursor_row or 0
        self._fill(self._current_pair)
        if self._current:
            table.move_cursor(row=min(row, len(self._current) - 1))

    def _fill(self, pair) -> None:
        number, kind = pair
        entry = banks.bank(number)
        table = self.query_one("#slot-table", DataTable)
        self._current_pair = pair
        self._current = self._visible(pair)
        self._filling = True
        try:
            table.clear()
            for item in self._current:
                name = self.catalog.display_name(item.bank.id, item.kind,
                                                 item.number)
                if self.catalog.differs(item.bank.id, item.kind, item.number):
                    shown = f"[b]{name}[/b] [dim]*[/dim]"
                elif self.catalog.is_live(item.bank.id, item.kind,
                                          item.number):
                    shown = f"[b]{name}[/b]"
                else:
                    shown = name
                marked = self._favorite_keys(item.bank.id, item.kind)
                label = (f"{item.bank.label} {item.kind[:4]} "
                         f"{item.number:02d}"
                         if self.view_mode == VIEW_ALL_FAVOURITES
                         else f"{item.number:02d}")
                table.add_row(
                    "[b]♥[/b]" if item.number in marked else "",
                    label, shown, key=item.key)
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
            f"Bank Select LSB [b]{item.bank.sysex}[/b]   Program Change "
            f"[b]{item.number}[/b]   [dim]the wire byte is sent; which bank "
            f"a Bank Select actually reaches is not established[/dim]"
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
            f"bank select LSB {entry.bank.sysex} + program change "
            f"{entry.number} sent for {entry} — which bank a Bank Select "
            f"actually reaches is not established")

    def action_read_names(self) -> None:
        if self._busy:
            self.notify_status("busy", refused=True)
            return
        self._busy = True
        self._read_worker(*self._current_pair)

    @work(thread=True)
    def _read_worker(self, bank_id: int, kind: str) -> None:
        """**MIDI only** -- the catalog is updated on the main thread."""
        try:
            self.call_from_thread(
                self.notify_status,
                f"asking for bank {bank_id} {kind}s in one message "
                f"(silent)...")
            with self._lock:
                kind, names = self.device.read_bank_names(bank_id, kind)
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
            + (f"; {differing} differ from the catalog" if differing else ""))

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

    def action_multi(self) -> None:
        """Show the Multisets. One request; makes no sound."""
        if self._busy:
            self.notify_status("busy", refused=True)
            return
        self._busy = True
        self._multi_worker()

    @work(thread=True)
    def _multi_worker(self) -> None:
        """**MIDI only** -- the screen is built on the main thread."""
        try:
            with self._lock:
                raw = self.device.read_multi()
        except Exception as exc:
            self.call_from_thread(self.notify_status,
                                  f"multi: {exc}", refused=True)
            return
        finally:
            self._busy = False
        self.call_from_thread(self._show_multi, raw)

    def _show_multi(self, raw: bytes) -> None:
        from kws import multi as multi_mod

        try:
            sets = multi_mod.parse(m.parse(raw).data())
        except Exception as exc:
            self.notify_status(f"multi: {exc}", refused=True)
            return
        self.push_screen(MultiScreen(
            sets, bank_label=lambda n: _bank_label(n)))
        self.notify_status(f"{len(sets)} multisets")

    def action_device_info(self) -> None:
        if self._busy:
            self.notify_status("busy", refused=True)
            return
        self._busy = True
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
        finally:
            self._busy = False
        self.call_from_thread(self._show_info, fields)

    def _show_info(self, fields) -> None:
        lines = [f"connection      {self.device.description}", ""]
        if fields:
            lines.append(f"manufacturer    {fields['manufacturer']:#04x}"
                         + ("  (Korg)" if fields["is_korg"] else ""))
            lines.append(f"family          {fields['family']}"
                         + ("  (Wavestation)" if fields.get("is_wavestation")
                            else ""))
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
Two panes. Left: the twelve banks, each listed twice -- once for its 50
performances, once for its 35 patches. Right: that bank-and-kind's slots.

  RAM 1-3   writable; somebody's own work lives here.
  ROM 4-11  read-only.
  Card      writable; the card in the slot.

A performance combines up to eight parts, each pointing at a patch. A
patch is a sound: four oscillators playing waves or wave sequences. They
are different objects; performance 7 is not patch 7.

  ⏎   select the slot on the unit -- it will sound
  r   read this bank's names from the unit -- SILENT, nothing is selected
  /   search names
  m   the 32 Multisets: which performance answers on each MIDI channel
  f   favourite this slot; F steps all -> this bank's -> every favourite
  t   tags; n   note (both hang off a favourite, so press f first)
  [ ] step the send channel; c set it by number
  i   device identity and the unit's global channel

**A program change is not a bank change.** Which of the twelve banks a
Bank Select actually reaches is not established (see TODO 1), so kwsed
sends the bank's wire byte with the program change and says which bank
it assumed, rather than claiming a selection it cannot verify.

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
    parser.add_argument("--config", default=None,
                        help="settings cache (default: ./config.toml)")
    parser.add_argument("--favorites", default=None,
                        help="favourites database (default: "
                             "the per-platform data directory)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    from kws import config as cfg

    config_path = getattr(args, "config", None) or cfg.DEFAULT_PATH
    if args.midi_channel is not None and not 1 <= args.midi_channel <= 16:
        sys.exit("error: --midi-channel is 1-16")

    if args.demo:
        from kwsed.demo import DemoDevice

        device = DemoDevice()
    else:
        from kws import bridge as b

        b.install_clean_exit()
        # The channel on the command line wins; otherwise the one used last
        # time, so it does not have to be given again every run.
        remembered = cfg.load_channel(config_path)
        try:
            if args.port:
                device = b.WavestationBridge.open(
                    args.port, recv_port=args.recv_port,
                    channel=((args.midi_channel - 1) if args.midi_channel
                             else (remembered if remembered is not None
                                   else 0)))
            else:
                device = b.WavestationBridge.autodetect(
                    on_try=lambda n: print(f"  probing {n}...",
                                           file=sys.stderr))
        except Exception as exc:
            sys.exit(f"error: {exc}")

        # Remember whatever channel is actually in use -- which after an
        # autodetect is the one the unit reported, not the one asked for.
        try:
            cfg.save_channel(getattr(device, "channel", 0), config_path)
        except OSError:
            pass

    from kws.favorites import Favorites

    try:
        store = Favorites(args.favorites)
    except Exception as exc:
        # _migrate refuses a database written by a newer build, which is the
        # right refusal, and sqlite3 raises on a file that is not a database
        # at all. Every other startup failure here leaves by sys.exit; these
        # two used to print a traceback.
        raise SystemExit(f"error: {exc}")
    app = KwsedApp(device, catalog=cat.load(args.catalog),
                   favorites=store, config_path=config_path)
    try:
        app.run()
    finally:
        store.close()
        device.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
