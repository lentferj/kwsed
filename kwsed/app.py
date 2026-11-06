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
from textual.screen import ModalScreen
from textual.widgets import DataTable, Header, Input, Label, Static

from kws import banks
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
from vinsynlib.ui.modals import RatingFilter, RatingFilterScreen

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
    "V rating filter",
    "z undo",
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

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("e", "edit", "Edit", show=False),
    ]

    def __init__(
        self,
        multisets: Sequence[Any],
        bank_label: Optional[Callable[[int], str]] = None,
        on_edit: Optional[Callable[[Sequence[Any]], None]] = None,
    ) -> None:
        super().__init__()
        self._multisets = list(multisets)
        self._bank_label = bank_label or (lambda n: str(n))
        self._on_edit = on_edit

    def action_edit(self) -> None:
        if self._on_edit is not None:
            self._on_edit(self._multisets)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("[b]Multi mode -- 32 Multisets[/b]")
            yield DataTable(id="multi-table", cursor_type="row")
            yield Static("", id="multi-detail")
            yield Static("[dim]arrows to move, esc / q to close[/dim]")

    def on_mount(self) -> None:
        table = self.query_one("#multi-table", DataTable)
        for label, key in (("#", "num"), ("name", "name"), ("channels used", "used")):
            table.add_column(label, key=key)
        for entry in self._multisets:
            used = sum(1 for part in entry.parts if part.used)
            table.add_row(
                f"{entry.number:02d}",
                entry.name or "[dim]--[/dim]",
                str(used),
                key=str(entry.number),
            )
        self._detail(0)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
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
                f"perf {part.performance:02d}  level {part.level}"
            )
        half = (len(lines) + 1) // 2
        rows = [f"{a:38s}{b}" for a, b in zip(lines[:half], lines[half:] + [""])]
        self.query_one("#multi-detail", Static).update("\n".join(rows))

    def action_close(self) -> None:
        self.dismiss(None)


class MultiEditScreen(ModalScreen[Optional[Dict[str, Any]]]):
    """Edit the Multi Mode Setup (0x06). **Destructive.**"""

    DEFAULT_CSS = """
    MultiEditScreen { align: center middle; }
    MultiEditScreen > Vertical {
        width: 80; height: auto; border: thick $error;
        background: $surface; padding: 1 2;
    }
    MultiEditScreen .armed { color: $error; text-style: bold; }
    """

    BINDINGS = [
        Binding("escape", "close", "Cancel"),
        Binding("q", "close", "Cancel"),
        Binding("a", "arm", "Arm", show=False),
        Binding("w", "fire", "Write", show=False),
    ]

    def __init__(
        self,
        *,
        multisets: Sequence[Any],
        device: Any = None,
        on_write: Optional[Callable[[bytes], Any]] = None,
        backup_dir: Optional[str] = None,
    ) -> None:
        super().__init__()
        self._multisets = list(multisets)
        self._device = device
        self._on_write = on_write
        self._backup_dir = backup_dir
        self._armed = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("[b]Edit Multi Mode Setup[/b]")
            yield Static(f"{len(self._multisets)} multisets", classes="what")
            yield Static("", id="multi-edit-state")
            yield Static("[dim]a to arm, then w to write. esc cancels.[/dim]")

    def action_arm(self) -> None:
        self._armed = True
        self.query_one("#multi-edit-state", Static).update(
            "[b]ARMED[/b] - w writes the Multi Mode Setup. Nothing sent yet."
        )

    def action_fire(self) -> None:
        if not self._armed:
            self.query_one("#multi-edit-state", Static).update(
                "[dim]not armed - press a first[/dim]"
            )
            return
        self._armed = False
        from kws import multi as multi_mod

        encoded = multi_mod.encode(self._multisets)
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
            self.query_one("#multi-edit-state", Static).update(
                f"[b]write failed[/b] {exc}"
            )
            return
        try:
            app = self.app
            if hasattr(app, "_record_write"):
                payload = result if isinstance(result, dict) else {"backup": None}
                app._record_write(payload)
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


class KwsedApp(App[None]):
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
        Binding("V", "rating_filter", "Rating filter"),
        Binding("z", "undo", "Undo"),
    ]

    def __init__(
        self,
        device: Any,
        *,
        catalog: Optional[cat.Catalog] = None,
        favorites: Optional[Favorites] = None,
        config_path: Optional[str] = None,
        live_path: Optional[str] = None,
        allow_write: bool = True,
    ) -> None:
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
        self.rating_filter: RatingFilter | None = None
        self.catalog = catalog if catalog is not None else cat.empty()
        #: Where hardware-read names are cached between runs. None disables
        #: persistence, as in tests and --demo.
        self._live_path = live_path
        self._lock = threading.RLock()
        #: The left pane lists bank-and-kind pairs, because a Wavestation
        #: bank holds 50 performances *and* 35 patches and they are
        #: different things. A pane listing banks alone would need a second
        #: control to say which kind, for no gain.
        self._pairs: List[Tuple[int, str]] = [
            (entry.number, kind)
            for entry in banks.BANKS
            for kind in (banks.Kind.PERFORMANCE, banks.Kind.PATCH)
        ]
        self._current_pair = self._pairs[0]
        self._current: List[banks.Slot] = []
        self._busy_gate = BusyGate()
        self._exchange = threading.Lock()
        self._write_gate = EditGate(armed=allow_write)
        self._filling = False
        #: Scan state
        self._scanning = False
        self._scan_cancel = threading.Event()
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
        message = self._undo_log.settle(results)
        self.notify_status(message)

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

    def on_mount(self) -> None:
        self.title = "kwsed"
        table = self.query_one("#bank-table", DataTable)
        for label, key in (
            ("bank", "bank"),
            ("kind", "kind"),
            ("n", "n"),
            ("", "rw"),
            ("♥", "fav"),
        ):
            table.add_column(label, key=key)
        for number, kind in self._pairs:
            entry = banks.bank(number)
            marked = len(self._favorite_keys(entry.id, kind))
            table.add_row(
                entry.label,
                kind[:4],
                str(banks.count_for(kind)),
                "" if entry.writable else "ro",
                # blank rather than "0": a column of zeroes is
                # noise, and the eye wants the few that differ
                str(marked) if marked else "",
                key=f"{number}:{kind}",
            )

        slots = self.query_one("#slot-table", DataTable)
        # The favourite marker leads the row: a gutter mark the eye runs
        # down, rather than a character glued to the name.
        for label, key in (("♥", "fav"), ("#", "num"), ("name", "name")):
            slots.add_column(label, key=key)
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

    # --- filling ------------------------------------------------------------

    def _favorite_keys(self, bank_id: str, kind: str) -> Set[int]:
        if self.favorites is None:
            return set()
        return self.favorites.keys_for_bank(favorite_bank_id(bank_id, kind))

    def _all_favourites(self) -> List[banks.Slot]:
        """Every favourite in every bank and kind, in listing order."""
        if self.favorites is None:
            return []
        wanted: Dict[str, Set[int]] = {}
        for favourite in self.favorites.all(order="bank"):
            wanted.setdefault(favourite.bank_id, set()).add(favourite.number)
        out: List[banks.Slot] = []
        for number, kind in self._pairs:
            entry = banks.bank(number)
            numbers = wanted.get(favorite_bank_id(entry.id, kind))
            if not numbers:
                continue
            out.extend(s for s in banks.slots(number, kind) if s.number in numbers)
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
                fav = self.favorites.get(entry.bank.id, entry.number)
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
        key = favorite_bank_id(item.bank.id, item.kind)
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
        bank_id = favorite_bank_id(entry.bank.id, entry.kind)
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
        table = self.query_one("#slot-table", DataTable)
        row = table.cursor_row or 0
        self._fill(self._current_pair)
        if self._current:
            table.move_cursor(row=min(row, len(self._current) - 1))

    def _fill(self, pair: Tuple[int, str]) -> None:
        table = self.query_one("#slot-table", DataTable)
        self._current_pair = pair
        self._current = self._visible(pair)
        self._filling = True
        try:
            table.clear()
            for item in self._current:
                name = self.catalog.display_name(item.bank.id, item.number, item.kind)
                if self.catalog.differs(item.bank.id, item.number, item.kind):
                    shown = f"[b]{name}[/b] [dim]*[/dim]"
                elif self.catalog.is_live(item.bank.id, item.number, item.kind):
                    shown = f"[b]{name}[/b]"
                else:
                    shown = name
                marked = self._favorite_keys(item.bank.id, item.kind)
                label = (
                    f"{item.bank.label} {item.kind[:4]} {item.number:02d}"
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

    def _detail(self, row: int) -> None:
        if not self._current or row >= len(self._current):
            self.query_one("#detail", Static).update("")
            return
        item = self._current[row]
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

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if self._filling:
            return
        if event.data_table.id == "bank-table":
            row = event.cursor_row
            if 0 <= row < len(self._pairs):
                self._fill(self._pairs[row])
        elif event.data_table.id == "slot-table":
            self._detail(event.cursor_row)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
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
        if not self._exchange.acquire(blocking=False):
            self.notify_status("busy", refused=True)
            return
        self._select_worker(self._current[row])

    @work(thread=True)
    def _select_worker(self, entry: banks.Slot) -> None:
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
        self.call_from_thread(
            self.notify_status,
            f"bank select LSB {entry.bank.sysex} + program change "
            f"{entry.number} sent for {entry} — which bank a Bank Select "
            f"actually reaches is not established",
        )

    def action_read_names(self) -> None:
        if self._busy:
            self.notify_status("busy", refused=True)
            return
        if not self._exchange.acquire(blocking=False):
            self.notify_status("busy", refused=True)
            return
        self._read_worker(*self._current_pair)

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
            self._fill(self._current_pair)
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
        total = len(self._pairs)
        try:
            for number, kind in self._pairs:
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
        return any(self.catalog.is_live(banks.bank(n).id, 0, k) for n, k in self._pairs)

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
        self._multi_sets = sets
        self.push_screen(
            MultiScreen(
                sets,
                bank_label=lambda n: _bank_label(n),
                on_edit=self._edit_multi_sets,
            )
        )
        self.notify_status(f"{len(sets)} multisets")

    def _edit_multi_sets(self, sets: Sequence[Any]) -> None:
        self.push_screen(
            MultiEditScreen(
                multisets=sets,
                device=self.device,
                backup_dir=None,
            )
        )

    def action_edit_multi(self) -> None:
        sets = getattr(self, "_multi_sets", None)
        if not sets:
            self.notify_status("read multi first (m)", refused=True)
            return
        if self._busy:
            self.notify_status("busy", refused=True)
            return
        if not self._exchange.acquire(blocking=False):
            self.notify_status("busy", refused=True)
            return
        self.push_screen(
            MultiEditScreen(multisets=sets, device=self.device, backup_dir=None)
        )
        self._exchange.release()

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
Two panes. Left: the twelve banks, each listed twice -- once for its 50
performances, once for its 35 patches. Right: that bank-and-kind's slots.

  RAM 1-3   writable; somebody's own work lives here.
  ROM 4-11  read-only.
  Card      writable; the card in the slot.

A `*` marks a name the unit and the catalog disagree about. In a
writable bank (RAM 1-3, Card) that means somebody saved their own work
over the factory preset; in a ROM bank it cannot mean that -- ROM does
not change -- so the printed list is wrong, and the unit is right.

A performance combines up to eight parts, each pointing at a patch. A
patch is a sound: four oscillators playing waves or wave sequences. They
are different objects; performance 7 is not patch 7.

  ⏎   select the slot on the unit -- it will sound
  r   read this bank's names from the unit -- SILENT, nothing is selected
  /   search names
  m   channels: the 32 Multisets, and which performance answers on each
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
    app = KwsedApp(
        device,
        catalog=cat.load(args.catalog),
        favorites=store,
        config_path=config_path,
        live_path=live_path,
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
