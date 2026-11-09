# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# The legend itself is now vinsynlib's, which carries the copyright of every
# sibling it was assembled from; see src/vinsynlib/ui/hints.py there.
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
import contextlib
import sys
import threading
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from vinsynlib import cli
from vinsynlib.ui.report import ReportScreen
from vinsynlib.ui.work import QUIET_BRIDGE_ERRORS, BusyGate, close_when_idle
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import DataTable, Header, Input, Label, Static

from kws import banks
from kws.multi import MULTISETS
from kws import cards as _cards
from kws import physical as _physical
from kws import messages as m
from kws import catalog as cat
from kws import livenames as _livenames
from kws import scan as _scan
from kws import undo as _undo
from kwsed.gate import EditGate

# The legend's wrapping and the widget that draws it are the family's; see
# src/vinsynlib/keys.py and src/vinsynlib/ui/hints.py, which carry the
# copyright of every sibling the shared version was assembled from. The
# library's KeyHints takes the blocks as a constructor argument where the
# local one read this module's global, so the one construction site below
# passes KEY_HINTS explicitly. The CSS selector is unchanged -- same class
# name, so the stylesheet rule still matches.
from vinsynlib.keys import wrap_blocks  # re-exported: it is in __all__
from vinsynlib.ui.hints import KeyHints
from vinsynlib.ui.modals import ChoiceScreen, RatingFilter, RatingFilterScreen

#: Family quiet set plus this tool's own bridge errors.
_BRIDGE_ERRORS: tuple[type[BaseException], ...] = (
    *QUIET_BRIDGE_ERRORS,
    ValueError,
    LookupError,
    RuntimeError,
    SystemError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kws.favorites import Favorite, Favorites

__all__ = ["KwsedApp", "main", "build_parser", "KEY_HINTS", "wrap_blocks"]

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
    "↑↓ move",
    "tab pane",
    "⏎ select on the unit",
    "r read names",
    "/ search",
    "[ ] channel",
    "c set channel",
    "f favourite",
    "F favourites view",
    "t tags",
    "n note",
    # `m` means channels everywhere in a browser, and a Wavestation Multiset
    # *is* a channel assignment: Multiset 3 is what answers on MIDI channel 3.
    # So this is a clarification rather than a change of function. The
    # `multi` subcommand keeps the instrument's own word for the object, which
    # docs/UX-SPEC.md section 2 permits; the legend names what the key does.
    "m channels",
    "e edit multi",
    "S scan",
    "C capture card",
    "L label card",
    "X card sweep",
    "V rating filter",
    "z undo",
    "w write gate",
    "i device",
    "? help",
    "q quit",
)


def _bank_label(sysex_byte: int) -> str:
    """A part's bank number as a person reads it.

    The number in a Multiset is the instrument's own, which is not the
    position a person sees the banks in -- byte 3 is the Card, not ROM 4.
    """
    try:
        return banks.by_sysex(sysex_byte).label
    except LookupError:
        return f"bank {sysex_byte}"


def _clip(text: str, limit: int = 26) -> str:
    """Clip ``text`` to at most ``limit`` characters.

    A trailing "…" marks the clip. Text that already fits is returned
    unchanged. Non-positive limits yield just the ellipsis.
    """
    if len(text) <= limit:
        return text
    if limit <= 1:
        return "…"
    return text[: limit - 1] + "…"


class MultiEditScreen(ModalScreen[Optional[Dict[str, Any]]]):
    """Edit one Multiset inside the 32-record Multi Mode Setup. **Destructive.**"""

    DEFAULT_CSS = """
    MultiEditScreen { align: center middle; }
    MultiEditScreen > Vertical {
        width: 84; height: auto; border: thick $error;
        background: $surface; padding: 1 2;
    }
    MultiEditScreen .armed { color: $error; text-style: bold; }
    MultiEditScreen DataTable { height: 16; }
    """

    BINDINGS = [
        Binding("escape", "close", "Cancel"),
        Binding("q", "close", "Cancel"),
        Binding("a", "arm", "Arm", show=False),
        Binding("w", "fire", "Write", show=False),
        Binding("space", "toggle", "Used/unused", show=False),
        Binding("plus", "nudge(1)", "Nudge +", show=False),
        Binding("minus", "nudge(-1)", "Nudge -", show=False),
        Binding("b", "bank", "Bank", show=False),
        Binding("n", "rename", "Rename", show=False),
    ]

    def __init__(
        self,
        *,
        multisets: Sequence[Any],
        selected: int = 0,
        device: Any = None,
        on_write: Optional[Callable[[bytes], Any]] = None,
        backup_dir: Optional[str] = None,
        catalog: Any = None,
        name_for: Optional[Callable[[Any], str]] = None,
    ) -> None:
        super().__init__()
        # A copy: the original list and its entries are not mutated.
        self._multisets_copy: List[Any] = list(multisets)
        if not (0 <= selected < len(self._multisets_copy)):
            selected = 0
        self._selected = selected
        self._device = device
        self._on_write = on_write
        self._backup_dir = backup_dir
        self._catalog = catalog
        self._name_for = name_for
        self._armed = False
        self._table: Optional[DataTable[Any]] = None

    # --- helpers ----------------------------------------------------------

    def _ms(self) -> Any:
        return self._multisets_copy[self._selected]

    def _title(self) -> str:
        ms = self._ms()
        name = getattr(ms, "name", "") or ""
        return f"Edit Multiset {getattr(ms, 'number', self._selected):02d} — {name}"

    def _name_for_part(self, part: Any) -> str:
        if not getattr(part, "used", False):
            return ""
        if self._name_for is not None:
            try:
                return str(self._name_for(part)) or ""
            except Exception:
                return ""
        if self._catalog is not None:
            try:
                from kws import banks as _banks

                bank_entry = _banks.by_sysex(part.bank)
                return str(
                    self._catalog.display_name(
                        bank_entry.id, part.performance, _banks.Kind.PERFORMANCE
                    )
                )
            except Exception:
                return ""
        return ""

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._title(), id="multi-edit-title")
            yield Static(
                "⏎ edits the cell · + / - nudge · space on/off · "
                "n rename · a arm, w write · esc cancels",
                classes="hint",
            )
            table: DataTable[Any] = DataTable(id="multi-edit-table", cursor_type="cell")
            table.add_column("ch", key="ch")
            table.add_column("perf", key="perf")
            table.add_column("name", key="name")
            table.add_column("level", key="level")
            table.add_column("used", key="on")
            self._table = table
            yield table
            yield Static("", id="multi-edit-state")
            yield Static("[dim]a to arm, then w to write. esc cancels.[/dim]")

    def on_mount(self) -> None:
        self._populate()
        try:
            self._table.focus()  # type: ignore[union-attr]
        except Exception:  # noqa: S110
            pass

    def _populate(self) -> None:
        table = self._table
        if table is None:
            try:
                table = self.query_one("#multi-edit-table", DataTable)
            except Exception:
                return
        self._table = table
        try:
            coord = table.cursor_coordinate
            row = int(coord.row)
            column = int(coord.column)
        except Exception:
            row = 0
            column = 0
        table.clear()
        ms = self._ms()
        for i in range(16):
            part = ms.parts[i] if i < len(ms.parts) else None
            table.add_row(*self._row_cells(i, part), key=f"r{i}")
        if table.row_count and len(table.columns):
            row = max(0, min(row, table.row_count - 1))
            column = max(0, min(column, len(table.columns) - 1))
            table.move_cursor(row=row, column=column)

    def _row_cells(self, i: int, part: Any) -> tuple[Any, ...]:
        if part is None:
            return (f"{i + 1:02d}", "--", "", "--", "--")
        if getattr(part, "used", False):
            perf = f"{_bank_label(part.bank)} {part.performance:02d}"
            name = self._name_for_part(part)
            used = "used"
        else:
            perf = "--"
            name = ""
            used = "--"
        return (f"{i + 1:02d}", perf, name, str(part.level), used)

    def _channel_index(self) -> int:
        table = self._table
        if table is None:
            return 0
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            key = row_key.value
            if isinstance(key, str) and key.startswith("r"):
                return int(key[1:])
        except Exception:  # noqa: S110
            pass
        try:
            return int(table.cursor_coordinate.row)
        except Exception:
            return 0

    def _column_key(self) -> str:
        table = self._table
        if table is None:
            return ""
        try:
            _, column_key = table.coordinate_to_cell_key(table.cursor_coordinate)
            return str(column_key.value)
        except Exception:
            return ""

    def _rebuild_row(self, index: int) -> None:
        table = self._table
        if table is None:
            return
        ms = self._ms()
        part = ms.parts[index] if index < len(ms.parts) else None
        cells = self._row_cells(index, part)
        try:
            for col_idx, col_key in enumerate(("ch", "perf", "name", "level", "on")):
                table.update_cell(f"r{index}", col_key, cells[col_idx])
        except Exception:
            self._populate()

    def _set_part(self, index: int, new_part: Any) -> None:
        from dataclasses import replace as _replace

        ms = self._ms()
        parts = list(ms.parts)
        if index >= len(parts):
            return
        parts[index] = new_part
        new_ms = _replace(ms, parts=tuple(parts))
        self._multisets_copy[self._selected] = new_ms
        self._rebuild_row(index)
        self._rebuild_title()

    def _rebuild_title(self) -> None:
        try:
            self.query_one("#multi-edit-title", Label).update(self._title())
        except Exception:  # noqa: S110
            pass

    # --- editing ---------------------------------------------------------

    def on_data_table_cell_selected(self, event: Any) -> None:
        try:
            if event.data_table.id != "multi-edit-table":
                return
        except Exception:  # noqa: S110
            pass
        col = self._column_key()
        idx = self._channel_index()
        if col == "perf":
            self._edit_perf(idx)
        elif col == "level":
            self._edit_level(idx)
        elif col == "on":
            self.action_toggle()

    def _edit_perf(self, index: int) -> None:
        ms = self._ms()
        part = ms.parts[index]
        current = str(part.performance) if part.used else "off"

        def done(text: Optional[str]) -> None:
            if text is None:
                return
            raw = text.strip().lower()
            if raw in ("", "off", "unused", "no"):
                from dataclasses import replace as _replace

                new_part = _replace(part, performance=50)
                self._set_part(index, new_part)
                return
            try:
                value = int(raw)
            except ValueError:
                self._status(f"performance must be 0-49 or off, not {text!r}")
                return
            if value < 0 or value > 49:
                self._status("performance must be 0-49 or off")
                return
            from dataclasses import replace as _replace

            new_part = _replace(part, performance=value)
            self._set_part(index, new_part)

        self.app.push_screen(
            PromptScreen("performance (0-49, off/empty unused):", current),
            done,
        )

    def _edit_level(self, index: int) -> None:
        ms = self._ms()
        part = ms.parts[index]

        def done(text: Optional[str]) -> None:
            if text is None or not text.strip():
                return
            try:
                value = int(text.strip())
            except ValueError:
                self._status(f"level must be 0-127, not {text!r}")
                return
            if value < 0 or value > 127:
                self._status("level must be 0-127")
                return
            from dataclasses import replace as _replace

            new_part = _replace(part, level=value)
            self._set_part(index, new_part)

        self.app.push_screen(PromptScreen("level (0-127):", str(part.level)), done)

    def action_toggle(self) -> None:  # type: ignore[override]
        idx = self._channel_index()
        ms = self._ms()
        part = ms.parts[idx]
        from dataclasses import replace as _replace

        if part.used:
            new_part = _replace(part, performance=50)
        else:
            new_part = _replace(part, performance=0)
        self._set_part(idx, new_part)

    def action_nudge(self, by: int) -> None:
        idx = self._channel_index()
        ms = self._ms()
        part = ms.parts[idx]
        new_level = part.level + int(by)
        if new_level < 0 or new_level > 127:
            self._status(f"level must be 0-127, not {new_level}")
            return
        from dataclasses import replace as _replace

        new_part = _replace(part, level=new_level)
        self._set_part(idx, new_part)

    def action_bank(self) -> None:
        idx = self._channel_index()
        ms = self._ms()
        part = ms.parts[idx]

        def done(text: Optional[str]) -> None:
            if text is None:
                return
            raw = text.strip()
            if not raw:
                return
            bank_sysex = self._parse_bank(raw)
            if bank_sysex is None:
                self._status(f"bank must be 0-11 or CARD/C, not {text!r}")
                return
            from dataclasses import replace as _replace

            new_part = _replace(part, bank=bank_sysex)
            self._set_part(idx, new_part)

        self.app.push_screen(
            PromptScreen(
                f"bank (0-11, CARD/C), now {_bank_label(part.bank)}:",
                _bank_label(part.bank),
            ),
            done,
        )

    def _parse_bank(self, text: str) -> Optional[int]:
        upper = text.strip().upper()
        if upper in ("CARD", "C"):
            try:
                from kws import banks as _banks

                return _banks.bank("CARD").sysex
            except Exception:
                return None
        # label / id match
        try:
            from kws import banks as _banks

            for b in _banks.BANKS:
                if upper == b.label.upper() or upper == b.id.upper():
                    return b.sysex
        except Exception:  # noqa: S110
            pass
        try:
            n = int(text.strip())
        except ValueError:
            return None
        try:
            from kws import banks as _banks

            if 0 <= n < len(_banks.BANKS):
                return _banks.BANKS[n].sysex
        except Exception:
            return None
        return None

    def action_rename(self) -> None:
        ms = self._ms()
        current = getattr(ms, "name", "") or ""

        def done(text: Optional[str]) -> None:
            if text is None:
                return
            new_name = text.strip()[:15]
            if not new_name:
                return
            from dataclasses import replace as _replace

            new_ms = _replace(ms, name=new_name)
            self._multisets_copy[self._selected] = new_ms
            self._rebuild_title()

        self.app.push_screen(PromptScreen("name (max 15):", current), done)

    # --- arm / write ------------------------------------------------------

    def action_arm(self) -> None:
        self._armed = True
        self._status("[b]ARMED[/b] - w writes the Multi Mode Setup. Nothing sent yet.")

    def action_fire(self) -> None:  # noqa: C901
        if not self._armed:
            self._status("not armed - press a first", refused=True)
            return
        self._armed = False
        app = None
        try:
            app = self.app
        except Exception:
            app = None
        if app is not None and hasattr(app, "_multi_edit_write_worker"):
            if getattr(app, "_busy", False):
                self._status("busy", refused=True)
                return
            exchange = getattr(app, "_exchange", None)
            if exchange is not None:
                try:
                    if not exchange.acquire(blocking=False):
                        self._status("busy", refused=True)
                        return
                except Exception:  # noqa: S110
                    pass
            app._multi_edit_write_worker(
                list(self._multisets_copy), self._device, self._backup_dir, self
            )
            return
        # Fallback (no app): direct, for unit contexts.
        from kws import multi as multi_mod

        encoded = multi_mod.encode(self._multisets_copy)
        try:
            if self._on_write is not None:
                result = self._on_write(encoded)
            else:
                device = self._device
                kwargs: Dict[str, Any] = {}
                if self._backup_dir is not None:
                    kwargs["backup_dir"] = self._backup_dir
                else:
                    try:
                        from kws import backup as _backup

                        kwargs["backup_dir"] = _backup.default_dir()
                    except Exception:  # noqa: S110
                        pass
                kwargs["verify"] = True
                result = device.write_multi(encoded, **kwargs)
        except Exception as exc:
            self._status(f"write failed {exc}", refused=True)
            return
        try:
            if app is not None and hasattr(app, "_record_write"):
                payload = result if isinstance(result, dict) else {"backup": None}
                app._record_write(payload)
            if app is not None and hasattr(app, "_multi_sets"):
                app._multi_sets = list(self._multisets_copy)
                app._multisets = list(self._multisets_copy)  # type: ignore[attr-defined]
        except Exception:  # noqa: S110
            pass
        self.dismiss(result if isinstance(result, dict) else {"result": result})

    def _status(self, message: str, *, refused: bool = False) -> None:
        try:
            status = self.query_one("#multi-edit-state", Static)
            status.update(message)
        except Exception:  # noqa: S110
            pass

    def _write_failed(self, message: str) -> None:
        self._status(f"write failed {message}", refused=True)

    def _write_succeeded(self, result: Any) -> None:
        try:
            app = self.app
            if hasattr(app, "_record_write"):
                payload = result if isinstance(result, dict) else {"backup": None}
                app._record_write(payload)
            if hasattr(app, "_multi_sets"):
                app._multi_sets = list(self._multisets_copy)
            if hasattr(app, "_multisets"):
                app._multisets = list(self._multisets_copy)
            if hasattr(app, "_after_multi_write"):
                app._after_multi_write()
        except Exception:  # noqa: S110
            pass
        self.dismiss(result if isinstance(result, dict) else {"result": result})

    def action_close(self) -> None:
        self.dismiss(None)


class SendScreen(ModalScreen[Optional[Dict[str, Any]]]):
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

    def __init__(
        self,
        *,
        path: str,
        kind: str,
        destination: banks.Bank,
        current: Optional[Dict[int, str]] = None,
        on_send: Optional[Callable[[banks.Bank, str], None]] = None,
    ) -> None:
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
            yield Static("[dim]a to arm, then w to write. esc cancels.[/dim]")

    def _describe(self) -> str:
        lines = [
            f"source      {self._path}",
            f"destination {self._destination.label}  "
            f"({'writable' if self._destination.writable else 'ROM'})",
            f"kind        {self._kind}",
        ]
        if self._current:
            shown = ", ".join(
                f"{n:03d} {name}" for n, name in sorted(self._current.items())[:5]
            )
            more = " ..." if len(self._current) > 5 else ""
            lines.append(f"replacing   {shown}{more}")
        else:
            lines.append("replacing   (names not read)")
        return "\n".join(lines)

    def on_mount(self) -> None:
        if not self._destination.writable:
            self.query_one("#send-state", Static).update(
                "[b]$error[/b] this bank is ROM and cannot be written"
            )

    def action_arm(self) -> None:
        if not self._destination.writable:
            return
        self._armed = True
        self.query_one("#send-state", Static).update(
            f"[b]ARMED[/b] - w replaces {self._destination.label} "
            f"{self._kind}s. Nothing has been sent yet."
        )

    def action_fire(self) -> None:
        """Only while armed, and only once."""
        if not self._armed:
            self.query_one("#send-state", Static).update(
                "[dim]not armed - press a first[/dim]"
            )
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
                    f"[b]write failed[/b] {exc}"
                )
                return
        self.dismiss({"destination": self._destination.number, "kind": self._kind})

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


MULTI_BANK = -1
MULTI_KIND = "multiset"


class KwsedApp(App[None]):
    """The browser."""

    CSS = """
    Screen { layers: base; }
    #panes { height: 1fr; }
    /* Sized to its content. A fixed width does not truncate the end
       of a label -- the table scrolls sideways and clips the
       *start*, so a bank reads as nonsense rather than as narrow. */
    #bank-table {
        width: auto; min-width: 20; max-width: 64;
        border-right: solid $panel;
    }
    #slot-table { width: 1fr; }
    DataTable { height: 1fr; }
    /* Fixed: `height: auto` under-counts a wrapped line in this Textual
       version, and the last line was clipped. Content is kept to two
       lines, so four gives a wrap of headroom. */
    #detail { height: 5; padding: 0 1; border-top: solid $panel; }
    #status {
        height: auto; max-height: 3; background: $boost; color: $text; padding: 0 1;
    }
    """

    BINDINGS = [
        # Enter is NOT bound here: a focused DataTable consumes it for its
        # own row selection, so it arrives as RowSelected instead.
        Binding("r", "read_names", "Read names"),
        Binding("left_square_bracket", "channel_down", "Channel -"),
        Binding("right_square_bracket", "channel_up", "Channel +"),
        Binding("c", "pick_channel", "Set channel"),
        # Description matches the legend: `m` opens the channel assignment,
        # which is what a Multiset is. The action keeps its own name.
        Binding("m", "multi", "Channels"),
        Binding("e", "edit_multi", "Edit multi", show=False),
        Binding("f", "toggle_favorite", "Favourite"),
        Binding("F", "cycle_favorites", "Favourites view"),
        Binding("t", "edit_tags", "Tags"),
        Binding("n", "edit_note", "Note"),
        Binding("slash", "search", "Search"),
        Binding("i", "device_info", "Device"),
        Binding("question_mark", "help", "Help"),
        Binding("tab", "switch_pane", "Switch pane"),
        Binding("q", "quit", "Quit"),
        Binding("S", "scan_device", "Scan"),
        Binding("C", "capture_card", "Capture card"),
        Binding("L", "label_card", "Label card"),
        Binding("X", "card_sweep", "Card sweep", show=False),
        Binding("V", "rating_filter", "Rating filter"),
        Binding("z", "undo", "Undo"),
        Binding("w", "toggle_write_gate", "Write gate"),
    ]

    def __init__(
        self,
        device: Any,
        *,
        catalog: Optional[cat.Catalog] = None,
        favorites: Optional[Favorites] = None,
        config_path: Optional[str] = None,
        live_path: Optional[str] = None,
        cards_path: Optional[str] = None,
        allow_write: bool = False,
        auto_capture: bool = False,
        card_mode: str = "hcard",
        show_card_defaults: bool = False,
        physical_path: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.device = device
        #: Probe the active HCard bank once when the browser starts. Off by
        #: default so tests and embedders never touch the unit on launch;
        #: main() turns it on for a real session. The card is switched
        #: physically, so this read is the only way to know the bank.
        self._auto_capture = auto_capture
        #: Injected, never opened here, so a test cannot write into the
        #: user's real database.
        self.favorites = favorites
        #: Where the send channel is remembered. Injected and None
        #: in tests, so a test cannot drop a config into the
        #: working directory.
        self._config_path = config_path
        self.view_mode = VIEW_ALL
        self.rating_filter: RatingFilter | None = None
        self.catalog = catalog if catalog is not None else cat.empty()
        #: Where hardware-read names are cached between runs. None disables
        #: persistence, as in tests and --demo.
        self._live_path = live_path
        #: The HCard archive, beside the favourites database. None keeps
        #: captures in memory only, as in tests and --demo.
        self._cards_path = cards_path
        self._archive: Dict[int, _cards.CapturedBank] = (
            _cards.load(cards_path) if cards_path is not None else {}
        )
        # Card mode handling.
        from kws import config as _cfg

        if card_mode not in _cfg.CARD_MODES:
            card_mode = _cfg.DEFAULT_CARD_MODE
        self._card_mode = card_mode
        self._show_card_defaults = show_card_defaults
        self._physical_path = physical_path
        self._physical = _physical.load(physical_path) if physical_path else {}
        self._active_physical = (
            _physical.active(physical_path) if physical_path else None
        )
        self._current_physical: Optional[str] = None
        self._current_physical_kind: Optional[str] = None
        self._lock = threading.RLock()
        #: The left pane lists bank-and-kind pairs, because a Wavestation
        #: bank holds 50 performances *and* 35 patches and they are
        #: different things. A pane listing banks alone would need a second
        #: control to say which kind, for no gain.
        #:
        #: The physical Card is not one row pair: an HCard multiplexes
        #: forty banks behind it (docs/HCARD.md, decision 4-A1), so it
        #: expands into forty sub-banks, each listed once per kind.
        self._build_row_model()
        self._current_pair = self._pairs[0]
        self._current_card: Optional[int] = None
        self._current_multi: Optional[int] = None
        self._slot_mode: Optional[str] = None
        self._multisets: List[Any] = []
        self._multi_sets = self._multisets
        #: Which HCard bank was last confirmed or asserted active. Persisted
        #: so a new session can warn before auditioning from a different
        #: bank: the card is switched physically and the synth cannot be
        #: asked (docs/HCARD.md, TODO item 25).
        self._active_card = (
            _cards.load_active(cards_path) if cards_path is not None else None
        )
        self._current: List[banks.Slot] = []
        self._busy_gate = BusyGate()
        self._exchange = threading.Lock()
        self._write_gate = EditGate(armed=allow_write)
        self._filling = False
        #: Scan state
        self._scanning = False
        self._scan_cancel = threading.Event()
        #: Card sweep: one bank at a time, waiting for a person to select
        #: the next bank on the HCard itself.
        self._card_sweeping = False
        if config_path is not None:
            try:
                from kws import config as _cfg

                self._scan_offer_dismissed = _cfg.load_scan_offer_dismissed(config_path)
            except Exception:
                self._scan_offer_dismissed = False
        else:
            self._scan_offer_dismissed = False

        self._undo_log = _undo.make_log(
            apply_hook=self._undo_apply,
            gate_hook=self._undo_gate,
            status_hook=self.notify_status,
        )

    @property
    def _busy(self) -> bool:
        return (
            self._busy_gate.check() is not None
            or self._busy_gate.is_busy()
            or self._exchange.locked()
        )

    @_busy.setter
    def _busy(self, value: bool) -> None:
        if value:
            if not self._exchange.locked():
                self._exchange.acquire()
            try:
                self._busy_gate.refuse("busy")
            except RuntimeError:
                pass
        else:
            if self._exchange.locked():
                try:
                    self._exchange.release()
                except RuntimeError:
                    pass
            with self._busy_gate._lock:
                self._busy_gate._refused = None

    def _undo_gate(self) -> str | None:
        return None if self._write_gate.armed else "the write gate is locked"

    def _record_write(  # noqa: C901
        self, *args: Any, **kwargs: Any
    ) -> None:
        kind: Any = kwargs.pop("kind", None)
        destination: Any = kwargs.pop("destination", kwargs.pop("bank", None))
        original: Any = kwargs.pop("original", kwargs.pop("data", None))
        mode: Any = kwargs.pop("mode", None)
        if kind is None and destination is None and original is None and len(args) == 1:
            payload = args[0]
            if isinstance(payload, dict):
                backup_path = payload.get("backup")
                destination = payload.get("bank", payload.get("destination"))
                kind = payload.get("kind")
                if backup_path is not None:
                    try:
                        from kws import backup as _backup

                        loaded = _backup.load(backup_path)
                        original = loaded.blocks.get("dump")
                        if destination is None:
                            destination = loaded.slot
                        if kind is None:
                            kind = loaded.kind
                    except Exception:
                        original = None
                args = ()
        if kind is None or destination is None or original is None:
            if len(args) >= 3:
                a, b, c = args[0], args[1], args[2]
                if isinstance(a, str) and not isinstance(b, str):
                    kind, destination, original = a, b, c
                elif isinstance(b, str) and not isinstance(a, str):
                    destination, kind, original = a, b, c
                else:
                    destination, kind, original = a, b, c
            elif len(args) == 2:
                destination, original = args[0], args[1]
                if kind is None:
                    kind = banks.Kind.PATCH
        if kind is None or destination is None or original is None:
            try:
                self.notify_status("write not recorded for undo", refused=True)
            except Exception:  # noqa: S110
                pass
            return
        label = f"bank {destination}"
        record = _undo.Record(label=label, payload=(kind, destination, original))
        self._undo_log.push(record)
        if mode is not None:
            try:
                self._unit_mode = mode
            except Exception:  # noqa: S110
                pass

    def _undo_apply(self, record: _undo.Record) -> bool:  # noqa: C901
        try:
            payload = record.payload
            backup_path: Any = None
            bank: Any = None
            kind: Any = None
            data: Any = None
            if isinstance(payload, dict):
                backup_path = payload.get("backup")
                bank = payload.get("bank", payload.get("destination"))
                kind = payload.get("kind")
                data = payload.get("data", payload.get("original"))
            elif isinstance(payload, (tuple, list)):
                if len(payload) == 1 and isinstance(payload[0], str):
                    backup_path = payload[0]
                elif len(payload) >= 3:
                    first, second, third = payload[0], payload[1], payload[2]
                    if isinstance(first, str) and not isinstance(second, str):
                        kind, bank, data = first, second, third
                    elif isinstance(second, str) and not isinstance(first, str):
                        bank, kind, data = first, second, third
                    else:
                        bank, kind, data = first, second, third
                elif len(payload) == 2:
                    bank, data = payload[0], payload[1]
                    kind = banks.Kind.PATCH
            elif isinstance(payload, str):
                backup_path = payload
            if backup_path is not None and data is None:
                from kws import backup as _backup

                loaded = _backup.load(backup_path)
                data = loaded.blocks.get("dump")
                if bank is None:
                    bank = loaded.slot
                if kind is None:
                    kind = loaded.kind
            if bank is None or kind is None or data is None:
                raise ValueError("incomplete undo payload")

            import inspect

            is_multi = str(kind).lower() == "multi" or (
                isinstance(bank, str) and bank.lower() == "multi"
            )
            kwargs: dict[str, Any] = {}
            if is_multi:
                try:
                    sig = inspect.signature(self.device.write_multi)
                    if "backup_dir" in sig.parameters:
                        from kws import backup as _backup

                        kwargs["backup_dir"] = _backup.default_dir()
                    if "verify" in sig.parameters:
                        kwargs["verify"] = True
                    if "timeout" in sig.parameters:
                        pass
                except (TypeError, ValueError, AttributeError):
                    pass
                result = self.device.write_multi(bytes(data), **kwargs)
            else:
                kwargs = {}
                try:
                    sig = inspect.signature(self.device.write_bank)
                    if "backup_dir" in sig.parameters:
                        from kws import backup as _backup

                        kwargs["backup_dir"] = _backup.default_dir()
                    if "verify" in sig.parameters:
                        kwargs["verify"] = True
                except (TypeError, ValueError):
                    pass
                result = self.device.write_bank(bank, kind, bytes(data), **kwargs)
            if isinstance(result, dict):
                verified = result.get("verified", True)
                if verified is False:
                    return False
            return True
        except Exception as exc:
            if threading.current_thread() is threading.main_thread():
                self.notify_status(f"undo failed: {exc}", refused=True)
            return False

    def _undo_settle(
        self, results: list[bool], failed: list[Any] | None = None
    ) -> None:
        for record in reversed(failed or []):
            self._undo_log.push(record)
        self._undo_log.settle(results)

    @work(thread=True)
    def _undo_worker(self, records: list[Any]) -> None:
        results: list[bool] = []
        failed: list[Any] = []
        try:
            for record in records:
                with self._busy_gate, self._lock:
                    ok = self._undo_apply(record)
                results.append(ok)
                if not ok:
                    failed.append(record)
        finally:
            try:
                self._exchange.release()
            except RuntimeError:
                pass
        self.call_from_thread(self._undo_settle, results, failed)

    def action_undo(self) -> None:
        if not self._write_gate.armed:
            self.notify_status("write gate is locked", refused=True)
            return
        if self._busy:
            self.notify_status("busy", refused=True)
            return
        records = self._undo_log.plan(1)
        if not records:
            self.notify_status("nothing to undo", refused=True)
            return
        if not self._exchange.acquire(blocking=False):
            self.notify_status("busy", refused=True)
            return
        self._undo_worker(records)

    def action_toggle_write_gate(self) -> None:
        self._write_gate.toggle()
        self.notify_status(self._write_gate.describe())

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="panes"):
            yield DataTable(id="bank-table", cursor_type="row")
            yield DataTable(id="slot-table", cursor_type="row")
        yield Static("", id="detail")
        yield Static("", id="status")
        # The library's widget takes the blocks; the local one read this
        # module's KEY_HINTS global, which a library cannot do.
        yield KeyHints(KEY_HINTS)

    def _row_key(
        self,
        pair: Tuple[int, str],
        card: Optional[int],
        multi: Optional[int] = None,
        physical: Optional[str] = None,
    ) -> str:
        """The stable key of one left-pane row."""
        if physical is not None:
            return f"physical:{physical or 'none'}:{pair[1]}"
        if multi is not None:
            return f"multi:{multi:02d}"
        number, kind = pair
        if card is None:
            return f"{number}:{kind}"
        return f"card:{card:02d}:{kind}"

    def _build_row_model(self) -> None:
        """Rebuild ``_pairs`` and the row-parallel lists for the current mode.

        Pure data: no widget mutation. Called from ``__init__`` and after a
        card archive change (capture, label) so the left pane reflects the
        new set of physical cards.
        """
        self._real_pairs: List[Tuple[int, str]] = [
            (entry.number, kind)
            for entry in banks.BANKS
            if entry.id != "CARD"
            for kind in (banks.Kind.PERFORMANCE, banks.Kind.PATCH)
        ]
        _card_pairs: List[Tuple[int, str]] = []
        _extra_cards: List[Optional[int]] = []
        _extra_physical: List[Optional[str]] = []
        if self._card_mode == "hcard":
            _card_pairs = [
                (banks.bank("CARD").number, kind)
                for _number in _cards.bank_numbers()
                for kind in (banks.Kind.PERFORMANCE, banks.Kind.PATCH)
            ]
            _extra_cards = [
                number for number in _cards.bank_numbers() for _kind in range(2)
            ]
            _extra_physical = [None] * len(_card_pairs)
        elif self._card_mode == "cards":
            ids = sorted(self._physical.keys())
            if not ids:
                ids = [""]
            _card_pairs = [
                (banks.bank("CARD").number, kind)
                for _id in ids
                for kind in (banks.Kind.PERFORMANCE, banks.Kind.PATCH)
            ]
            _extra_cards = [None] * len(_card_pairs)
            _extra_physical = [_id for _id in ids for _ in (0, 1)]
        # mode "none": no extra pairs.

        self._pairs: List[Tuple[int, str]] = (
            self._real_pairs + _card_pairs + [(MULTI_BANK, MULTI_KIND)] * MULTISETS
        )
        self._row_cards: List[Optional[int]] = (
            [None] * len(self._real_pairs) + _extra_cards + [None] * MULTISETS
        )
        self._row_physical: List[Optional[str]] = (
            [None] * len(self._real_pairs) + _extra_physical + [None] * MULTISETS
        )
        #: Which Multiset (0..31) each row of ``_pairs`` is, or None.
        self._row_multis: List[Optional[int]] = [None] * (
            len(self._real_pairs) + len(_card_pairs)
        ) + list(range(MULTISETS))

    def _apply_row_model(self) -> None:
        """Clear the bank table and re-add rows for the current model.

        Columns are left in place; the cursor is preserved where possible.
        """
        table = self.query_one("#bank-table", DataTable)
        cursor_row = table.cursor_row
        table.clear()
        for pair, card, multi, physical in zip(
            self._pairs, self._row_cards, self._row_multis, self._row_physical
        ):
            table.add_row(
                *self._bank_row(pair, card, multi, physical),
                key=self._row_key(pair, card, multi, physical),
            )
        if self._pairs:
            try:
                table.move_cursor(row=min(cursor_row or 0, len(self._pairs) - 1))
            except Exception:  # noqa: S110
                pass

    def _clip(self, text: str, limit: int = 26) -> str:
        """Instance wrapper around the module-level :func:`_clip`."""
        return _clip(text, limit)

    def _card_row_name(self, number: int) -> str:
        """The name cell of one HCard sub-bank.

        Not captured is greyed rather than hidden: the forty banks are the
        reason the Card row exists, and a bank that has not been read is
        still a place a capture can land.
        """
        captured = self._archive.get(number)
        entry = _cards.card(number)
        pcm = f" · needs {entry.needs_pcm}" if entry.needs_pcm else ""
        active = self._active_card == number
        # Only once a bank is known can the others be marked as *not* it;
        # with nothing confirmed every row stays neutral rather than all
        # being greyed.
        inactive = self._active_card is not None and not active
        prefix = "• " if active else ""
        if captured is None:
            body = f"not captured{pcm}"
            if inactive:
                return f"[dim][i]{body}[/i][/dim]"
            return f"{prefix}[dim]{body}[/dim]"
        title = _clip(_cards.bank_title(captured, entry))
        # A `*` marks a real read name that disagrees with the printed
        # table -- only for non-user banks, and never for a user label.
        marker = ""
        if (
            not (captured.label or "").strip()
            and (captured.name or "").strip()
            and entry.category != _cards.Category.USER
            and captured.name.strip().casefold() != entry.name.strip().casefold()
        ):
            marker = " [dim]*[/dim]"
        body = f"{prefix}{title}{marker}"
        if inactive:
            return f"[dim][i]{body}[/i][/dim]"
        return body

    def _bank_row(
        self,
        pair: Tuple[int, str],
        card: Optional[int],
        multi: Optional[int] = None,
        physical: Optional[str] = None,
    ) -> List[str]:
        if physical is not None:
            number, kind = pair
            if physical in ("", None):
                label = "[dim]insert a card and press C[/dim]"
            else:
                card_obj = self._physical.get(physical)
                raw_label = card_obj.label if card_obj is not None else ""
                label = self._clip(raw_label) if raw_label else f"Card {physical}"
                if physical == self._active_physical:
                    label = f"• {label}"
            heart = ""
            if physical not in ("", None):
                marked = len(self._favorite_keys(_physical.scope_id(physical), kind))
                heart = str(marked) if marked else ""
            return [
                f"Card {physical or '—'}",
                kind[:4],
                str(banks.count_for(kind)),
                "",
                label,
                heart,
            ]
        if multi is not None:
            multisets = getattr(self, "_multisets", None)
            if multisets is None:
                multisets = getattr(self, "_multi_sets", []) or []
            name = ""
            if 0 <= multi < len(multisets):
                name = getattr(multisets[multi], "name", "") or ""
            if not name:
                name = "[dim]--[/dim]"
            return [f"Multi {multi:02d}", "set", "16", "", name, ""]
        number, kind = pair
        entry = banks.bank(number)
        marked = len(self._favorite_keys(entry.id, kind)) if card is None else 0
        label = entry.label if card is None else f"Card {card:02d}"
        if (
            card is not None
            and self._active_card is not None
            and card != self._active_card
        ):
            label = f"[dim][i]{label}[/i][/dim]"
        return [
            label,
            kind[:4],
            str(banks.count_for(kind)),
            "" if entry.writable else "ro",
            "" if card is None else self._card_row_name(card),
            # blank rather than "0": a column of zeroes is
            # noise, and the eye wants the few that differ.
            # Card rows all share the one CARD favourite count, so
            # repeating it on forty rows is noise too.
            "" if card is not None else (str(marked) if marked else ""),
        ]

    def on_mount(self) -> None:
        self.title = "kwsed"
        table = self.query_one("#bank-table", DataTable)
        for label, key in (
            ("bank", "bank"),
            ("kind", "kind"),
            ("n", "n"),
            ("", "rw"),
            ("name", "name"),
            ("♥", "fav"),
        ):
            table.add_column(label, key=key)
        for pair, card, multi, physical in zip(
            self._pairs, self._row_cards, self._row_multis, self._row_physical
        ):
            table.add_row(
                *self._bank_row(pair, card, multi, physical),
                key=self._row_key(pair, card, multi, physical),
            )

        self._ensure_slot_columns("slots")
        if self._live_path is not None:
            _livenames.apply_to(self.catalog, self._live_path)
        self._fill(self._current_pair)
        table.focus()
        self.notify_status(
            f"{len(self.catalog)} names from the catalog"
            if self.catalog
            else "no catalog -- numbers only. Build one with "
            "tools/extract_catalog.py, or press r to read from the unit."
        )
        # Offer to scan for names if there is no catalog at all.
        self.set_timer(0.6, self._maybe_offer_scan)
        # Confirm which HCard bank is in front of the synth. The card is
        # switched physically, so a read is the only way to know; it is
        # silent and changes nothing on the unit.
        if self._auto_capture and self._cards_path is not None:
            self.set_timer(1.4, self._auto_capture_card)

    # --- filling ------------------------------------------------------------

    def _scoped_bank_id(self, item: banks.Slot) -> str:
        """Bank id used for favourites on the row currently under the cursor.

        In ``cards`` mode a real archived card owns its own key space
        (``CARD/c1``); otherwise the item's own bank id is used. HCard
        card-row scoping is part 3c and intentionally left as a seam here.
        """
        if (
            self._current_physical not in (None, "")
            and self._current_physical in self._physical
        ):
            return _physical.scope_id(self._current_physical)
        return item.bank.id

    def _favorite_keys(self, bank_id: str, kind: str) -> Set[int]:
        if self.favorites is None:
            return set()
        return self.favorites.keys_for_bank(favorite_bank_id(bank_id, kind))

    def _all_favourites(self) -> List[banks.Slot]:
        """Every favourite in every bank and kind, in listing order.

        In ``cards`` mode only the active physical card's favourites are
        included (under its scoped id); other cards' favourites are
        visible on their archive row when selected, not in this view. Other
        modes keep the previous shared-``CARD`` behaviour as a footprint
        for part 3c.
        """
        if self.favorites is None:
            return []
        wanted: Dict[str, Set[int]] = {}
        for favourite in self.favorites.all(order="bank"):
            wanted.setdefault(favourite.bank_id, set()).add(favourite.number)
        out: List[banks.Slot] = []
        # The Card's forty sub-banks share the one CARD key (decision 8-B1),
        # so a favourite there is listed once, not forty times.
        pairs = self._real_pairs + [
            (banks.bank("CARD").number, kind)
            for kind in (banks.Kind.PERFORMANCE, banks.Kind.PATCH)
        ]
        for number, kind in pairs:
            entry = banks.bank(number)
            numbers = wanted.get(favorite_bank_id(entry.id, kind))
            if not numbers:
                continue
            out.extend(s for s in banks.slots(number, kind) if s.number in numbers)
        # Per-card favourites for the active card (cards mode only).
        if (
            self._card_mode == "cards"
            and self._active_physical not in (None, "")
            and self._physical
        ):
            card_number = banks.bank("CARD").number
            scoped = _physical.scope_id(self._active_physical)
            for kind in (banks.Kind.PERFORMANCE, banks.Kind.PATCH):
                numbers = wanted.get(favorite_bank_id(scoped, kind))
                if not numbers:
                    continue
                out.extend(
                    s for s in banks.slots(card_number, kind) if s.number in numbers
                )
        return self._by_rating(out)

    def _visible(self, pair: Tuple[int, str]) -> List[banks.Slot]:
        number, kind = pair
        if self.view_mode == VIEW_ALL_FAVOURITES:
            return self._all_favourites()
        entries = banks.slots(number, kind)
        if self.view_mode == VIEW_BANK_FAVOURITES:
            marked = self._favorite_keys(banks.bank(number).id, kind)
            entries = [e for e in entries if e.number in marked]
        return self._by_rating(entries)

    def _by_rating(self, entries: List[banks.Slot]) -> List[banks.Slot]:
        if self.rating_filter is None:
            return entries
        out = []
        for entry in entries:
            if self.favorites is None:
                rating = 0
            else:
                fav = self.favorites.get(
                    favorite_bank_id(self._scoped_bank_id(entry), entry.kind),
                    entry.number,
                )
                rating = fav.rating if fav is not None else 0
            if self.rating_filter.at_least:
                if rating >= self.rating_filter.stars:
                    out.append(entry)
            elif rating == self.rating_filter.stars:
                out.append(entry)
        return out

    def _cursor_entry(self) -> Optional[banks.Slot]:
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
            self.notify_status("this device has no settable channel", refused=True)
            return
        self._remember_channel(channel)
        self.notify_status(f"sending on MIDI channel {channel + 1}")

    def _remember_channel(self, channel: int) -> None:
        """Save it for next time. Only OSError is swallowed."""
        # __init__ always assigns _config_path, so the getattr guard that used
        # to stand here was defending against an attribute that cannot be
        # missing -- and it hid the type from everything downstream.
        if self._config_path is None:
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
        def apply(text: Optional[str]) -> None:
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
            PromptScreen("MIDI channel (1-16):", str(self._channel() + 1)), apply
        )

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
        key = favorite_bank_id(self._scoped_bank_id(item), item.kind)
        name = self.catalog.display_name(item.bank.id, item.number, item.kind)
        # Favorites.toggle, not remove-then-add: un-favouriting keeps
        # the row so a rating, tags, a note and the original date
        # survive being toggled off and on. As a delete followed by a
        # fresh add this destroyed all four, and this is one keystroke.
        now = self.favorites.toggle(key, item.number, name=name)
        self.notify_status(f"{'favourited' if now else 'unfavourited'} {name or item}")
        self._refill_keeping_place()

    def _favourite_under_cursor(
        self,
    ) -> Tuple[Optional[Tuple[str, int]], Optional[Favorite]]:
        """The favourite row for the slot under the cursor, if there is one.

        Tags and notes hang off a favourite, so there is nothing to attach
        them to until `f` has been pressed. Saying so is better than
        silently creating one: the user asked to annotate, not to favourite.
        """
        entry = self._cursor_entry()
        if entry is None or self.favorites is None:
            return None, None
        bank_id = favorite_bank_id(self._scoped_bank_id(entry), entry.kind)
        number = entry.number
        return (bank_id, number), self.favorites.get(bank_id, number)

    def action_edit_tags(self) -> None:
        where, existing = self._favourite_under_cursor()
        if where is None:
            return
        if existing is None:
            self.notify_status("not a favourite yet -- press f first", refused=True)
            return

        def apply(value: Optional[str]) -> None:
            if value is None or self.favorites is None:
                return
            self.favorites.set_tags(where[0], where[1], value)
            self._refill_keeping_place()
            self.notify_status("tags set")

        self.push_screen(PromptScreen("Tags (comma separated):", existing.tags), apply)

    def action_edit_note(self) -> None:
        where, existing = self._favourite_under_cursor()
        if where is None:
            return
        if existing is None:
            self.notify_status("not a favourite yet -- press f first", refused=True)
            return

        def apply(value: Optional[str]) -> None:
            if value is None or self.favorites is None:
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
            where = (
                "this bank" if self.view_mode == VIEW_BANK_FAVOURITES else "any bank"
            )
            self.notify_status(
                f"{VIEW_LABELS[self.view_mode]} - nothing favourited in {where} yet"
            )
        else:
            self.notify_status(f"{VIEW_LABELS[self.view_mode]} - {shown}")

    def action_rating_filter(self) -> None:
        if self.favorites is None:
            self.notify_status("no favourites database")
            return
        current = self.rating_filter or RatingFilter(stars=0, at_least=False)

        def apply(value: RatingFilter | None) -> None:
            if value is None:
                return
            if value.stars == 0 and value.at_least:
                self.rating_filter = None
                self.notify_status("rating filter cleared")
            else:
                self.rating_filter = value
                if value.at_least:
                    self.notify_status(f"rating filter: at least {value.stars} star(s)")
                else:
                    self.notify_status(f"rating filter: exactly {value.stars} star(s)")
            self._refill_keeping_place()

        self.push_screen(RatingFilterScreen(current), apply)

    def _refill_keeping_place(self) -> None:
        if self._current_physical is not None:
            kind = self._current_physical_kind
            if kind is None:
                kind = self._current_pair[1]
            self._fill_physical(self._current_physical, kind)
            return
        if self._current_multi is not None:
            self._fill_multiset(self._current_multi)
            return
        table = self.query_one("#slot-table", DataTable)
        row = table.cursor_row or 0
        self._fill(self._current_pair, self._current_card)
        if self._current:
            table.move_cursor(row=min(row, len(self._current) - 1))

    def _card_slot_name(self, card: int, kind: str, number: int) -> str:
        captured = self._archive.get(card)
        if captured is None:
            return ""
        return captured.names(kind).get(number, "")

    def _slot_shown_name(self, item: banks.Slot, card: Optional[int]) -> str:
        if card is not None:
            name = self._card_slot_name(card, item.kind, item.number)
            if name:
                return f"[b]{name}[/b]"
            if card not in self._archive:
                return "[dim]not captured[/dim]"
            return "[dim](unnamed)[/dim]"
        name = self.catalog.display_name(item.bank.id, item.number, item.kind)
        if self.catalog.differs(item.bank.id, item.number, item.kind):
            return f"[b]{name}[/b] [dim]*[/dim]"
        if self.catalog.is_live(item.bank.id, item.number, item.kind):
            return f"[b]{name}[/b]"
        return name

    def _ensure_slot_columns(self, mode: str) -> None:
        if mode == self._slot_mode:
            return
        table = self.query_one("#slot-table", DataTable)
        table.clear(columns=True)
        if mode == "slots":
            for label, key in (("♥", "fav"), ("#", "num"), ("name", "name")):
                table.add_column(label, key=key)
        else:
            for label, key in (
                ("ch", "ch"),
                ("perf", "perf"),
                ("name", "name"),
                ("level", "level"),
                ("used", "on"),
            ):
                table.add_column(label, key=key)
        self._slot_mode = mode

    def _fill(self, pair: Tuple[int, str], card: Optional[int] = None) -> None:
        self._current_multi = None
        self._current_physical = None
        self._current_physical_kind = None
        self._ensure_slot_columns("slots")
        table = self.query_one("#slot-table", DataTable)
        self._current_pair = pair
        self._current_card = card
        self._current = self._visible(pair)
        self._filling = True
        try:
            table.clear()
            for item in self._current:
                shown = self._slot_shown_name(item, card)
                marked = self._favorite_keys(item.bank.id, item.kind)
                where = f"Card {card:02d}" if card is not None else item.bank.label
                label = (
                    f"{where} {item.kind[:4]} {item.number:02d}"
                    if self.view_mode == VIEW_ALL_FAVOURITES
                    else f"{item.number:02d}"
                )
                table.add_row(
                    "[b]♥[/b]" if item.number in marked else "",
                    label,
                    shown,
                    key=item.key,
                )
        finally:
            self._filling = False
        self._detail(0)

    def _fill_multiset(self, number: int) -> None:
        self._current_multi = number
        self._current_card = None
        self._current_physical = None
        self._current_physical_kind = None
        self._current = []
        self._ensure_slot_columns("multi")
        table = self.query_one("#slot-table", DataTable)
        table.clear()
        multisets = getattr(self, "_multisets", None)
        if multisets is None:
            multisets = getattr(self, "_multi_sets", []) or []
        if 0 <= number < len(multisets):
            ms = multisets[number]
            for part in ms.parts:
                if part.used:
                    perf = f"{_bank_label(part.bank)} {part.performance:02d}"
                    try:
                        bank_entry = banks.by_sysex(part.bank)
                        name = self.catalog.display_name(
                            bank_entry.id, part.performance, banks.Kind.PERFORMANCE
                        )
                    except Exception:
                        name = ""
                    used = "used"
                else:
                    perf = "--"
                    name = ""
                    used = "--"
                table.add_row(
                    f"{part.channel:02d}",
                    perf,
                    name,
                    str(part.level),
                    used,
                    key=f"multi:{number:02d}:{part.channel:02d}",
                )
        else:
            for i in range(16):
                table.add_row(
                    f"{i + 1:02d}",
                    "--",
                    "[dim]press m to read[/dim]" if i == 0 else "",
                    "--",
                    "--",
                    key=f"multi:{number:02d}:{i + 1:02d}",
                )
        ms_name = ""
        if 0 <= number < len(multisets):
            ms_name = getattr(multisets[number], "name", "") or ""
        with contextlib.suppress(NoMatches):
            self.query_one("#detail", Static).update(
                f"[b]Multi {number:02d} {ms_name}[/b]\n"
                "[dim]m reads the setup and e edits it[/dim]"
            )

    def _fill_physical(self, card_id: str, kind: str) -> None:
        self._current_physical = card_id
        self._current_physical_kind = kind
        self._current_card = None
        self._current_multi = None
        self._ensure_slot_columns("slots")
        table = self.query_one("#slot-table", DataTable)
        self._filling = True
        try:
            table.clear()
            if card_id in ("", None) or card_id not in self._physical:
                table.add_row(
                    "",
                    "00",
                    "[dim]insert a card and press C[/dim]",
                    key="physical:none:000",
                )
                self._current = []
            else:
                card = self._physical[card_id]
                if kind == banks.Kind.PERFORMANCE:
                    names = card.performances
                else:
                    names = card.patches
                # Actually kind may be "performance"/"patch" strings? Check Kind values.
                if kind not in (banks.Kind.PERFORMANCE, banks.Kind.PATCH):
                    # normalize
                    pass
                scoped = _physical.scope_id(card_id)
                marked = self._favorite_keys(scoped, kind)
                count = banks.count_for(kind)
                self._current = banks.slots("CARD", kind) if card_id else []
                # banks.slots needs bank number; CARD bank
                try:
                    self._current = banks.slots(banks.bank("CARD").number, kind)
                except Exception:
                    self._current = []
                for n in range(count):
                    name = names.get(n, "")
                    shown = f"[b]{name}[/b]" if name else "[dim](unnamed)[/dim]"
                    table.add_row(
                        "[b]♥[/b]" if n in marked else "",
                        f"{n:02d}",
                        shown,
                        key=f"physical:{card_id}:{kind}:{n:03d}",
                    )
        finally:
            self._filling = False
        # Detail
        if card_id in ("", None) or card_id not in self._physical:
            with contextlib.suppress(NoMatches):
                self.query_one("#detail", Static).update(
                    "[dim]insert a card and press C[/dim]"
                )
            return
        card = self._physical[card_id]
        active_marker = " • active" if self._active_physical == card_id else ""
        verified = "verified" if card.verified else "unverified"
        digest_prefix = card.digest[:12]
        with contextlib.suppress(NoMatches):
            self.query_one("#detail", Static).update(
                f"[b]{card.label or card.id}[/b]{active_marker}\n"
                f"[dim]{verified} · {digest_prefix} · "
                f"C captures the inserted card; L labels this one[/dim]"
            )

    def _detail(self, row: int) -> None:  # noqa: C901
        if self._current_physical is not None:
            if (
                self._current_physical in ("", None)
                or self._current_physical not in self._physical
            ):
                with contextlib.suppress(NoMatches):
                    self.query_one("#detail", Static).update(
                        "[dim]insert a card and press C[/dim]"
                    )
                return
            card = self._physical[self._current_physical]
            if row is None or row >= len(self._current):
                with contextlib.suppress(NoMatches):
                    self.query_one("#detail", Static).update(
                        f"[b]{card.label or card.id}[/b]\n"
                        f"[dim]C captures the inserted card; L labels this one[/dim]"
                    )
                return
            item = self._current[row]
            name = ""
            if self._current_physical_kind == banks.Kind.PERFORMANCE:
                name = card.performances.get(item.number, "")
            else:
                name = card.patches.get(item.number, "")
            shown = f"[b]{name}[/b]" if name else "[dim](unnamed)[/dim]"
            with contextlib.suppress(NoMatches):
                self.query_one("#detail", Static).update(
                    f"[b]{card.label or card.id} {item.kind} {item.number:02d}[/b]  "
                    f"{shown}\n"
                    f"[dim]C captures the inserted card; L labels this one[/dim]"
                )
            return
        if self._current_multi is not None:
            multisets = getattr(self, "_multisets", None)
            if multisets is None:
                multisets = getattr(self, "_multi_sets", []) or []
            number = self._current_multi
            if not (0 <= number < len(multisets)):
                return
            ms = multisets[number]
            parts = getattr(ms, "parts", ())
            if row is None or row >= len(parts):
                return
            part = parts[row]
            try:
                bank_entry = banks.by_sysex(part.bank)
                name = self.catalog.display_name(
                    bank_entry.id, part.performance, banks.Kind.PERFORMANCE
                )
            except Exception:
                name = ""
            perf = (
                f"{_bank_label(part.bank)} {part.performance:02d}"
                if part.used
                else "--"
            )
            with contextlib.suppress(NoMatches):
                self.query_one("#detail", Static).update(
                    f"[b]Multi {number:02d} {getattr(ms, 'name', '') or ''}[/b]  "
                    f"ch {part.channel}  {perf}  {name}\n"
                    f"[dim]level {part.level} · "
                    f"{'used' if part.used else 'unused'}[/dim]"
                )
            return
        if not self._current or row >= len(self._current):
            with contextlib.suppress(NoMatches):
                self.query_one("#detail", Static).update("")
            return
        item = self._current[row]
        if self._current_card is not None:
            self._detail_card(item)
            return
        name = self.catalog.display_name(item.bank.id, item.number, item.kind)
        note = ""
        if self.catalog.differs(item.bank.id, item.number, item.kind):
            # The two meanings of a difference, split the only way that is
            # true: by writability. A writable bank holds somebody's own
            # work; a ROM one cannot change, so there the catalog is what
            # is wrong.
            note = "   [dim]" + cat.disagreement_words(item.bank.writable) + "[/dim]"
        elif not item.bank.writable:
            note = "   [dim]ROM — read only[/dim]"
        # A RowHighlighted event can still be queued when the app is torn
        # down, after the default screen has gone. Updating a widget that
        # is no longer there is not a reason to fail the session.
        with contextlib.suppress(NoMatches):
            self.query_one("#detail", Static).update(
                f"[b]{item.bank.label} {item.kind} {item.number:02d}[/b]  "
                f"{name}{note}\n"
                f"[dim]Bank Select LSB[/dim] [b]{item.bank.midi_bank}[/b]   "
                f"[dim]Program Change[/dim] "
                f"[b]{item.bank.program_base + item.number}[/b]"
            )

    def _detail_card(self, item: banks.Slot) -> None:
        """The detail line for one HCard sub-bank."""
        number = self._current_card
        if number is None:  # pragma: no cover - the caller checked
            return
        entry = _cards.card(number)
        captured = self._archive.get(number)
        name = self._card_slot_name(number, item.kind, item.number)
        status = _cards.card_words(captured is not None, entry.needs_pcm)
        if captured is None:
            status += " — press C to capture it"
            shown = name or "[dim](no archive)[/dim]"
        else:
            status = f"captured {captured.captured_at[:16].replace('T', ' ')}"
            if entry.needs_pcm is not None:
                status += f" · needs {entry.needs_pcm}"
            if self._active_card == number:
                status += " · active"
            if captured.verified is False:
                status += " · unverified"
            if not (captured.label or "").strip() and not (captured.name or "").strip():
                # The card name is unreadable, so an unlabelled bank would
                # otherwise stay "(unnamed)" with no way to fix it.
                status += " · L to label"
            shown = name or "[dim](unnamed)[/dim]"
        with contextlib.suppress(NoMatches):
            self.query_one("#detail", Static).update(
                f"[b]Card {number:02d} {item.kind} {item.number:02d}[/b]  "
                f"{shown}\n"
                f"{_cards.bank_title(captured, entry)}   [dim]{status}[/dim]"
            )

    def notify_status(self, message: str, *, refused: bool = False) -> None:
        prefix = "[b]refused[/b] · " if refused else ""
        self.query_one("#status", Static).update(prefix + message)

    # --- navigation ---------------------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if self._filling:
            return
        if event.data_table.id == "bank-table":
            row = event.cursor_row
            if 0 <= row < len(self._row_multis):
                physical = (
                    self._row_physical[row] if row < len(self._row_physical) else None
                )
                multi = self._row_multis[row]
                if physical is not None:
                    self._fill_physical(physical, self._pairs[row][1])
                elif multi is not None:
                    self._fill_multiset(multi)
                else:
                    self._fill(self._pairs[row], self._row_cards[row])
        elif event.data_table.id == "slot-table":
            self._detail(event.cursor_row)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a slot. It arrives here, not as a binding."""
        if event.data_table.id == "slot-table":
            self.action_select()
            return
        if event.data_table.id == "bank-table":
            row = event.cursor_row
            if 0 <= row < len(self._row_multis):
                multi = self._row_multis[row]
                if multi is not None:
                    self.action_select_multi(multi)

    def action_switch_pane(self) -> None:
        left = self.query_one("#bank-table", DataTable)
        right = self.query_one("#slot-table", DataTable)
        (right if left.has_focus else left).focus()

    # --- operations ---------------------------------------------------------

    def action_select(self) -> None:  # noqa: C901
        table = self.query_one("#slot-table", DataTable)
        row = table.cursor_row
        if not 0 <= row < len(self._current):
            return
        entry = self._current[row]
        # The SR has no patch mode: a Program Change selects a Performance,
        # never a patch -- cf8be55 made the bridge refuse below, and refusing
        # here, before the busy/exchange dance, keeps that friendly instead
        # of a raised error from the worker. This check runs for every row,
        # including card sub-bank rows, so a card PATCH row is refused as a
        # patch rather than silently allowed.
        if entry.kind == banks.Kind.PATCH:
            self.notify_status(
                "a patch is not independently playable on the SR -- it is "
                "heard through a Performance that uses it; select a "
                "Performance instead",
                refused=True,
            )
            return
        if self._current_physical is not None:
            if (
                self._current_physical in ("", None)
                or self._current_physical not in self._physical
            ):
                self.notify_status(
                    "select a captured card first; C captures the inserted one",
                    refused=True,
                )
                return
            if self._current_physical != self._active_physical:
                self.notify_status(
                    "that card is not the one in the slot; insert it and "
                    "press C to activate",
                    refused=True,
                )
                return
        if self._current_card is not None:
            if self._active_card is None:
                self.notify_status(
                    "the app has not confirmed which HCard bank is active; "
                    f"select Card {self._current_card:02d} on the HCard and "
                    "press C once, then select again",
                    refused=True,
                )
                return
            if self._current_card != self._active_card:
                self.notify_status(
                    f"the HCard was on Card {self._active_card:02d} at the "
                    f"last capture; select Card {self._current_card:02d} on "
                    f"the HCard first, or press C to re-check",
                    refused=True,
                )
                return
        if self._busy:
            self.notify_status("busy", refused=True)
            return
        if not self._exchange.acquire(blocking=False):
            self.notify_status("busy", refused=True)
            return
        self._select_worker(entry, card=self._current_card)

    @work(thread=True)
    def _select_worker(self, entry: banks.Slot, card: Optional[int] = None) -> None:
        """**MIDI only.**"""
        failure: BaseException | None = None
        try:
            with self._busy_gate, self._lock:
                self.device.select(entry)
        except _BRIDGE_ERRORS as exc:
            failure = exc
        finally:
            self._exchange.release()
        if failure is not None:
            self.call_from_thread(
                self.notify_status, f"select: {failure}", refused=True
            )
            return
        if card is not None:
            self.call_from_thread(
                self.notify_status,
                f"selected Card {card:02d} from the HCard bank active on "
                f"the unit (active HCard bank) — slot {entry.number:02d}",
            )
            return
        self.call_from_thread(
            self.notify_status,
            f"bank select LSB {entry.bank.midi_bank} + program change "
            f"{entry.bank.program_base + entry.number} sent for {entry}",
        )

    def action_read_names(self) -> None:
        if self._current_physical is not None:
            self.notify_status(
                "press C to capture the card",
                refused=True,
            )
            return
        if self._current_card is not None:
            self.notify_status(
                "this is one of the Card's forty banks -- select it on the "
                "HCard and press C to capture it",
                refused=True,
            )
            return
        if self._current_multi is not None:
            self.notify_status(
                "a Multiset has no name list to read -- press m to read the setup",
                refused=True,
            )
            return
        if self._busy:
            self.notify_status("busy", refused=True)
            return
        if not self._exchange.acquire(blocking=False):
            self.notify_status("busy", refused=True)
            return
        self._read_worker(*self._current_pair)

    def _auto_capture_card(self) -> None:
        """Startup probe: file or confirm the active card bank.

        The card is switched physically and the synth cannot report the
        bank, so a read at launch is the only way to know which one is in
        front of it. Silent and read-only; skipped while the scan offer is
        up or the browser is busy, and the user can always press C.
        """
        if self._cards_path is None or self._busy or self._scanning:
            return
        if not self._archive:
            # Nothing captured yet, so there is no active bank to confirm.
            # Probing anyway would make a missing card cost the full dump
            # timeout at every launch; C is the explicit first capture.
            return
        if isinstance(self.screen, _scan.ScanOfferScreen):
            return
        self.action_capture_card(startup=True)

    def action_capture_card(self, startup: bool = False) -> None:
        """Capture the inserted card.

        In ``cards`` mode the inserted card is read, identified by content
        digest, and filed in the physical archive. In ``hcard`` mode the
        active HCard bank is captured as before. ``none`` refuses.

        ``startup`` is the automatic launch probe.
        """
        if self._card_mode == "none":
            self.notify_status("card support is off (card_mode = none)", refused=True)
            return
        if self._card_mode == "cards":
            if self._busy:
                self.notify_status("busy", refused=True)
                return
            if not self._exchange.acquire(blocking=False):
                self.notify_status("busy", refused=True)
                return
            self.notify_status("reading the inserted card…")
            self._physical_capture_worker(startup=startup)
            return
        if self._card_sweeping:
            self.notify_status("a card sweep is running", refused=True)
            return
        if self._busy:
            self.notify_status("busy", refused=True)
            return
        if not self._exchange.acquire(blocking=False):
            self.notify_status("busy", refused=True)
            return
        self.notify_status(
            "capturing the Card bank — reading performances and patches "
            "(a few seconds)…"
        )
        self._capture_card_worker(self._current_card, startup=startup)

    def action_label_card(self) -> None:  # noqa: C901
        """Set a local label on the captured card under the cursor.

        Labels are archive metadata only -- nothing is written to the card
        or to any slot. **No MIDI.**
        """
        if self._card_mode == "none":
            self.notify_status("card support is off (card_mode = none)", refused=True)
            return
        if self._card_mode == "cards":
            if (
                self._current_physical in (None, "")
                or self._current_physical not in self._physical
            ):
                self.notify_status(
                    "select a captured card first; C captures the inserted one",
                    refused=True,
                )
                return
            card_id = self._current_physical
            card = self._physical[card_id]
            current_label = card.label

            def apply_label(value: Optional[str]) -> None:
                if value is None:
                    return
                try:
                    updated = _physical.set_label(card_id, value, self._physical_path)
                except LookupError as exc:
                    self.notify_status(str(exc), refused=True)
                    return
                self._physical = (
                    _physical.load(self._physical_path)
                    if self._physical_path
                    else self._physical
                )
                self._build_row_model()
                self._apply_row_model()
                kind = self._current_physical_kind or banks.Kind.PERFORMANCE
                self._fill_physical(card_id, kind)
                self.notify_status(
                    f"labelled Card {card_id}: {updated.label or card_id}"
                )

            self.push_screen(PromptScreen("Card label:", current_label), apply_label)
            return
        if self._current_card is None:
            self.notify_status(
                "no card row selected; capture a bank first with C",
                refused=True,
            )
            return
        number = self._current_card
        captured = self._archive.get(number)
        if captured is None:
            self.notify_status(
                f"Card {number:02d} is not captured yet; capture it first with C",
                refused=True,
            )
            return

        def apply(value: Optional[str]) -> None:
            if value is None:
                return
            try:
                updated = _cards.set_label(number, value, self._cards_path)
            except LookupError as exc:
                self.notify_status(str(exc), refused=True)
                return
            self._archive[number] = updated
            self._refresh_card_row(number)
            if self._current_card == number:
                self._fill(self._current_pair, number)
            title = _cards.bank_title(updated, _cards.card(number))
            self.notify_status(f"labelled Card {number:02d}: {title}")

        self.push_screen(PromptScreen("Card bank name:", captured.label), apply)

    def action_card_sweep(self) -> None:
        if self._card_mode != "hcard":
            self.notify_status(
                "card sweep is only available in hcard mode", refused=True
            )
            return
        """Walk the HCard's forty banks, prompting for each selection.

        The card cannot be switched remotely, so between banks this stops
        and asks the person to select the bank physically. Enter reads and
        files it; Escape stops cleanly between banks. **Read-only** on the
        unit throughout.
        """
        if self._card_sweeping:
            self.notify_status("a card sweep is running", refused=True)
            return
        if self._busy:
            self.notify_status("busy", refused=True)
            return
        if self._scanning:
            self.notify_status("a scan is running", refused=True)
            return
        if not self._exchange.acquire(blocking=False):
            self.notify_status("busy", refused=True)
            return
        self._card_sweeping = True
        self.notify_status("card sweep: select bank 00 on the HCard")
        self._card_sweep_next(0, 0)

    def _card_sweep_next(self, index: int, filed: int) -> None:
        if index >= len(_cards.CARDS):
            self._card_sweep_finish(filed, len(_cards.CARDS))
            return
        entry = _cards.card(index)
        self.push_screen(
            _scan.CardSweepScreen(index, entry.name, entry.needs_pcm),
            lambda proceed: self._card_sweep_answered(index, filed, proceed),
        )

    def _card_sweep_answered(
        self, index: int, filed: int, proceed: Optional[bool]
    ) -> None:
        if not proceed:
            self._card_sweep_finish(filed, index)
            return
        self.notify_status(
            "capturing the Card bank — reading performances and patches "
            "(a few seconds)…"
        )
        self._capture_card_worker(index, next_index=index + 1, filed=filed, sweep=True)

    def _card_sweep_finish(self, filed: int, stopped_at: int) -> None:
        self._card_sweeping = False
        if self._exchange.locked():
            try:
                self._exchange.release()
            except RuntimeError:  # pragma: no cover - already released
                pass
        if stopped_at >= len(_cards.CARDS):
            self.notify_status(f"card sweep finished: filed {filed} of 40 banks")
        else:
            self.notify_status(
                f"card sweep stopped between banks: filed {filed}, "
                f"stopped before bank {stopped_at:02d}"
            )

    @work(thread=True)
    def _capture_card_worker(
        self,
        expected: Optional[int],
        *,
        next_index: Optional[int] = None,
        filed: int = 0,
        sweep: bool = False,
        startup: bool = False,
    ) -> None:
        """**MIDI only** -- the archive is updated on the main thread."""
        failure: BaseException | None = None
        payload: Any = None
        try:
            with self._busy_gate, self._lock:
                reader = getattr(self.device, "read_card_name", None)
                name = reader() if callable(reader) else None
                perf_dump = self.device.read_bank("CARD", banks.Kind.PERFORMANCE)
                patch_dump = self.device.read_bank("CARD", banks.Kind.PATCH)
                performances = perf_dump.names()
                patches = patch_dump.names()
                digest = _cards.content_digest(perf_dump.records, patch_dump.records)
            payload = (name, performances, patches, digest)
        except _BRIDGE_ERRORS as exc:
            failure = exc
        finally:
            # A sweep holds the exchange across the whole walk; a single
            # capture owns and releases it here.
            if not sweep:
                self._exchange.release()
        if failure is not None:
            if startup and isinstance(failure, TimeoutError):
                # A launch probe that finds no card is normal, not a fault:
                # the Card bank only answers when a card is in the slot.
                self.call_from_thread(
                    self.notify_status,
                    "no card answered at startup — the card slot may be "
                    "empty, or the HCard is not seated. Insert it and press "
                    "C to capture the active bank.",
                )
            else:
                self.call_from_thread(
                    self.notify_status, f"capture: {failure}", refused=True
                )
            if next_index is not None:
                self.call_from_thread(self._card_sweep_next, next_index, filed)
            return
        self.call_from_thread(
            self._file_capture, payload, expected, next_index, filed, startup
        )

    def _file_capture(  # noqa: C901
        self,
        payload: Any,
        expected: Optional[int],
        next_index: Optional[int] = None,
        filed: int = 0,
        startup: bool = False,
    ) -> bool:
        name, performances, patches, digest = payload
        by_digest = _cards.resolve_by_digest(digest, self._archive)
        resolved = (
            by_digest
            if by_digest is not None
            else _cards.resolve(
                name,
                performances.get(0),
                patches.get(0),
                captured=self._archive,
                digest=digest,
            )
        )
        expected_given = expected is not None
        evidence_bank: Optional[int] = by_digest if by_digest is not None else resolved

        verified = False
        number: Optional[int] = None
        message_suffix = ""
        if expected_given:
            if evidence_bank is not None and evidence_bank != expected:
                # different bank
                number = evidence_bank
                verified = True
                message_suffix = (
                    f" — the card is Card {number:02d}, not "
                    f"{expected:02d} — filed under {number:02d}. "
                    f"Switch the HCard to {expected:02d} if that is the "
                    f"bank you meant."
                )
            elif evidence_bank == expected:
                number = expected
                verified = True
                message_suffix = " — confirmed"
            else:
                # no evidence
                number = expected
                verified = False
                message_suffix = (
                    " — unverified; select that bank on the HCard and "
                    "capture again (filed as bank "
                    f"{expected:02d} on your say-so)"
                )
        elif evidence_bank is not None:
            number = evidence_bank
            verified = by_digest is not None
            if verified:
                message_suffix = " — confirmed"
        else:
            number = None

        if number is None:
            if startup:
                self.notify_status(
                    "the active card bank is not in the archive yet; select "
                    "it on the HCard and press C to file it"
                )
            else:
                self.notify_status(
                    "capture: could not tell which Card bank this is. Select "
                    "its row and capture again to file it there.",
                    refused=True,
                )
            if next_index is not None:
                self._card_sweep_next(next_index, filed)
            return False

        existing = self._archive.get(number)
        captured = _cards.capture(
            number,
            name or "",
            performances,
            patches,
            label=existing.label if existing is not None else "",
            digest=digest,
            verified=verified,
        )
        previous_active = self._active_card
        self._archive[number] = captured
        self._active_card = number
        if self._cards_path is not None:
            _cards.save(captured, self._cards_path)
            _cards.set_active(number, self._cards_path)
        # Only two rows change: the captured bank gains the "•" marker, and
        # the bank that was active becomes inactive. Everyone else's grey
        # state is untouched.
        self._refresh_card_row(number)
        if previous_active is not None and previous_active != number:
            self._refresh_card_row(previous_active)
        if self._current_card == number:
            self._fill(self._current_pair, number)
        message = (
            f"captured Card {number:02d} {captured.name!r}: "
            f"{len(performances)} performances, {len(patches)} patches"
        )
        if not name:
            message += (
                " (no card name read; the card name is not readable over "
                "MIDI, press L to label it)"
            )
        message += message_suffix
        self.notify_status(message)
        if next_index is not None:
            self._card_sweep_next(next_index, filed + 1)
        return True

    @work(thread=True)
    def _physical_capture_worker(self, startup: bool = False) -> None:
        """**MIDI only** -- the archive is updated on the main thread."""
        failure: BaseException | None = None
        payload: Any = None
        try:
            with self._busy_gate, self._lock:
                perf_dump = self.device.read_bank("CARD", banks.Kind.PERFORMANCE)
                patch_dump = self.device.read_bank("CARD", banks.Kind.PATCH)
                perf_names = perf_dump.names()
                patch_names = patch_dump.names()
                digest = _cards.content_digest(perf_dump.records, patch_dump.records)
            payload = (perf_names, patch_names, digest)
        except _BRIDGE_ERRORS as exc:
            failure = exc
        finally:
            if self._exchange.locked():
                try:
                    self._exchange.release()
                except RuntimeError:
                    pass
        if failure is not None:
            self.call_from_thread(
                self.notify_status, f"capture: {failure}", refused=True
            )
            return
        self.call_from_thread(self._file_physical_capture, payload, startup)

    def _file_physical_capture(self, payload: Any, startup: bool = False) -> None:
        perf_names, patch_names, digest = payload
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        self._physical = (
            _physical.load(self._physical_path)
            if self._physical_path
            else self._physical
        )
        match = _physical.find_by_digest(digest, self._physical)
        if match is not None:
            card = self._physical[match]
            updated = _physical.PhysicalCard(
                id=card.id,
                label=card.label,
                digest=digest,
                captured_at=now,
                performances=dict(perf_names),
                patches=dict(patch_names),
                verified=True,
            )
            # save
            _physical.save(updated, self._physical_path)
            self._physical = (
                _physical.load(self._physical_path)
                if self._physical_path
                else self._physical
            )
            # set active
            _physical.set_active(updated.id, self._physical_path)
            self._active_physical = updated.id
            self._physical = (
                _physical.load(self._physical_path)
                if self._physical_path
                else self._physical
            )
            self._build_row_model()
            self._apply_row_model()
            self._fill_physical(updated.id, banks.Kind.PERFORMANCE)
            self.notify_status(
                f"captured Card {updated.label or updated.id} (confirmed)"
            )
            return

        # no match
        if not self._physical:
            # empty archive -> prompt label
            def on_label(value: Optional[str]) -> None:
                if not value:
                    self.notify_status("capture aborted", refused=True)
                    return
                cid = _physical.next_id(self._physical)
                card = _physical.PhysicalCard(
                    id=cid,
                    label=value.strip(),
                    digest=digest,
                    captured_at=now,
                    performances=dict(perf_names),
                    patches=dict(patch_names),
                    verified=False,
                )
                _physical.save(card, self._physical_path)
                self._physical = (
                    _physical.load(self._physical_path)
                    if self._physical_path
                    else self._physical
                )
                _physical.set_active(card.id, self._physical_path)
                self._active_physical = card.id
                self._physical = (
                    _physical.load(self._physical_path)
                    if self._physical_path
                    else self._physical
                )
                self._build_row_model()
                self._apply_row_model()
                self._fill_physical(card.id, banks.Kind.PERFORMANCE)
                self.notify_status(f"new card {card.label or card.id}")

            self.push_screen(
                PromptScreen(
                    "Name this card:",
                    f"Card {_physical.next_id(self._physical)}",
                ),
                on_label,
            )
            return

        # archive has cards -> ChoiceScreen
        labels = [c.label or c.id for c in self._physical.values()]
        choices = labels + ["New card"]

        def on_choice(index: Optional[int]) -> None:
            if index is None:
                self.notify_status("capture aborted", refused=True)
                return
            if index == len(choices) - 1:
                # New card -> label prompt
                def on_label(value: Optional[str]) -> None:
                    if not value:
                        self.notify_status("capture aborted", refused=True)
                        return
                    cid = _physical.next_id(self._physical)
                    card = _physical.PhysicalCard(
                        id=cid,
                        label=value.strip(),
                        digest=digest,
                        captured_at=now,
                        performances=dict(perf_names),
                        patches=dict(patch_names),
                        verified=False,
                    )
                    _physical.save(card, self._physical_path)
                    self._physical = (
                        _physical.load(self._physical_path)
                        if self._physical_path
                        else self._physical
                    )
                    _physical.set_active(card.id, self._physical_path)
                    self._active_physical = card.id
                    self._physical = (
                        _physical.load(self._physical_path)
                        if self._physical_path
                        else self._physical
                    )
                    self._build_row_model()
                    self._apply_row_model()
                    self._fill_physical(card.id, banks.Kind.PERFORMANCE)
                    self.notify_status(f"new card {card.label or card.id}")

                self.push_screen(
                    PromptScreen(
                        "Name this card:",
                        f"Card {_physical.next_id(self._physical)}",
                    ),
                    on_label,
                )
                return
            # adopted existing card
            target_id = list(self._physical.keys())[index]
            card = self._physical[target_id]
            updated = _physical.PhysicalCard(
                id=card.id,
                label=card.label,
                digest=digest,
                captured_at=now,
                performances=dict(perf_names),
                patches=dict(patch_names),
                verified=False,
            )
            _physical.save(updated, self._physical_path)
            self._physical = (
                _physical.load(self._physical_path)
                if self._physical_path
                else self._physical
            )
            _physical.set_active(updated.id, self._physical_path)
            self._active_physical = updated.id
            self._physical = (
                _physical.load(self._physical_path)
                if self._physical_path
                else self._physical
            )
            self._build_row_model()
            self._apply_row_model()
            self._fill_physical(updated.id, banks.Kind.PERFORMANCE)
            self.notify_status(
                f"captured Card {updated.label or updated.id} (unverified — adopted)"
            )

        self.push_screen(
            ChoiceScreen("This card is not in the archive", choices), on_choice
        )

    def _refresh_card_row(self, number: int) -> None:
        """Rewrite the bank and name cells of one card row.

        Both depend on which bank is active (the label is greyed when it is
        not), so a capture that changes the active bank refreshes every
        card row, not just its own.
        """
        table = self.query_one("#bank-table", DataTable)
        card_number = banks.bank("CARD").number
        for kind in (banks.Kind.PERFORMANCE, banks.Kind.PATCH):
            key = self._row_key((card_number, kind), number)
            try:
                cells = self._bank_row((card_number, kind), number)
                table.update_cell(key, "bank", cells[0])
                table.update_cell(key, "name", self._card_row_name(number))
            except Exception:  # noqa: S110 - the row may be filtered away
                pass

    @work(thread=True)
    def _read_worker(self, bank_id: int, kind: str) -> None:
        """**MIDI only** -- the catalog is updated on the main thread."""
        failure: BaseException | None = None
        result_kind = kind
        result_names: Any = None
        try:
            self.call_from_thread(
                self.notify_status,
                f"asking for bank {bank_id} {kind}s in one message (silent)...",
            )
            with self._busy_gate, self._lock:
                result_kind, result_names = self.device.read_bank_names(bank_id, kind)
        except _BRIDGE_ERRORS as exc:
            failure = exc
        finally:
            self._exchange.release()
        if failure is not None:
            self.call_from_thread(self.notify_status, f"read: {failure}", refused=True)
            return
        self.call_from_thread(self._adopt, bank_id, result_kind, result_names)

    def _adopt(self, bank_id: int, kind: str, names: Dict[int, str]) -> None:
        entry = banks.bank(bank_id)
        for number, name in names.items():
            self.catalog.set_live_name(entry.id, number, name, kind)
        differing = sum(1 for n in names if self.catalog.differs(entry.id, n, kind))
        if self._current_pair == (entry.number, kind):
            self._fill(self._current_pair, self._current_card)
        if self._live_path is not None:
            flat = {
                _livenames.key(entry.id, number, kind): name
                for number, name in names.items()
            }
            # Merge rather than replace: other banks' names must survive.
            _livenames.save(flat, self._live_path)
        message = f"read {len(names)} {kind} names from {entry.label}"
        if differing:
            # Not "differ from the catalog" flat: on a writable bank the
            # unit's names are somebody's work worth its own line, and on
            # a ROM one the catalog is the thing that is wrong.
            message += f"; {differing} differ: " + cat.disagreement_words(
                entry.writable
            )
        self.notify_status(message)

    def action_scan_device(self) -> None:
        if self._scanning:
            self._scan_cancel.set()
            self.notify_status("stopping the scan after the current bank", refused=True)
            return
        if self._busy:
            self.notify_status("busy", refused=True)
            return
        if self._live_path is None:
            self.notify_status(
                "this is the demo -- it invents its names, and scanning "
                "it would write fiction into the live cache. Nothing has "
                "been sent.",
                refused=True,
            )
            return
        if not self._exchange.acquire(blocking=False):
            self.notify_status("busy", refused=True)
            return
        self._scan_cancel.clear()
        self._scanning = True
        self.push_screen(_scan.ScanScreen(self._scan_cancel))
        self._scan_worker()

    @work(thread=True)
    def _scan_worker(self) -> None:
        done = 0
        total = len(self._real_pairs)
        try:
            for number, kind in self._real_pairs:
                if self._scan_cancel.is_set():
                    break
                try:
                    with self._busy_gate, self._lock:
                        result_kind, names = self.device.read_bank_names(number, kind)
                except Exception as exc:
                    self.call_from_thread(
                        self.notify_status, f"scan {number}: {exc}", refused=True
                    )
                    continue
                self.call_from_thread(self._adopt, number, result_kind, names)
                self.call_from_thread(
                    self._scan_progress, f"scanned {banks.bank(number).id} {kind}"
                )
                done += 1
        finally:
            self._exchange.release()
            self.call_from_thread(self._scan_done, done, total)

    def _scan_progress(self, line: str) -> None:
        screen = self.screen
        if screen and isinstance(screen, _scan.ScanScreen):
            screen.set_progress(line)

    def _scan_done(self, done: int, total: int) -> None:
        self._scanning = False
        screen = self.screen
        if screen and isinstance(screen, _scan.ScanScreen):
            self.pop_screen()
        if done:
            self.notify_status(f"scanned {done} of {total} bank(s)")
        else:
            self.notify_status("scan stopped")

    def _has_cached_names(self) -> bool:
        return any(
            self.catalog.is_live(banks.bank(n).id, slot.number, k)
            for n, k in self._real_pairs
            for slot in banks.slots(n, k)
        )

    def _maybe_offer_scan(self) -> None:
        if (
            self._live_path is None
            or bool(self.catalog)
            or self._has_cached_names()
            or self._busy
            or self._scanning
            or self._scan_offer_dismissed
        ):
            return
        self.push_screen(
            _scan.ScanOfferScreen(_scan.SCAN_OFFER), self._scan_offer_answered
        )

    def _scan_offer_answered(self, answer: str | None) -> None:
        if answer == "never":
            self._scan_offer_dismissed = True
            if self._config_path is not None:
                from kws import config as _cfg

                _cfg.save_scan_offer_dismissed(True, self._config_path)
            return
        if answer == "scan":
            if self._busy:
                self.notify_status("busy", refused=True)
                return
            self.action_scan_device()

    def action_search(self) -> None:
        def done(text: Optional[str]) -> None:
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
        if not self._exchange.acquire(blocking=False):
            self.notify_status("busy", refused=True)
            return
        self._multi_worker()

    def action_select_multi(self, number: int) -> None:
        """Make a Multiset current on the unit (Multi mode)."""
        if self._busy:
            self.notify_status("busy", refused=True)
            return
        if not self._exchange.acquire(blocking=False):
            self.notify_status("busy", refused=True)
            return
        self._multi_select_worker(number)

    @work(thread=True)
    def _multi_select_worker(self, number: int) -> None:
        failure: BaseException | None = None
        try:
            with self._busy_gate, self._lock:
                self.device.select_multi(number)
        except _BRIDGE_ERRORS as exc:
            failure = exc
        finally:
            self._exchange.release()
        if failure is not None:
            self.call_from_thread(
                self.notify_status, f"select multi: {failure}", refused=True
            )
            return
        self.call_from_thread(
            self.notify_status,
            f"selected Multiset {number:02d} on the unit — switch the unit "
            "to MULTI mode to hear it",
        )

    @work(thread=True)
    def _multi_worker(self) -> None:
        """**MIDI only** -- the screen is built on the main thread."""
        failure: BaseException | None = None
        raw: Any = None
        try:
            with self._busy_gate, self._lock:
                raw = self.device.read_multi()
        except _BRIDGE_ERRORS as exc:
            failure = exc
        finally:
            self._exchange.release()
        if failure is not None:
            self.call_from_thread(self.notify_status, f"multi: {failure}", refused=True)
            return
        self.call_from_thread(self._show_multi, raw)

    def _refresh_multi_rows(self) -> None:
        """Rewrite the Multi rows' name cells from ``self._multisets``."""
        table = self.query_one("#bank-table", DataTable)
        for number in range(len(self._multisets)):
            try:
                name = getattr(self._multisets[number], "name", "") or ""
                table.update_cell(
                    f"multi:{number:02d}", "name", name or "[dim]--[/dim]"
                )
            except Exception:  # noqa: S110
                pass

    def _after_multi_write(self) -> None:
        """The editor stored a new setup: show what it wrote."""
        self._refresh_multi_rows()
        if self._current_multi is not None:
            self._fill_multiset(self._current_multi)

    def _show_multi(self, raw: bytes) -> None:
        from kws import multi as multi_mod

        packet = m.parse(raw)
        if packet is None:
            self.notify_status(
                "multi: the reply was not a Wavestation message", refused=True
            )
            return
        try:
            sets = multi_mod.parse(packet.data())
        except Exception as exc:
            self.notify_status(f"multi: {exc}", refused=True)
            return
        self._multi_sets = list(sets)
        self._multisets = list(sets)
        # Refresh the Multi rows in the bank table so their name cells
        # show the freshly read Multiset names.
        self._refresh_multi_rows()
        table = self.query_one("#bank-table", DataTable)
        number = self._current_multi if self._current_multi is not None else 0
        if not (0 <= number < len(sets)):
            number = 0
        self._current_multi = number
        try:
            row_index = table.get_row_index(f"multi:{number:02d}")
            table.move_cursor(row=row_index)
        except Exception:  # noqa: S110
            pass
        table.focus()
        self._fill_multiset(number)
        self.notify_status(
            f"read {len(sets)} Multisets — Enter selects one on the unit "
            "(Multi mode), e edits"
        )

    def action_edit_multi(self) -> None:
        sets = getattr(self, "_multisets", None)
        if not sets:
            self.notify_status("read multi first (m)", refused=True)
            return
        if self._busy:
            self.notify_status("busy", refused=True)
            return
        index = getattr(self, "_current_multi", None)
        if index is None or not (0 <= index < len(sets)):
            index = 0
        self.push_screen(
            MultiEditScreen(
                multisets=sets,
                selected=index,
                device=self.device,
                backup_dir=None,
                catalog=getattr(self, "catalog", None),
            )
        )

    @work(thread=True)
    def _multi_edit_write_worker(  # noqa: C901
        self,
        multisets: Sequence[Any],
        device: Any,
        backup_dir: Optional[str],
        screen: Any,
    ) -> None:
        failure: BaseException | None = None
        result: Any = None
        try:
            from kws import multi as multi_mod

            encoded = multi_mod.encode(multisets)
            with self._busy_gate, self._lock:
                kwargs: Dict[str, Any] = {}
                if backup_dir is not None:
                    kwargs["backup_dir"] = backup_dir
                else:
                    try:
                        from kws import backup as _backup

                        kwargs["backup_dir"] = _backup.default_dir()
                    except Exception:  # noqa: S110
                        pass
                kwargs["verify"] = True
                result = device.write_multi(encoded, **kwargs)
        except BaseException as exc:
            failure = exc
        finally:
            try:
                self._exchange.release()
            except RuntimeError:
                pass
        if failure is not None:
            self.call_from_thread(
                self.notify_status, f"multi write: {failure}", refused=True
            )
            try:
                self.call_from_thread(screen._write_failed, str(failure))
            except Exception:  # noqa: S110
                pass
            return
        verified = result.get("verified") if isinstance(result, dict) else None
        if verified is not True:
            mismatch = (
                result.get("mismatch")
                if isinstance(result, dict) and result.get("mismatch") is not None
                else "the read-back did not confirm the write"
            )
            mismatch_text = str(mismatch)
            self.call_from_thread(
                self.notify_status, f"multi write: {mismatch_text}", refused=True
            )
            try:
                self.call_from_thread(screen._write_failed, mismatch_text)
            except Exception:  # noqa: S110
                pass
            return
        try:
            if hasattr(self, "_record_write"):
                payload = result if isinstance(result, dict) else {"backup": None}
                self._record_write(payload)
            if hasattr(self, "_multi_sets"):
                self._multi_sets = list(multisets)
                self._multisets = list(multisets)
        except Exception:  # noqa: S110
            pass
        try:
            self.call_from_thread(screen._write_succeeded, result)
        except Exception:  # noqa: S110
            pass

    def action_device_info(self) -> None:
        if self._busy:
            self.notify_status("busy", refused=True)
            return
        if not self._exchange.acquire(blocking=False):
            self.notify_status("busy", refused=True)
            return
        self._info_worker()

    @work(thread=True)
    def _info_worker(self) -> None:
        """**MIDI only** -- the report is built on the main thread."""
        failure: BaseException | None = None
        fields: Any = None
        try:
            with self._busy_gate, self._lock:
                fields = self.device.identify()
        except _BRIDGE_ERRORS as exc:
            failure = exc
        finally:
            self._exchange.release()
        if failure is not None:
            self.call_from_thread(
                self.notify_status, f"device: {failure}", refused=True
            )
            return
        self.call_from_thread(self._show_info, fields)

    def _show_info(self, fields: Optional[Dict[str, Any]]) -> None:
        lines = [f"connection      {self.device.description}", ""]
        if fields:
            lines.append(
                f"manufacturer    {fields['manufacturer']:#04x}"
                + ("  (Korg)" if fields["is_korg"] else "")
            )
            lines.append(
                f"family          {fields['family']}"
                + ("  (Wavestation)" if fields.get("is_wavestation") else "")
            )
            lines.append(f"global channel  {fields['channel'] + 1}")
            lines.append("")
            lines.append("The global channel comes from the unit. Every")
            lines.append("System Exclusive request is answered only on it.")
        else:
            lines.append("nothing answered an Identity Request")
        lines.append("")
        lines.append(
            f"catalog         {len(self.catalog)} names"
            + (f" from {self.catalog.source}" if self.catalog.source else "")
        )
        lines.append("")
        lines.append("A `*` marks a name the unit and the catalog disagree")
        lines.append("about, and the two sides of that split by writability:")
        lines.append("on a writable bank (RAM 1-3, Card) it is somebody's own")
        lines.append("work, saved over the factory preset; on a ROM bank the")
        lines.append("printed list is wrong, and the unit is right.")
        self.push_screen(ReportScreen("Device", "\n".join(lines)))

    def action_help(self) -> None:
        self.push_screen(ReportScreen("kwsed", _HELP))


_HELP = """\
Two panes. Left: the banks, each listed once per kind -- once for its 50
performances, once for its 35 patches. Right: that bank-and-kind's slots.

  RAM 1-3   writable; somebody's own work lives here.
  ROM 4-11  read-only.
  Card      the HCard's forty banks, not one bank -- see below.

A performance combines up to eight parts, each pointing at a patch. A
patch is a sound: four oscillators playing waves or wave sequences. They
are different objects; performance 7 is not patch 7.

**The Card is an HCard.**  One card slot holds forty banks, switched
physically on the card itself. kwsed cannot switch them and the synth
cannot report which one is in front of it. Every bank is listed; the one
marked `•` is the last confirmed active bank, and the others are greyed.

  C   capture the bank currently on the card: read it, work out which of
      the forty it is from its content, and file its names (SILENT)
  X   card sweep: walk 00-39, asking you to select each bank on the
      card between reads (SILENT)
  L   label a captured bank. This is kwsed's own label, not the card's
      name: the card name is unreadable over MIDI, and writing it is not
      implemented yet (TODO 23)
  ⏎   on a card row, selects the slot from whichever bank is active on
      the card. kwsed refuses when that is not the row you are on, or
      when no bank has been confirmed yet -- select the bank on the
      HCard and press C once.

A first capture of a bank is filed on your say-so (unverified); later
captures are confirmed by a content digest, and a capture that is a
different bank than the row says so and asks you to switch the HCard.

  ⏎   select the Performance on the unit -- it will sound. A patch is
      never selected: the SR has no patch mode, so Enter refuses a patch
      row -- a patch is heard through a Performance that uses it.
      Patches are read, written and favourited like any other slot.
  r   read this bank's names from the unit -- SILENT, nothing is selected
  S   scan the ten internal banks (RAM 1-3, ROM 4-11) -- SILENT; card
      banks are captured with C or X
  /   search names
  m   read the 32 Multisets and jump to them: each assigns a performance to
      the 16 MIDI channels. They live in the SR's internal memory, not on
      the card; a Multiset refers to performances, including card ones.
      ⏎ on a Multiset row makes it current on the unit (switch the unit to
      MULTI mode to hear it). e edits it as a 16-channel table.
  f   favourite this slot; F steps all -> this bank's -> every favourite
  t   tags; n   note (both hang off a favourite, so press f first)
  V   filter the list by favourite rating
  [ ] step the send channel; c set it by number
  z   undo the last write (the write gate must be armed)
  w   arm or lock the write gate (or start with --allow-write)
  i   device identity and the unit's global channel
  ?   this help; q quit

A `*` marks a name the unit and the catalog disagree about. In a
writable bank (RAM 1-3) that means somebody saved their own work over the
factory preset; in a ROM bank it cannot mean that -- ROM does not change
-- so the printed list is wrong, and the unit is right.

**Bank Select is established.**  The SR has six MIDI banks (CC32 0-5),
each holding two 50-performance banks: RAM1/RAM2 = 0, ROM11/CARD = 1,
RAM3/ROM4 = 2, ROM5/ROM6 = 3, ROM7/ROM8 = 4, ROM9/ROM10 = 5. CC0 is 0 and
the Program Change is 0-49 for the first bank, 50-99 for the second.
Enter sends exactly that and says what it sent. The unit's "Bank Select"
MIDI-receive parameter must be ON.

Moving the cursor sends nothing at all.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = cli.make_parser(
        prog="kwsed", description="Browse a Korg Wavestation SR's sounds."
    )
    # --scan is offered even though a Wavestation's ports are whatever ALSA
    # reports behind the user's interface: re-probing finds nothing a name
    # hint could not, which is exactly why --scan is a request to re-probe
    # and not the default. The port remembered last time is the default.
    # channel=False: the option this tool wants is the unit's own global
    # channel (--device-channel), and on this instrument that number is also
    # the channel selections are sent on -- they are not two numbers here.
    cli.add_common_arguments(
        parser,
        port=True,
        scan=True,
        recv_port=True,
        channel=False,
        device_channel=True,
        catalog=True,
        config=True,
        favorites=True,
        allow_write=True,
    )
    parser.add_argument(
        "--live-names",
        "--live",
        dest="live_names",
        default=None,
        help=(
            "cache of names read off the unit "
            f"(default: {_livenames.default_path()}; safe to delete)"
        ),
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cli.validate_common(args)
    from kws import config as cfg

    if getattr(args, "config", None) is None:
        cfg.migrate_legacy_config("kwsed")
        import os

        os.makedirs(cfg.config_dir("kwsed"), exist_ok=True)
    config_path = getattr(args, "config", None) or cfg.DEFAULT_PATH

    # Either a DemoDevice or a WavestationBridge: the two share the handful
    # of operations the browser calls, and neither is a base of the other.
    device: Any
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
                    args.port,
                    recv_port=args.recv_port,
                    # --midi-channel is now --device-channel, with the old
                    # spelling still accepted: the number is the unit's own
                    # global channel, not the channel a program change goes
                    # out on, and the spec says those must not share a word.
                    channel=(
                        (args.device_channel - 1)
                        if args.device_channel
                        else (remembered if remembered is not None else 0)
                    ),
                )
            else:
                # One Identity Request to the remembered port; the sweep
                # only when there is nothing remembered, when the
                # remembered port no longer answers, or on --scan.
                device = b.WavestationBridge.connect(
                    config_path=config_path,
                    # --midi-channel is now --device-channel, with the old
                    # spelling still accepted: the number is the unit's own
                    # global channel, not the channel a program change goes
                    # out on, and the spec says those must not share a word.
                    channel=(
                        (args.device_channel - 1)
                        if args.device_channel
                        else (remembered if remembered is not None else 0)
                    ),
                    scan=args.scan,
                    on_try=lambda n: print(f"  probing {n}...", file=sys.stderr),
                )
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
    live_path = (
        None
        if getattr(args, "demo", False)
        else (getattr(args, "live_names", None) or _livenames.default_path())
    )
    cards_path = None if getattr(args, "demo", False) else _cards.default_path()
    card_mode = cfg.load_card_mode(config_path)
    show_card_defaults = cfg.load_show_card_defaults(config_path)
    physical_path = None if getattr(args, "demo", False) else _physical.default_path()
    app = KwsedApp(
        device,
        catalog=cat.load(args.catalog),
        favorites=store,
        config_path=config_path,
        live_path=live_path,
        cards_path=cards_path,
        allow_write=getattr(args, "allow_write", False),
        # The HCard is switched physically; one read at launch is the only
        # way to know which bank is in front of the synth (read-only).
        auto_capture=cards_path is not None,
        card_mode=card_mode,
        show_card_defaults=show_card_defaults,
        physical_path=physical_path,
    )
    try:
        app.run()
    finally:
        store.close()
        with close_when_idle(device, gate=app._busy_gate):
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
