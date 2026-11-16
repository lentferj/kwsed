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
import os
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Iterable,
    Sequence,
    Set,
    Tuple,
    TypeVar,
)

from vinsynlib import cli
from vinsynlib.ui.report import ReportScreen
from vinsynlib.ui.work import QUIET_BRIDGE_ERRORS, BusyGate, close_when_idle
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip
from textual.widgets import DataTable, Header, Input, Label, Static

from kws import bankstore
from kws import import_audit
from kws import importers
from kws import banks
from kws import deps
from kws import relations
from kws import perfcopy
from kws import records as rec
from kws.multi import MULTISETS
from kws import multisets as _multisets_mod
from kws.fx import EFFECT_TYPES, FxBlock
from kws import cards as _cards
from kws import physical as _physical
from kws import messages as m
from kws import catalog as cat
from kws import livenames as _livenames
from kws import scan as _scan
from kws import undo as _undo
from kwsed.gate import EditGate
from kwsed.file_picker import FilePickerScreen
from kwsed.import_companions import find_companions, unique_equivalent_companions
from kwsed.import_screen import BankImportScreen
from kwsed.import_bank import (
    OVERFLOW_CARDS_ONLY,
    OVERFLOW_PATCHES,
    OVERFLOW_SELF_CONTAINED,
    ImportPassState,
    PreflightImportScreen,
    WholeBankChoice,
    WholeBankImportScreen,
    _entry_bank,
)
from kwsed import import_intent
from kwsed.import_single import SingleImportScreen
from kwsed.import_source import (
    SourceEntry,
    make_source_entry,
    merge_source_components,
    origin_for_unit,
    same_source_bank,
    unit_bank_choices,
)
from kwsed.target_bank import ImportMenuScreen, TargetBankScreen
from kwsed.target_screen import TargetSlotScreen

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
from vinsynlib.ui.confirm import ConfirmScreen

#: Family quiet set plus this tool's own bridge errors.
_BRIDGE_ERRORS: tuple[type[BaseException], ...] = (
    *QUIET_BRIDGE_ERRORS,
    ValueError,
    LookupError,
    RuntimeError,
    SystemError,
)

#: The plural block names a bank-bundle backup holds, with the singular kind
#: each one is written and read as. In the send order -- wave sequences first
#: and performances last, as the bridge sends them.
_BUNDLE_BLOCKS: Tuple[Tuple[str, str], ...] = (
    ("waveseq", "waveseq"),
    ("patches", banks.Kind.PATCH),
    ("performances", banks.Kind.PERFORMANCE),
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


# This `bank_id` is the favourites key space, not the model's `scope_id`.
def favorite_bank_id(bank_id: str, kind: str) -> str:
    """The key a favourite is filed under.

    The kind is part of it, in full. A bank holds 50 performances and 35
    patches, and both words begin with P -- the same collision that once
    merged them in ``Slot.key`` would merge a favourited performance with
    the patch of the same number.
    """
    return f"{bank_id}:{kind}"


def _split_groups_plannable(groups: Sequence[perfcopy.SplitGroup]) -> bool:
    """Whether every group of a split packing can be planned at all.

    A group whose plan carries rejections -- a patch it names is not
    loaded, or one performance alone exceeds the bank -- must not be
    written: the fence would put a broken half of the split onto the unit.
    """
    return all(group.plan is None or not group.plan.rejected for group in groups)


@dataclass(frozen=True)
class ViewSlot:
    """A slot plus the scoped bank it came from.

    The all-favourites view spans the HCard's forty banks and the physical
    cards, and every one of those slots has ``bank.id == "CARD"``. A plain
    :class:`kws.banks.Slot` therefore cannot say which bank a row belongs
    to, and two banks' Performance 03 would collide as the same DataTable
    key. This carries the scope (``CARD/25``, ``CARD/c1``) and the
    favourite's stored name alongside the slot.
    """

    slot: banks.Slot
    #: Favourites key space (vinsynlib's schema), not the model's `scope_id`.
    bank_id: str
    name: str = ""

    @property
    def bank(self) -> banks.Bank:
        return self.slot.bank

    @property
    def kind(self) -> str:
        return self.slot.kind

    @property
    def number(self) -> int:
        return self.slot.number

    @property
    def display(self) -> str:
        return self.slot.display

    @property
    def key(self) -> str:
        return f"{self.bank_id}:{self.kind}:{self.number:03d}"

    def __str__(self) -> str:
        return f"{self.bank.label} {self.kind} {self.number:02d}"


#: What a listing row may be: a plain slot, or one of the all-favourites
#: rows that wrap a slot plus the scoped bank it came from. Everything
#: that holds a row -- the visible listing itself, the cursor's entry,
#: the name and scope helpers -- accepts either.
SlotLike = banks.Slot | ViewSlot
SlotT = TypeVar("SlotT", banks.Slot, ViewSlot)


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
    # `m` reads the 32 Multisets from the unit and lands on them; the right
    # pane is the selected one's 16 channels. The hint names the object the
    # key reads, matching `kwscli multi` and `e edit multi` -- "channels"
    # named the view and hid the device read the key performs.
    "m multi",
    "e edit multi",
    "p patches",
    "S scan",
    "C capture card",
    "L label card",
    "X card sweep",
    "V rating filter",
    "d deps",
    "D deps pane",
    "y copy perf",
    "x FX",
    "B copy bank",
    "I import",
    "z undo",
    "w write gate",
    "i device",
    "? help",
    "q quit",
)

#: A muted red background for a Multiset row/channel whose recording no
#: longer matches the device. Used as a Rich background, not CSS: DataTable
#: rows have no style hook of their own.
MULTI_WARNING_COLOR = "#5c2626"
MULTI_WARNING_STYLE = f"on {MULTI_WARNING_COLOR}"


class WarningTable(DataTable[Any]):
    """A DataTable that can paint whole rows with the warning background.

    A cell's Rich background stops at the cell's content; the table's own
    cell padding and the unused width to the right stay in the theme
    background. Overlaying the rendered strip makes the warning a single
    continuous line, including the gaps and the empty tail.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._warning_rows: Set[Any] = set()
        self._warning_style = Style(bgcolor=MULTI_WARNING_COLOR)

    def set_warning_rows(self, rows: Iterable[Any]) -> None:
        fresh = set(rows)
        if fresh != self._warning_rows:
            self._warning_rows = fresh
            self.refresh()

    def _render_line(self, y: int, x1: int, x2: int, base_style: Style) -> Strip:
        strip = super()._render_line(y, x1, x2, base_style)
        try:
            row_key, _ = self._get_offsets(y)
        except (LookupError, IndexError):
            return strip
        if row_key not in self._warning_rows:
            return strip
        return Strip(
            [
                Segment(
                    segment.text,
                    (segment.style or Style()) + self._warning_style,
                )
                for segment in strip._segments
            ]
        )


# The family caps the legend and lets an app pick the cap. The default of
# two lines is right for the canonical list, but kwsed's is longer: at a
# hundred columns it needs four, and a two-line cap would hide `z` undo,
# `w` write gate and `B` copy bank behind the pointer. Four shows the
# whole list at that width and still caps it on a narrow terminal.
KeyHints.MAX_LINES = 4

#: The confirmation before an effect-type change. The number and the
#: parameter bytes belong together, and the unit reinterprets the old
#: bytes, so this is a decision, not a refresh. Wrapped to the shared
#: ConfirmScreen's 60-column dialog; a single long line was truncated.
FX_TYPE_WARNING = (
    "Changing the effect type loads a factory package\n"
    "on the unit. The other parameter bytes keep\n"
    "their stored values until the unit reinterprets\n"
    "them."
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


@dataclass(frozen=True)
class PerformanceRow:
    """One row of the performance picker."""

    bank: int  #: the Multiset part's bank, as a System Exclusive byte
    number: int
    name: str
    favourite: bool
    rating: int


@dataclass
class BankWriteOutcome:
    """One fenced bundle write of one bank inside a cross-bank import.

    ``result`` is the bridge's ``write_bank_bundle`` verdict for this
    bank; ``verified`` says its read-back compared every kind and
    matched. ``built`` is what was built for the bank (a split group's
    payload, or the overflow target's share) so the main thread can
    file it exactly as a success path does, and ``group`` is the split
    group the bank carried, when it is one.
    """

    bank: Any
    result: Any = None
    verified: bool = False
    built: Any = None
    group: Any = None


@dataclass
class MultiBankOutcome:
    """What a cross-bank import wrote, and where it stopped.

    Cross-bank writes are not atomic: each bank is its own fenced
    bundle, and a later bank can fail after an earlier one has landed.
    The outcome is the honest report of that instead of an exception
    that would lose the completed banks: ``completed`` is exactly what
    landed (to book-keep on the main thread), ``failed_bank`` the bank
    the write stopped on, ``not_written`` the banks never attempted,
    and ``ok`` is false unless every attempted bank verified.
    """

    completed: List[BankWriteOutcome] = field(default_factory=list)
    failed_bank: Optional[Any] = None
    error: str = ""
    not_written: List[Any] = field(default_factory=list)
    #: The overflow import's full build, when this outcome is one.
    built: Any = None
    #: The split groups whose source slots did not land: the group the
    #: failure hit and every group it stopped before. The session's
    #: remainder keeps their slots so ``n`` never rewrites a landed one.
    skipped_groups: List[Any] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error == "" and self.failed_bank is None


@dataclass(frozen=True)
class _FrozenPlanBank:
    """The worker's view of the source bank: its scope, label and records.

    The write worker must never read the session's live sources: a plan
    the approval covered is built from a snapshot taken at the write
    boundary, and this frozen stand-in carries the snapshot of the
    source bank's own records into the worker, which plans and builds
    from it (and from the frozen map the snapshot belongs to) alone.
    """

    label: str
    scope: str
    source: deps.Source


class PerformancePickerScreen(ModalScreen[Optional[Tuple[int, int]]]):
    """Choose a Performance for a Multiset channel, with the browser's filters.

    Rows are every Performance in every bank. ``F`` cycles all performances
    and the favourites, ``V`` applies the star-rating filter, and Enter
    returns ``(bank sysex byte, performance number)``. Favourites are shown
    with a star and their rating; toggling one is the browser's job.
    """

    DEFAULT_CSS = """
    PerformancePickerScreen { align: center middle; }
    PerformancePickerScreen > Vertical {
        width: 84; height: 80%; border: thick $accent;
        background: $surface; padding: 1 2;
    }
    PerformancePickerScreen DataTable { height: 1fr; }
    PerformancePickerScreen .hint { color: $text-muted; }
    """

    BINDINGS = [
        Binding("escape", "close", "Cancel"),
        Binding("q", "close", "Cancel"),
        Binding("enter", "select", "Select", priority=True),
        Binding("F", "cycle_view", "Favourites view"),
        Binding("V", "rating_filter", "Rating filter"),
    ]

    def __init__(
        self,
        rows: Sequence[Any],
        *,
        title: str = "Choose a Performance",
        current: Optional[Tuple[int, int]] = None,
    ) -> None:
        super().__init__()
        self._rows = list(rows)
        self._title = title
        self._current = current
        self._favourites_only = False
        self._rating: Optional[RatingFilter] = None
        self._visible: List[Any] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._title, id="perf-picker-title")
            yield DataTable(id="perf-picker", cursor_type="row", zebra_stripes=True)
            yield Static("", id="perf-picker-state")
            yield Static(
                "[dim]↑/↓ move · enter chooses · F favourites · "
                "V rating filter · esc cancels[/dim]",
                classes="hint",
            )

    def on_mount(self) -> None:
        table = self.query_one("#perf-picker", DataTable)
        table.add_column("bank", key="bank")
        table.add_column("perf", key="perf")
        table.add_column("name", key="name")
        table.add_column("fav", key="fav")
        table.add_column("rating", key="rating")
        self._populate()
        table.focus()

    def _filtered(self) -> List[Any]:
        rows = self._rows
        if self._favourites_only:
            rows = [row for row in rows if row.favourite]
        if self._rating is not None and self._rating.stars:
            if self._rating.at_least:
                rows = [row for row in rows if row.rating >= self._rating.stars]
            else:
                rows = [row for row in rows if row.rating == self._rating.stars]
        return list(rows)

    def _populate(self) -> None:
        table = self.query_one("#perf-picker", DataTable)
        where = table.cursor_row
        table.clear()
        self._visible = self._filtered()
        for row in self._visible:
            table.add_row(
                _bank_label(row.bank),
                f"{row.number:02d}",
                row.name,
                "★" if row.favourite else "",
                str(row.rating) if row.rating else "",
                key=f"{row.bank}:{row.number}",
            )
        if self._current is not None:
            try:
                table.move_cursor(
                    row=self._visible.index(
                        next(
                            row
                            for row in self._visible
                            if (row.bank, row.number) == self._current
                        )
                    )
                )
            except (StopIteration, ValueError):
                pass
        elif where is not None and self._visible:
            table.move_cursor(row=max(0, min(where, len(self._visible) - 1)))
        state = self.query_one("#perf-picker-state", Static)
        state.update(f"{len(self._visible)} performances")

    def action_cycle_view(self) -> None:
        self._favourites_only = not self._favourites_only
        self._populate()

    def action_rating_filter(self) -> None:
        current = self._rating or RatingFilter(stars=0, at_least=False)

        def apply(value: Optional[RatingFilter]) -> None:
            if value is None:
                return
            self._rating = None if not value.stars else value
            self._populate()

        self.app.push_screen(RatingFilterScreen(current), apply)

    def action_select(self) -> None:
        table = self.query_one("#perf-picker", DataTable)
        row = table.cursor_row
        if row is None or not (0 <= row < len(self._visible)):
            return
        chosen = self._visible[row]
        self.dismiss((chosen.bank, chosen.number))

    def action_close(self) -> None:
        self.dismiss(None)


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
        Binding("s", "stage", "Stage to RAM", show=False),
        Binding("n", "rename", "Rename", show=False),
        Binding("x", "edit_fx", "FX", show=False),
        Binding("r", "record", "Record", show=False),
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
        sources: Optional[Dict[str, Any]] = None,
        archive: Optional[Dict[int, Any]] = None,
        physical: Optional[Dict[str, Any]] = None,
        resident_scope: Optional[str] = None,
        on_edit_fx: Optional[Callable[[Any], None]] = None,
        favorites: Any = None,
        card_scope: Optional[str] = None,
        snapshots: Any = None,
        mismatches: Any = None,
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
        self._sources: Dict[str, Any] = dict(sources) if sources else {}
        self._archive: Dict[int, Any] = dict(archive) if archive else {}
        self._physical: Dict[str, Any] = dict(physical) if physical else {}
        self._resident_card_scope: Optional[str] = resident_scope
        self._desired_scopes: Dict[int, str] = {}
        self._staged_plan: Optional[Any] = None
        self._armed = False
        self._table: Optional[DataTable[Any]] = None
        self._on_edit_fx = on_edit_fx
        self._favorites = favorites
        self._card_scope = card_scope
        self._snapshots = snapshots
        self._mismatches = mismatches

    # --- helpers ----------------------------------------------------------

    def _ms(self) -> Any:
        return self._multisets_copy[self._selected]

    def _intended_card_scope(self, channel: int) -> Optional[str]:
        """The card bank a card channel means, if it uses the Card slot.

        A pending choice wins; otherwise keep the card already recorded for
        this channel, even when only its performance number was nudged.
        Losing that would silently collapse every card channel onto whichever
        bank happens to be active at write time.
        """
        from kws import banks as _banks

        part = self._ms().parts[channel]
        if part.bank != _banks.bank("CARD").sysex:
            return None
        scope = self._desired_scopes.get(channel)
        if scope and scope != "CARD":
            return scope
        snapshot = (self._snapshots or {}).get(self._selected)
        binding = snapshot.channels.get(channel) if snapshot is not None else None
        if binding is not None:
            recorded = getattr(binding, "card_scope", None)
            if recorded:
                return str(recorded)
        if self._card_scope and self._card_scope != "CARD":
            return self._card_scope
        return None

    def _intended_card_scopes(self) -> Dict[int, str]:
        """Channel indices to intended card scope, for the used card parts."""
        from kws import banks as _banks

        card_bank = _banks.bank("CARD").sysex
        scopes: Dict[int, str] = {}
        for index, part in enumerate(self._ms().parts[:16]):
            if not getattr(part, "used", False) or part.bank != card_bank:
                continue
            scope = self._intended_card_scope(index)
            if scope:
                scopes[index] = scope
        return scopes

    def _mixed_card_scopes(self) -> List[str]:
        """The distinct card banks this Multiset asks for, sorted."""
        return sorted(set(self._intended_card_scopes().values()))

    def _is_mixed_cards(self) -> bool:
        return len(self._mixed_card_scopes()) > 1

    def _snapshot(self) -> Any:
        """What this Multiset points at now, for the live multi catalog."""
        from kws import multi as _multi_mod

        ms = self._ms()
        encoded = _multi_mod.encode(self._multisets_copy)
        at = self._selected * _multi_mod.MULTISET_SIZE
        record = encoded[at : at + _multi_mod.MULTISET_SIZE]

        def card_scope_for(channel: int) -> Optional[str]:
            return self._intended_card_scope(channel)

        def name_for(bank: int, number: int, scope: Optional[str]) -> str:
            if scope:
                return self._scope_name(scope, number)
            part = _multi_mod.Part(
                channel=1, bank=bank, performance=number, level=0, flag=0
            )
            return self._name_for_part(part)

        def card_digest_for(scope: Optional[str]) -> str:
            if not scope or not scope.startswith("CARD/"):
                return ""
            key = scope[5:]
            if key.isdigit():
                captured = self._archive.get(int(key))
                return getattr(captured, "digest", "") or ""
            card = self._physical.get(key.lower())
            return getattr(card, "digest", "") or ""

        return _multisets_mod.build_snapshot(
            ms,
            record,
            card_scope_for=card_scope_for,
            name_for=name_for,
            card_digest_for=card_digest_for,
        )

    def _title(self) -> str:
        ms = self._ms()
        name = getattr(ms, "name", "") or ""
        title = f"Edit Multiset {getattr(ms, 'number', self._selected):02d} — {name}"
        if self._is_mixed_cards():
            title += " — MIXED CARDS"
        return title

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

    def _resident_scope(self) -> Optional[str]:
        return self._resident_card_scope

    def _scope_label(self, scope: str) -> str:
        if scope == "CARD":
            return "Card"
        if scope.startswith("CARD/"):
            suffix = scope[len("CARD/") :]
            return f"Card {suffix}"
        return scope

    def _scope_name(self, scope: str, number: int) -> str:
        if scope == "CARD":
            return ""
        if not scope.startswith("CARD/"):
            return ""
        suffix = scope[len("CARD/") :]
        if suffix.lower().startswith("c"):
            key = suffix.lower()
            phys = self._physical.get(key)
            if phys is None:
                return ""
            try:
                return str(phys.performances.get(number, ""))
            except Exception:
                return ""
        try:
            bank_num = int(suffix)
        except ValueError:
            return ""
        arch = self._archive.get(bank_num)
        if arch is None:
            return ""
        try:
            return str(arch.performances.get(number, ""))
        except Exception:
            return ""

    def _parse_card_scope(self, suffix: str) -> Optional[str]:
        if suffix.lower().startswith("c"):
            digits = suffix[1:].strip()
            try:
                n = int(digits)
                return f"CARD/c{n}"
            except ValueError:
                if digits:
                    return f"CARD/c{digits.lower()}"
                return None
        try:
            n = int(suffix)
        except ValueError:
            return None
        return f"CARD/{n:02d}"

    def _parse_scope(self, text: str) -> Optional[str]:
        raw = text.strip()
        if not raw:
            return None
        upper = raw.upper()
        if upper in ("CARD", "C"):
            return "CARD"
        if upper.startswith("CARD/"):
            return self._parse_card_scope(raw[len("CARD/") :].strip())
        try:
            from kws import banks as _banks

            b = _banks.bank(raw)
            return b.id
        except Exception:  # noqa: S110
            pass
        try:
            n = int(raw)
        except ValueError:
            return None
        try:
            from kws import banks as _banks

            if 0 <= n <= 11:
                return _banks.bank(n).id
        except Exception:
            return None
        return None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._title(), id="multi-edit-title")
            yield Static(
                "⏎ chooses the performance (favourites/ratings) · + / - nudge · "
                "space on/off · b bank · s stage · n rename · x FX · "
                "r record · a arm, w write · esc cancels",
                classes="hint",
            )
            table: DataTable[Any] = WarningTable(
                id="multi-edit-table", cursor_type="cell"
            )
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
        marks = {
            mismatch.channel
            for mismatch in (self._mismatches or {}).get(self._selected, [])
        }
        for i in range(16):
            part = ms.parts[i] if i < len(ms.parts) else None
            table.add_row(*self._row_cells(i, part), key=f"r{i}")
        if isinstance(table, WarningTable):
            table.set_warning_rows({f"r{i}" for i in marks})
        if table.row_count and len(table.columns):
            row = max(0, min(row, table.row_count - 1))
            column = max(0, min(column, len(table.columns) - 1))
            table.move_cursor(row=row, column=column)
        found = (self._mismatches or {}).get(self._selected, [])
        if self._is_mixed_cards():
            scopes = ", ".join(
                self._scope_label(scope) for scope in self._mixed_card_scopes()
            )
            self._status(
                f"MIXED CARDS ({scopes}) — the unit uses the active card; "
                "s stages into RAM"
            )
        elif found:
            self._status(
                "recorded: "
                + "; ".join(
                    f"ch {mismatch.channel + 1:02d} {mismatch.text}"
                    for mismatch in found[:2]
                )
            )
        self._rebuild_title()

    def _row_cells(self, i: int, part: Any) -> tuple[Any, ...]:
        if part is None:
            return (f"{i + 1:02d}", "--", "", "--", "--")
        if getattr(part, "used", False):
            from kws import banks as _banks

            snapshot = (self._snapshots or {}).get(self._selected)
            binding = snapshot.channels.get(i) if snapshot is not None else None
            scope = self._desired_scopes.get(i)
            if scope is not None and str(scope).startswith("CARD"):
                # A pending choice (the picker, or b) is the current truth;
                # the snapshot may record a different bank.
                perf = f"{self._scope_label(scope)} {part.performance:02d}"
                name = self._scope_name(scope, part.performance)
            elif (
                binding is not None
                and binding.card_scope
                and part.bank == _banks.bank("CARD").sysex
                and part.performance == binding.performance
            ):
                perf = f"{self._scope_label(binding.card_scope)} {part.performance:02d}"
                name = binding.name
            else:
                perf = f"{_bank_label(part.bank)} {part.performance:02d}"
                name = self._name_for_part(part)
            marks = {
                mismatch.channel
                for mismatch in (self._mismatches or {}).get(self._selected, [])
            }
            used = "used !" if i in marks else "used"
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
        self._staged_plan = None
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

    def _performance_rows(self) -> List[PerformanceRow]:
        from kws import banks as _banks

        rows: List[PerformanceRow] = []
        for bank in _banks.BANKS:
            is_card = bank.id == "CARD"
            # The card part names the slot, not which bank is in it; the
            # active card's scope is where its names and favourites live.
            scope = (self._card_scope or bank.id) if is_card else bank.id
            for number in range(_banks.PERFORMANCES_PER_BANK):
                name = self._scope_name(scope, number) if is_card else ""
                if not name and self._catalog is not None:
                    try:
                        name = str(
                            self._catalog.display_name(
                                bank.id, number, _banks.Kind.PERFORMANCE
                            )
                            or ""
                        )
                    except Exception:
                        name = ""
                favourite = False
                rating = 0
                if self._favorites is not None:
                    try:
                        fav = self._favorites.get(
                            favorite_bank_id(scope, _banks.Kind.PERFORMANCE),
                            number,
                        )
                    except Exception:
                        fav = None
                    if fav is not None:
                        favourite = True
                        rating = int(fav.rating or 0)
                rows.append(PerformanceRow(bank.sysex, number, name, favourite, rating))
        return rows

    def _perf_chosen(self, index: int, choice: Optional[Tuple[int, int]]) -> None:
        if choice is None:
            return
        from dataclasses import replace as _replace
        from kws import banks as _banks

        bank_sysex, number = choice
        part = self._ms().parts[index]
        new_part = _replace(part, bank=bank_sysex, performance=number)
        if bank_sysex == _banks.bank("CARD").sysex:
            # The slot alone says nothing about which HCard bank was meant;
            # keep the active/viewed bank so the multi catalog can warn
            # after a swap. Bare "CARD" is the honest unknown.
            scope = self._card_scope
            self._desired_scopes[index] = scope if scope and scope != "CARD" else "CARD"
        else:
            self._desired_scopes.pop(index, None)
        self._set_part(index, new_part)
        self._status(f"channel {index + 1:02d}: {_bank_label(bank_sysex)} {number:02d}")

    def _edit_perf(self, index: int) -> None:
        part = self._ms().parts[index]
        self.app.push_screen(
            PerformancePickerScreen(
                self._performance_rows(),
                title=f"Performance for channel {index + 1:02d}",
                current=(part.bank, part.performance) if part.used else None,
            ),
            lambda choice: self._perf_chosen(index, choice),
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
            scope = self._parse_scope(raw)
            if scope is None:
                self._status(f"bank must be 0-11 or CARD/C, not {text!r}")
                return
            from dataclasses import replace as _replace
            from kws import banks as _banks

            if scope.startswith("CARD"):
                bank_sysex = _banks.bank("CARD").sysex
                self._desired_scopes[idx] = scope
            else:
                bank_sysex = _banks.bank(scope).sysex
                self._desired_scopes.pop(idx, None)
            new_part = _replace(part, bank=bank_sysex)
            self._set_part(idx, new_part)
            self._staged_plan = None
            self._status(f"bank set to {scope}")

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

    def action_stage(self) -> None:
        from kws import banks as _banks
        from kws import multistage as _ms_mod
        from kws import relations as _rels

        ms = self._ms()
        desired: List[Any] = []
        for i, part in enumerate(list(ms.parts)[:16]):
            if not getattr(part, "used", False):
                perf = None
            else:
                scope = self._intended_card_scope(i)
                if scope is None:
                    try:
                        scope = _banks.by_sysex(part.bank).id
                    except LookupError:
                        scope = None
                perf = None if scope is None else _rels.PerfId(scope, part.performance)
            desired.append(
                _ms_mod.DesiredChannel(
                    channel=i + 1,
                    perf=perf,
                    level=getattr(part, "level", 127),
                    flag=getattr(part, "flag", 1),
                )
            )
        try:
            plan = _ms_mod.plan_multi_staging(
                ms,
                desired,
                self._sources,
                target_banks=("RAM3",),
                mode="append",
                resident_card_scope=self._resident_scope(),
                stage_all_cards=True,
            )
            if not plan.fits:
                plan2 = _ms_mod.plan_multi_staging(
                    ms,
                    desired,
                    self._sources,
                    target_banks=("RAM1", "RAM2", "RAM3"),
                    mode="append",
                    resident_card_scope=self._resident_scope(),
                    stage_all_cards=True,
                )
                if plan2.fits:
                    plan = plan2
        except (LookupError, ValueError) as exc:
            self._status(f"staging: {exc}", refused=True)
            return
        if not plan.fits:
            reasons = "; ".join(plan.rejected) if plan.rejected else "does not fit"
            self._status(f"staging: {reasons}", refused=True)
            return
        self._staged_plan = plan
        self._multisets_copy[self._selected] = plan.updated_multiset
        self._populate()
        self._status(self._stage_summary(plan))

    def _stage_summary(self, plan: Any) -> str:
        n = sum(1 for c in plan.channels if getattr(c, "staged", False))
        if n == 0:
            return "nothing to stage; press a then w to write"
        banks_labels = sorted(
            {
                str(c.target_bank.label)
                for c in plan.channels
                if getattr(c, "staged", False) and c.target_bank is not None
            }
        )
        banks_text = ", ".join(banks_labels)
        slots: List[int] = []
        for c in plan.channels:
            if getattr(c, "staged", False):
                slots.extend(list(getattr(c, "target_patch_slots", ()) or ()))
        if slots:
            lo = min(slots)
            hi = max(slots)
            return (
                f"staged {n} card perf(s) into {banks_text} "
                f"(patches {lo:02d}..{hi:02d}); press a then w to write"
            )
        return f"staged {n} card perf(s) into {banks_text}; press a then w to write"

    def action_arm(self) -> None:
        # Staging is a separate, explicit step (`s`). Arming must not write
        # a RAM bank as a side effect: the picker now records card scopes,
        # and auto-staging on arm silently rewrote Multi channels into RAM3.
        self._armed = True
        if self._staged_plan is not None:
            self._status(
                "ARMED - w writes the staged bank(s) and the Multi Mode Setup. "
                "Nothing sent yet."
            )
        else:
            self._status(
                "ARMED - w writes the Multi Mode Setup only; s stages its "
                "card performances into RAM first. Nothing sent yet."
            )

    def action_fire(self) -> None:
        if not self._armed:
            self._status("not armed - press a first", refused=True)
            return
        if self._is_mixed_cards():
            self._confirm_mixed_write()
            return
        self._write_now()

    def _confirm_mixed_write(self) -> None:
        by_scope: Dict[str, List[int]] = {}
        for channel, scope in self._intended_card_scopes().items():
            by_scope.setdefault(scope, []).append(channel + 1)
        scope_lines = [
            f"  {self._scope_label(scope)}: "
            + ", ".join(f"{channel:02d}" for channel in sorted(channels))
            for scope, channels in sorted(by_scope.items())
        ]
        active = self._card_scope or "CARD"
        active_label = self._scope_label(active) if active != "CARD" else "unknown"
        prompt = "\n".join(
            [
                "MIXED CARD REFERENCES",
                "",
                "This Multiset uses performances from:",
                *scope_lines,
                "",
                "The Wavestation has one CARD slot and stores",
                "only performance numbers, not the card.",
                f"All card channels will use the active card ({active_label}).",
                "",
                "Press s first to stage them into RAM and keep",
                "this Multiset self-contained.",
                "",
                "Write to the active card anyway?",
            ]
        )
        self.app.push_screen(
            ConfirmScreen(
                prompt,
                confirm="Write anyway",
                cancel="Cancel",
                danger=True,
            ),
            self._mixed_confirmed,
        )

    def _mixed_confirmed(self, confirmed: Optional[bool]) -> None:
        if not confirmed:
            self._armed = False
            self._status(
                "write cancelled — s stages the card performances into RAM; "
                "a then w writes to the active card anyway"
            )
            return
        self._armed = True
        self._write_now()

    def _write_now(self) -> None:  # noqa: C901
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
            if self._staged_plan is not None:
                app._multi_edit_write_worker(
                    list(self._multisets_copy),
                    self._device,
                    self._backup_dir,
                    self,
                    self._staged_plan,
                    snapshot=self._snapshot(),
                )
            else:
                app._multi_edit_write_worker(
                    list(self._multisets_copy),
                    self._device,
                    self._backup_dir,
                    self,
                    snapshot=self._snapshot(),
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

    def action_record(self) -> None:
        """Record what this Multiset points at, without writing anything.

        The live multi catalog is what lets a later load say "this expects
        Card 39, not the card that is in the slot". A Multiset written by
        kwsed is recorded automatically; this covers the ones that already
        exist on the unit, and records exactly the state on screen.
        """
        app = self.app
        if not hasattr(app, "_record_multi_snapshot"):
            self._status("this context cannot record the multi catalog")
            return
        snapshot = self._snapshot()
        app._record_multi_snapshot(snapshot)
        # A plain `r` records the screen, not the device. Re-run the check
        # against what was last read so a mixed-card record shows its warning
        # immediately rather than only after the next m/C.
        if hasattr(app, "_refresh_multi_checks"):
            app._refresh_multi_checks()
        self._populate()
        self._status(
            f"recorded Multi {snapshot.number:02d}: "
            f"{len(snapshot.channels)} channel(s), nothing sent"
        )

    def action_edit_fx(self) -> None:
        """Open the FX block of the selected Multiset."""
        if self._on_edit_fx is None:
            self._status("FX editing is not available here", refused=True)
            return
        self._on_edit_fx(self._ms())

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
            written = list(self._multisets_copy)
            if hasattr(app, "_multi_sets"):
                app._multi_sets = written
            if hasattr(app, "_multisets"):
                app._multisets = written
            if hasattr(app, "_record_multi_snapshot"):
                # The screen holds the state that was written (after staging,
                # if any). Pass the written setups so the check runs against
                # them rather than the previous read.
                app._record_multi_snapshot(self._snapshot(), written)
            if hasattr(app, "_after_multi_write"):
                app._after_multi_write()
        except Exception:  # noqa: S110
            pass
        self.dismiss(result if isinstance(result, dict) else {"result": result})

    def action_close(self) -> None:
        self.dismiss(None)


class FxScreen(ModalScreen[None]):
    """Editable view of one performance's (or Multiset's) FX block."""

    DEFAULT_CSS = """
    FxScreen { align: center middle; }
    FxScreen > Vertical {
        width: 84; height: auto; border: thick $accent;
        background: $surface; padding: 1 2;
    }
    FxScreen DataTable { height: auto; max-height: 30; }
    """

    BINDINGS = [
        Binding("escape", "close", "Cancel"),
        Binding("q", "close", "Cancel"),
        Binding("enter", "edit", "Edit", priority=True),
        Binding("plus", "bump(1)", "Nudge +", show=False),
        Binding("equals_sign", "bump(1)", "Nudge +", show=False),
        Binding("minus", "bump(-1)", "Nudge -", show=False),
        Binding("a", "arm", "Arm", show=False),
        Binding("w", "fire", "Write", show=False),
    ]

    def __init__(
        self,
        block: FxBlock,
        mode: Optional[int] = None,
        on_write: Optional[Callable[[FxBlock, "FxScreen"], Any]] = None,
        *,
        gate_armed: Optional[bool] = None,
        gate: Any = None,
        subject: str = "performance",
        control_channel: Optional[int] = None,
    ) -> None:
        super().__init__()
        self._block = block
        self._mode = mode
        self._on_write = on_write
        self._armed = False
        self._gate_armed = gate_armed
        self._gate = gate
        self._subject = subject
        self._control_channel = control_channel
        self._pending_type: Optional[Tuple[int, int]] = None
        self._rows: list[tuple[Any, ...]] = self._descriptors(block)
        self.rows = self._rows
        self.row_descriptors = self._rows
        self.descriptors = self._rows

    def _descriptors(self, block: FxBlock) -> list[tuple[Any, ...]]:
        rows: list[tuple[Any, ...]] = []
        if self._control_channel is not None:
            rows.append(("control", None, "control"))
        rows.append(("routing", None, "series"))
        rows.append(("type", 0, "type"))
        for param in block.fx1.rows():
            rows.append(("param", 0, param))
        rows.append(("type", 1, "type"))
        for param in block.fx2.rows():
            rows.append(("param", 1, param))
        rows.append(("mix", None, "mix"))
        rows.append(("mod_source", None, "mod_source"))
        rows.append(("mod_amount", None, "mod_amount"))
        return rows

    def _is_gate_armed(self) -> bool:
        if self._gate is not None:
            try:
                return bool(self._gate.armed)
            except Exception:  # noqa: S110
                pass
        if self._gate_armed is not None:
            return bool(self._gate_armed)
        try:
            app = self.app
            wg = getattr(app, "_write_gate", None)
            if wg is not None:
                return bool(wg.armed)
        except Exception:  # noqa: S110
            pass
        return False

    def _gate_text(self) -> str:
        if self._gate is not None:
            try:
                return str(self._gate.describe())
            except Exception:  # noqa: S110
                pass
        return "write gate ARMED" if self._is_gate_armed() else "write gate locked"

    def mode_banner(self) -> str:
        if self._mode == 0:
            return "performance FX are heard (OMNI/POLY)"
        if self._mode == 1:
            return "the Multiset's FX are heard (MULTI/MONO)"
        return "MIDI mode unknown"

    def _title_text(self) -> str:
        return f"FX - {self._subject} ({self._gate_text()})"

    def _status(self, message: str) -> None:
        try:
            self.query_one("#fx-state", Static).update(str(message))
        except Exception:  # noqa: S110
            pass

    def write_failed(self, message: str) -> None:
        try:
            self.query_one("#fx-state", Static).update(str(message))
        except Exception:  # noqa: S110
            pass

    def write_done(self, block: Any = None) -> None:
        if block is not None:
            self._block = block
            self._rows = self._descriptors(self._block)
            self.rows = self._rows
            self.row_descriptors = self._rows
            self.descriptors = self._rows
        self.dismiss(None)

    def _write_failed(self, message: str) -> None:
        self.write_failed(message)

    def _write_succeeded(self, result: Any = None) -> None:
        self.write_done(self._block)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._title_text(), id="fx-title")
            yield Static(self.mode_banner(), id="fx-mode-banner")
            yield DataTable(id="fx-table", cursor_type="row")
            yield Static("", id="fx-state")
            yield Static(
                "a arm · w write · +/- nudge · enter edit · esc/q close",
                id="fx-footer",
            )

    def on_mount(self) -> None:
        self._draw_table()
        try:
            self.query_one("#fx-table", DataTable).focus()
        except Exception:  # noqa: S110
            pass

    def _draw_table(self) -> None:
        table = self.query_one("#fx-table", DataTable)
        try:
            coord = table.cursor_coordinate
            row = int(coord.row)
            column = int(coord.column)
        except Exception:
            row = 0
            column = 0
        table.clear()
        try:
            table.add_column("param", key="param")
            table.add_column("value", key="value")
        except Exception:  # noqa: S110
            pass
        for i, desc in enumerate(self._rows):
            table.add_row(self._label(desc), self._value(desc), key=str(i))
        if table.row_count:
            row = max(0, min(row, table.row_count - 1))
            column = max(0, min(column, len(table.columns) - 1))
            table.move_cursor(row=row, column=column)

    def _label(self, desc: Any) -> str:
        kind = desc[0]
        fx_index = desc[1]
        if kind == "control":
            return "FX control channel"
        if kind == "routing":
            return "routing"
        if kind == "type":
            return "FX 1" if fx_index == 0 else "FX 2"
        if kind == "param":
            param = desc[2]
            return str(param.label)
        if kind == "mix":
            return "Mix"
        if kind == "mod_source":
            return "Mod source"
        if kind == "mod_amount":
            return "Mod amount"
        return str(kind)

    def _value(self, desc: Any) -> str:
        kind = desc[0]
        fx_index = desc[1]
        block = self._block
        if kind == "control":
            from kws.multi import control_channel_text

            return control_channel_text(self._control_channel or 0)
        if kind == "routing":
            return "series" if block.series else "parallel"
        if kind == "type":
            effect = block.fx1 if fx_index == 0 else block.fx2
            return effect.name
        if kind == "param":
            param = desc[2]
            effect = block.fx1 if fx_index == 0 else block.fx2
            try:
                v = effect.display(param)
            except ValueError:
                return "?"
            return str(v)
        if kind == "mix":
            return f"{block.mix[0]} / {block.mix[1]}"
        if kind == "mod_source":
            return f"{block.mod_source[0]} / {block.mod_source[1]}"
        if kind == "mod_amount":
            return f"{block.mod_amount[0]} / {block.mod_amount[1]}"
        return ""

    def _effect(self, fx_index: int) -> Any:
        return self._block.fx1 if fx_index == 0 else self._block.fx2

    def _current_desc(self) -> Optional[Any]:
        try:
            table = self.query_one("#fx-table", DataTable)
        except Exception:
            return None
        if not table.row_count:
            return None
        index = table.cursor_row
        if not (0 <= index < len(self._rows)):
            return None
        return self._rows[index]

    def _rebuild(self) -> None:
        self._rows = self._descriptors(self._block)
        self.rows = self._rows
        self.row_descriptors = self._rows
        self.descriptors = self._rows
        self._draw_table()
        try:
            self.query_one("#fx-title", Label).update(self._title_text())
        except Exception:  # noqa: S110
            pass

    def _apply_block(self, new_block: FxBlock) -> None:
        self._block = new_block
        self._rebuild()

    def action_edit(self) -> None:
        desc = self._current_desc()
        if desc is None:
            return
        kind = desc[0]
        fx_index = desc[1]
        if kind == "type":
            self._choose_type(fx_index)
        elif kind == "param":
            param = desc[2]
            effect = self._effect(fx_index)
            if getattr(param, "names", None) is not None:
                self._choose_enum(fx_index, param)
            else:
                current = ""
                try:
                    current = str(effect.display(param))
                except ValueError:
                    current = ""
                self._prompt_param(fx_index, param, current)
        elif kind == "routing":
            self._choose_routing()
        elif kind == "mix":
            self._prompt_two("Mix", self._block.mix, 0, 15)
        elif kind == "mod_source":
            self._prompt_two("Mod source", self._block.mod_source, 0, 15)
        elif kind == "mod_amount":
            self._prompt_two("Mod amount", self._block.mod_amount, -15, 15)

    def action_enter(self) -> None:
        self.action_edit()

    def _choose_type(self, fx_index: int) -> None:
        effect = self._effect(fx_index)
        names = [t.name for t in EFFECT_TYPES]
        current = effect.type
        if current < 0 or current >= len(names):
            current = 0

        def picked(index: Optional[int]) -> None:
            if index is None:
                return
            if index == effect.type:
                self._status("the effect type is unchanged")
                return
            self._pending_type = (fx_index, index)
            self.app.push_screen(
                ConfirmScreen(FX_TYPE_WARNING),
                self._type_confirmed,
            )

        self.app.push_screen(ChoiceScreen("FX type", names, current), picked)

    def _type_confirmed(self, confirmed: Optional[bool]) -> None:
        pending = self._pending_type
        self._pending_type = None
        if not confirmed or pending is None:
            return
        fx_index, new_type = pending
        effect = self._effect(fx_index).with_type(new_type)
        import dataclasses

        if fx_index == 0:
            new_block = dataclasses.replace(self._block, fx1=effect)
        else:
            new_block = dataclasses.replace(self._block, fx2=effect)
        self._apply_block(new_block)

    def _choose_enum(self, fx_index: int, param: Any) -> None:
        names = list(getattr(param, "names", None) or ())
        effect = self._effect(fx_index)
        try:
            current_val = effect.display(param)
        except ValueError:
            current_val = names[0] if names else ""
        current = names.index(current_val) if current_val in names else 0

        def picked(index: Optional[int]) -> None:
            if index is None:
                return
            if not (0 <= index < len(names)):
                return
            self._apply_param(fx_index, param, names[index])

        self.app.push_screen(ChoiceScreen(param.label, names, current), picked)

    def _prompt_param(self, fx_index: int, param: Any, current: str) -> None:
        low = getattr(param, "low", 0)
        high = getattr(param, "high", 0)

        def done(text: Optional[str]) -> None:
            if text is None:
                return
            text = text.strip()
            if not text:
                return
            try:
                value = int(text)
            except ValueError:
                self.write_failed(f"{text!r} is not a number")
                return
            self._apply_param(fx_index, param, value)

        self.app.push_screen(
            PromptScreen(f"{param.label} ({low} to {high})", current), done
        )

    def _apply_param(self, fx_index: int, param: Any, value: Any) -> None:
        if isinstance(value, str) and getattr(param, "names", None) is None:
            try:
                value = int(value.strip())
            except ValueError:
                pass
        effect = self._effect(fx_index)
        try:
            updated = effect.with_value(param, value)
        except Exception as exc:
            self.write_failed(str(exc))
            return
        import dataclasses

        if fx_index == 0:
            new_block = dataclasses.replace(self._block, fx1=updated)
        else:
            new_block = dataclasses.replace(self._block, fx2=updated)
        self._apply_block(new_block)

    def _choose_routing(self) -> None:
        names = ["Parallel", "Series"]
        current = 1 if self._block.series else 0

        def picked(index: Optional[int]) -> None:
            if index is None:
                return
            import dataclasses

            new_block = dataclasses.replace(self._block, series=(index == 1))
            self._apply_block(new_block)

        self.app.push_screen(ChoiceScreen("routing", names, current), picked)

    def _prompt_two(
        self, label: str, current: Tuple[int, int], low: int, high: int
    ) -> None:
        current_text = f"{current[0]} / {current[1]}"

        def done(text: Optional[str]) -> None:
            if text is None:
                return
            text = text.strip()
            if not text:
                return
            parts = text.replace("/", " ").split()
            if len(parts) != 2:
                self.write_failed("expected two numbers: a / b")
                return
            try:
                a = int(parts[0])
                b = int(parts[1])
            except ValueError:
                self.write_failed(f"{text!r} is not two numbers")
                return
            if not (low <= a <= high and low <= b <= high):
                self.write_failed(f"{label} takes {low} to {high}")
                return
            import dataclasses

            if label == "Mix":
                new_block = dataclasses.replace(self._block, mix=(a, b))
            elif label == "Mod source":
                new_block = dataclasses.replace(self._block, mod_source=(a, b))
            else:
                new_block = dataclasses.replace(self._block, mod_amount=(a, b))
            self._apply_block(new_block)

        self.app.push_screen(
            PromptScreen(f"{label} ({low} to {high} / {low} to {high})", current_text),
            done,
        )

    def action_bump(self, delta: int) -> None:  # noqa: C901
        desc = self._current_desc()
        if desc is None:
            return
        kind = desc[0]
        fx_index = desc[1]
        if kind == "type":
            self._status("the effect type is a list of names")
            return
        if kind == "param":
            param = desc[2]
            effect = self._effect(fx_index)
            try:
                current = effect.display(param)
            except ValueError:
                self.write_failed("value out of range")
                return
            names = getattr(param, "names", None)
            if names is not None:
                try:
                    i = names.index(current)
                except ValueError:
                    self.write_failed("value out of range")
                    return
                j = i + int(delta)
                if not (0 <= j < len(names)):
                    self._status(f"{param.label} has no value there")
                    return
                self._apply_param(fx_index, param, names[j])
                return
            try:
                self._apply_param(fx_index, param, int(current) + int(delta))
            except Exception:
                self._status(f"{param.label} out of range")
            return
        if kind == "routing":
            import dataclasses

            self._apply_block(
                dataclasses.replace(self._block, series=not self._block.series)
            )
            return
        if kind in ("mix", "mod_source", "mod_amount"):
            field = (
                "mix"
                if kind == "mix"
                else ("mod_source" if kind == "mod_source" else "mod_amount")
            )
            cur = getattr(self._block, field)
            low, high = (-15, 15) if kind == "mod_amount" else (0, 15)
            new_val = cur[0] + int(delta)
            if not (low <= new_val <= high):
                self._status(f"{kind} out of range")
                return
            import dataclasses

            self._apply_block(
                dataclasses.replace(self._block, **{field: (new_val, cur[1])})  # type: ignore[arg-type]
            )
            return

    def action_arm(self) -> None:
        if not self._is_gate_armed():
            self.write_failed("gate locked")
            return
        self._armed = True
        self._status("armed")

    def action_fire(self) -> None:
        if not self._armed:
            self._status("not armed - press a first")
            return
        self._armed = False
        if self._on_write is None:
            self.write_failed("no write handler")
            return
        try:
            self._on_write(self._block, self)
        except Exception as exc:
            self.write_failed(str(exc))

    def _find_desc_index(self, target: Any) -> Optional[int]:
        if isinstance(target, int):
            if 0 <= target < len(self._rows):
                return target
            return None
        if isinstance(target, tuple):
            for i, d in enumerate(self._rows):
                if d == target:
                    return i
        return None

    def _apply_to_desc(self, desc: Any, value: Any) -> None:  # noqa: C901
        kind = desc[0]
        fx_index = desc[1]
        if kind == "type":
            names = [t.name for t in EFFECT_TYPES]
            if isinstance(value, str):
                if value not in names:
                    self.write_failed(f"unknown type {value!r}")
                    return
                value = names.index(value)
            if not isinstance(value, int):
                self.write_failed("type takes an index or name")
                return
            effect = self._effect(fx_index).with_type(int(value))
            import dataclasses

            if fx_index == 0:
                self._apply_block(dataclasses.replace(self._block, fx1=effect))
            else:
                self._apply_block(dataclasses.replace(self._block, fx2=effect))
            return
        if kind == "param":
            param = desc[2]
            self._apply_param(fx_index, param, value)
            return
        if kind == "routing":
            if isinstance(value, str):
                series = value.strip().lower().startswith("s")
            else:
                series = bool(value)
            import dataclasses

            self._apply_block(dataclasses.replace(self._block, series=series))
            return
        if kind in ("mix", "mod_source", "mod_amount"):
            field = (
                "mix"
                if kind == "mix"
                else ("mod_source" if kind == "mod_source" else "mod_amount")
            )
            if isinstance(value, str):
                parts = value.replace("/", " ").split()
                if len(parts) != 2:
                    self.write_failed("expected two numbers: a / b")
                    return
                try:
                    a = int(parts[0])
                    b = int(parts[1])
                except ValueError:
                    self.write_failed(f"{value!r} is not two numbers")
                    return
                value = (a, b)
            if not (isinstance(value, (tuple, list)) and len(value) == 2):
                self.write_failed("expected two numbers: a / b")
                return
            a, b = int(value[0]), int(value[1])
            low, high = (-15, 15) if kind == "mod_amount" else (0, 15)
            if not (low <= a <= high and low <= b <= high):
                self.write_failed(f"{kind} takes {low} to {high}")
                return
            import dataclasses

            self._apply_block(
                dataclasses.replace(self._block, **{field: (a, b)})  # type: ignore[arg-type]
            )
            return

    def edit(self, *args: Any, **kwargs: Any) -> None:  # noqa: C901
        index: Optional[int] = None
        value: Any = None
        if "index" in kwargs or "row" in kwargs:
            index = kwargs.get("index", kwargs.get("row"))
        if "value" in kwargs or "v" in kwargs:
            value = kwargs.get("value", kwargs.get("v"))
        if len(args) == 1:
            if value is None and index is None:
                value = args[0]
            elif index is None:
                index = args[0]
        elif len(args) >= 2:
            first, second = args[0], args[1]
            if isinstance(first, int) and (isinstance(second, (int, str, tuple, list))):
                if 0 <= first < len(self._rows):
                    index = first
                    value = second
                else:
                    value = first
            else:
                idx = self._find_desc_index(first)
                if idx is not None:
                    index = idx
                    value = second
                else:
                    value = first
        if index is None:
            index = (
                self._find_desc_index(kwargs.get("descriptor"))
                if "descriptor" in kwargs
                else None
            )
        if index is None:
            try:
                index = self.query_one("#fx-table", DataTable).cursor_row
            except Exception:
                index = 0
        if index is None or not (0 <= index < len(self._rows)):
            index = 0
        if value is None:
            return
        desc = self._rows[index]
        self._apply_to_desc(desc, value)

    def edit_value(self, *args: Any, **kwargs: Any) -> None:
        self.edit(*args, **kwargs)

    def apply_value(self, *args: Any, **kwargs: Any) -> None:
        self.edit(*args, **kwargs)

    def set_value(self, *args: Any, **kwargs: Any) -> None:
        self.edit(*args, **kwargs)

    def edit_selected(self, *args: Any, **kwargs: Any) -> None:
        self.edit(*args, **kwargs)

    def apply(self, *args: Any, **kwargs: Any) -> None:
        self.edit(*args, **kwargs)

    def update_value(self, *args: Any, **kwargs: Any) -> None:
        self.edit(*args, **kwargs)

    def on_edit(self, *args: Any, **kwargs: Any) -> None:
        self.edit(*args, **kwargs)

    def edit_row(self, *args: Any, **kwargs: Any) -> None:
        self.edit(*args, **kwargs)

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


class BankCopyScreen(ModalScreen[Optional[Dict[str, Any]]]):
    """Copy one whole bank into another. **Destructive.**

    A sibling of :class:`SendScreen`, with the same three deliberate steps,
    because a whole-bank copy replaces all fifty performances, all
    thirty-five patches and the wave-sequence pool at once:

    1. The destination's **current names** are shown, because "Card" means
       nothing and the names it is about to replace mean everything.
    2. **Arm.** Nothing has been sent yet.
    3. **Fire**, a different key, only while armed.

    Opening and arming send nothing, and the names come from the cache --
    no device read happens here. The write itself is one bundle through the
    bridge's fence: the destination is backed up first and every kind is
    read back and verified. The window also offers the unit's own MOVE as a
    method, still backed up and verified here.
    """

    METHODS = ("fence", "device")

    DEFAULT_CSS = """
    BankCopyScreen { align: center middle; }
    BankCopyScreen > Vertical {
        width: 80; height: auto; border: thick $error;
        background: $surface; padding: 1 2;
    }
    BankCopyScreen .armed { color: $error; text-style: bold; }
    BankCopyScreen .what { height: auto; }
    """

    BINDINGS = [
        Binding("escape", "close", "Cancel"),
        Binding("q", "close", "Cancel"),
        Binding("m", "method", "Method", show=False),
        Binding("a", "arm", "Arm", show=False),
        # Deliberately not Enter, and deliberately not beside "a": the fire
        # key should not be the one a finger is already resting on.
        Binding("w", "fire", "Write", show=False),
    ]

    def __init__(self, app: Any, source: banks.Bank, destination: banks.Bank) -> None:
        super().__init__()
        self._app = app
        self._source = source
        self._destination = destination
        self._armed = False
        self._method = "fence"

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("[b]Copy a whole bank[/b]")
            yield Static(self._describe(), classes="what")
            yield Static("", id="bank-copy-state")
            yield Static(
                "[dim]a to arm, m for method, then w to write. esc cancels.[/dim]"
            )

    def _method_description(self) -> str:
        """The method line, aligned with the rest of the description."""
        if self._method == "device":
            return "the unit's MOVE: backed up and verified here"
        return "kwsed: read, remap, write, verify"

    def _method_short(self) -> str:
        return "the unit's MOVE" if self._method == "device" else "kwsed's fence"

    def _method_sentence(self) -> str:
        if self._method == "device":
            return "device MOVE — the unit copies, kwsed backs up and verifies"
        return "kwsed fence — reads, remaps and writes"

    def _describe(self) -> str:
        lines = [
            f"source      {self._source.label}",
            f"destination {self._destination.label}  "
            f"({'writable' if self._destination.writable else 'ROM'})",
            "kind        all three: wave sequences, patches, performances",
            f"method      {self._method_description()}",
            "backup      the destination's current content is saved first "
            "(one envelope)",
        ]
        source = self._app._sources_with_store().get(self._destination.id)
        if source is None:
            lines.append("replacing   (names not read)")
            return "\n".join(lines)
        # Occupied slots only: an empty record decodes to blanks and would
        # bury the names that actually matter. Performances and patches are
        # numbered in their own banks, so they get their own lines rather
        # than one run-on list where patch 0 would read as performance 50.
        perfs = [
            (i, record) for i, record in enumerate(source.performances) if any(record)
        ]
        patches = [
            (i, record) for i, record in enumerate(source.patches) if any(record)
        ]
        if perfs:
            shown = ", ".join(
                f"{i:03d} {m.decode_name(record)}" for i, record in perfs[:5]
            )
            lines.append(f"replacing   perf  {shown}{' ...' if len(perfs) > 5 else ''}")
        if patches:
            shown = ", ".join(
                f"{i:03d} {m.decode_name(record)}" for i, record in patches[:3]
            )
            lines.append(
                f"            patch {shown}{' ...' if len(patches) > 3 else ''}"
            )
        if not perfs and not patches:
            lines.append("replacing   (destination is empty)")
        return "\n".join(lines)

    def action_method(self) -> None:
        """Switch between kwsed's own copy and the unit's own MOVE."""
        self._method = "device" if self._method == "fence" else "fence"
        state = self.query_one("#bank-copy-state", Static)
        if self._armed:
            # The armed state is a promise about what `w` will do, and the
            # method just changed what that is.
            self._armed = False
            state.update(
                f"[b]disarmed[/b] - the method changed to {self._method_short()}. "
                "Press a to arm again."
            )
            return
        state.update(f"method: {self._method_sentence()}")

    def action_arm(self) -> None:
        self._armed = True
        self.query_one("#bank-copy-state", Static).update(
            f"[b]ARMED[/b] - w replaces {self._destination.label} with "
            f"{self._source.label} via {self._method_short()}. "
            "Nothing has been sent yet."
        )

    def action_fire(self) -> None:
        """Only while armed, and only once."""
        if not self._armed:
            self.query_one("#bank-copy-state", Static).update(
                "[dim]not armed - press a first[/dim]"
            )
            return
        self._armed = False
        # A failed write must not dismiss: the screen is the only feedback
        # the user gets, and the bridge already reported the backup's
        # read-back mismatch. The worker's done callback leaves it open.
        self._app._copy_bank_write(self._source, self._destination, self, self._method)

    def _write_failed(self, message: str) -> None:
        self.query_one("#bank-copy-state", Static).update(
            f"[b]write failed[/b] {message}"
        )

    def _write_done(self, info: Dict[str, Any]) -> None:
        self.dismiss(info)

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

_COPY_HELP = """\
Copy a Performance and the patches it uses into a writable bank.

  b   choose the destination bank: RAM 1, RAM 2, RAM 3 or the Card.
  t   choose an occupied destination slot to overwrite.
  m   choose the copy mode:
        append  keep the destination's performances and add this one;
                the destination must still fit the performance and its patches.
        clean   replace the whole destination bank with this performance.
  a   arm the copy; nothing is written yet.
  w   write the armed copy through the read, backup and verify fence.
  z   after a write, the browser's undo restores the destination.
  esc / q   cancel without writing.

The preview below shows the destination, the mode, the planned performance
slot and patch slots, and the reasons a copy cannot fit. In append mode a
destination that is already full can refuse the new performance; switch to
clean or choose another bank.
"""


class CopyPerformanceScreen(ModalScreen[Optional[Dict[str, Any]]]):
    DEFAULT_CSS = """
    CopyPerformanceScreen { align: center middle; }
    CopyPerformanceScreen > Vertical {
        width: 84; height: auto; border: thick $accent;
        background: $surface; padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Cancel", show=False),
        Binding("q", "close", "Cancel", show=False),
        Binding("b", "bank", "Bank", show=False),
        Binding("t", "target", "Target", show=False),
        Binding("m", "mode", "Mode", show=False),
        Binding("a", "arm", "Arm", show=False),
        Binding("w", "fire", "Write", show=False),
        Binding("question_mark", "help", "Help", show=False),
    ]

    def __init__(self, app: Any, perf: relations.PerfId, source_name: str) -> None:
        super().__init__()
        self._app = app
        self.perf = perf
        self.source_name = source_name
        self._dest_index = 0
        self._modes = ("append", "clean")
        self._mode_index = 0
        self._target_slot: Optional[int] = None
        self._armed = False
        self._destinations = banks.writable_banks()
        self._loading: set[str] = set()
        self._load_error: Optional[str] = None

    def _destination(self) -> Any:
        return self._destinations[self._dest_index]

    def _mode(self) -> str:
        if self._target_slot is not None:
            return "replace"
        return self._modes[self._mode_index]

    def _skip_readonly_card(self) -> None:
        dest = self._destination()
        if (
            dest.label == "Card" or dest.id == "CARD"
        ) and self._app._active_card in range(0, 25):
            # refuse and advance
            self._app.notify_status(
                f"Card {self._app._active_card:02d} is a read-only HCard ROM bank; "
                "select a user bank (25-39) or RAM 1-3",
                refused=True,
            )
            self._dest_index = (self._dest_index + 1) % len(self._destinations)
            self._skip_readonly_card()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Copy Performance", id="copy-perf-title")
            yield Static("", id="copy-perf-state")
            yield Static(
                "[dim]b destination · t target · m mode · a arm · w write · "
                "? help · esc cancel[/dim]"
            )

    def on_mount(self) -> None:
        self._skip_readonly_card()
        self._ensure_context()

    def _missing_scopes(self) -> list[str]:
        app = self._app
        merged = app._sources_with_store()
        scopes: list[str] = []
        for sid in (self.perf.scope_id, self._destination().id):
            if sid not in merged and sid not in scopes:
                scopes.append(sid)
        return scopes

    def _ensure_context(self) -> None:
        missing = self._missing_scopes()
        if missing and not self._loading and self._load_error is None:
            self._loading = set(missing)
            self._app._copy_perf_load_worker(missing, self)
        self._refresh()

    def _context_loaded(self) -> None:
        self._loading.clear()
        self._load_error = None
        # A destination changed while this load was in flight may still be
        # missing; re-check rather than leaving the screen waiting forever.
        self._ensure_context()

    def _context_failed(self, message: str) -> None:
        self._loading.clear()
        self._load_error = message
        self._refresh()
        self._app.notify_status(message, refused=True)

    def _plan_preview(self) -> List[str]:
        """The destination, mode, and dry-run plan lines."""
        app = self._app
        dest = self._destination()
        mode = self._mode()
        mode_text = (
            f"overwrite slot {self._target_slot:02d}"
            if self._target_slot is not None
            else mode
        )
        lines: List[str] = [
            f"destination bank: {dest.label}",
            f"copy mode: {mode_text}",
            "b bank · t target slot · m append/clean",
        ]
        if self._load_error is not None:
            return lines + [f"cannot plan: {self._load_error}"]
        missing = self._missing_scopes()
        if missing:
            return lines + [f"waiting for {', '.join(sorted(missing))}..."]
        merged = app._sources_with_store()
        dest_src = merged.get(dest.id)
        if dest_src is not None:
            active = perfcopy.active_performances(dest_src)
            used_patches = sum(1 for patch in dest_src.patches if any(patch))
            lines.append(
                f"destination active performances: {len(active)}; "
                f"free performance slots: {50 - len(active)}; "
                f"patches used: {used_patches}/35"
            )
        if mode == "clean":
            return self._plan_preview_clean(lines, merged, dest)
        return self._plan_preview_slot(lines, merged, dest, mode)

    def _plan_preview_clean(
        self, lines: List[str], merged: Any, dest: Any
    ) -> List[str]:
        try:
            result = perfcopy.copy_into_bank(
                merged,
                dest.id,
                [self.perf],
                mode="clean",
                replace_slot=self._target_slot,
                destination_bank=dest,
            )
        except Exception as exc:
            return lines + [f"cannot copy: {exc}"]
        plan = result.plan
        if plan.rejected:
            reasons = "; ".join(plan.rejected)
            if len(plan.rejected) > 2:
                reasons = "; ".join(plan.rejected[:2]) + (
                    f"; ... ({len(plan.rejected) - 2} more)"
                )
            lines.append(f"cannot copy: {reasons}")
            return lines
        chosen = next(
            (
                alloc
                for alloc in reversed(plan.allocations)
                if alloc.source_perf == self.perf
            ),
            None,
        )
        if chosen is not None:
            lines.append(f"target Performance slot {chosen.target_perf_slot:02d}")
            slots = sorted(set(chosen.part_patch_slots.values()))
            if slots:
                lines.append(f"target Patch slots {slots[0]:02d}..{slots[-1]:02d}")
        lines.append(
            f"budget: Performances {len(plan.allocations)}/50, "
            f"Patches {plan.patch_count}/35, "
            f"Wave Seqs {len(plan.sequence_map)}/32, "
            f"Steps {plan.steps_used}/501"
        )
        return lines

    def _plan_preview_slot(
        self, lines: List[str], merged: Any, dest: Any, mode: str
    ) -> List[str]:
        target_src = merged.get(dest.id)
        if target_src is None:
            return lines + ["waiting for destination bank..."]
        if mode == "replace" and self._target_slot is not None:
            slot = int(self._target_slot)
        else:
            found = next(
                (
                    index
                    for index in range(50)
                    if perfcopy.is_empty_performance(target_src.performances[index])
                ),
                None,
            )
            if found is None:
                return lines + ["cannot copy: no free performance slot"]
            slot = int(found)
        try:
            slot_plan: Any = perfcopy.plan_slot_import(
                merged, dest.id, {slot: self.perf}, destination_bank=dest
            )
        except Exception as exc:
            return lines + [f"cannot copy: {exc}"]
        if slot_plan.rejected:
            reasons = "; ".join(slot_plan.rejected)
            if len(slot_plan.rejected) > 2:
                reasons = "; ".join(slot_plan.rejected[:2]) + (
                    f"; ... ({len(slot_plan.rejected) - 2} more)"
                )
            lines.append(f"cannot copy: {reasons}")
            return lines
        chosen = slot_plan.allocations[0] if slot_plan.allocations else None
        lines.append(f"target Performance slot {slot:02d}")
        if chosen is not None:
            slots = sorted(set(chosen.part_patch_slots.values()))
            if slots:
                lines.append(f"target Patch slots {slots[0]:02d}..{slots[-1]:02d}")
        lines.append(
            f"budget: Patches {slot_plan.patch_count}/35, "
            f"Wave Seqs {slot_plan.sequence_count}/32, "
            f"Steps {slot_plan.steps_used}/501"
        )
        return lines

    def _refresh(self) -> None:
        dest = self._destination()
        mode = self._mode()
        state_lines: List[str] = [
            f"source: {self.source_name or self.perf.scope_id}:{self.perf.number:02d}",
        ]
        state_lines.extend(self._plan_preview())
        if self._armed:
            state_lines.append("ARMED — press w to write")
        try:
            self.query_one("#copy-perf-state", Static).update("\n".join(state_lines))
            label_mode = (
                f"overwrite {self._target_slot:02d}"
                if self._target_slot is not None
                else mode
            )
            self.query_one("#copy-perf-title", Label).update(
                f"Copy Performance → {dest.label} ({label_mode})"
            )
        except Exception:  # noqa: S110
            pass

    def action_bank(self) -> None:
        choices = [bank.label for bank in self._destinations]
        self._app.push_screen(
            TargetBankScreen(
                "Destination bank",
                choices,
                self._dest_index,
                status_message=(
                    "The copy writes one whole bank. RAM 1-3 are writable; "
                    "the Card is writable only when it is a user card."
                ),
            ),
            self._bank_chosen,
        )

    def _bank_chosen(self, index: Optional[int]) -> None:
        if index is None:
            return
        self._dest_index = index
        self._target_slot = None
        self._armed = False
        self._load_error = None
        self._skip_readonly_card()
        self._ensure_context()

    def action_mode(self) -> None:
        self._app.push_screen(
            ChoiceScreen(
                "Copy mode",
                [
                    "append — keep destination performances, add this one",
                    "clean — replace the whole destination bank",
                ],
                self._mode_index,
            ),
            self._mode_chosen,
        )

    def _mode_chosen(self, index: Optional[int]) -> None:
        if index is None:
            return
        self._mode_index = index
        self._target_slot = None
        self._armed = False
        self._refresh()

    def action_target(self) -> None:
        dest = self._destination()
        merged = self._app._sources_with_store()
        dest_src = merged.get(dest.id)
        if dest_src is None or self._loading or self._missing_scopes():
            self._app.notify_status(
                "still reading the destination bank; try again in a moment",
                refused=True,
            )
            return
        self._app.push_screen(
            TargetSlotScreen(
                scope=dest.id,
                records=dest_src.performances,
                current=self._target_slot or 0,
            ),
            self._target_chosen,
        )

    def _target_chosen(self, slot: Optional[int]) -> None:
        if slot is None:
            return
        dest = self._destination()
        dest_src = self._app._sources_with_store().get(dest.id)
        if dest_src is None or not 0 <= slot < len(dest_src.performances):
            return
        record = dest_src.performances[slot]
        self._armed = False
        if perfcopy.is_empty_performance(record):
            first_free = next(
                (
                    index
                    for index, candidate in enumerate(dest_src.performances)
                    if perfcopy.is_empty_performance(candidate)
                ),
                None,
            )
            if first_free != slot:
                self._app.notify_status(
                    f"append uses the first free slot "
                    f"{first_free:02d}; choose that slot or use overwrite",
                    refused=True,
                )
                return
            self._target_slot = None
            self._mode_index = self._modes.index("append")
            self._app.notify_status(f"target slot {slot:02d} is free (append)")
        else:
            self._target_slot = slot
        self._refresh()

    def action_help(self) -> None:
        self._app.push_screen(ReportScreen("Copy Performance", _COPY_HELP))

    def action_arm(self) -> None:
        self._armed = True
        self._refresh()
        self._app.notify_status("armed")

    def action_fire(self) -> None:
        if not self._armed:
            self._app.notify_status("press a to arm first", refused=True)
            return
        if self._load_error is not None:
            self._app.notify_status(self._load_error, refused=True)
            return
        if self._loading or self._missing_scopes():
            self._app.notify_status(
                "still reading the banks; try again in a moment", refused=True
            )
            return
        dest = self._destination()
        mode = self._mode()
        self._app._copy_perf_write_worker(
            self.perf, dest, mode, self, replace_slot=self._target_slot
        )

    def action_close(self) -> None:
        self.dismiss(None)

    def _write_failed(self, msg: str) -> None:
        self._armed = False
        self._refresh()
        self._app.notify_status(msg, refused=True)


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
    #bank-table.stale-cursor > .datatable--cursor {
        background: #5c2626;
    }
    #slot-table { width: 1fr; }
    /* The dependency pane is a third column, hidden with `D`. It scrolls
       rather than truncates: the report can be longer than the screen. */
    #deps-pane {
        width: 1fr; min-width: 24; height: 1fr;
        border-left: solid $panel;
    }
    #deps-pane-body { height: auto; padding: 0 1; }
    DataTable { height: 1fr; }
    /* Fixed: `height: auto` under-counts a wrapped line in this Textual
       version, and the last line was clipped. Content is kept to two
       lines, so four gives a wrap of headroom. */
    #detail { height: 5; padding: 0 1; border-top: solid $panel; }
    #status {
        height: auto; max-height: 3; background: $boost; color: $text; padding: 0 1;
    }
    #progress {
        height: 1; background: $boost; color: $accent; padding: 0 1;
        display: none;
    }
    #status.warning {
        background: $error 30%;
    }
    """

    BINDINGS = [
        # Enter is NOT bound here: a focused DataTable consumes it for its
        # own row selection, so it arrives as RowSelected instead.
        Binding("r", "read_names", "Read names"),
        Binding("left_square_bracket", "channel_down", "Channel -"),
        Binding("right_square_bracket", "channel_up", "Channel +"),
        Binding("c", "pick_channel", "Set channel"),
        # Description matches the legend: `m` reads the Multisets and opens
        # them, so the hint names the object rather than its channel view.
        Binding("m", "multi", "Multi"),
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
        Binding("d", "show_deps", "Deps"),
        Binding("D", "toggle_deps_pane", "Deps pane"),
        Binding("p", "toggle_patches", "Patches"),
        Binding("y", "copy_performance", "Copy perf"),
        Binding("x", "edit_fx", "FX"),
        Binding("B", "copy_bank", "Copy bank"),
        Binding("I", "import", "Import"),
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
        bankstore_dir: Optional[str] = None,
        multisets_path: Optional[str] = None,
        auto_read_multi: bool = False,
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
        self._bankstore_dir = bankstore_dir
        #: The live multi catalog: what each Multiset pointed at when it was
        #: written, so a later load can say "this expects Card 39 / RAM1 05
        #: 'Warm Pad'". Separate from the static name catalog.
        self._multisets_path = multisets_path
        self._multi_catalog: Dict[int, Any] = (
            _multisets_mod.load(multisets_path) if multisets_path is not None else {}
        )
        self._multi_mismatches: Dict[int, List[Any]] = {}
        #: Read the 32 Multisets once at launch so the browser shows them
        #: without pressing m. Off in tests/embedders; main() turns it on.
        self._auto_read_multi_enabled = auto_read_multi
        #: Where write envelopes are saved. ``None`` means the platform's
        #: backup directory; tests and embedders point it at a temporary one
        #: so a run never drops envelopes into the user's real archive.
        self._backup_dir: Optional[str] = None
        self._import_folder = ""
        self._import_cursor = 0
        self._sources: Dict[str, deps.Source] = {}
        #: Whole-bank import pass state (plan R4 c), keyed by import
        #: session ("path|source scope"): which source slots earlier passes
        #: wrote into which target. A later `n` pass offers what is left.
        self._import_passes: Dict[str, Dict[str, Any]] = {}
        #: One-shot plan-approval nonces already spent (plan P3). A whole
        #: -bank write consumes its approval's nonce on the main thread
        #: before the device worker starts; a second use of the same
        #: approval is refused before anything touches the unit again.
        self._consumed_plan_nonces: Set[str] = set()
        self._active_physical = (
            _physical.active(physical_path) if physical_path else None
        )
        self._current_physical: Optional[str] = None
        self._current_physical_kind: Optional[str] = None
        self._lock = threading.RLock()
        #: Transfer progress for the bar under the status line. The hook is
        #: installed on the device so a dump crossing the wire has something
        #: to show, whether the call runs on a worker thread or not.
        self._progress_label = ""
        self._progress_value = 0.0
        self._progress_active = False
        self._progress_generation = 0
        self._progress_hold = 0
        #: Whether this unit takes a single-patch EXECUTE_WRITE. None until
        #: an audition has tried; False makes later auditions skip the
        #: attempt and go straight to the bank dump.
        self._preaudit_single_writes: Optional[bool] = None
        #: True while an audition's staging is on the unit and `z` can still
        #: take it back out; the import screen's exit says so.
        self._pending_staging = False
        self._quit_after_undo = False
        try:
            self.device.progress = self._progress_report
        except Exception:  # noqa: S110 - decoration, not protocol
            pass
        #: Whether the bank list also shows patch rows. Patches cannot be
        #: auditioned or copied on their own, so a viewer that only wants
        #: performances can hide them without losing any action. Remembered
        #: between runs when a settings path was given.
        if config_path is not None:
            from kws import config as _ui_cfg

            _ui_cfg.migrate_ui_state(config_path)
            self._show_patches = _ui_cfg.load_show_patches(config_path)
            self._deps_pane_visible = _ui_cfg.load_deps_pane(config_path)
            self._import_folder = _ui_cfg.load_import_folder(config_path)
        else:
            self._show_patches = True
            self._deps_pane_visible = True
        #: The merged sources and relation graph for the live pane, rebuilt
        #: only when ``_sources`` changes so cursor movement reads no disk.
        self._deps_sources_cache: Optional[Dict[str, deps.Source]] = None
        self._deps_sources_key: Any = None
        self._deps_graph: Any = None
        self._deps_graph_key: Any = None
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
        self._current: Sequence[SlotLike] = []
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

    def _remember_bank(
        self,
        scope_id: str,
        *,
        performances: Optional[bytes] = None,
        patches: Optional[bytes] = None,
        waveseq: Optional[bytes] = None,
    ) -> None:
        existing = self._sources.get(scope_id)
        perfs_list: List[bytes] = []
        patches_list: List[bytes] = []
        ws: Optional[bytes] = None
        if existing is not None:
            perfs_list = list(existing.performances)
            patches_list = list(existing.patches)
            ws = existing.waveseq
        if performances is not None:
            perfs_list = [
                performances[i * rec.PERFORMANCE_SIZE : (i + 1) * rec.PERFORMANCE_SIZE]
                for i in range(len(performances) // rec.PERFORMANCE_SIZE)
            ]
        if patches is not None:
            patches_list = [
                patches[i * rec.PATCH_SIZE : (i + 1) * rec.PATCH_SIZE]
                for i in range(len(patches) // rec.PATCH_SIZE)
            ]
        if waveseq is not None:
            ws = waveseq
        self._sources[scope_id] = deps.Source(
            scope_id=scope_id,
            performances=perfs_list,
            patches=patches_list,
            waveseq=ws,
        )
        if self._bankstore_dir is not None:
            try:
                bankstore.save(
                    scope_id,
                    performances=performances,
                    patches=patches,
                    waveseq=waveseq,
                    directory=self._bankstore_dir,
                )
            except OSError:
                pass

    @staticmethod
    def _waveseq_bytes(raw: bytes) -> Optional[bytes]:
        try:
            packet = m.parse(raw)
        except Exception:
            return None
        if packet is None:
            return None
        if getattr(packet, "function", None) != m.Function.ALL_WAVESEQ_DUMP:
            return None
        try:
            data = packet.data()
        except Exception:
            return None
        if data is None or len(data) < rec.WAVESEQ_BANK_SIZE:
            return None
        return bytes(data[: rec.WAVESEQ_BANK_SIZE])

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
                if self._record_bundle_write(payload):
                    return
                if self._record_composite_write(payload):
                    return
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
        # A dict, not a tuple: a bank label ("Card") and a kind ("patch") are
        # both strings, so a positional payload cannot say which is which.
        record = _undo.Record(
            label=label,
            payload={"kind": kind, "bank": destination, "data": original},
        )
        self._undo_log.push(record)
        if mode is not None:
            try:
                self._unit_mode = mode
            except Exception:  # noqa: S110
                pass

    @staticmethod
    def _decoded_backup_block(raw: Any) -> bytes:
        """A backup block as the plain bytes a write expects.

        A fence backup holds the framed System Exclusive as it arrived; a
        hand-saved envelope may hold the decoded records already. A frame
        is decoded with the parser, anything else is taken as it stands.
        Bytes that do not decode stay framed, and the write's own size
        check rejects what would otherwise be sent twice nibble-encoded.
        """
        data = bytes(raw)
        if not data.startswith(b"\xf0"):
            return data
        try:
            packet = m.parse(data)
        except Exception:
            return data
        if packet is None:
            return data
        try:
            return bytes(packet.data())
        except Exception:
            return data

    @staticmethod
    def _undo_ok(result: Any) -> bool:
        """A write's result as the undo log's success flag.

        ``None`` or a result without a verdict means the device had no
        read-back to offer; only an explicit ``verified: False`` keeps a
        record, so the next ``z`` retries a write it could not confirm.
        """
        return not (isinstance(result, dict) and result.get("verified") is False)

    @staticmethod
    def _backup_dump(blocks: Any, *kinds: str) -> Any:
        """The one dump a backup envelope holds, or ``None``.

        The fence writes its blocks under ``dump``; a hand-saved envelope
        may name the kind instead, or hold just one block of any name.
        """
        if not isinstance(blocks, dict):
            return None
        for name in ("dump", *kinds):
            if blocks.get(name) is not None:
                return blocks[name]
        if len(blocks) == 1:
            return next(iter(blocks.values()))
        return None

    def _write_kwargs(self, *methods: Any) -> Dict[str, Any]:
        """The keyword arguments every given write signature accepts.

        A test double may take fewer than the bridge does; inspecting the
        signature is what keeps one undo record working against both. The
        backup directory is the app's, so an undo writes its envelope
        beside every other write's instead of into the default archive.
        """
        import inspect

        kwargs: Dict[str, Any] = {}
        for method in methods:
            if method is None:
                continue
            try:
                parameters = inspect.signature(method).parameters
            except (TypeError, ValueError):
                continue
            if "backup_dir" in parameters:
                from kws import backup as _backup

                kwargs["backup_dir"] = self._backup_dir or _backup.default_dir()
            if "verify" in parameters:
                kwargs["verify"] = True
        return kwargs

    @staticmethod
    def _normalize_backup_entry(entry: Any) -> Optional[Dict[str, Any]]:
        """One composite entry as ``{"path": ...}`` or an inline envelope.

        ``None`` when the entry names no backup at all -- a composite
        record that cannot undo one of its entries would undo less than
        was written, so such a result is never recorded.
        """
        if isinstance(entry, str):
            return {"path": entry}
        if not isinstance(entry, dict):
            return None
        path = entry.get("path", entry.get("backup"))
        if isinstance(path, str) and path:
            return {"path": path}
        if isinstance(entry.get("blocks"), dict):
            return {
                "kind": str(entry.get("kind") or ""),
                "slot": entry.get("slot"),
                "blocks": dict(entry["blocks"]),
            }
        return None

    def _record_bundle_write(self, payload: Any) -> bool:
        """Record a bank-bundle write as **one** undo record.

        A bundle result carries one backup envelope holding every kind the
        write replaced, so one ``z`` must restore all of them together.
        The record's payload is ``{"kind": "bank-bundle", "bank": ...,
        "blocks": {kind: bytes}}`` with the pre-write blocks. ``False``
        when the payload is not a bundle result -- the caller then tries
        the plain shapes -- or when the backup cannot be read, in which
        case the caller records nothing and says so.
        """
        from kws import backup as _backup

        if not isinstance(payload, dict):
            return False
        backup_path = payload.get("backup")
        if not isinstance(backup_path, str):
            return False
        try:
            loaded = _backup.load(backup_path)
        except Exception:
            return False
        blocks = getattr(loaded, "blocks", None)
        bundle: Dict[str, bytes] = {}
        for name, _kind in _BUNDLE_BLOCKS:
            if isinstance(blocks, dict) and name in blocks:
                bundle[name] = bytes(blocks[name])
        if not bundle:
            return False
        destination = payload.get("bank", payload.get("destination")) or getattr(
            loaded, "slot", None
        )
        if destination is None:
            return False
        label = f"bank {destination} bundle"
        record = _undo.Record(
            label=label,
            payload={
                "kind": "bank-bundle",
                "bank": destination,
                "blocks": bundle,
            },
        )
        self._undo_log.push(record)
        return True

    def _record_composite_write(self, payload: Any) -> bool:
        """Record a composite write as **one** undo record.

        A composite result carries a list of backup paths -- the shape the
        multi staging emits -- optional backups inline as envelopes, and
        optionally a ``multi`` backup path of its own. The record's payload
        is ``{"backups": [...], "multi": path}``, so one ``z`` restores
        every entry of the write, in the order they were written.
        """
        if not isinstance(payload, dict):
            return False
        entries = payload.get("backups")
        if not isinstance(entries, list) or not entries:
            return False
        backups: List[Dict[str, Any]] = []
        for entry in entries:
            normalized = self._normalize_backup_entry(entry)
            if normalized is None:
                return False
            backups.append(normalized)
        multi = payload.get("multi")
        if multi is None:
            multi_path = None
        else:
            normalized = self._normalize_backup_entry(multi)
            if normalized is None or "path" not in normalized:
                return False
            multi_path = normalized["path"]
        record = _undo.Record(
            label=f"composite write ({len(backups)} backup"
            f"{'s' if len(backups) != 1 else ''})",
            payload={"backups": backups, "multi": multi_path},
        )
        self._undo_log.push(record)
        return True

    def _undo_apply(self, record: _undo.Record) -> bool:  # noqa: C901
        try:
            payload = record.payload
            if isinstance(payload, dict):
                if str(payload.get("kind", "")).lower() == "bank-bundle":
                    bundle_bank = payload.get("bank", payload.get("destination"))
                    blocks = payload.get("blocks")
                    if not isinstance(blocks, dict) or bundle_bank is None:
                        raise ValueError("incomplete undo payload")
                    return self._undo_ok(self._write_bundle_undo(bundle_bank, blocks))
                if isinstance(payload.get("backups"), list):
                    return self._undo_composite(payload)
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

            is_multi = str(kind).lower() == "multi" or (
                isinstance(bank, str) and bank.lower() == "multi"
            )
            if is_multi:
                result = self._undo_write_multi(data)
            else:
                result = self._undo_write_bank(bank, kind, data)
            return self._undo_ok(result)
        except Exception as exc:
            if threading.current_thread() is threading.main_thread():
                self.notify_status(f"undo failed: {exc}", refused=True)
            return False

    def _undo_composite(self, payload: Dict[str, Any]) -> bool:
        """Restore every backup of one composite write, atomically.

        Each entry names a backup envelope: a bank-bundle one restores its
        bundle, a ``multi`` one goes through ``write_multi``, and a plain
        bank one is written back as its kind. Any entry that fails to
        verify keeps the whole record, so the next ``z`` retries it all --
        a restore that only half happened is not one reported as done.
        """
        backups = payload.get("backups")
        if not isinstance(backups, list) or not backups:
            raise ValueError("incomplete undo payload")
        for entry in backups:
            if not self._undo_ok(self._undo_backup_entry(entry)):
                return False
        multi = payload.get("multi")
        if multi is not None:
            return self._undo_ok(self._undo_backup_entry(multi))
        return True

    def _undo_entry_parts(self, entry: Any) -> Tuple[str, Any, Any]:
        """``(kind, slot, blocks)`` for one composite entry, or ValueError.

        An entry names a backup by path, or is one -- either the write
        result that carries its path or an inline envelope holding
        ``kind``/``slot``/``blocks``.
        """
        from kws import backup as _backup

        path: Any = None
        if isinstance(entry, str):
            path = entry
        elif isinstance(entry, dict):
            path = entry.get("path", entry.get("backup"))
        if isinstance(path, str) and path:
            record = _backup.load(path)
            return (
                str(getattr(record, "kind", "") or ""),
                getattr(record, "slot", None),
                getattr(record, "blocks", None) or {},
            )
        if isinstance(entry, dict) and isinstance(entry.get("blocks"), dict):
            return (str(entry.get("kind") or ""), entry.get("slot"), entry["blocks"])
        raise ValueError("a composite entry names no backup")

    def _undo_backup_entry(self, entry: Any) -> Dict[str, Any]:
        """Restore one entry of a composite undo payload.

        What the named envelope holds decides how it goes back: the bundle
        whole, the Multisets through ``write_multi``, anything else as one
        whole bank. The plain dump is decoded first -- the envelope holds
        the framed message that arrived, and the write expects records.
        """
        kind, slot, blocks = self._undo_entry_parts(entry)
        if kind == "bank-bundle":
            if not slot:
                raise ValueError("the bank-bundle backup names no bank")
            return self._write_bundle_undo(slot, blocks)
        if not kind:
            raise ValueError("the backup names no kind")
        if not isinstance(blocks, dict):
            raise ValueError("the backup holds no dump")
        dump = self._backup_dump(blocks, kind)
        if dump is None:
            raise ValueError("the backup holds no dump")
        if not slot:
            raise ValueError("the backup names no bank")
        if kind == "multi":
            return self._undo_write_multi(dump)
        if kind == "patch":
            return self._undo_write_patch(dump)
        return self._undo_write_bank(slot, kind, self._decoded_backup_block(dump))

    def _write_bundle_undo(self, bank: Any, blocks: Dict[str, Any]) -> Dict[str, Any]:
        """Write one bank-bundle back on behalf of an undo.

        The device that has the bundle write takes the bundle whole --
        falling back to one plain write per kind would restore the same
        bytes but give up each kind's own read-back. The blocks are
        decoded first: the record holds the framed messages the backup
        did, and this is where they become the plain bytes the write
        expects.
        """
        payloads: Dict[str, bytes] = {}
        for name, _kind in _BUNDLE_BLOCKS:
            if name in blocks:
                payloads[name] = self._decoded_backup_block(blocks[name])
        if not payloads:
            raise ValueError("the bank-bundle backup holds no whole bank")
        kwargs: Dict[str, Any] = {}
        for name, _kind in _BUNDLE_BLOCKS:
            if name in payloads:
                kwargs[name] = payloads[name]
        bundle = getattr(self.device, "write_bank_bundle", None)
        if callable(bundle):
            kwargs.update(self._write_kwargs(bundle))
            result: Dict[str, Any] = bundle(bank, **kwargs)
            return result
        method = self.device.write_bank
        bundle_kwargs = {**kwargs, **self._write_kwargs(method)}
        found: Optional[Dict[str, Any]] = None
        for name, kind in _BUNDLE_BLOCKS:
            if name in payloads:
                written: Dict[str, Any] = method(
                    bank, kind, payloads[name], **bundle_kwargs
                )
                found = written
        if found is None:
            raise ValueError("the bank-bundle backup holds no whole bank")
        return found

    def _undo_write_patch(self, framed: Any) -> Dict[str, Any]:
        """One single-patch write on behalf of an undo.

        The backup is the framed single patch dump, which names its own
        bank (the sysex bank byte) and slot, so the restore needs nothing
        stored beside it. Used by the audition staging, whose undo is one
        entry per staged patch rather than a whole bank.
        """
        from kws import backup as _backup

        raw = bytes(framed) if isinstance(framed, (bytes, bytearray)) else b""
        packet = m.parse(raw)
        if packet is None or packet.function != m.Function.SINGLE_PATCH_DUMP:
            raise ValueError("the patch backup is not a single patch dump")
        bank_sysex = packet.payload[0]
        number = packet.payload[1]
        entry = next((b for b in banks.BANKS if b.sysex == bank_sysex), None)
        if entry is None:
            raise ValueError(f"no bank answers to sysex bank {bank_sysex}")
        result: Dict[str, Any] = self.device.write_patch(
            entry.number,
            number,
            bytes(packet.data()),
            backup_dir=_backup.default_dir(),
            verify=True,
            wait_status=False,
        )
        return result

    def _undo_write_bank(self, bank: Any, kind: Any, data: Any) -> Dict[str, Any]:
        """One whole-bank write on behalf of an undo.

        The backup holds the framed message that arrived, so it is decoded
        here before it is written back -- exactly as the bundle path does.
        A payload that is already decoded passes through untouched.
        """
        method = self.device.write_bank
        result: Dict[str, Any] = method(
            bank, kind, self._decoded_backup_block(data), **self._write_kwargs(method)
        )
        return result

    def _undo_write_multi(self, data: Any) -> Dict[str, Any]:
        """One Multi Mode Setup write on behalf of an undo."""
        method = self.device.write_multi
        result: Dict[str, Any] = method(bytes(data), **self._write_kwargs(method))
        return result

    def _undo_settle(
        self, results: list[bool], failed: list[Any] | None = None
    ) -> None:
        for record in reversed(failed or []):
            self._undo_log.push(record)
        self._undo_log.settle(results)
        if results and all(results):
            self._pending_staging = False
            if self._quit_after_undo:
                self._quit_after_undo = False
                self.exit()
        elif self._quit_after_undo:
            self._quit_after_undo = False
            self.notify_status(
                "the staging could not be restored; z retries", warn=True
            )

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

    def _read_source_offline(self, scope: str) -> deps.Source:
        self._guard_card_slot(scope)
        bank = self._bank_for_scope(scope)
        perf_raw = self.device.read_raw(bank.sysex, m.Function.ALL_PERFORMANCE_DUMP)
        patch_raw = self.device.read_raw(bank.sysex, m.Function.ALL_PATCH_DUMP)
        ws_raw = self.device.read_raw(bank.sysex, m.Function.ALL_WAVESEQ_DUMP)
        pp = m.parse(perf_raw)
        pa = m.parse(patch_raw)
        pw = m.parse(ws_raw)
        pb = pp.data() if pp is not None else b""
        qb = pa.data() if pa is not None else b""
        wb = pw.data() if pw is not None else b""
        if len(wb) > rec.WAVESEQ_BANK_SIZE:
            wb = wb[: rec.WAVESEQ_BANK_SIZE]
        perfs_list = [
            pb[i * rec.PERFORMANCE_SIZE : (i + 1) * rec.PERFORMANCE_SIZE]
            for i in range(len(pb) // rec.PERFORMANCE_SIZE)
        ]
        patches_list = [
            qb[i * rec.PATCH_SIZE : (i + 1) * rec.PATCH_SIZE]
            for i in range(len(qb) // rec.PATCH_SIZE)
        ]
        return deps.Source(
            scope_id=scope, performances=perfs_list, patches=patches_list, waveseq=wb
        )

    def _before_blocks(self, source: Any, bank: Any) -> Dict[str, bytes]:
        from kws import banks as _banks
        from kws import messages as _m

        result: Dict[str, bytes] = {}
        if source is None:
            return result
        perf_data = b"".join(getattr(source, "performances", ()) or ())
        expected_perf = _banks.PERFORMANCES_PER_BANK * rec.PERFORMANCE_SIZE
        if len(perf_data) == expected_perf:
            result["performances"] = _m.frame_bulk(
                _m.Function.ALL_PERFORMANCE_DUMP,
                bank.sysex,
                perf_data,
                channel=self.device.channel,
            )
        patch_data = b"".join(getattr(source, "patches", ()) or ())
        expected_patch = _banks.PATCHES_PER_BANK * rec.PATCH_SIZE
        if len(patch_data) == expected_patch:
            result["patches"] = _m.frame_bulk(
                _m.Function.ALL_PATCH_DUMP,
                bank.sysex,
                patch_data,
                channel=self.device.channel,
            )
        ws = getattr(source, "waveseq", None)
        if ws is not None and len(ws) == rec.WAVESEQ_BANK_SIZE:
            result["waveseq"] = _m.frame_bulk(
                _m.Function.ALL_WAVESEQ_DUMP,
                bank.sysex,
                ws,
                channel=self.device.channel,
            )
        return result

    def _build_copy_perf(
        self,
        local_sources: Dict[str, Any],
        dest_scope: str,
        perf: Any,
        mode: str,
        replace_slot: Optional[int],
        destination: Any,
    ) -> Any:
        if mode == "clean":
            return perfcopy.copy_into_bank(
                local_sources,
                dest_scope,
                [perf],
                mode=mode,
                replace_slot=replace_slot,
                destination_bank=destination,
            )
        if mode == "replace" and replace_slot is not None:
            chosen_slot = int(replace_slot)
        else:
            dest_src = local_sources.get(dest_scope)
            found = None
            if dest_src is not None:
                found = next(
                    (
                        index
                        for index in range(50)
                        if perfcopy.is_empty_performance(dest_src.performances[index])
                    ),
                    None,
                )
            if found is None:
                raise RuntimeError("no free performance slot")
            chosen_slot = int(found)
        return perfcopy.copy_slots_into_bank(
            local_sources,
            dest_scope,
            {chosen_slot: perf},
            destination_bank=destination,
        )

    def _device_worker(self, work: Any, done: Any, label: str = "working") -> None:
        """Run one device operation off the UI thread, then report back.

        Every long SysEx exchange belongs here: a bank dump takes the wire
        about ten seconds each way, and on the UI thread the bar could not
        move and the app could not repaint. ``done(result, failure)`` runs
        on the main thread, so it may touch widgets. Without a running app
        -- unit tests call these flows directly -- the work runs inline.
        """
        if not getattr(self, "is_running", False):
            failure: BaseException | None = None
            result: Any = None
            try:
                result = work()
            except BaseException as exc:
                failure = exc
            done(result, failure)
            return
        self._progress_hold_begin(label)
        self._device_worker_thread(work, done)

    @work(thread=True)
    def _device_worker_thread(self, work: Any, done: Any) -> None:
        failure: BaseException | None = None
        result: Any = None
        try:
            if not self._exchange.acquire(blocking=False):
                raise RuntimeError("busy")
            try:
                with self._busy_gate, self._lock:
                    result = work()
            finally:
                self._exchange.release()
        except BaseException as exc:
            failure = exc
        try:
            self.call_from_thread(done, result, failure)
        except Exception:  # noqa: S110 - the app may be shutting down
            pass
        finally:
            # The bar must end even when the report itself failed: a raise
            # in ``done`` once left it frozen at the last percentage, with
            # no operation running and nothing to click.
            try:
                self.call_from_thread(self._progress_hold_end)
            except Exception:  # noqa: S110 - the app may be shutting down
                pass

    @work(thread=True)
    def _copy_perf_write_worker(
        self,
        perf: Any,
        destination: Any,
        mode: str,
        screen: Any,
        *,
        replace_slot: Optional[int] = None,
    ) -> None:
        failure: BaseException | None = None
        payload: Any = None
        try:
            if not self._exchange.acquire(blocking=False):
                raise RuntimeError("busy")
            try:
                with self._busy_gate, self._lock:
                    local_sources = dict(self._sources)
                    src_scope = perf.scope_id
                    if src_scope not in local_sources:
                        local_sources[src_scope] = self._read_source_offline(src_scope)
                    dest_scope = destination.id
                    if dest_scope not in local_sources:
                        local_sources[dest_scope] = self._read_source_offline(
                            dest_scope
                        )
                    built = self._build_copy_perf(
                        local_sources, dest_scope, perf, mode, replace_slot, destination
                    )
                    if not built.plan.allocations:
                        raise RuntimeError("no performance slot allocated")
                    if built.plan.rejected:
                        raise RuntimeError(
                            "copy does not fit: " + "; ".join(built.plan.rejected)
                        )
                    from kws import backup as _backup

                    where = self._backup_dir or _backup.default_dir()
                    before = self._before_blocks(
                        local_sources.get(dest_scope), destination
                    )
                    write_result = self.device.write_bank_bundle(
                        destination.number,
                        performances=built.performances,
                        patches=built.patches,
                        waveseq=built.waveseq,
                        backup_dir=where,
                        before_blocks=before or None,
                    )
                    if (
                        not isinstance(write_result, dict)
                        or write_result.get("verified") is not True
                    ):
                        raise RuntimeError(
                            str(
                                write_result.get("mismatch")
                                if isinstance(write_result, dict)
                                else write_result
                            )
                        )
                payload = (
                    perf,
                    destination,
                    mode,
                    local_sources,
                    built,
                    write_result,
                    screen,
                )
            finally:
                try:
                    self._exchange.release()
                except RuntimeError:
                    pass
        except Exception as exc:
            failure = exc
        if failure is not None:
            self.call_from_thread(screen._write_failed, str(failure))
            self.call_from_thread(self.notify_status, f"copy: {failure}", refused=True)
            return
        self.call_from_thread(
            self._copy_perf_finished,
            payload[0],
            payload[1],
            payload[2],
            payload[3],
            payload[4],
            payload[5],
            payload[6],
        )

    @staticmethod
    def _source_from_built(scope: str, built: Any) -> deps.Source:
        """A cache entry for a bank a copy just wrote, not the one it replaced."""
        perfs = [
            built.performances[
                i * rec.PERFORMANCE_SIZE : (i + 1) * rec.PERFORMANCE_SIZE
            ]
            for i in range(len(built.performances) // rec.PERFORMANCE_SIZE)
        ]
        patches = [
            built.patches[i * rec.PATCH_SIZE : (i + 1) * rec.PATCH_SIZE]
            for i in range(len(built.patches) // rec.PATCH_SIZE)
        ]
        return deps.Source(
            scope_id=scope,
            performances=perfs,
            patches=patches,
            waveseq=built.waveseq,
        )

    @staticmethod
    def _names_in_blob(blob: bytes, size: int) -> Dict[int, str]:
        return {
            index: m.decode_name(blob[index * size : (index + 1) * size])
            for index in range(len(blob) // size)
        }

    def _save_written_names(
        self,
        entry: Any,
        perf_names: Dict[int, str],
        patch_names: Dict[int, str],
    ) -> None:
        for kind, names in (
            (banks.Kind.PERFORMANCE, perf_names),
            (banks.Kind.PATCH, patch_names),
        ):
            for number, name in names.items():
                self.catalog.set_live_name(entry.id, number, name, kind)
        if self._live_path is None:
            return
        flat = {
            _livenames.key(entry.id, number, kind): name
            for kind, names in (
                (banks.Kind.PERFORMANCE, perf_names),
                (banks.Kind.PATCH, patch_names),
            )
            for number, name in names.items()
        }
        try:
            _livenames.save(flat, self._live_path)
        except OSError:
            pass

    def _save_written_card(
        self,
        scope_id: str,
        perf_names: Dict[int, str],
        patch_names: Dict[int, str],
    ) -> None:
        """An HCard bank is displayed from the capture archive, not the catalog."""
        if scope_id == "CARD":
            number = self._current_card
        elif scope_id.startswith("CARD/"):
            key = scope_id[5:]
            number = int(key) if key.isdigit() else self._current_card
        else:
            return
        if number is None:
            return
        captured = self._archive.get(number)
        if captured is None:
            return
        updated = _cards.CapturedBank(
            number=captured.number,
            name=captured.name,
            captured_at=captured.captured_at,
            performances=perf_names,
            patches=patch_names,
            label=captured.label,
            digest=captured.digest,
            verified=False,
        )
        self._archive[number] = updated
        if self._cards_path is not None:
            try:
                _cards.save(updated, self._cards_path)
            except OSError:
                pass

    def _refresh_written_bank(self, scope_id: str, built: Any) -> None:
        """Refresh the browser for the bank a write just changed.

        The built bytes are already the post-write state; re-reading every
        bank (``S``) would be slow and would send another `RECEIVING SYSEX`
        request. Update this bank's live names and the visible rows instead.
        """
        try:
            entry = banks.bank(scope_id)
        except LookupError:
            if not scope_id.startswith("CARD/"):
                return
            entry = banks.bank("CARD")

        perf_blob = getattr(built, "performances", b"") or b""
        patch_blob = getattr(built, "patches", b"") or b""
        perf_names = self._names_in_blob(perf_blob, rec.PERFORMANCE_SIZE)
        patch_names = self._names_in_blob(patch_blob, rec.PATCH_SIZE)
        self._save_written_names(entry, perf_names, patch_names)
        self._save_written_card(scope_id, perf_names, patch_names)

        # Some callers (CLI-style tests) invoke the import methods without
        # an App screen stack. The data is refreshed above; there is no
        # table to repaint in that case.
        if not getattr(self, "_screen_stack", None):
            return
        if self._current_pair[0] == entry.number:
            self._refill_keeping_place()
        else:
            self._refresh_deps_pane()

    def _copy_perf_finished(
        self,
        perf: Any,
        destination: Any,
        mode: str,
        local_sources: Dict[str, deps.Source],
        built: Any,
        write_result: Any,
        screen: Any,
    ) -> None:
        for sid, src in local_sources.items():
            self._sources[sid] = src
        # local_sources holds the destination as it was read *before* the
        # write; the built bank is what the bank now holds.
        self._sources[destination.id] = self._source_from_built(destination.id, built)
        self._refresh_deps_pane()
        if self._bankstore_dir is not None:
            try:
                bankstore.save(
                    destination.id,
                    performances=built.performances,
                    patches=built.patches,
                    waveseq=built.waveseq,
                    directory=self._bankstore_dir,
                )
            except OSError:
                pass
        self._refresh_written_bank(destination.id, built)
        slot = (
            built.plan.allocations[0].target_perf_slot if built.plan.allocations else 0
        )
        patch_slots = sorted(
            {v for a in built.plan.allocations for v in a.part_patch_slots.values()}
        )
        try:
            self._record_write(
                write_result if isinstance(write_result, dict) else {"verified": True}
            )
        except Exception:  # noqa: S110
            pass
        self.notify_status(
            f"copied {perf.scope_id}:{perf.number:02d} into "
            f"{destination.label} perf {slot:02d}, verified"
        )
        screen.dismiss(
            {
                "ok": True,
                "bank": destination.label,
                "perf": slot,
                "patches": patch_slots,
            }
        )

    def _copy_bank_write(
        self, source: Any, destination: Any, screen: Any, method: str = "fence"
    ) -> None:
        """One whole-bank copy off the UI thread, by the chosen method."""

        def work() -> Any:
            from kws import backup as _backup

            where = self._backup_dir or _backup.default_dir()
            return self.device.copy_bank(
                source.number,
                destination.number,
                backup_dir=where,
                method=method,
            )

        def done(result: Any, failure: Any) -> None:
            self._copy_bank_done(result, failure, source, destination, screen)

        self._device_worker(
            work, done, f"copying {source.label} to {destination.label}"
        )

    @staticmethod
    def _bank_records(blob: bytes, size: int) -> List[bytes]:
        return [blob[i * size : (i + 1) * size] for i in range(len(blob) // size)]

    def _copy_bank_done(
        self,
        result: Any,
        failure: Any,
        source: Any,
        destination: Any,
        screen: Any,
    ) -> None:
        if failure is not None:
            screen._write_failed(str(failure))
            self.notify_status(f"copy bank: {failure}", refused=True)
            return
        if not isinstance(result, dict) or result.get("verified") is not True:
            message = (
                result.get("mismatch") if isinstance(result, dict) else str(result)
            )
            screen._write_failed(str(message))
            self.notify_status(f"copy bank: {message}", refused=True)
            return
        # The bridge and the demo both report what they wrote; a foreign
        # device double may not. The write itself succeeded either way, so
        # a missing payload only skips the cache and archive updates.
        written = result.get("written")
        if isinstance(written, dict) and {"performances", "patches", "waveseq"} <= set(
            written
        ):
            self._remember_bank(
                destination.id,
                performances=written["performances"],
                patches=written["patches"],
                waveseq=written["waveseq"],
            )
            built = SimpleNamespace(
                performances=written["performances"],
                patches=written["patches"],
                waveseq=written["waveseq"],
            )
            self._refresh_written_bank(destination.id, built)
            self._refresh_multi_checks()
            if destination.id == "CARD":
                self._adopt_copied_card(written)
        try:
            self._record_write(result)
        except Exception:  # noqa: S110
            pass
        # Notify before dismissing, as copy-perf does: the status line is
        # the browser's confirmation once the screen is gone.
        method = result.get("method", "fence")
        if method == "device":
            note = (
                f"copied {source.label} into {destination.label} "
                "via the unit's move, verified"
            )
            if result.get("source_restored"):
                note += " (source restored)"
        else:
            note = f"copied {source.label} into {destination.label}, verified"
        self.notify_status(note)
        screen._write_done(
            {
                "ok": True,
                "source": source.label,
                "destination": destination.label,
                "verified": True,
            }
        )

    def _adopt_copied_card(self, written: Dict[str, bytes]) -> None:
        """Re-digest a verified copy into the archive of the active card.

        The copy is fully automatic: the user chose the Card as the
        destination, so an archive that still described the old content
        would be stale. ``hcard`` mode updates the active HCard bank in
        ``cards.json``; ``cards`` mode updates the active ordinary card in
        ``physical-cards.json``. No prompt stands between the write and
        the record.
        """
        perf_records = self._bank_records(written["performances"], rec.PERFORMANCE_SIZE)
        patch_records = self._bank_records(written["patches"], rec.PATCH_SIZE)
        digest = _cards.content_digest(perf_records, patch_records)
        perf_names = {i: m.decode_name(r) for i, r in enumerate(perf_records)}
        patch_names = {i: m.decode_name(r) for i, r in enumerate(patch_records)}
        if self._card_mode == "hcard":
            self._adopt_copied_hcard(digest, perf_names, patch_names)
        elif self._card_mode == "cards":
            self._adopt_copied_physical(digest, perf_names, patch_names, written)

    def _adopt_copied_hcard(
        self, digest: str, perf_names: Dict[int, str], patch_names: Dict[int, str]
    ) -> None:
        # The write went to whatever bank the physical HCard is showing,
        # which is the *active* bank -- not necessarily the sub-bank the
        # browser happens to be looking at. Fall back to the viewed bank
        # when no active one has been confirmed yet.
        number = (
            self._active_card if self._active_card is not None else self._current_card
        )
        if number is None:
            return
        captured = self._archive.get(number)
        if captured is None:
            return
        updated = _cards.CapturedBank(
            number=captured.number,
            name=captured.name,
            captured_at=captured.captured_at,
            performances=perf_names,
            patches=patch_names,
            label=captured.label,
            digest=digest,
            verified=True,
        )
        self._archive[number] = updated
        if self._cards_path is not None:
            try:
                _cards.save(updated, self._cards_path)
            except OSError:
                pass
        if getattr(self, "_screen_stack", None):
            self._refresh_card_row(number)
            self._refresh_multi_checks()

    def _adopt_copied_physical(
        self,
        digest: str,
        perf_names: Dict[int, str],
        patch_names: Dict[int, str],
        written: Dict[str, bytes],
    ) -> None:
        from datetime import UTC, datetime

        card_id = self._active_physical
        card = self._physical.get(card_id) if card_id else None
        if card is None:
            return
        updated = _physical.PhysicalCard(
            id=card.id,
            label=card.label,
            digest=digest,
            captured_at=datetime.now(UTC).isoformat(),
            performances=perf_names,
            patches=patch_names,
            verified=True,
        )
        self._remember_physical(updated, updated.id)
        self._remember_bank(
            _physical.scope_id(updated.id),
            performances=written["performances"],
            patches=written["patches"],
            waveseq=written["waveseq"],
        )
        if getattr(self, "_screen_stack", None):
            self._build_row_model()
            self._apply_row_model()
            self._show_physical_row(updated.id)
            self._refresh_multi_checks()

    async def action_quit(self) -> None:
        """Confirm before leaving with an audition's staging still applied."""
        if not self._pending_staging:
            self.exit()
            return
        self.push_screen(
            ChoiceScreen(
                "Leave with staged audition changes?",
                [
                    "restore the staging, then quit",
                    "quit and forget the staged changes",
                    "cancel — stay in kwsed",
                ],
                0,
            ),
            self._quit_choice,
        )

    def _quit_choice(self, index: Optional[int]) -> None:
        if index == 0:
            self._restore_staging_then_quit()
            return
        if index == 1:
            # The undo record lives in this process; leaving drops it and
            # the staged bytes stay on the unit until overwritten.
            self._pending_staging = False
            self.exit()
            return
        self.notify_status("quit cancelled")

    def _restore_staging_then_quit(self) -> None:
        if not self._undo_log:
            self.exit()
            return
        if self._busy or self._exchange.locked():
            self.notify_status("busy — try quitting again in a moment", warn=True)
            return
        # Choosing "restore" is a deliberate write, so the gate may be armed
        # for it even when the user left it locked.
        if not self._write_gate.armed:
            self._write_gate.toggle()
        self._quit_after_undo = True
        self.action_undo()

    def action_undo(self) -> None:
        if not self._write_gate.armed:
            self.notify_status("write gate is locked", refused=True)
            return
        if self._busy:
            self.notify_status("busy", refused=True)
            return
        # Undo rewrites what the staging cache believes it knows; drop it so
        # the next audition reads the restored bank once instead of trusting
        # the pre-undo contents.
        screen: Any = self.screen
        try:
            screen._staging_cache = None
        except Exception:  # noqa: S110 - the cache is an optimisation
            pass
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
            yield WarningTable(id="bank-table", cursor_type="row")
            yield WarningTable(id="slot-table", cursor_type="row")
            with VerticalScroll(id="deps-pane", can_focus=False):
                yield Static("", id="deps-pane-body")
        yield Static("", id="detail")
        yield Static("", id="status")
        yield Static("", id="progress")
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
        kinds = (
            (banks.Kind.PERFORMANCE, banks.Kind.PATCH)
            if self._show_patches
            else (banks.Kind.PERFORMANCE,)
        )
        self._real_pairs: List[Tuple[int, str]] = [
            (entry.number, kind)
            for entry in banks.BANKS
            if entry.id != "CARD"
            for kind in kinds
        ]
        _card_pairs: List[Tuple[int, str]] = []
        _extra_cards: List[Optional[int]] = []
        _extra_physical: List[Optional[str]] = []
        if self._card_mode == "hcard":
            _card_pairs = [
                (banks.bank("CARD").number, kind)
                for _number in _cards.bank_numbers()
                for kind in kinds
            ]
            _extra_cards = [
                number for number in _cards.bank_numbers() for _kind in kinds
            ]
            _extra_physical = [None] * len(_card_pairs)
        elif self._card_mode == "cards":
            ids = sorted(self._physical.keys())
            if not ids:
                ids = [""]
            _card_pairs = [
                (banks.bank("CARD").number, kind) for _id in ids for kind in kinds
            ]
            _extra_cards = [None] * len(_card_pairs)
            _extra_physical = [_id for _id in ids for _ in kinds]
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
        # Keep the highlight handler quiet while the table is emptied and
        # refilled: a queued RowHighlighted for row 0 would otherwise land
        # after the caller has filled the pane for a captured card and pull
        # the browser back to the RAM row.
        prior_filling = self._filling
        self._filling = True
        try:
            cursor_row = table.cursor_row
            table.clear()
            for pair, card, multi, physical in zip(
                self._pairs, self._row_cards, self._row_multis, self._row_physical
            ):
                cells = self._bank_row(pair, card, multi, physical)
                if multi is not None and multi in self._multi_mismatches:
                    cells = self._tint_cells(cells)
                table.add_row(
                    *cells,
                    key=self._row_key(pair, card, multi, physical),
                )
            if self._pairs:
                with contextlib.suppress(Exception):
                    table.move_cursor(row=min(cursor_row or 0, len(self._pairs) - 1))
        finally:
            self._filling = prior_filling
        if isinstance(table, WarningTable):
            table.set_warning_rows(
                {f"multi:{number:02d}" for number in self._multi_mismatches}
            )

    def _update_stale_cursor(self) -> None:
        """Redden the selected row when its Multiset is mismatched.

        A cell's own background wins over the cursor style, but the cursor
        style covers the rest of the selected row; without this the blue
        selection hides most of the warning on exactly the row in view.
        """
        try:
            table = self.query_one("#bank-table", DataTable)
        except Exception:
            return
        row = table.cursor_row
        multi = self._row_multis[row] if 0 <= row < len(self._row_multis) else None
        if multi is not None and multi in self._multi_mismatches:
            table.add_class("stale-cursor")
        else:
            table.remove_class("stale-cursor")

    def _show_physical_row(self, card_id: str) -> None:
        """Put the bank cursor on a captured card's performance row.

        A re-modelled table leaves the cursor wherever it was, which after a
        capture is usually row 0 (RAM 1). The capture paths want the card
        whose slots they just filled selected, so find its performance row
        and move there without letting the highlight handler clobber the
        pane. Rows come in performance/patch pairs, hence the kind check.
        """
        table = self.query_one("#bank-table", DataTable)
        for index, (pair, physical) in enumerate(zip(self._pairs, self._row_physical)):
            if physical == card_id and pair[1] == banks.Kind.PERFORMANCE:
                prior_filling = self._filling
                self._filling = True
                try:
                    with contextlib.suppress(Exception):
                        table.move_cursor(row=index)
                finally:
                    self._filling = prior_filling
                return

    def _clip(self, text: str, limit: int = 26) -> str:
        """Instance wrapper around the module-level :func:`_clip`."""
        return _clip(text, limit)

    def _card_row_name(self, number: int) -> str:
        """The name cell of one HCard sub-bank.

        Not captured is greyed rather than hidden: the forty banks are the
        reason the Card row exists, and a bank that has not been read is
        still a place a capture can land. With ``show_card_defaults`` the
        manual's expected name takes the place of ``not captured`` so an
        unread bank can still be recognised.
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
            # The manual's names run to 40 characters, but the bank column
            # is 64 wide; the normal title clip keeps the row inside the
            # pane. "Performance 2" and "Performance 3" still separate
            # (their digits differ before the cut), and the detail pane
            # shows the full name.
            body = (
                _clip(f"{entry.name}{pcm}", 27)
                if self._show_card_defaults
                else f"not captured{pcm}"
            )
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

    #: A muted red for a Multiset row whose recording no longer matches.
    MULTI_WARNING_STYLE = MULTI_WARNING_STYLE

    def _tint_cells(self, cells: Sequence[Any]) -> List[Any]:
        from rich.text import Text

        return [
            Text.from_markup(str(cell), style=self.MULTI_WARNING_STYLE)
            for cell in cells
        ]

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
        scoped = entry.id if card is None else _cards.scope_id(card)
        marked = len(self._favorite_keys(scoped, kind))
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
            str(marked) if marked else "",
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
        with contextlib.suppress(NoMatches):
            self.query_one("#deps-pane").display = self._deps_pane_visible
        self._refresh_deps_pane()
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
        if self._auto_read_multi_enabled:
            # After the launch card probe; if it is still busy, retry.
            self.set_timer(2.2, self._auto_multi_read)

    # --- filling ------------------------------------------------------------

    def _scoped_bank_id(self, item: SlotLike) -> str:
        """Bank id used for favourites on the row currently under the cursor.

        The order matters: a selected HCard sub-bank (``CARD/07``) wins,
        then an archived physical card (``CARD/c1``), then the item's own
        bank. The HCard rows are browse overlays over the one ``CARD``
        slot, so without the first case every bank would share the old
        ``CARD`` favourites.
        """
        scoped = getattr(item, "bank_id", None)
        if isinstance(scoped, str) and scoped:
            return scoped
        if self._current_card is not None:
            return _cards.scope_id(self._current_card)
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

    def _all_favourites(self) -> List[ViewSlot]:
        """Every favourite in every bank and kind, in listing order.

        The scope is carried on each row: all HCard banks share the synth's
        one ``CARD`` bank, so a plain Slot cannot say which bank a
        favourite came from, and two banks' Performance 03 would collide.
        The shared ``CARD`` favourites are never read; per-card keys
        replaced them.
        """
        if self.favorites is None:
            return []
        if self._card_mode == "hcard":
            card_scopes = {
                _cards.scope_id(number) for number in range(len(_cards.CARDS))
            }
        elif self._card_mode == "cards":
            card_scopes = {_physical.scope_id(card_id) for card_id in self._physical}
        else:
            card_scopes = set()
        card_number = banks.bank("CARD").number
        out: List[ViewSlot] = []
        for favourite in self.favorites.all(order="bank"):
            bank_id, separator, kind = favourite.bank_id.rpartition(":")
            if not separator or kind not in (
                banks.Kind.PERFORMANCE,
                banks.Kind.PATCH,
            ):
                continue
            if bank_id == "CARD":
                continue
            if bank_id in card_scopes:
                number = card_number
            else:
                try:
                    number = banks.bank(bank_id).number
                except LookupError:
                    continue
            try:
                slot = banks.slot(number, kind, favourite.number)
            except LookupError:
                continue
            out.append(ViewSlot(slot, bank_id, favourite.name))
        return self._by_rating(out)

    def _visible(self, pair: Tuple[int, str]) -> Sequence[SlotLike]:
        number, kind = pair
        if self.view_mode == VIEW_ALL_FAVOURITES:
            return self._all_favourites()
        entries = banks.slots(number, kind)
        if self.view_mode == VIEW_BANK_FAVOURITES:
            scoped = (
                _cards.scope_id(self._current_card)
                if self._current_card is not None
                else banks.bank(number).id
            )
            marked = self._favorite_keys(scoped, kind)
            entries = [e for e in entries if e.number in marked]
        return self._by_rating(entries)

    def _by_rating(self, entries: List[SlotT]) -> List[SlotT]:
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

    def _cursor_entry(self) -> Optional[SlotLike]:
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
        scoped = self._scoped_bank_id(item)
        key = favorite_bank_id(scoped, item.kind)
        resolved = self._scope_slot_name(scoped, item.kind, item.number)
        name = "" if not resolved or resolved == "(unnamed)" else resolved
        # Favorites.toggle, not remove-then-add: un-favouriting keeps
        # the row so a rating, tags, a note and the original date
        # survive being toggled off and on. As a delete followed by a
        # fresh add this destroyed all four, and this is one keystroke.
        now = self.favorites.toggle(key, item.number, name=name)
        self.notify_status(f"{'favourited' if now else 'unfavourited'} {name or item}")
        self._refill_keeping_place()
        self._refresh_bank_hearts()

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

    def action_copy_performance(self) -> None:
        entry = self._cursor_entry()
        if entry is None:
            self.notify_status("nothing under the cursor", refused=True)
            return
        if entry.kind == banks.Kind.PATCH:
            self.notify_status(
                "select a Performance to copy "
                "(patches travel with the Performance that uses them)",
                refused=True,
            )
            return
        perf = relations.PerfId(self._scoped_bank_id(entry), entry.number)
        source_name = ""
        try:
            source_name = self._scope_slot_name(
                self._scoped_bank_id(entry), entry.kind, entry.number
            )
        except Exception:
            source_name = ""
        self._open_copy_target_chooser(perf, source_name)

    def action_copy_bank(self) -> None:
        """Copy the bank under the cursor, all three kinds, into another.

        The source is whichever bank the cursor is in -- RAM or Card, in any
        direction. Opening the picker sends nothing; the screen that follows
        does the three-step arm/fire dance.
        """
        entry = self._cursor_entry()
        if entry is None:
            self.notify_status("nothing under the cursor", refused=True)
            return
        try:
            source = self._bank_for_scope(self._scoped_bank_id(entry))
        except Exception as exc:
            self.notify_status(f"copy bank: {exc}", refused=True)
            return
        if not source.writable:
            self.notify_status(
                f"{source.label} is ROM and cannot be copied from", refused=True
            )
            return
        if not self._write_gate.armed:
            self.notify_status("write gate is locked", refused=True)
            return
        writable = [b for b in banks.writable_banks() if b.number != source.number]
        if self._card_mode == "none" and (
            source.id == "CARD" or any(b.id == "CARD" for b in writable)
        ):
            self.notify_status("card support is off (card_mode = none)", refused=True)
            return
        if (
            source.id == "CARD"
            and self._card_mode == "hcard"
            and self._active_card in range(0, 25)
        ):
            self.notify_status(
                f"Card {self._active_card:02d} is a read-only HCard ROM bank; "
                "select a user bank (25-39) or RAM 1-3",
                refused=True,
            )
            return
        if not writable:
            self.notify_status("no writable destination banks", refused=True)
            return
        self.push_screen(
            TargetBankScreen("Copy bank to", [b.label for b in writable]),
            lambda index: self._copy_bank_target_chosen(source, writable, index),
        )

    def _copy_bank_target_chosen(
        self,
        source: banks.Bank,
        writable: Sequence[banks.Bank],
        target_index: Optional[int],
    ) -> None:
        if target_index is None:
            return
        try:
            destination = writable[target_index]
        except (IndexError, TypeError):
            return
        if self._card_mode == "none" and (
            source.id == "CARD" or destination.id == "CARD"
        ):
            self.notify_status("card support is off (card_mode = none)", refused=True)
            return
        if (
            destination.id == "CARD"
            and self._card_mode == "hcard"
            and self._active_card in range(0, 25)
        ):
            self.notify_status(
                f"Card {self._active_card:02d} is a read-only HCard ROM bank; "
                "select a user bank (25-39) or RAM 1-3",
                refused=True,
            )
            return
        self.push_screen(BankCopyScreen(self, source, destination))

    def _open_copy_target_chooser(
        self, perf: relations.PerfId, source_name: str = ""
    ) -> None:
        writable = banks.writable_banks()
        if not writable:
            self.notify_status("no writable target banks", refused=True)
            return
        self.push_screen(
            TargetBankScreen("Copy to bank", [b.label for b in writable]),
            lambda index: self._copy_target_chosen(perf, source_name, writable, index),
        )

    def _copy_target_chosen(
        self,
        perf: relations.PerfId,
        source_name: str,
        writable: Sequence[Any],
        target_index: Optional[int],
    ) -> None:
        if target_index is None:
            return
        try:
            target = writable[target_index]
        except (IndexError, TypeError):
            return
        sources = dict(self._sources_with_store())
        target_source = sources.get(target.id)
        if target_source is None:
            try:
                target_source = self._read_source_offline(target.id)
            except Exception as exc:
                self.notify_status(
                    f"copy: could not read {target.label}: {exc}", refused=True
                )
                return
            self._sources[target.id] = target_source
            sources[target.id] = target_source
        source = sources.get(perf.scope_id)
        if source is None:
            source = self._load_unit_source(
                perf.scope_id, origin_for_unit(perf.scope_id)
            )
            if source is None:
                return
            sources[perf.scope_id] = source
        origin = origin_for_unit(perf.scope_id)
        read_only = not self._unit_scope_is_writable(perf.scope_id)
        entry = make_source_entry(
            label=origin,
            scope=perf.scope_id,
            source=source,
            origin=origin,
            existing=sources,
            read_only=read_only,
        )
        sources[entry.scope] = entry.source
        sources[target.id] = target_source
        free = next(
            (
                slot
                for slot, record in enumerate(target_source.performances)
                if perfcopy.is_empty_performance(record)
            ),
            None,
        )
        if free is None:
            self.notify_status(f"copy: {target.label} has no free slot", refused=True)
            return
        initial = {free: relations.PerfId(entry.scope, perf.number)}
        screen = SingleImportScreen(
            path=origin,
            banks=[entry],
            sources=sources,
            target_bank=target,
            target_source=target_source,
            gate=self._write_gate,
            initial_assignments=initial,
            on_source_request=self._single_source_menu,
            on_audition_request=self._audition_focused,
        )
        self._begin_staging_prefetch(screen)
        self.push_screen(
            screen,
            lambda choice: self._import_single_chosen(
                choice,
                target,
                entry,
                sources,
                origin,
                [entry],
                writable,
            ),
        )
        self.notify_status(
            f"copy {source_name or perf} into {target.label} slot {free:02d}; "
            "adjust or ctrl+s"
        )

    def _unit_scope_is_writable(self, scope: str) -> bool:
        if scope == "CARD" or scope.startswith("CARD/"):
            return True
        try:
            return bool(banks.bank(scope).writable)
        except LookupError:
            return False

    def action_import(self) -> None:
        self.push_screen(
            ImportMenuScreen(
                "Import", ["Whole bank", "Single / multiple performances"]
            ),
            self._import_kind_chosen,
        )

    def _import_kind_chosen(self, kind: Optional[int]) -> None:
        if kind is None:
            return
        if kind == 0:
            self._open_whole_source_menu()
            return
        self._open_single_source_menu()

    def _open_file_picker(self, kind: int) -> None:
        picker = FilePickerScreen(
            start=self._import_folder or "",
            start_row=self._import_cursor,
        )

        def done(path: Optional[str]) -> None:
            self._import_folder = str(picker.folder)
            from kws import config as _cfg

            if self._config_path is not None:
                try:
                    _cfg.save_import_folder(str(picker.folder), self._config_path)
                except OSError:
                    pass
            self._import_cursor = picker.last_row
            if path:
                self._import_path_chosen(kind, path)
            elif kind == 0:
                self._open_whole_source_menu()
            else:
                # Escape from the file picker goes back to the source chooser.
                self._open_single_source_menu()

        self.push_screen(picker, done)

    def _open_whole_source_menu(self) -> None:
        choices = ["From file…"] + self._unit_source_labels()
        self.push_screen(
            ImportMenuScreen("Whole bank source", choices),
            self._whole_source_chosen,
        )

    def _unit_source_labels(self) -> List[str]:
        return [
            origin_for_unit(
                self._active_card_scope() if choice.scope == "CARD" else choice.scope
            )
            for choice in unit_bank_choices()
        ]

    def _open_single_source_menu(self) -> None:
        choices = ["From file…"] + self._unit_source_labels()
        self.push_screen(
            ImportMenuScreen("Single / multiple source", choices),
            self._single_initial_source_chosen,
        )

    def _single_initial_source_chosen(self, index: Optional[int]) -> None:
        if index is None:
            self.action_import()
            return
        if index == 0:
            self._open_file_picker(1)
            return
        try:
            choice = unit_bank_choices()[index - 1]
        except (IndexError, TypeError):
            return
        scope = self._active_card_scope() if choice.scope == "CARD" else choice.scope
        origin = origin_for_unit(scope)
        source = self._load_unit_source(scope, origin)
        if source is None:
            return
        writable = banks.writable_banks()
        self.push_screen(
            TargetBankScreen("Target bank", [b.label for b in writable]),
            lambda ti: self._unit_single_target_chosen(
                source, scope, origin, writable, ti
            ),
        )

    def _unit_single_target_chosen(
        self,
        source: deps.Source,
        scope: str,
        origin: str,
        writable: Sequence[Any],
        target_index: Optional[int],
    ) -> None:
        if target_index is None:
            self._open_single_source_menu()
            return
        try:
            target = writable[target_index]
        except (IndexError, TypeError):
            return
        sources = dict(self._sources_with_store())
        target_source = sources.get(target.id)
        if target_source is None:
            try:
                target_source = self._read_source_offline(target.id)
            except Exception as exc:
                self.notify_status(
                    f"import: could not read {target.label}: {exc}", refused=True
                )
                return
            self._sources[target.id] = target_source
        sources[target.id] = target_source
        entry = make_source_entry(
            label=origin,
            scope=scope,
            source=source,
            origin=origin,
            existing=sources,
            read_only=not self._unit_scope_is_writable(scope),
        )
        sources[entry.scope] = entry.source
        screen = SingleImportScreen(
            path=origin,
            banks=[entry],
            sources=sources,
            target_bank=target,
            target_source=target_source,
            gate=self._write_gate,
            on_source_request=self._single_source_menu,
            on_audition_request=self._audition_focused,
        )
        self._begin_staging_prefetch(screen)
        self.push_screen(
            screen,
            lambda choice: self._import_single_chosen(
                choice, target, entry, sources, origin, [entry], writable
            ),
        )

    def _whole_source_chosen(self, index: Optional[int]) -> None:
        if index is None:
            self.action_import()
            return
        if index == 0:
            self._open_file_picker(0)
            return
        try:
            choice = unit_bank_choices()[index - 1]
        except (IndexError, TypeError):
            return
        scope = self._active_card_scope() if choice.scope == "CARD" else choice.scope
        origin = origin_for_unit(scope)
        source = self._load_unit_source(scope, origin)
        if source is None:
            return
        entry = make_source_entry(
            label=origin,
            scope=scope,
            source=source,
            origin=origin,
            existing={},
            read_only=choice.read_only,
        )
        whole_banks: Any = [entry]
        self.push_screen(
            WholeBankImportScreen(
                path=origin,
                banks=whole_banks,
                gate=self._write_gate,
                card_mode=self._card_mode,
                companion_loaded=self._companion_loaded_snapshot(),
            ),
            lambda picked: self._import_whole_bank_chosen(whole_banks, picked, origin),
        )

    def _load_unit_source(self, scope: str, label: str) -> Optional[deps.Source]:
        source = self._sources_with_store().get(scope)
        if source is None:
            self.notify_status(f"reading {label}…")
            try:
                source = self._read_source_offline(scope)
            except Exception as exc:
                self.notify_status(
                    f"import: could not read {label}: {exc}", refused=True
                )
                return None
            self._sources[scope] = source
        performances = getattr(source, "performances", None) or []
        patches = getattr(source, "patches", None) or []
        if len(performances) != 50 or len(patches) != 35:
            self.notify_status(
                f"import: could not read {label}: incomplete source", refused=True
            )
            return None
        return source

    def _import_path_chosen(self, kind: int, path: str) -> None:
        try:
            banks_list = importers.read_file(path)
        except (importers.UnsupportedFormat, ValueError, OSError) as exc:
            self.notify_status(f"import: {exc}", refused=True)
            return
        if not banks_list:
            self.notify_status("import: no banks found")
            return
        if kind == 0:
            self.push_screen(
                WholeBankImportScreen(
                    path=path,
                    banks=banks_list,
                    gate=self._write_gate,
                    card_mode=self._card_mode,
                    companion_loaded=self._companion_loaded_snapshot(),
                ),
                lambda choice: self._import_whole_bank_chosen(banks_list, choice, path),
            )
            return
        self._open_source_chooser(path, banks_list)

    def _open_source_chooser(self, path: str, banks_list: Sequence[Any]) -> None:
        self.push_screen(
            BankImportScreen(path=path, banks=banks_list),
            lambda index: self._import_single_source_chosen(path, banks_list, index),
        )

    def _save_imported_bank(self, bank: Any) -> None:
        self._sources[bank.scope] = bank.source
        if self._bankstore_dir is not None:
            try:
                bankstore.save(
                    bank.scope,
                    performances=b"".join(bank.source.performances) or None,
                    patches=b"".join(bank.source.patches) or None,
                    waveseq=bank.source.waveseq,
                    directory=self._bankstore_dir,
                )
            except OSError as exc:
                self.notify_status(f"import: {exc}", refused=True)
                return
        perfs = bank.source.performances
        patches = bank.source.patches
        n_perf = sum(1 for r in perfs if r and any(r))
        n_patch = sum(1 for r in patches if r and any(r))
        has_ws = bool(bank.source.waveseq)
        msg = (
            f"imported {bank.label} into cache {bank.scope}: "
            f"{n_perf} performances, {n_patch} patches, "
            f"waveseq={'yes' if has_ws else 'no'}"
        )
        if getattr(bank, "multisets", ()):
            msg += "; multisets not cached"
        self.notify_status(msg)

    def _import_whole_bank_chosen(
        self, banks_list: Sequence[Any], choice: Any, path: str = ""
    ) -> None:
        if choice is None:
            # Back to the file picker, not the browser.
            self._import_kind_chosen(0)
            return
        try:
            bank = banks_list[choice.source_index]
        except (IndexError, TypeError, AttributeError):
            return
        if bank is None:
            return
        if not getattr(choice, "write_to_unit", False):
            self._save_imported_bank(bank)
            return
        self._import_whole_bank_write(bank, choice, banks_list=banks_list, path=path)

    def _whole_bank_bytes(
        self,
        bank: Any,
        sources: Mapping[str, deps.Source],
        target: Any,
        mode: Any,
        written: Optional[Mapping[int, str]] = None,
    ) -> Any:
        """The bytes a whole-bank write sends, chosen by the import mode.

        ``AS_IS`` stays :func:`kws.perfcopy.relocate_bank` byte for byte;
        the self-contained modes plan the chosen source's contained
        performances into the target and build them. Both build the one
        target bank and nothing else (plan R5).

        A self-contained write places one pass at a time (plan R4 c): the
        partition splits the not-yet-written contained performances into
        bank-sized passes, and this write carries the first of them into
        ``target`` — packed through ``Pass.perf_slot_map`` for a
        continuation pass, the mode's own placement for the first one.
        ``written`` is the session's source-slot → target map from the
        earlier passes.
        """
        if mode is perfcopy.ImportMode.AS_IS:
            return perfcopy.relocate_bank(bank.source, target)
        from kws import containment

        # The file the bank came from is not necessarily in the session,
        # so the planner sees it under its own scope.
        plan_sources = dict(sources)
        plan_sources[bank.scope] = bank.source
        audit = containment.audit_containment(plan_sources, bank.scope)
        written_map = dict(written or {})
        remainder = containment.remaining_performances(audit, written_map)
        if not remainder:
            raise ValueError("no remaining performances")
        remaining = set(remainder)
        remainder_audit = replace(
            audit,
            performances=[
                entry for entry in audit.performances if entry.slot in remaining
            ],
        )
        passes = containment.partition_self_contained(bank.source, remainder_audit)
        first = passes[0]
        pass_slots = set(first.slots)
        pass_audit = replace(
            audit,
            performances=[
                entry for entry in audit.performances if entry.slot in pass_slots
            ],
        )
        perf_slots: Optional[Mapping[int, int]] = (
            first.perf_slot_map if written_map else None
        )
        plan = perfcopy.plan_self_contained(
            plan_sources, pass_audit, target, mode, perf_slots=perf_slots
        )
        if not plan.fits:
            raise ValueError("; ".join(plan.rejected))
        return perfcopy.build_self_contained(plan_sources, plan)

    def _resolve_whole_bank_target(self, choice: Any) -> tuple[Any, str]:
        from kws import banks as _banks

        target_scope = str(getattr(choice, "target_bank", "") or "")
        if not target_scope:
            target_scope = "CARD"
        try:
            target = _banks.bank(target_scope)
        except LookupError:
            if target_scope.startswith("CARD/"):
                target = _banks.bank("CARD")
            else:
                raise
        return target, target_scope

    def _read_target_source_for_write(
        self, sources: dict[str, Any], target_scope: str, target: Any
    ) -> Any:
        target_source = sources.get(target_scope)
        if target_source is None and target_scope != getattr(target, "id", None):
            target_source = sources.get(target.id)
        if target_source is None:
            try:
                target_source = self._read_source_offline(target_scope)
            except Exception:
                target_source = self._read_source_offline(target.id)
        return target_source

    def _whole_bank_intent_sources(
        self, banks_list: Sequence[Any]
    ) -> Dict[str, deps.Source]:
        """The file's own sources, keyed by scope: the map the plan showed.

        The approval describes a plan built from the file's banks and
        nothing else -- the import screen's own source map -- so the app
        rebuilds it from exactly those sources, and the write is planned
        from them too. A cache entry that was never on screen must not
        change what was approved, and must not change what is written.
        """
        sources: Dict[str, deps.Source] = {}
        for entry in banks_list:
            scope = getattr(entry, "scope", None)
            source = getattr(entry, "source", None)
            if isinstance(scope, str) and source is not None:
                sources[scope] = source
        return sources

    def _companion_loaded_snapshot(self) -> Dict[str, Tuple[Any, ...]]:
        """The loaded copies' performances, keyed by scope, for the screen.

        The import screen decides a companion choice's branch -- the
        bank's patches and wave sequences only, or the whole bank when
        the loaded copy is empty of performances -- at the moment it
        mints the approval, and the app decides the same branch at the
        write boundary from the same loaded copies. Nothing writes while
        the modal is up, so a snapshot taken here is the state both will
        see; a bank absent from the map counts as not loaded, which is
        the conservative branch.
        """
        snapshot: Dict[str, Tuple[Any, ...]] = {}
        for scope, source in self._sources_with_store().items():
            snapshot[scope] = tuple(getattr(source, "performances", []) or ())
        return snapshot

    def _intent_pass_slots(
        self,
        choice: Any,
        sources: Mapping[str, deps.Source],
        source_scope: str,
    ) -> Tuple[int, ...]:
        """The source slots this visit's write carries, as the screen minted.

        The same derivation every screen dismissal does: a split
        session's ``n`` pass carries the split's remainder; a
        self-contained plain write carries the contained slots no earlier
        pass has written; an as-is write or a first pass carries no pass
        slots at all -- the whole plan is the write.
        """
        mode = getattr(choice, "mode", perfcopy.ImportMode.AS_IS)
        session_key = str(getattr(choice, "session_key", "") or "")
        remainder = self._split_pass_remainder(session_key) if session_key else []
        if (
            remainder
            and self._split_pass_kind(session_key) in ("split", OVERFLOW_CARDS_ONLY)
            and str(getattr(choice, "overflow_kind", "") or "")
            == OVERFLOW_SELF_CONTAINED
        ):
            return tuple(remainder)
        written = self._import_pass_written(session_key)
        if mode is perfcopy.ImportMode.AS_IS or not written:
            return ()
        from kws import containment

        audit = containment.audit_containment(sources, source_scope)
        return tuple(containment.remaining_performances(audit, set(written)))

    def _whole_bank_intent(
        self,
        bank: Any,
        choice: Any,
        banks_list: Optional[Sequence[Any]],
        path: str,
    ) -> Optional[Tuple[import_intent.WholeBankIntent, Dict[str, deps.Source]]]:
        """Check the choice's plan approval before anything can touch the unit.

        The import screen minted a one-shot approval for the exact plan it
        showed; this is where the app holds it against the same plan,
        rebuilt from the same file banks with the same planners. A missing
        token, a missing file, a plan the planners refuse, a fingerprint
        that does not match, or a token already spent refuses here --
        before any device read, backup or write, and before the preflight
        question or the overflow planning runs.

        The nonce is consumed here too, on the main thread, with one
        exception (P4): a companion choice whose write is the patches-and-
        wave-sequences branch goes through
        :meth:`_write_companion_patches`, which validates and consumes the
        same token itself at its own write boundary; consuming it here as
        well would be a double spend. Every other whole-bank write -- the
        plain, overflow, split and all-Init companion branches -- is
        validated and consumed in this one place.
        """
        approval = getattr(choice, "approval", None)
        if approval is None:
            self.notify_status(
                "import: no plan approval for this write; approve it on "
                "the import screen first",
                refused=True,
            )
            return None
        if banks_list is None:
            self.notify_status(
                "import: the plan's file banks are gone; the approval "
                "cannot be checked",
                refused=True,
            )
            return None
        sources = self._whole_bank_intent_sources(banks_list)
        # The branch a companion choice runs is decided here, from the
        # same loaded copies the screen decided it from when it minted:
        # the token must name the branch that actually runs.
        companion_only = bool(getattr(choice, "companion_only", False))
        companion_patches_only = companion_only and (
            self._companion_write_patches_only(choice)
        )
        companion_all_init = companion_only and not companion_patches_only
        try:
            target, _target_scope = self._resolve_whole_bank_target(choice)
            intent = import_intent.build_intent(
                source_id=f"{path}|{bank.scope}",
                source_scope=str(bank.scope),
                sources=sources,
                source=bank.source,
                target=target,
                mode=getattr(choice, "mode", perfcopy.ImportMode.AS_IS),
                overflow_kind=str(
                    getattr(choice, "overflow_kind", OVERFLOW_PATCHES)
                    or OVERFLOW_PATCHES
                ),
                overflow_bank=str(getattr(choice, "overflow_bank", "") or ""),
                preflight_answer=str(getattr(choice, "preflight", "") or ""),
                session_id=str(getattr(choice, "session_key", "") or ""),
                pass_slots=self._intent_pass_slots(choice, sources, str(bank.scope)),
                # The banks this session already landed are part of the
                # plan: a retry's token is a different plan from the
                # first attempt's even where the write list is the same,
                # so the first attempt's consumed token cannot be
                # replayed for the remainder. The screen mints from the
                # same derivation (its pass state carries
                # ``skipped_banks``); both must agree.
                skipped_banks=self._skipped_banks_for(
                    str(getattr(choice, "session_key", "") or "")
                ),
                companion_only=bool(getattr(choice, "companion_only", False)),
                companion_all_init=companion_all_init,
            )
        except (IndexError, LookupError, ValueError, TypeError, AttributeError) as exc:
            self.notify_status(
                f"import: this write cannot be planned: {exc}", refused=True
            )
            return None
        if not import_intent.approval_matches(approval, intent):
            self.notify_status(
                "import: the plan approval does not match this write",
                refused=True,
            )
            return None
        if approval.nonce in self._consumed_plan_nonces:
            self.notify_status(
                "import: this plan approval was already used", refused=True
            )
            return None
        # A companion write on the patches-only branch starts its own
        # worker inside the deferred step; its token is validated and
        # consumed there, at that write's boundary. Consuming it here too
        # would be a double spend.
        if not companion_patches_only:
            self._consumed_plan_nonces.add(approval.nonce)
        return intent, sources

    def _mint_whole_bank_approval(
        self,
        bank: Any,
        choice: Any,
        banks_list: Optional[Sequence[Any]],
        path: str,
        companion_all_init: Optional[bool] = None,
    ) -> Optional[import_intent.PlanApproval]:
        """Mint the approval an answer given after the screen just confirmed.

        The screen mints for the choice it dismisses with. The preflight
        question and the dangling-reference popup are answered
        afterwards, and the answer is what fixes the final plan, so the
        app mints its one-shot approval for the amended choice here --
        from the same file banks, through the same shared builder. A plan
        the planners refuse mints nothing and says why.

        A companion choice's branch -- the bank's patches and wave
        sequences only, or the whole bank when its loaded copy is empty
        of performances -- is part of the plan. ``companion_all_init``
        decides it explicitly when the caller already knows which branch
        the write will run (the preflight chain's companion writes are
        always the patches-only kind); ``None`` decides it from the same
        loaded copies the write boundary will, so a mint made here can
        never disagree with the check that follows it.
        """
        if banks_list is None:
            self.notify_status(
                "import: the plan's file banks are gone; the approval cannot be minted",
                refused=True,
            )
            return None
        sources = self._whole_bank_intent_sources(banks_list)
        if companion_all_init is None:
            companion_all_init = bool(
                getattr(choice, "companion_only", False)
            ) and not self._companion_write_patches_only(choice)
        try:
            target, _target_scope = self._resolve_whole_bank_target(choice)
            intent = import_intent.build_intent(
                source_id=f"{path}|{bank.scope}",
                source_scope=str(bank.scope),
                sources=sources,
                source=bank.source,
                target=target,
                mode=getattr(choice, "mode", perfcopy.ImportMode.AS_IS),
                overflow_kind=str(
                    getattr(choice, "overflow_kind", OVERFLOW_PATCHES)
                    or OVERFLOW_PATCHES
                ),
                overflow_bank=str(getattr(choice, "overflow_bank", "") or ""),
                preflight_answer=str(getattr(choice, "preflight", "") or ""),
                session_id=str(getattr(choice, "session_key", "") or ""),
                pass_slots=self._intent_pass_slots(choice, sources, str(bank.scope)),
                # The same skipped-bank derivation the write boundary
                # rebuilds with, so a mint made here can never disagree
                # with the check that follows it.
                skipped_banks=self._skipped_banks_for(
                    str(getattr(choice, "session_key", "") or "")
                ),
                companion_only=bool(getattr(choice, "companion_only", False)),
                companion_all_init=companion_all_init,
            )
        except (IndexError, LookupError, ValueError, TypeError, AttributeError) as exc:
            self.notify_status(
                f"import: this write cannot be planned: {exc}", refused=True
            )
            return None
        return import_intent.new_approval(intent)

    def _frozen_source(self, source: deps.Source) -> deps.Source:
        """A copy of ``source`` whose records no other code can change.

        The record *lists* are what the planners walk (a session source's
        list can be re-pointed by any later load), and each record and the
        wave-sequence blob are copied as immutable ``bytes``, so a mutable
        fixture record cannot be edited under the snapshot either.
        ``bank_number`` travels with the copy: ``relocate_bank`` needs it
        to recognise the source bank's own references.
        """
        return deps.Source(
            scope_id=source.scope_id,
            performances=[bytes(record) for record in source.performances or ()],
            patches=[bytes(record) for record in source.patches or ()],
            waveseq=None if source.waveseq is None else bytes(source.waveseq),
            bank_number=source.bank_number,
        )

    def _frozen_plan_sources(
        self, bank: Any, sources: Mapping[str, deps.Source]
    ) -> Tuple[_FrozenPlanBank, Dict[str, deps.Source]]:
        """Snapshot the plan's inputs once, on the main thread (P7b).

        The token is validated against the live records, and the worker
        must plan and build from exactly what was validated. Between the
        two, other code can run -- another device worker's completion
        lands on the main thread and may re-point a session source's
        records -- so the whole-bank write carries this frozen snapshot
        into the worker instead: every scope becomes a fresh
        :class:`kws.deps.Source` with copied lists, and the source bank
        becomes a frozen stand-in over its own snapshot, so the worker's
        builders (``relocate_bank``, ``build_self_contained``,
        ``build_overflow_import``, ``build_split_partition``) read only
        records no other code can mutate. The session and cache updates
        stay on the main thread, in ``done``; the worker still only talks
        to the device.
        """
        frozen: Dict[str, deps.Source] = {}
        for scope, source in sources.items():
            frozen[scope] = self._frozen_source(source)
        scope = str(getattr(bank, "scope", "") or "")
        bank_source = getattr(bank, "source", None)
        if bank_source is not None and scope and scope not in frozen:
            frozen[scope] = self._frozen_source(bank_source)
        snapshot = frozen.get(scope) or (
            self._frozen_source(bank_source) if bank_source is not None else None
        )
        if snapshot is None:
            raise ValueError("the write's source bank carries no records")
        return (
            _FrozenPlanBank(
                label=str(getattr(bank, "label", "") or ""),
                scope=scope,
                source=snapshot,
            ),
            frozen,
        )

    def _import_whole_bank_write(
        self,
        bank: Any,
        choice: Any,
        *,
        banks_list: Optional[Sequence[Any]] = None,
        path: str = "",
    ) -> None:
        if not self._write_gate.armed:
            self.notify_status("write gate is locked", refused=True)
            return
        intent_and_sources = self._whole_bank_intent(bank, choice, banks_list, path)
        if intent_and_sources is None:
            return
        _intent, sources = intent_and_sources
        if self._deferred_companion_step(bank, choice, banks_list, path):
            return
        from kws import banks as _banks

        # The plan's inputs are frozen here, once, on the main thread:
        # everything below -- the split planning and the worker's own
        # planning and building -- reads this snapshot, never the live
        # session sources, so what was approved is what is sent even if
        # another worker's completion mutates a source in between (P7b).
        plan_bank, sources = self._frozen_plan_sources(bank, sources)
        target, target_scope = self._resolve_whole_bank_target(choice)
        if target_scope.startswith("CARD/"):
            # The unit has one card slot: a write to CARD/07 lands on
            # whatever card is in the slot. Refuse unless the slot holds
            # that card, or the bytes would overwrite the wrong physical
            # card while being filed under the requested archive scope.
            try:
                self._guard_card_slot(target_scope)
            except ValueError as exc:
                self.notify_status(str(exc), refused=True)
                return
        # The write is planned from the same file sources the approval
        # was checked against: what was approved is what will be sent.
        label = getattr(bank, "label", "")
        mode = getattr(choice, "mode", perfcopy.ImportMode.AS_IS)
        # Only a pass flow (ctrl+s / n) carries a session key. Companion
        # writes leave it empty so they neither continue nor pollute a
        # session: repeating a self-contained import starts over.
        session_key = str(getattr(choice, "session_key", "") or "")
        written = self._import_pass_written(session_key)
        conflict = self._preaudit_conflicting_slots(
            target,
            range(_banks.PERFORMANCES_PER_BANK),
            range(_banks.PATCHES_PER_BANK),
        )

        overflow_id = str(getattr(choice, "overflow_bank", "") or "")
        if overflow_id:
            overflow_conflict = self._preaudit_conflicting_slots(
                _banks.bank(overflow_id),
                range(_banks.PERFORMANCES_PER_BANK),
                range(_banks.PATCHES_PER_BANK),
            )
            conflict = "; ".join(filter(None, (conflict, overflow_conflict)))
        split_plan, proceed, split_remaining = self._split_choice_plan(
            plan_bank, sources, target, mode, choice
        )
        if not proceed:
            return
        cards_only = self._cards_only_choice(choice, session_key)
        split_statement = self._split_choice_statement(
            split_plan, session_key, split_remaining, cards_only
        )

        # The session's overflow marker is read here, on the main thread:
        # a worker only ever talks to the device.
        overflow_skip = self._overflow_session_written(session_key, overflow_id)

        def work() -> Any:
            return self._whole_write_work(
                plan_bank,
                sources,
                target,
                mode,
                written,
                split_plan,
                overflow_id,
                skip_overflow=overflow_skip,
            )

        def done(result: Any, failure: BaseException | None) -> None:
            self._whole_write_done(
                result,
                failure,
                choice=choice,
                label=label,
                target=target,
                target_scope=target_scope,
                conflict=conflict,
                banks_list=banks_list,
                path=path,
                split_remainder=split_remaining,
            )

        into = target_scope if target_scope != target.id else target.label
        status = self._whole_write_status(
            label,
            into,
            conflict,
            split_statement,
            cards_only=cards_only,
            overflow_note=self._overflow_status_note(session_key, overflow_id),
        )
        self.notify_status(status, warn=bool(conflict))
        self._device_worker(work, done, f"importing {label} into {into}")

    def _overflow_session_written(self, key: str, overflow_id: str) -> bool:
        """Whether this import session already landed ``overflow_id``.

        A patches-only overflow whose target write failed has left the
        overflow bank on the unit: the patches the target's performances
        name are there. The retry must not send them again -- the
        marker is per session key, and a fresh visit of the same file
        starts a new session, so it never leaks across imports.
        """
        if not key or not overflow_id:
            return False
        state = self._import_passes.get(key) or {}
        return str(state.get("overflow_written") or "") == overflow_id

    def _skipped_banks_for(self, key: str) -> Tuple[str, ...]:
        """The physical banks this import session already landed, sorted.

        This is the one derivation both the mint and the rebuild use: a
        token minted for a retry of a partial attempt and the plan the
        write boundary rebuilds from that same token must agree on which
        banks the session has already landed, or every retry would be
        refused. Two flows record what landed:

        - a patches-only overflow whose target write failed leaves
          ``overflow_written`` in the session state -- the retry sends
          the target only;
        - a split or cards-only pass records the landed groups'
          physical bank ids as ``banks_written``.

        A fresh session key maps to no state, so a first attempt (and
        every companion write, which carries no session key at all)
        skips nothing.
        """
        if not key:
            return ()
        state = self._import_passes.get(key) or {}
        skipped: Set[str] = set()
        overflow = str(state.get("overflow_written") or "")
        if overflow:
            skipped.add(overflow)
        for bank_id in state.get("banks_written") or ():
            if bank_id:
                skipped.add(str(bank_id))
        return tuple(sorted(skipped))

    def _overflow_status_note(self, key: str, overflow_id: str) -> str:
        """The line the pre-write status carries when the overflow landed.

        Skipping the overflow write silently would hide where the bytes
        go: the status says the target is all this pass sends.
        """
        if not self._overflow_session_written(key, overflow_id):
            return ""
        try:
            overflow_label = banks.bank(overflow_id).label
        except LookupError:
            overflow_label = overflow_id
        return (
            f"the overflow bank {overflow_label} already landed; "
            "this write sends the target only"
        )

    def _whole_write_work(
        self,
        bank: Any,
        sources: Mapping[str, deps.Source],
        target: Any,
        mode: Any,
        written: Mapping[int, str],
        split_plan: Any,
        overflow_id: str,
        skip_overflow: bool = False,
    ) -> Any:
        """The bytes one whole-bank write sends, chosen by the flow.

        A self-contained split writes each bank's share, a patches-only
        overflow writes the overflow bank first and then the target, and
        the plain write builds the one target bank (plan R5). A split or
        overflow flow returns a :class:`MultiBankOutcome` rather than
        raising away the banks that already landed. ``bank`` and
        ``sources`` are the frozen snapshot taken at the write boundary
        (P7b): the worker plans and builds from it alone, never from the
        session's live sources.
        """
        from kws import backup as _backup

        if split_plan is not None:
            return self._write_split_banks(split_plan, _backup.default_dir())
        if overflow_id:
            return self._write_with_overflow(
                bank,
                sources,
                target,
                mode,
                _backup.default_dir(),
                overflow_id,
                skip_overflow=skip_overflow,
            )
        built = self._whole_bank_bytes(bank, sources, target, mode, written=written)
        # The bridge reads the unit before writing it. Handing it the
        # cached bank instead would (a) skip the write when the file
        # matches the cache -- telling the user it wrote a card it never
        # touched -- and (b) back up bytes that may not be on the unit.
        result = self.device.write_bank_bundle(
            target.number,
            performances=built.performances,
            patches=built.patches,
            waveseq=built.waveseq,
            backup_dir=_backup.default_dir(),
            before_blocks=None,
        )
        return built, result

    @staticmethod
    def _whole_write_status(
        label: str,
        into: str,
        conflict: str,
        split_statement: str,
        *,
        cards_only: bool = False,
        overflow_note: str = "",
    ) -> str:
        """What is about to be written, per bank when it is a split.

        The plain and patches-only wordings are unchanged: ``writing X into
        Y...``, and the same line behind the pre-audit warning. A
        self-contained split replaces the destination with each bank's
        share, because one destination name would hide the second write.
        A cards-only pass says so, and the statement names the card and
        how much is left for the next one. ``overflow_note`` states a
        retry that sends the target only, because the overflow bank
        already landed in this session.
        """
        if split_statement:
            flow = "cards-only groups" if cards_only else "a self-contained split"
            status = f"writing {label} as {flow} — {split_statement}"
        else:
            status = f"writing {label} into {into}..."
        if overflow_note:
            status = f"{status} — {overflow_note}"
        if conflict:
            suffix = status if split_statement else "writing anyway"
            return f"warning: this import overwrites {conflict} — {suffix}"
        return status

    def _whole_write_done(
        self,
        result: Any,
        failure: BaseException | None,
        *,
        choice: Any,
        label: str,
        target: Any,
        target_scope: str,
        conflict: str,
        banks_list: Optional[Sequence[Any]],
        path: str,
        split_remainder: Optional[Sequence[int]] = None,
    ) -> None:
        """Report one fenced whole-bank write and offer what comes next."""
        if failure is not None:
            self.notify_status(f"import: {failure}", refused=True)
            return
        if isinstance(result, MultiBankOutcome):
            # A cross-bank flow: the outcome carries what landed, what
            # failed and what was never attempted.
            self._multi_bank_done(
                result,
                choice=choice,
                label=label,
                target=target,
                target_scope=target_scope,
                banks_list=banks_list,
                path=path,
                split_remainder=split_remainder,
            )
            return
        built, write_result = result
        verdict = self._bundle_result_error(write_result)
        if verdict:
            # The unit did not store what was sent: a failed bank, whose
            # backup is the way back. No undo record, no cache update, no
            # pass state -- and nothing says the import succeeded.
            self.notify_status(f"import: {verdict}", refused=True)
            return
        into = target_scope if target_scope != target.id else target.label
        self._import_whole_bank_written(
            label, target, built, write_result, conflict, target_scope=target_scope
        )
        mode = getattr(choice, "mode", perfcopy.ImportMode.AS_IS)
        session_key = str(getattr(choice, "session_key", "") or "")
        if mode is perfcopy.ImportMode.AS_IS or not session_key:
            # An as-is (or sessionless) write leaves every part pointing
            # where the file points; the file may carry the bank they name.
            # A failure here must not look like a failed write: say so and
            # let the person carry on.
            try:
                self._offer_companions_after_write(choice, None, banks_list, path)
            except Exception as exc:
                self.notify_status(
                    f"imported, but the follow-up check failed: {exc}",
                    refused=True,
                )
            return
        self._import_pass_after_write(
            choice, session_key, built, into, banks_list, path, mode
        )

    def _multi_bank_done(
        self,
        outcome: MultiBankOutcome,
        *,
        choice: Any,
        label: str,
        target: Any,
        target_scope: str,
        banks_list: Optional[Sequence[Any]],
        path: str,
        split_remainder: Optional[Sequence[int]] = None,
    ) -> None:
        """Dispatch a cross-bank write's structured outcome.

        A full success takes the exact path and wording today's success
        takes; a partial one is reported as what it is -- some banks
        landed, some did not -- without ever claiming the import
        succeeded.
        """
        into = target_scope if target_scope != target.id else target.label
        overflow_id = str(getattr(choice, "overflow_bank", "") or "")
        overflow_kind = str(
            getattr(choice, "overflow_kind", OVERFLOW_PATCHES) or OVERFLOW_PATCHES
        )
        session_key = str(getattr(choice, "session_key", "") or "")
        split_pass = bool(session_key) and (
            split_remainder is not None
            or self._split_pass_kind(session_key) in ("split", OVERFLOW_CARDS_ONLY)
        )
        if not outcome.ok:
            self._multi_bank_partial(
                outcome,
                choice=choice,
                label=label,
                target=target,
                target_scope=target_scope,
                banks_list=banks_list,
                path=path,
                split_remainder=split_remainder,
            )
            return
        if overflow_id and overflow_kind == OVERFLOW_SELF_CONTAINED and not split_pass:
            # A split the offered banks hold in full: nothing continues.
            self._split_written(outcome, label)
            return
        if split_pass:
            self._split_pass_done(
                outcome, choice, label, banks_list, path, split_remainder
            )
            return
        if overflow_id:
            self._overflow_written(
                outcome, label, into, target, target_scope, overflow_id
            )
            return
        # A cross-bank flow without a session still has every bank's
        # bookkeeping to do; the split report names each bank it wrote.
        self._split_written(
            outcome, label, cards_only=self._cards_only_choice(choice, session_key)
        )

    def _multi_bank_partial(
        self,
        outcome: MultiBankOutcome,
        *,
        choice: Any,
        label: str,
        target: Any,
        target_scope: str,
        banks_list: Optional[Sequence[Any]],
        path: str,
        split_remainder: Optional[Sequence[int]] = None,
    ) -> None:
        """Book-keep and report a cross-bank write that stopped part way.

        The banks that landed are recorded exactly as a full success
        records them -- undo, session, cache -- and the failed and
        unattempted banks get nothing. The status names what was
        written and what was not, and never says the import succeeded.
        """
        overflow_id = str(getattr(choice, "overflow_bank", "") or "")
        overflow_kind = str(
            getattr(choice, "overflow_kind", OVERFLOW_PATCHES) or OVERFLOW_PATCHES
        )
        session_key = str(getattr(choice, "session_key", "") or "")
        if not outcome.completed:
            # Nothing landed: today's plain failure, and nothing recorded.
            self.notify_status(f"import: {outcome.error}", refused=True)
            return
        for entry in outcome.completed:
            if entry.group is not None:
                scope = getattr(
                    entry.group.target_bank, "id", str(entry.group.target_bank)
                )
                self._remember_written_bank(
                    entry.group.target_bank,
                    entry.built,
                    entry.result,
                    target_scope=scope,
                )
            elif overflow_id and getattr(entry.bank, "id", "") == overflow_id:
                if hasattr(self, "_record_write"):
                    self._record_write(entry.result)
                self._remember_overflow(entry.bank, outcome.built)
            else:
                self._remember_written_bank(
                    target,
                    outcome.built.destination,
                    entry.result,
                    target_scope=target_scope,
                )
        mode = getattr(choice, "mode", perfcopy.ImportMode.AS_IS)
        split_pass = bool(session_key) and (
            split_remainder is not None
            or self._split_pass_kind(session_key) in ("split", OVERFLOW_CARDS_ONLY)
        )
        if split_pass:
            self._multi_bank_partial_split(
                outcome,
                choice,
                mode,
                banks_list,
                path,
                session_key,
                split_remainder,
            )
            return
        message = self._multi_bank_partial_status(outcome)
        landed_overflow = bool(overflow_id) and any(
            getattr(entry.bank, "id", "") == overflow_id for entry in outcome.completed
        )
        patches_overflow = landed_overflow and overflow_kind == OVERFLOW_PATCHES
        if patches_overflow:
            # The overflow bank carries the patches the target's
            # performances name: it landed, the target did not. Mark the
            # session so a retry sends the target only, and say so.
            message += self._patches_overflow_partial_note(overflow_id, session_key)
        self.notify_status(message, refused=True)
        if (
            patches_overflow
            and session_key
            and banks_list
            and getattr(choice, "continue_pass", False)
            and getattr(self, "is_running", False)
        ):
            # The screen comes back with the same session (P7c): its pass
            # state carries this session key and the landed overflow bank
            # as ``skipped_banks``, so a retry minted there is the same
            # plan the write boundary rebuilds from the session's state --
            # and the boundary then skips the landed bank itself. Without
            # the re-offer, the next visit minted a fresh session's plan
            # and rewrote the bank that already landed.
            self._reopen_whole_bank_import(banks_list, path, session_key, mode)

    def _patches_overflow_partial_note(self, overflow_id: str, key: str) -> str:
        """Mark the session's landed overflow bank, and say what is left.

        The session keeps ``overflow_written`` so the retry's mint and
        the write boundary both skip the landed bank; the note is the
        line the partial status carries, and it never says the import
        succeeded.
        """
        if key:
            state = self._import_passes.setdefault(key, {"written": {}, "targets": []})
            state["overflow_written"] = overflow_id
        try:
            overflow_label = banks.bank(overflow_id).label
        except LookupError:
            overflow_label = overflow_id
        return (
            f" — the overflow bank {overflow_label} holds the patches "
            "the target's performances name; write the target again"
        )

    def _multi_bank_partial_split(
        self,
        outcome: MultiBankOutcome,
        choice: Any,
        mode: Any,
        banks_list: Optional[Sequence[Any]],
        path: str,
        session_key: str,
        split_remainder: Optional[Sequence[int]],
    ) -> None:
        """Keep a failed split pass's landed groups and re-offer ``n``.

        The groups that landed are in the session's written map; the
        groups the failure hit or stopped before go back into the
        remainder, so the next pass writes exactly the not-written
        source slots and never rewrites a landed group.
        """
        cards_only = self._cards_only_choice(choice, session_key)
        if session_key:
            written_now = self._record_split_pass_groups(session_key, outcome)
            prior = (
                list(split_remainder)
                if split_remainder is not None
                else self._split_pass_remainder(session_key)
            )
            placed = set(written_now)
            remaining = [slot for slot in prior if slot not in placed]
            # The groups the failure stopped before were not in the
            # first pass's own leftover: their slots go back too.
            lost = {
                int(slot)
                for group in outcome.skipped_groups
                for slot in group.performances
            }
            remaining += [
                slot for slot in lost if slot not in placed and slot not in remaining
            ]
            self._record_split_session_state(
                session_key, written_now, remaining, choice, mode, cards_only
            )
        self.notify_status(self._multi_bank_partial_status(outcome), refused=True)
        if (
            session_key
            and getattr(choice, "continue_pass", False)
            and banks_list
            and getattr(self, "is_running", False)
        ):
            self._reopen_whole_bank_import(banks_list, path, session_key, mode)

    def _multi_bank_partial_status(self, outcome: MultiBankOutcome) -> str:
        """One honest line for a cross-bank write that stopped part way.

        The error leads; the completed banks follow with their backups,
        then the banks the write never reached. Nothing here says the
        import succeeded.
        """
        parts = [f"import: {outcome.error}"]
        written = "; ".join(
            f"{getattr(entry.bank, 'label', entry.bank)} (verified; "
            f"{self._counted(self._backup_count(entry.result), 'backup', 'backups')})"
            for entry in outcome.completed
        )
        if written:
            parts.append(f"written: {written}")
        if outcome.failed_bank is not None:
            # The write stopped on this bank. A refused transfer may have
            # changed nothing; a read-back mismatch or a mid-transfer error
            # may have changed it -- the honest word is "unverified".
            parts.append(
                f"failed or unverified (may have changed): "
                f"{getattr(outcome.failed_bank, 'label', outcome.failed_bank)}"
            )
        not_written = ", ".join(
            getattr(bank, "label", str(bank)) for bank in outcome.not_written
        )
        if not_written:
            parts.append(f"not written: {not_written}")
        return " — ".join(parts)

    @staticmethod
    def _backup_count(result: Any) -> int:
        """The backup envelopes a write result names: one bundle, one backup."""
        if not isinstance(result, dict):
            return 0
        if isinstance(result.get("backups"), list):
            return len(result["backups"])
        return 1 if result.get("backup") else 0

    @staticmethod
    def _import_left_dangling(choice: Any) -> bool:
        """Whether this write leaves parts pointing at another bank.

        Only as-is does: the self-contained modes select performances that
        already point at their own bank or ROM, so a question about a
        companion would describe a write the person did not ask for.
        """
        if getattr(choice, "mode", perfcopy.ImportMode.AS_IS) is not (
            perfcopy.ImportMode.AS_IS
        ):
            return False
        kind = str(
            getattr(choice, "overflow_kind", OVERFLOW_PATCHES) or OVERFLOW_PATCHES
        )
        if kind in (OVERFLOW_SELF_CONTAINED, OVERFLOW_CARDS_ONLY):
            # The performances that pointed outside travelled with all
            # their patches: both banks are self-contained already.
            return False
        # A patches-only overflow wrote the patches and repointed the
        # parts at them: complete as well.
        return not str(getattr(choice, "overflow_bank", "") or "")

    def _companions_named_by_parts(
        self,
        bank: Any,
        banks_list: Sequence[Any],
        source_index: int,
        *,
        require_missing: bool = True,
    ) -> List[Tuple[int, int, Any]]:
        """``(part count, file index, bank)`` for companions the parts name.

        With ``require_missing`` the companion is only reported when the
        *loaded copy* does not already show those patches: the post-write
        fallback asks only when something is actually missing. The
        preflight passes ``False`` -- it asks every time, naming the
        loaded copy's state in the question itself.
        """
        from kws import containment

        try:
            scope = str(bank.scope)
            sources = dict(self._sources_with_store())
            sources[scope] = bank.source
            audit = containment.audit_containment(sources, scope)
        except (LookupError, ValueError, TypeError, AttributeError):
            return []
        named: List[Tuple[int, int, Any]] = []
        for index, entry in enumerate(banks_list):
            if index == source_index or entry is bank:
                continue
            companion = _entry_bank(entry)
            if companion is None:
                continue
            count = audit.parts_per_bank.get(companion.number, 0)
            if not count:
                continue
            slots = audit.patch_records.get(companion.number, ())
            # ``companion`` is the bank table's entry; the file's records are
            # on the imported bank, which is what the unit must match.
            # The preflight asks even when the unit already matches, so the
            # question is predictable; the post-write fallback only asks
            # when something is actually missing.
            file_source = getattr(entry, "source", None)
            if require_missing and self._unit_matches(companion, file_source, slots):
                continue
            named.append((count, index, companion))
        return named

    def _unit_matches(
        self, companion: Any, file_source: Any, slots: Sequence[int]
    ) -> bool:
        """Whether the *loaded copy* of ``companion`` shows those patches.

        The loaded copy is the session's bank or the cache -- never a
        fresh reading of the unit -- so a ``True`` here is a snapshot,
        not hardware truth. The wave sequences are compared too when the
        file carries them: identical patch bytes that sit on top of
        different sequence bytes are not the patches the file means.
        """
        current = self._sources_with_store().get(companion.id)
        if current is None or file_source is None:
            return False
        file_patches = list(getattr(file_source, "patches", []) or [])
        for slot in slots:
            if slot >= len(current.patches) or slot >= len(file_patches):
                return False
            if bytes(current.patches[slot]) != bytes(file_patches[slot]):
                return False
        file_waveseq = getattr(file_source, "waveseq", None)
        matches_waveseq = file_waveseq is None or bytes(
            current.waveseq or b""
        ) == bytes(file_waveseq)
        return matches_waveseq

    def _deferred_companion_step(
        self,
        bank: Any,
        choice: Any,
        banks_list: Optional[Sequence[Any]],
        path: str,
    ) -> bool:
        """Run what stands between this choice and its write, if anything.

        Either the preflight question -- asked before anything is sent,
        with cancel writing nothing at all -- or the patches-only
        companion write handles the choice here. ``False`` says the
        ordinary whole-bank write may proceed.
        """
        named = self._preflight_companion(bank, choice, banks_list, path)
        if named and banks_list is not None:
            # Nothing has been sent: the question decides what travels.
            self._ask_preflight(bank, choice, banks_list, path, named)
            return True
        if self._companion_write_patches_only(choice):
            self._write_companion_patches_only(bank, choice, banks_list, path)
            return True
        return False

    def _preflight_companion(
        self,
        bank: Any,
        choice: Any,
        banks_list: Optional[Sequence[Any]],
        path: str,
    ) -> Optional[List[Tuple[int, int, Any]]]:
        """The companions a plain write should ask about before it runs.

        The question is asked when the choice comes from the import
        screen's own write keys (ctrl+s or n, which carry
        ``continue_pass``), the write is a plain one -- no overflow of its
        own, nothing answered yet, no split session continuing -- and the
        audit of the source bank shows parts naming writable banks that
        the same file carries. Every such bank is listed, most-referenced
        first, as ``(part count, file index, bank)``; a bank the parts
        name but the file does not carry cannot be written here, so it is
        not a question. The target being a named bank itself is not a
        question either: those parts already point where they will play.
        """
        if not banks_list or not path:
            return None
        if not getattr(self, "is_running", False):
            return None
        if not bool(getattr(choice, "continue_pass", False)):
            return None
        if str(getattr(choice, "preflight", "") or ""):
            return None
        if not self._import_left_dangling(choice):
            return None
        session_key = str(getattr(choice, "session_key", "") or "")
        if session_key and self._split_pass_kind(session_key) in (
            "split",
            OVERFLOW_CARDS_ONLY,
        ):
            return None
        try:
            target, _target_scope = self._resolve_whole_bank_target(choice)
            source_index = int(getattr(choice, "source_index", 0) or 0)
        except (LookupError, TypeError, ValueError):
            return None
        # Ask even when the unit already holds those patches: whether the
        # bank was loaded before is exactly the kind of thing a person
        # should be told before a write, not inferred for them.
        named = self._companions_named_by_parts(
            bank, banks_list, source_index, require_missing=False
        )
        named = [item for item in named if item[2].number != target.number]
        if not named:
            return None
        # The most-referenced companion comes first; ties keep the file's
        # own order, so the list is stable from one visit to the next.
        named.sort(key=lambda item: (-item[0], item[1]))
        return named

    def _ask_preflight(
        self,
        bank: Any,
        choice: Any,
        banks_list: Optional[Sequence[Any]],
        path: str,
        named: Sequence[Tuple[int, int, Any]],
    ) -> None:
        """Offer the three answers while nothing has been written yet.

        Every named companion is listed, one line each, with its part
        count and how many of its patches the performances use. The state
        of each bank comes from the loaded copies -- the session and the
        cache -- never from the unit, and the modal says so: the unit is
        not re-read before the question is asked.
        """
        if banks_list is None:
            return
        lines = self._preflight_companion_lines(bank, banks_list, named)
        listed = "\n".join(
            f"· {label} — {self._counted(count, 'part', 'parts')} name it, "
            f"{patches} of its patch slots are used"
            + (" · its loaded copy already shows these patches" if loaded else "")
            for label, count, patches, loaded in lines
        )
        labels = ", ".join(label for label, _count, _patches, _loaded in lines)
        body = (
            f"The performances in {getattr(bank, 'label', '')} name banks "
            f"this file also carries ({labels}). Nothing has been written "
            f"yet.\n\n{listed}\n\n"
            "These states are a snapshot of the loaded copies, not of the "
            "unit: the unit is not re-read before this question."
        )
        choices = (
            "Write every named bank and this bank — each named bank's "
            "patches and wave sequences only (its performances stay as "
            "they are), then this bank",
            f"Write only this bank — those parts will play whatever {labels} hold now",
            "Cancel — open the import screen to pick a self-contained or "
            "cards-only pass",
        )
        # Push on the next refresh, not inside the import screen's own
        # dismissal: a screen pushed in that same turn can be popped
        # again while the stack settles, which is the trap the dangling
        # popup already avoids.
        self.call_after_refresh(
            self._push_preflight,
            body,
            choices,
            bank,
            choice,
            banks_list,
            path,
            named,
        )

    def _preflight_companion_lines(
        self,
        bank: Any,
        banks_list: Sequence[Any],
        named: Sequence[Tuple[int, int, Any]],
    ) -> List[Tuple[str, int, int, bool]]:
        """``(label, parts, patches, loaded)`` per named companion.

        ``loaded`` says whether the *loaded copy* of that bank -- session
        or cache, never the unit -- already shows the patches the parts
        name. It is evidence about what this app has seen, not about what
        the instrument holds right now.
        """
        from kws import containment

        try:
            scope = str(bank.scope)
            sources = dict(self._sources_with_store())
            sources[scope] = bank.source
            audit = containment.audit_containment(sources, scope)
        except (LookupError, ValueError, TypeError, AttributeError):
            audit = None
        lines: List[Tuple[str, int, int, bool]] = []
        for count, index, companion in named:
            entry = banks_list[index] if index < len(banks_list) else None
            file_source = getattr(entry, "source", None)
            slots = (
                audit.patch_records.get(companion.number, ())
                if audit is not None
                else ()
            )
            loaded = self._unit_matches(companion, file_source, slots)
            lines.append((companion.label, count, len(slots), loaded))
        return lines

    def _push_preflight(
        self,
        body: str,
        choices: Sequence[str],
        bank: Any,
        choice: Any,
        banks_list: Sequence[Any],
        path: str,
        named: Sequence[Tuple[int, int, Any]],
    ) -> None:
        """Put the preflight modal on the stack."""
        self.push_screen(
            PreflightImportScreen(body, choices=choices),
            lambda answer: self._preflight_answer(
                answer, bank, choice, banks_list, path, named
            ),
        )

    def _preflight_answer(
        self,
        answer: Optional[str],
        bank: Any,
        choice: Any,
        banks_list: Sequence[Any],
        path: str,
        named: Sequence[Tuple[int, int, Any]],
    ) -> None:
        """Carry out the preflight's answer; cancel writes nothing."""
        if answer == "both":
            self._preflight_write_both(bank, choice, banks_list, path, named)
            return
        if answer == "only":
            if not self._write_gate.armed:
                self.notify_status("write gate is locked", refused=True)
                return
            # The answer is what fixes the final plan: the approval for it
            # is minted here, and the write boundary checks it like any
            # other.
            only = replace(choice, preflight="only")
            approval = self._mint_whole_bank_approval(bank, only, banks_list, path)
            if approval is None:
                return
            self._import_whole_bank_write(
                bank,
                replace(only, approval=approval),
                banks_list=banks_list,
                path=path,
            )
            return
        # Cancel: no write at all. The screen comes back with the two
        # ways out of the dilemma it opened with.
        session_key = str(getattr(choice, "session_key", "") or "")
        mode = getattr(choice, "mode", perfcopy.ImportMode.AS_IS)
        self._reopen_whole_bank_import(
            banks_list,
            path,
            session_key,
            mode,
            notice=(
                "nothing was written — press o for a self-contained or "
                "cards-only pass that repoints those parts, or x to write "
                "the companion banks they name"
            ),
        )

    def _preflight_write_both(
        self,
        bank: Any,
        choice: Any,
        banks_list: Sequence[Any],
        path: str,
        named: Sequence[Tuple[int, int, Any]],
    ) -> None:
        """The preflight's first answer: every named companion, then the bank.

        Each companion's patches and wave sequences go out first, in the
        listed order, because a performance that names them must find
        them already on the unit; their performances are never sent, so
        those RAM banks keep whatever performances they hold. The bank's
        own write follows only after every companion has landed, marked
        so neither the preflight nor the post-write popup runs again. A
        companion that fails stops the run: what was written and what was
        not is reported, and the source bank is not sent. The ordered
        banks are said here, before the chain starts (P4): one approved
        token per physical write, in this order.
        """
        if not self._write_gate.armed:
            self.notify_status("write gate is locked", refused=True)
            return
        order = [
            f"{companion.label} (patches and wave sequences only)"
            for _count, _index, companion in named
        ]
        order.append(
            f"{getattr(bank, 'label', '') or 'the source bank'} (the whole bank)"
        )
        self.notify_status(
            "import: the writes, in order — "
            + "; ".join(order)
            + "; each bank is backed up and verified, and needs its own "
            "approval before it is sent"
        )
        self._preflight_companion_chain(bank, choice, banks_list, path, named, 0, [])

    def _preflight_companion_chain(
        self,
        bank: Any,
        choice: Any,
        banks_list: Sequence[Any],
        path: str,
        named: Sequence[Tuple[int, int, Any]],
        index: int,
        completed: List[str],
    ) -> None:
        """Write companion ``index``, then the rest, then the source bank.

        Each companion is its own fenced write, and its own approved one
        (P4): a token is minted for exactly that companion's write
        immediately before it runs, and the write validates and consumes
        it before any device I/O. ``completed`` carries the
        labels of the companions that already landed, so a later failure
        can name exactly what succeeded and what was never written.
        """
        if index >= len(named):
            # Every companion answered for: the source bank's own write is
            # the "both" plan, approved by the answer just given.
            both = replace(choice, preflight="both")
            approval = self._mint_whole_bank_approval(bank, both, banks_list, path)
            if approval is None:
                return
            self._import_whole_bank_write(
                bank,
                replace(both, approval=approval),
                banks_list=banks_list,
                path=path,
            )
            return
        _count, companion_index, companion = named[index]
        try:
            companion_entry = banks_list[companion_index]
        except (IndexError, TypeError):
            return
        pending = [item[2].label for item in named[index:]]
        pending.append(getattr(bank, "label", "") or "the source bank")
        note = (
            "written so far: "
            + (", ".join(completed) if completed else "nothing")
            + "; not written: "
            + ", ".join(pending)
        )
        # The companion's write is its own approved request (P4): one
        # token per physical bank write, minted here, immediately before
        # this companion's write, and consumed by it. The chain's
        # companion writes are always the patches-only kind, so the
        # branch is decided explicitly, never inferred from the bank's
        # loaded copy.
        mode = getattr(choice, "mode", perfcopy.ImportMode.AS_IS)
        companion_choice = WholeBankChoice(
            companion_index, companion.id, True, mode, companion_only=True
        )
        approval = self._mint_whole_bank_approval(
            companion_entry,
            companion_choice,
            banks_list,
            path,
            companion_all_init=False,
        )
        if approval is None:
            self.notify_status(
                self._companion_failure_message(
                    "the next companion could not be approved", note
                ),
                refused=True,
            )
            return

        def after_companion() -> None:
            completed.append(companion.label)
            self._preflight_companion_chain(
                bank, choice, banks_list, path, named, index + 1, completed
            )

        self._write_companion_patches(
            companion_entry,
            companion,
            after_companion,
            approval=approval,
            banks_list=banks_list,
            path=path,
            mode=mode,
            pending_note=note,
        )

    def _companion_write_patches_only(self, choice: Any) -> bool:
        """Whether a companion write must leave the bank's performances alone.

        The branch a companion choice will run, decided by the shared
        rule (:func:`kwsed.import_intent.companion_write_patches_only`)
        from the loaded copy of the chosen bank -- the session's or the
        cache's, never a fresh reading of the unit. The import screen
        decided the same branch from the snapshot it was opened with, so
        the token it minted names the branch that actually runs.
        """
        if not bool(getattr(choice, "companion_only", False)):
            return False
        bank_id = str(getattr(choice, "target_bank", "") or "")
        current = self._sources_with_store().get(bank_id)
        loaded: Optional[Sequence[Any]] = None
        if current is not None:
            performances = list(getattr(current, "performances", []) or [])
            loaded = tuple(performances)
        return import_intent.companion_write_patches_only(loaded)

    def _write_companion_patches_only(
        self,
        bank: Any,
        choice: Any,
        banks_list: Optional[Sequence[Any]],
        path: str,
    ) -> None:
        """The ``x`` companion write, patches and wave sequences only.

        The choice carries the token the screen minted for exactly this
        companion write; the write validates and consumes it itself,
        before anything touches the unit.
        """
        try:
            companion, _companion_scope = self._resolve_whole_bank_target(choice)
        except (LookupError, ValueError):
            return
        approval = getattr(choice, "approval", None)
        if approval is None:
            self.notify_status(
                "import: no plan approval for this companion write; approve "
                "it on the import screen first",
                refused=True,
            )
            return
        self._write_companion_patches(
            bank,
            companion,
            approval=approval,
            banks_list=banks_list if banks_list is not None else [bank],
            path=path,
            mode=getattr(choice, "mode", perfcopy.ImportMode.AS_IS),
        )

    def _frozen_companion_sources(
        self, entry: Any, banks_list: Sequence[Any]
    ) -> Tuple[Dict[str, deps.Source], Optional[deps.Source]]:
        """The companion write's frozen plan inputs (P7c).

        Every file bank's records become a frozen copy, exactly as the
        whole-bank write freezes its own sources; the companion entry's
        own frozen records come back as the second element -- the source
        the intent is planned from, and the only records the worker
        writes. An entry with no records freezes nothing of its own and
        comes back as ``None``; the caller reports that as before.
        """
        intent_sources: Dict[str, deps.Source] = {
            scope: self._frozen_source(source)
            for scope, source in self._whole_bank_intent_sources(banks_list).items()
        }
        frozen_entry = intent_sources.get(str(getattr(entry, "scope", "") or ""))
        if frozen_entry is None and getattr(entry, "source", None) is not None:
            frozen_entry = self._frozen_source(entry.source)
            intent_sources[str(getattr(entry, "scope", "") or "")] = frozen_entry
        return intent_sources, frozen_entry

    def _companion_write_intent(
        self,
        entry: Any,
        companion: Any,
        banks_list: Sequence[Any],
        path: str,
        mode: Any,
        *,
        sources: Optional[Mapping[str, deps.Source]] = None,
        source: Optional[deps.Source] = None,
    ) -> Optional[import_intent.WholeBankIntent]:
        """The intent one companion write must be approved for.

        Exactly this companion write and nothing else: the companion's
        own physical bank, the kinds its entry's source carries (patches
        and wave sequences; a companion write never sends performances),
        the entry's scope as the source scope, and no placements -- the
        bank's records travel as they are. The same shared builder every
        other whole-bank intent comes from, so a mint made anywhere
        against it and this check cannot spell one plan two ways.

        ``sources`` and ``source`` are the frozen snapshot the write
        boundary took (P7c); given, the intent is planned from it, so the
        token is validated against exactly the records the worker will
        write. Without them the intent is planned from the live file
        banks, as every other intent builder here does.
        """
        if sources is None:
            sources = self._whole_bank_intent_sources(banks_list)
        planned_source: Any = (
            source if source is not None else getattr(entry, "source", None)
        )
        try:
            return import_intent.build_intent(
                source_id=f"{path}|{getattr(entry, 'scope', '')}",
                source_scope=str(getattr(entry, "scope", "")),
                sources=sources,
                source=planned_source,
                target=companion,
                mode=mode,
                companion_only=True,
            )
        except (IndexError, LookupError, ValueError, TypeError, AttributeError) as exc:
            self.notify_status(
                f"import: this companion write cannot be planned: {exc}",
                refused=True,
            )
            return None

    def _companion_token_check(
        self,
        approval: import_intent.PlanApproval,
        intent: import_intent.WholeBankIntent,
        pending_note: str,
    ) -> Optional[str]:
        """The refusal a companion token earns, or ``None`` when it may write.

        The token must have been minted for exactly the intent this write
        would run, and never have been spent: a mismatched or replayed
        token refuses before any device call for that bank. The message
        carries the run's ``pending_note``, so a chain that stops here
        still names what landed and what did not.
        """
        if not import_intent.approval_matches(approval, intent):
            return self._companion_failure_message(
                "the companion write's plan approval does not match it",
                pending_note,
            )
        if approval.nonce in self._consumed_plan_nonces:
            return self._companion_failure_message(
                "this companion write's plan approval was already used",
                pending_note,
            )
        return None

    def _write_companion_patches(
        self,
        entry: Any,
        companion: Any,
        on_done: Optional[Callable[[], None]] = None,
        *,
        approval: import_intent.PlanApproval,
        banks_list: Sequence[Any],
        path: str,
        mode: Any = perfcopy.ImportMode.AS_IS,
        pending_note: str = "",
    ) -> None:
        """Write one companion bank's patches and wave sequences only.

        The RAM bank's performances are left exactly as they are: a
        companion write exists to fill the patch slots other
        performances' parts name, not to replace the performances that
        bank already holds. The write is its own approved request (P4):
        before anything touches the unit, the armed gate is checked and
        ``approval`` is held against the intent built for exactly this
        companion write -- its own physical bank, its own entry's source
        scope, the patches and wave sequences the file carries -- and its
        one-shot nonce is consumed. A missing, mismatched or replayed
        token refuses with zero device calls for that bank, and the run
        stops there: nothing claims the bank landed. ``pending_note`` is
        what a multi-companion run still owed when this write started; a
        failure reports it, so the banks that succeeded are never
        silently swallowed by the one that did not.

        The plan's inputs are frozen here too, once, on the main thread
        (P7c): the snapshot of the companion entry's records -- its
        performance list, patch list and wave-sequence bytes -- is taken
        before the token is checked, the token is validated against that
        same snapshot, and the worker writes only what the snapshot
        carries. Between the check and the worker, other code can run --
        another device worker's completion lands on the main thread and
        may re-point a source's records -- so planning and writing read
        records no other code can mutate, exactly as the whole-bank
        write does (:meth:`_frozen_plan_sources`, P7b).
        """
        from kws import backup as _backup
        from kws import banks as _banks

        if not self._write_gate.armed:
            self.notify_status(
                self._companion_failure_message("write gate is locked", pending_note),
                refused=True,
            )
            return
        # The snapshot is taken before the intent is built from it: the
        # token is then validated against exactly these records, and the
        # worker below reads only this frozen copy.
        intent_sources, frozen_entry = self._frozen_companion_sources(entry, banks_list)
        intent = self._companion_write_intent(
            entry,
            companion,
            banks_list,
            path,
            mode,
            sources=intent_sources,
            source=frozen_entry,
        )
        if intent is None:
            return
        refusal = self._companion_token_check(approval, intent, pending_note)
        if refusal is not None:
            self.notify_status(refusal, refused=True)
            return
        self._consumed_plan_nonces.add(approval.nonce)
        source = frozen_entry
        if source is None:
            self.notify_status(
                f"import: the file carries no records for {companion.label}, "
                "so it was not written",
                refused=True,
            )
            return
        patches_blob = b"".join(getattr(source, "patches", []) or []) or None
        waveseq = getattr(source, "waveseq", None)
        if patches_blob is None and waveseq is None:
            self.notify_status(
                f"import: the file carries no patches or wave sequences "
                f"for {companion.label}, so there is nothing to write",
                refused=True,
            )
            # Nothing to send is not a failure of the run: the rest of
            # the answer still goes out.
            if on_done is not None:
                on_done()
            return
        count = sum(
            1
            for record in getattr(source, "patches", []) or ()
            if record and any(record)
        )
        conflict = self._preaudit_conflicting_slots(
            companion, (), range(_banks.PATCHES_PER_BANK)
        )
        if conflict:
            self.notify_status(
                f"warning: this import overwrites {conflict} — writing anyway",
                warn=True,
            )
        backup_dir = _backup.default_dir()

        def work() -> Any:
            return self.device.write_bank_bundle(
                companion.number,
                patches=patches_blob,
                waveseq=waveseq,
                backup_dir=backup_dir,
                before_blocks=None,
            )

        def done(result: Any, failure: BaseException | None) -> None:
            self._companion_write_done(
                result,
                failure,
                companion=companion,
                source=source,
                count=count,
                pending_note=pending_note,
                on_done=on_done,
            )

        self._device_worker(work, done, f"writing patches into {companion.label}")

    def _companion_write_done(
        self,
        result: Any,
        failure: BaseException | None,
        *,
        companion: Any,
        source: Any,
        count: int,
        pending_note: str,
        on_done: Optional[Callable[[], None]],
    ) -> None:
        """Report one companion write: only a verified one counts as landed."""
        verdict = "" if failure is not None else self._bundle_result_error(result)
        if failure is not None or verdict:
            # A companion that was not verified must not be booked as
            # landed: the chain stops and nothing claims it is there.
            self.notify_status(
                self._companion_failure_message(failure or verdict, pending_note),
                refused=True,
            )
            return
        unchanged = self._bundle_sent_nothing(result)
        if hasattr(self, "_record_write"):
            self._record_write(result)
        self._remember_companion_patches(companion, source)
        if unchanged:
            self.notify_status(
                f"{companion.label} already matches the file — nothing was "
                f"sent; its performances were left as they were"
            )
        else:
            self.notify_status(
                f"wrote {self._counted(count, 'patch', 'patches')} into "
                f"{companion.label}; its performances were left as they were"
            )
        if on_done is not None:
            on_done()

    @staticmethod
    def _companion_failure_message(failure: Any, pending_note: str) -> str:
        """One failure line that still names what the run had done.

        A multi-companion run that stops halfway must not look like a
        clean failure of the whole answer: the banks written before the
        failure are listed as written, the rest as not written.
        """
        message = f"import: {failure}"
        if pending_note:
            message = f"{message} — {pending_note}"
        return message

    def _remember_companion_patches(self, companion: Any, source: Any) -> None:
        """Record the patches a companion write left on the unit.

        The session's copy of that bank keeps its own performances --
        they were never touched -- and takes the file's patches and wave
        sequences, so a later check sees the bank as written.
        """
        existing = self._sources.get(companion.id)
        if existing is None:
            # The bank may not have been loaded in this session. The unit's
            # performances were not touched by this write, so the cached
            # copy is the right record; an empty list would make every
            # performance in that bank look unloaded to the dependency
            # graph (and only the performances would be wrong).
            existing = self._sources_with_store().get(companion.id)
        performances = list(getattr(existing, "performances", []) or [])
        patches_blob = b"".join(getattr(source, "patches", []) or [])
        self._sources[companion.id] = deps.Source(
            scope_id=companion.id,
            performances=performances,
            patches=[
                patches_blob[i * rec.PATCH_SIZE : (i + 1) * rec.PATCH_SIZE]
                for i in range(len(patches_blob) // rec.PATCH_SIZE)
            ],
            waveseq=getattr(source, "waveseq", None),
        )
        if self._bankstore_dir is not None:
            try:
                bankstore.save(
                    companion.id,
                    patches=patches_blob or None,
                    waveseq=getattr(source, "waveseq", None),
                    directory=self._bankstore_dir,
                )
            except OSError:
                pass

    def _offer_companions_after_write(
        self,
        choice: Any,
        bank: Any,
        banks_list: Optional[Sequence[Any]],
        path: str,
    ) -> None:
        """Re-offer the import screen when the file carries the named bank.

        An as-is write sends every part pointing where the file points, and
        the screen that said so is gone by the time ctrl+s lands. When a
        part names a bank the unit has not been given -- but the same file
        also carries -- those performances are dead until the companion is
        written too, so the screen comes back with one focused line: press
        ``x`` to write the companion bank, or ``o`` for a self-contained
        pass that repoints the parts.
        """
        if not self._import_left_dangling(choice):
            return
        if str(getattr(choice, "preflight", "") or ""):
            # The preflight asked before the write; the popup stays a
            # fallback for writes nobody asked about.
            return
        if not getattr(self, "is_running", False) or not banks_list or not path:
            return
        try:
            source_index = int(getattr(choice, "source_index", 0) or 0)
        except (TypeError, ValueError):
            return
        if bank is None:
            try:
                bank = banks_list[source_index]
            except (IndexError, TypeError):
                return
        named = self._companions_named_by_parts(bank, banks_list, source_index)
        if not named:
            return
        count, index, companion = max(named, key=lambda item: (item[0], -item[1]))
        body = (
            f"{count} parts name {companion.label}, and this file carries "
            f"that bank too.\n\nThe import wrote {bank.label} only, so "
            f"those parts play whatever {companion.label} holds now — "
            "often Init, which makes the performance silent or wrong."
        )
        # Push on the next refresh, not inside the worker's completion:
        # a screen pushed in that same turn can be popped again while the
        # stack settles, which is a popup nobody ever sees.
        self.call_after_refresh(
            self._push_dangling_popup,
            body,
            companion.label,
            banks_list,
            index,
            companion,
            path,
        )

    def _push_dangling_popup(
        self,
        body: str,
        companion_label: str,
        banks_list: Sequence[Any],
        index: int,
        companion: Any,
        path: str,
    ) -> None:
        """Put the dangling-reference popup on the stack."""
        from kwsed.import_bank import DanglingRefsScreen

        self.push_screen(
            DanglingRefsScreen(body, write_label=f"the file's {companion_label} bank"),
            lambda answer: self._dangling_answer(
                answer, banks_list, index, companion, path
            ),
        )

    def _dangling_answer(
        self,
        answer: Optional[str],
        banks_list: Sequence[Any],
        companion_index: int,
        companion: Any,
        path: str,
    ) -> None:
        """Do what the popup promised: write the companion, or repoint."""
        if answer == "write":
            if not self._write_gate.armed:
                self.notify_status("write gate is locked", refused=True)
                return
            try:
                bank = banks_list[companion_index]
            except (IndexError, TypeError):
                return
            # The popup's answer is the companion write's confirmation: the
            # approval for it is minted here, from the same file banks the
            # popup described.
            companion_choice = WholeBankChoice(
                companion_index, companion.id, True, companion_only=True
            )
            approval = self._mint_whole_bank_approval(
                bank, companion_choice, banks_list, path
            )
            if approval is None:
                return
            self._import_whole_bank_write(
                bank,
                replace(companion_choice, approval=approval),
                banks_list=banks_list,
                path=path,
            )
            return
        if answer == "repoint":
            self._reopen_whole_bank_import(
                banks_list, path, "", perfcopy.ImportMode.AS_IS
            )

    @staticmethod
    def _bundle_sent_nothing(result: Any) -> bool:
        """Whether the bridge reported every kind unchanged and sent nothing."""
        return (
            isinstance(result, dict)
            and result.get("kinds") == []
            and bool(result.get("unchanged"))
        )

    @staticmethod
    def _bundle_result_error(result: Any) -> str:
        """The failure a bundle result reports about itself, or "" when clean.

        A ``verified: False`` or a non-None ``mismatch`` means the unit
        did not store what was sent: for the outcome that is a failed
        bank whose backup is the way back, not a success to celebrate.
        """
        if not isinstance(result, dict):
            return ""
        mismatch = result.get("mismatch")
        if mismatch is not None:
            return f"read-back mismatch: {mismatch}"
        if result.get("verified") is False:
            return "read-back mismatch"
        return ""

    def _write_bundle_once(
        self, bank_number: int, payload: Mapping[str, Any], backup_dir: str
    ) -> Tuple[Any, str]:
        """One fenced bundle write, as ``(result, error)``.

        The bridge raises when the transfer is refused; a clean return
        must still prove itself, so the result's own ``verified`` /
        ``mismatch`` verdict becomes the error the outcome names.
        Nothing raises here: the caller's outcome is the report.
        """
        try:
            result = self.device.write_bank_bundle(
                bank_number,
                backup_dir=backup_dir,
                before_blocks=None,
                **payload,
            )
        except Exception as exc:  # a refused write leaves the bank as it was
            return None, str(exc)
        return result, self._bundle_result_error(result)

    def _write_bank_steps(
        self,
        steps: Sequence[Tuple[Any, Dict[str, Any], Optional[BankWriteOutcome]]],
        backup_dir: str,
        *,
        built: Any = None,
    ) -> MultiBankOutcome:
        """Write each (bank, bundle payload, outcome shell) in order.

        Cross-bank writes are not atomic and must not pretend to be:
        when a bundle is refused or its read-back does not match, the
        later banks are not attempted and the outcome reports exactly
        what landed, what failed and what was never touched -- instead
        of raising away the banks that are already written. ``built`` is
        the overflow import's full build, when these steps are one.
        """
        completed: List[BankWriteOutcome] = []
        for index, (step_bank, payload, shell) in enumerate(steps):
            result, error = self._write_bundle_once(
                step_bank.number, payload, backup_dir
            )
            if error:
                return MultiBankOutcome(
                    completed=completed,
                    failed_bank=step_bank,
                    error=error,
                    not_written=[bank for bank, _p, _s in steps[index + 1 :]],
                    built=built,
                    skipped_groups=[
                        pending.group
                        for _b, _p, pending in steps[index:]
                        if pending is not None and pending.group is not None
                    ],
                )
            entry = shell if shell is not None else BankWriteOutcome(bank=step_bank)
            entry.result = result
            entry.verified = True
            completed.append(entry)
        return MultiBankOutcome(completed=completed, built=built)

    def _write_with_overflow(
        self,
        bank: Any,
        sources: Mapping[str, deps.Source],
        target: Any,
        mode: Any,
        backup_dir: str,
        overflow_id: str,
        skip_overflow: bool = False,
    ) -> MultiBankOutcome:
        """Write the overflow bank first, then the target.

        The overflow bank must exist on the unit before a performance that
        names it arrives, so its patches and wave sequences go out first.
        Each bank is its own fenced ``write_bank_bundle``: two backups, two
        read-back verifications, two independent undo points. With
        ``skip_overflow`` the overflow bank already landed in an earlier
        pass of this session (its write succeeded, the target's did not),
        so only the target goes out: the patches are already on the unit.
        """
        from kws import banks as _banks

        overflow = _banks.bank(overflow_id)
        plan_sources = dict(sources)
        plan_sources[bank.scope] = bank.source
        plan = perfcopy.plan_overflow_import(
            plan_sources, bank.scope, target, overflow, mode=mode
        )
        if not plan.fits:
            raise ValueError("; ".join(plan.rejected))
        built = perfcopy.build_overflow_import(plan_sources, plan)
        steps: List[Tuple[Any, Dict[str, Any], Optional[BankWriteOutcome]]] = []
        if not skip_overflow:
            steps.append(
                (
                    overflow,
                    {
                        "patches": built.overflow_patches,
                        "waveseq": built.overflow_waveseq,
                    },
                    None,
                )
            )
        steps.append(
            (
                target,
                {
                    "performances": built.destination.performances,
                    "patches": built.destination.patches,
                    "waveseq": built.destination.waveseq,
                },
                None,
            )
        )
        return self._write_bank_steps(steps, backup_dir, built=built)

    def _overflow_written(
        self,
        outcome: MultiBankOutcome,
        label: str,
        into: str,
        target: Any,
        target_scope: str,
        overflow_id: str = "",
    ) -> None:
        """Book-keeping after an overflow import landed.

        Both halves landed -- the overflow bank's patches first, then the
        target -- or, after a partial pass marked the session, the target
        alone with the overflow bank already holding what the parts name.
        """
        built = outcome.built
        overflow_entry = None
        if overflow_id:
            overflow_entry = next(
                (
                    entry
                    for entry in outcome.completed
                    if getattr(entry.bank, "id", "") == overflow_id
                ),
                None,
            )
        target_entry = next(
            (entry for entry in outcome.completed if entry is not overflow_entry),
            None,
        )
        if overflow_entry is not None and hasattr(self, "_record_write"):
            self._record_write(overflow_entry.result)
        if target_entry is not None:
            self._import_whole_bank_written(
                label,
                target,
                built.destination,
                target_entry.result,
                "",
                target_scope=target_scope,
            )
        if overflow_entry is not None:
            self._remember_overflow(overflow_entry.bank, built)
            target_result = target_entry.result if target_entry else None
            both_unchanged = self._bundle_sent_nothing(
                overflow_entry.result
            ) and self._bundle_sent_nothing(target_result)
            if both_unchanged:
                self.notify_status(
                    f"{into} and {overflow_entry.bank.label} already match the "
                    "file — nothing was sent"
                )
                return
            self.notify_status(
                f"imported {label} into {into}: "
                f"{len(built.destination.performances) // rec.PERFORMANCE_SIZE} "
                f"performances, {built.patch_count} patches repointed from "
                f"{overflow_entry.bank.label}; {overflow_entry.bank.label} also "
                f"received {built.patch_count} patches"
            )
            return
        # The overflow bank landed in an earlier pass; this write is the
        # target alone, and the status must not claim it received patches.
        overflow_label = overflow_id
        try:
            overflow_label = banks.bank(overflow_id).label
        except LookupError:
            pass
        self.notify_status(
            f"imported {label} into {into}: "
            f"{len(built.destination.performances) // rec.PERFORMANCE_SIZE} "
            f"performances, {built.patch_count} patches repointed from "
            f"{overflow_label}; the overflow bank already held them"
        )

    def _remember_overflow(self, overflow: Any, built: Any) -> None:
        """Update the session source and cache for the overflow bank."""
        existing = self._sources.get(overflow.id)
        performances = list(getattr(existing, "performances", []) or [])
        self._sources[overflow.id] = deps.Source(
            scope_id=overflow.id,
            performances=performances,
            patches=[
                built.overflow_patches[i * rec.PATCH_SIZE : (i + 1) * rec.PATCH_SIZE]
                for i in range(len(built.overflow_patches) // rec.PATCH_SIZE)
            ],
            waveseq=built.overflow_waveseq,
        )
        if self._bankstore_dir is not None:
            try:
                bankstore.save(
                    overflow.id,
                    patches=built.overflow_patches,
                    waveseq=built.overflow_waveseq,
                    directory=self._bankstore_dir,
                )
            except OSError:
                pass

    def _self_contained_split(
        self,
        bank: Any,
        sources: Mapping[str, deps.Source],
        target: Any,
        mode: Any,
        overflow_id: str,
    ) -> Tuple[Dict[str, deps.Source], Any]:
        """Partition the source over the target plus the overflow bank.

        The bank list is ordered as the partition expects: the target
        first, because it receives the contained performances, then the
        chosen overflow bank, which receives the performances that point
        outside the target with every patch they name. The result is the
        engineer's :class:`kws.perfcopy.SplitPartition`; ``fits`` says
        whether the two banks are enough.
        """
        from kws import banks as _banks

        overflow = _banks.bank(overflow_id)
        plan_sources = dict(sources)
        # The file the bank came from is not necessarily in the session,
        # so the planner sees it under its own scope.
        plan_sources[bank.scope] = bank.source
        partition = perfcopy.partition_self_contained_split(
            plan_sources, bank.scope, [target, overflow], mode=mode
        )
        return plan_sources, partition

    def _split_choice_plan(
        self,
        bank: Any,
        sources: Mapping[str, deps.Source],
        target: Any,
        mode: Any,
        choice: Any,
    ) -> Tuple[Optional[Tuple[Dict[str, deps.Source], Any]], bool, Optional[List[int]]]:
        """The self-contained split this choice asks for, and whether to write.

        ``(None, True, None)`` for a plain or patches-only write. For the
        self-contained kind it returns the partition, or ``(None, False,
        None)`` after stating the engine's rejections when the content
        cannot be planned at all -- nothing is written in that case.

        When the split needs more banks than the two the choice offers but
        every group can still be planned, the third element is the list of
        source slots the offered banks leave out: the write carries the
        groups that fit and the caller records the rest for the next card.
        A choice whose import session already carries a split remainder is
        a continuation: one more group of the remainder, into the target.

        The cards-only kind has no overflow bank at all: every non-empty
        performance is packed against the target, only the first group
        travels, and the other groups' slots are the next cards' load.
        """
        from kws import banks as _banks

        session_key = str(getattr(choice, "session_key", "") or "")
        remainder = self._split_pass_remainder(session_key) if session_key else []
        if remainder:
            if (
                self._split_pass_kind(session_key) == OVERFLOW_CARDS_ONLY
                and getattr(target, "id", "") != _banks.bank("CARD").id
            ):
                # A cards-only session continues onto the card, never onto
                # a RAM bank the screen's selector happens to show.
                self.notify_status(
                    "cards-only continuation needs the card in the slot, not "
                    f"{getattr(target, 'label', target)}: no RAM bank is written",
                    refused=True,
                )
                return None, False, None
            return (
                self._split_continuation_plan(bank, sources, target, mode, remainder),
                True,
                None,
            )
        kind = str(
            getattr(choice, "overflow_kind", OVERFLOW_PATCHES) or OVERFLOW_PATCHES
        )
        if kind == OVERFLOW_CARDS_ONLY:
            plan, left = self._cards_only_plan(bank, sources, target, mode)
            if plan is None:
                return None, False, None
            return plan, True, left
        overflow_id = str(getattr(choice, "overflow_bank", "") or "")
        if not overflow_id or kind != OVERFLOW_SELF_CONTAINED:
            return None, True, None
        split_plan = self._self_contained_split(
            bank, sources, target, mode, overflow_id
        )
        if split_plan[1].fits:
            return split_plan, True, None
        try:
            overflow = _banks.bank(overflow_id)
        except LookupError:
            overflow = None
        if overflow is not None:
            first_pass, remaining = self._split_first_pass_partition(
                split_plan[0], bank.scope, [target, overflow], mode
            )
        else:
            first_pass, remaining = None, []
        if first_pass is None:
            self.notify_status(
                "self-contained split does not fit: "
                + "; ".join(split_plan[1].rejected),
                refused=True,
            )
            return None, False, None
        return (split_plan[0], first_pass), True, remaining

    def _split_continuation_plan(
        self,
        bank: Any,
        sources: Mapping[str, deps.Source],
        target: Any,
        mode: Any,
        remainder: Sequence[int],
    ) -> Tuple[Dict[str, deps.Source], Any]:
        """One more group of a split, packed into the card now in the slot.

        The remainder's slots are packed against ``target`` and only the
        first group travels: the user swaps the card between passes, so
        each pass is one fenced write into whatever the slot holds now.
        """
        plan_sources = dict(sources)
        # The file the bank came from is not necessarily in the session,
        # so the planner sees it under its own scope.
        plan_sources[bank.scope] = bank.source
        groups = perfcopy.pack_self_contained_into(
            plan_sources, bank.scope, remainder, target, mode=mode
        )
        keep = [groups[0]] if groups else []
        return plan_sources, perfcopy.SplitPartition(groups=keep)

    def _cards_only_plan(
        self,
        bank: Any,
        sources: Mapping[str, deps.Source],
        target: Any,
        mode: Any,
    ) -> Tuple[Optional[Tuple[Dict[str, deps.Source], Any]], List[int]]:
        """The first cards-only group, and the slots left for later cards.

        Every non-empty performance of the source bank is packed, in slot
        order, into self-contained groups that each fit one bank; only the
        first group travels on this pass. No RAM bank is planned, so the
        only thing a later pass needs is the card now in the slot. A group
        that cannot be planned at all refuses the whole run, with the
        engine's own reasons, and ``(None, [])`` writes nothing.

        The write goes to the physical card slot whatever the screen's
        target selector showed. A non-Card target is a caller bug, not
        something to redirect: it is refused, because a cards-only pass
        that quietly landed in RAM would be the exact mistake this kind
        exists to avoid.
        """
        from kws import banks as _banks

        try:
            card = _banks.bank("CARD")
        except LookupError:
            self.notify_status("no card bank is available to write", refused=True)
            return None, []
        if getattr(target, "id", "") != card.id:
            self.notify_status(
                "cards-only pass needs the card in the slot, not "
                f"{getattr(target, 'label', target)}: no RAM bank is written",
                refused=True,
            )
            return None, []
        target = card
        plan_sources = dict(sources)
        # The file the bank came from is not necessarily in the session,
        # so the planner sees it under its own scope.
        plan_sources[bank.scope] = bank.source
        slots = perfcopy.active_performances(bank.source)
        if not slots:
            self.notify_status(
                "no active performances to place on a card", refused=True
            )
            return None, []
        try:
            groups = perfcopy.pack_self_contained_into(
                plan_sources, bank.scope, slots, target, mode=mode
            )
        except (LookupError, ValueError, TypeError, AttributeError) as exc:
            self.notify_status(f"cards-only split: {exc}", refused=True)
            return None, []
        reasons = [
            reason
            for group in groups
            if group.plan is not None
            for reason in group.plan.rejected
        ]
        if not groups or reasons:
            self.notify_status(
                "cards-only split cannot be planned: "
                + "; ".join(reasons or ["no group fits a card"]),
                refused=True,
            )
            return None, []
        first = groups[0]
        placed = set(first.performances)
        remaining = [slot for slot in slots if slot not in placed]
        return (plan_sources, perfcopy.SplitPartition(groups=[first])), remaining

    def _split_choice_statement(
        self,
        split_plan: Any,
        session_key: str,
        first_remainder: Optional[Sequence[int]],
        cards_only: bool,
    ) -> str:
        """What this split writes, and for cards only what is left over."""
        statement = self._split_statement(split_plan)
        if not statement or not cards_only:
            return statement
        left = self._cards_only_left(session_key, split_plan, first_remainder)
        return (
            statement
            + self._cards_only_remaining_statement(left)
            + " — writes go to the card in the slot, one group per card, "
            "no RAM bank"
        )

    def _cards_only_choice(self, choice: Any, session_key: str) -> bool:
        """Whether this write is a cards-only pass, or continues one."""
        kind = str(
            getattr(choice, "overflow_kind", OVERFLOW_PATCHES) or OVERFLOW_PATCHES
        )
        return (
            kind == OVERFLOW_CARDS_ONLY
            or self._split_pass_kind(session_key) == OVERFLOW_CARDS_ONLY
        )

    def _cards_only_left(
        self,
        session_key: str,
        split_plan: Any,
        first_remainder: Optional[Sequence[int]],
    ) -> List[int]:
        """The slots a cards-only pass leaves for the card after this one."""
        prior = (
            list(first_remainder)
            if first_remainder is not None
            else self._split_pass_remainder(session_key)
        )
        placed: Set[int] = set()
        if split_plan is not None:
            for group in split_plan[1].groups:
                placed.update(group.performances)
        return [slot for slot in prior if slot not in placed]

    def _cards_only_remaining_statement(
        self, remaining: Optional[Sequence[int]]
    ) -> str:
        """``, 17 performances remain for the next card``, or nothing left."""
        count = 0 if remaining is None else len(remaining)
        if not count:
            return ", nothing remains for another card"
        return (
            f", {self._counted(count, 'performance', 'performances')} "
            "remain for the next card"
        )

    def _split_first_pass_partition(
        self,
        plan_sources: Mapping[str, deps.Source],
        source_scope: str,
        offered: Sequence[Any],
        mode: Any,
    ) -> Tuple[Optional[perfcopy.SplitPartition], List[int]]:
        """The groups the offered banks hold, and the slots they leave out.

        The first bank receives the contained performances, each further
        bank one group of the performances that point outside it, every
        group packed to fit its bank. When a group cannot be planned at
        all -- a patch it names is not loaded, or one performance alone
        exceeds a bank -- the result is ``(None, [])`` and the caller
        refuses as before: writing a part of such a split would only move
        the failure onto the unit.
        """
        from kws import containment

        try:
            audit = containment.audit_containment(plan_sources, source_scope)
        except (LookupError, ValueError, TypeError, AttributeError):
            return None, []
        contained = [entry.slot for entry in audit.performances if entry.contained]
        non_contained = [
            entry.slot for entry in audit.performances if not entry.contained
        ]
        groups: List[perfcopy.SplitGroup] = []
        remaining: List[int] = []
        overflow_banks = list(offered[1:])
        if offered and contained:
            packed = perfcopy.pack_self_contained_into(
                plan_sources, source_scope, contained, offered[0], mode=mode
            )
            if not _split_groups_plannable(packed):
                return None, []
            groups.append(packed[0])
            placed = set(packed[0].performances)
            remaining.extend(slot for slot in contained if slot not in placed)
        if overflow_banks and non_contained:
            packed = perfcopy.pack_self_contained_into(
                plan_sources, source_scope, non_contained, overflow_banks[0], mode=mode
            )
            if not _split_groups_plannable(packed):
                return None, []
            keep = packed[: len(overflow_banks)]
            groups.extend(keep)
            placed_out: Set[int] = set()
            for group in keep:
                placed_out.update(group.performances)
            remaining.extend(slot for slot in non_contained if slot not in placed_out)
        return perfcopy.SplitPartition(groups=groups), remaining

    @staticmethod
    def _counted(count: int, singular: str, plural: str) -> str:
        """``1 patch`` / ``2 patches``: a count with the word it agrees with."""
        return f"{count} {singular if count == 1 else plural}"

    @staticmethod
    def _split_statement(split_plan: Any) -> str:
        """What each bank of a self-contained split will receive, or ""."""
        if split_plan is None:
            return ""
        parts: list[str] = []
        for group in split_plan[1].groups:
            patches = 0 if group.plan is None else group.plan.patch_count
            performances = KwsedApp._counted(
                len(group.performances), "performance", "performances"
            )
            parts.append(
                f"{group.target_bank.label} gets {performances} and "
                f"{KwsedApp._counted(patches, 'patch', 'patches')}"
            )
        return "; ".join(parts)

    def _write_split_banks(
        self,
        split_plan: Tuple[Dict[str, deps.Source], Any],
        backup_dir: str,
    ) -> MultiBankOutcome:
        """Build every group and write it as its own fenced bundle.

        One ``write_bank_bundle`` per bank, in partition order (the target
        first, then the overflow bank): each is a separate backup, a
        separate read-back verification and a separate undo point. The
        banks of a self-contained split do not reference each other, so
        the order is the partition's own, not a dependency. A failed bank
        stops the pass: the outcome names the groups that landed and the
        ones the failure hit or stopped before, and the session keeps
        their source slots so ``n`` never rewrites a landed group.
        """
        plan_sources, partition = split_plan
        built = perfcopy.build_split_partition(plan_sources, partition)
        steps: List[Tuple[Any, Dict[str, Any], Optional[BankWriteOutcome]]] = []
        for group, payload in zip(partition.groups, built):
            steps.append(
                (
                    group.target_bank,
                    {
                        "performances": payload.performances,
                        "patches": payload.patches,
                        "waveseq": payload.waveseq,
                    },
                    BankWriteOutcome(
                        bank=group.target_bank, built=payload, group=group
                    ),
                )
            )
        return self._write_bank_steps(steps, backup_dir)

    def _split_written(
        self,
        outcome: MultiBankOutcome,
        label: str,
        remaining: Optional[Sequence[int]] = None,
        *,
        cards_only: bool = False,
    ) -> None:
        """Book-keeping after every bank of a self-contained split landed.

        With ``remaining`` the split needs another card: the status says
        exactly how many performances are still to place and that the card
        must be switched before the next pass. ``cards_only`` names the
        pass flow, and an empty remainder says nothing is left for another
        card instead of staying silent.
        """
        summary: list[str] = []
        for entry in outcome.completed:
            group = entry.group
            scope = getattr(group.target_bank, "id", str(group.target_bank))
            self._import_whole_bank_written(
                label,
                group.target_bank,
                entry.built,
                entry.result,
                "",
                target_scope=scope,
            )
            patches = 0 if group.plan is None else group.plan.patch_count
            performances = self._counted(
                len(group.performances), "performance", "performances"
            )
            summary.append(
                f"{group.target_bank.label}: {performances}"
                f", {self._counted(patches, 'patch', 'patches')}"
            )
        if cards_only:
            message = (
                f"imported {label} as cards-only groups — "
                f"{len(outcome.completed)} card: " + "; ".join(summary)
            )
        else:
            message = (
                f"imported {label} as a self-contained split — "
                f"{len(outcome.completed)} banks: " + "; ".join(summary)
            )
        if remaining:
            message += (
                f" — {self._counted(len(remaining), 'performance', 'performances')}"
                " remain: switch the card on the unit (select it on the "
                "HCard and press C), then press n"
            )
        elif cards_only and remaining is not None:
            message += " — nothing remains for another card"
        self.notify_status(message)

    def _split_pass_remainder(self, key: str) -> List[int]:
        """The source slots a split still has to place, in source order."""
        state = self._import_passes.get(key)
        if not state:
            return []
        return [int(slot) for slot in state.get("split_remaining") or ()]

    def _split_pass_kind(self, key: str) -> str:
        """The session's pass kind: ``split`` here, the contained flow's own."""
        state = self._import_passes.get(key)
        if not state:
            return ""
        return str(state.get("pass_kind") or "")

    def _record_split_pass_groups(
        self, key: str, outcome: MultiBankOutcome
    ) -> Set[int]:
        """Record one split pass's placements; return the source slots it wrote.

        Each landed group's plan names exactly what the write put where, so
        the session's written map grows by the group's allocations and the
        remainder shrinks by the same slots. Only the groups in
        ``outcome.completed`` are recorded: a failed pass keeps the groups
        it never reached in the remainder. The landed groups' *physical*
        bank ids are recorded too, so a retry's plan approval can name the
        banks this session already landed (:meth:`_skipped_banks_for`).
        """
        written: Set[int] = set()
        landed_banks: Set[str] = set()
        for entry in outcome.completed:
            into = str(
                getattr(entry.group.target_bank, "label", entry.group.target_bank)
            )
            self._record_import_pass(key, entry.built, into)
            landed_banks.add(str(getattr(entry.group.target_bank, "id", "") or ""))
            plan = getattr(entry.built, "plan", None)
            for allocation in getattr(plan, "allocations", None) or []:
                written.add(int(allocation.source_perf.number))
        if landed_banks:
            state = self._import_passes.setdefault(key, {"written": {}, "targets": []})
            state.setdefault("banks_written", set()).update(landed_banks)
        return written

    def _record_split_session_state(
        self,
        key: str,
        written_now: Set[int],
        prior: Sequence[int],
        choice: Any,
        mode: Any,
        cards_only: bool,
    ) -> List[int]:
        """Fold one split pass into the session; return the slots left."""
        remaining = [slot for slot in prior if slot not in written_now]
        state = self._import_passes.setdefault(key, {"written": {}, "targets": []})
        # A session marker, not a secret.
        state["pass_kind"] = OVERFLOW_CARDS_ONLY if cards_only else "split"
        state["mode"] = mode
        state["split_remaining"] = remaining
        state.setdefault("split_target", str(getattr(choice, "target_bank", "") or ""))
        state.setdefault("source_index", int(getattr(choice, "source_index", 0) or 0))
        return remaining

    def _split_pass_done(
        self,
        outcome: MultiBankOutcome,
        choice: Any,
        label: str,
        banks_list: Optional[Sequence[Any]],
        path: str,
        first_remainder: Optional[Sequence[int]],
    ) -> None:
        """Book-keep a finished split pass and offer the next one.

        ``first_remainder`` is the too-short first pass's leftover slots,
        computed before the write; a continuation pass reads the session's
        own remainder and shrinks it by what this write placed. Either way
        the session ends up with the slots still to place, and the screen
        is re-offered so ``n`` can carry them onto the next card.
        """
        session_key = str(getattr(choice, "session_key", "") or "")
        mode = getattr(choice, "mode", perfcopy.ImportMode.AS_IS)
        cards_only = self._cards_only_choice(choice, session_key)
        reported: Optional[Sequence[int]] = first_remainder
        if session_key:
            written_now = self._record_split_pass_groups(session_key, outcome)
            prior = (
                list(first_remainder)
                if first_remainder is not None
                else self._split_pass_remainder(session_key)
            )
            remaining = self._record_split_session_state(
                session_key, written_now, prior, choice, mode, cards_only
            )
            reported = remaining
        self._split_written(outcome, label, reported, cards_only=cards_only)
        if (
            session_key
            and getattr(choice, "continue_pass", False)
            and banks_list
            and getattr(self, "is_running", False)
        ):
            self._reopen_whole_bank_import(banks_list, path, session_key, mode)

    def _import_pass_after_write(
        self,
        choice: Any,
        key: str,
        built: Any,
        into: str,
        banks_list: Optional[Sequence[Any]],
        path: str,
        mode: Any,
    ) -> None:
        """Book-keep and re-offer the screen after a self-contained pass."""
        self._record_import_pass(key, built, into)
        if (
            getattr(choice, "continue_pass", False)
            and banks_list
            and getattr(self, "is_running", False)
        ):
            self._reopen_whole_bank_import(banks_list, path, key, mode)

    def _import_pass_written(self, key: str) -> Dict[int, str]:
        """The session's written source-slot → target map for one import."""
        state = self._import_passes.get(key)
        if not state:
            return {}
        written = state.get("written") or {}
        return {int(slot): str(target) for slot, target in written.items()}

    def _record_import_pass(self, key: str, built: Any, into: str) -> None:
        """Remember which source slots this self-contained pass placed.

        The plan's allocations are exactly what the write put where: each
        one names the source performance slot and the target slot it
        landed in. The session keeps them under the import key so the
        next ``n`` pass offers only the remainder.
        """
        plan = getattr(built, "plan", None)
        allocations = getattr(plan, "allocations", None) or []
        if not allocations:
            return
        state = self._import_passes.setdefault(key, {"written": {}, "targets": []})
        written: Dict[int, str] = state.setdefault("written", {})
        for allocation in allocations:
            written[int(allocation.source_perf.number)] = into
        targets: list[str] = state.setdefault("targets", [])
        if into not in targets:
            targets.append(into)

    def _reopen_whole_bank_import(
        self,
        banks_list: Sequence[Any],
        path: str,
        key: str,
        mode: Any,
        notice: str = "",
    ) -> None:
        """Offer the next pass on the screen, with the session's pass state.

        ``notice`` is one plain line the screen shows instead of its pass
        line once: how to finish a write that left parts pointing at a bank
        the file also carries.
        """
        state = self._import_passes.get(key) or {}
        pass_kind = str(state.get("pass_kind") or "")
        split_state = pass_kind in ("split", OVERFLOW_CARDS_ONLY)
        self.push_screen(
            WholeBankImportScreen(
                path=path,
                banks=banks_list,
                gate=self._write_gate,
                card_mode=self._card_mode,
                mode=mode,
                pass_state=ImportPassState(
                    session_key=key,
                    written=self._import_pass_written(key),
                    split_remaining=tuple(
                        int(slot) for slot in state.get("split_remaining") or ()
                    )
                    if split_state
                    else (),
                    split_target=str(state.get("split_target") or "")
                    if split_state
                    else "",
                    split_kind=pass_kind if split_state else "split",
                    source_index=int(state.get("source_index") or 0),
                    # The banks this session already landed, as the
                    # screen's mint must carry them so its token agrees
                    # with the write boundary's rebuild (P7b).
                    skipped_banks=self._skipped_banks_for(key),
                ),
                notice=notice,
                companion_loaded=self._companion_loaded_snapshot(),
            ),
            lambda picked: self._import_whole_bank_chosen(banks_list, picked, path),
        )

    def _remember_written_bank(
        self,
        target: Any,
        relocated: Any,
        result: Any,
        target_scope: Optional[str] = None,
    ) -> int:
        """Record a landed write and update the session and the cache.

        The undo record, the session's source, the bankstore cache and
        the browser's refresh -- exactly what a success path does, minus
        the status line: the caller owns what the user is told. Returns
        the number of performances the bank now holds.
        """
        if target_scope is None:
            target_scope = getattr(target, "id", None) or str(target)
        if hasattr(self, "_record_write"):
            self._record_write(result)
        perfs = [
            relocated.performances[
                i * rec.PERFORMANCE_SIZE : (i + 1) * rec.PERFORMANCE_SIZE
            ]
            for i in range(len(relocated.performances) // rec.PERFORMANCE_SIZE)
        ]
        patches = [
            relocated.patches[i * rec.PATCH_SIZE : (i + 1) * rec.PATCH_SIZE]
            for i in range(len(relocated.patches) // rec.PATCH_SIZE)
        ]
        new_source = deps.Source(
            scope_id=target_scope,
            performances=perfs,
            patches=patches,
            waveseq=relocated.waveseq,
        )
        self._sources[target_scope] = new_source
        if self._bankstore_dir is not None:
            try:
                bankstore.save(
                    target_scope,
                    performances=relocated.performances,
                    patches=relocated.patches,
                    waveseq=relocated.waveseq,
                    directory=self._bankstore_dir,
                )
            except OSError:
                pass
        self._refresh_written_bank(target_scope, relocated)
        return len(perfs)

    def _import_whole_bank_written(
        self,
        label: str,
        target: Any,
        relocated: Any,
        result: Any,
        conflict: str = "",
        target_scope: Optional[str] = None,
    ) -> None:
        """Book-keeping after a whole-bank import has landed."""
        if target_scope is None:
            target_scope = getattr(target, "id", None) or str(target)
        into = target_scope if target_scope != target.id else target.label
        if (
            isinstance(result, dict)
            and result.get("kinds") == []
            and result.get("unchanged")
        ):
            # The bridge's fresh unit read already equalled every payload,
            # so nothing was sent. Saying "imported" would claim SysEx
            # that never left the cable; the pre-write backup was still
            # taken, so the undo point is recorded.
            self._remember_written_bank(target, relocated, result, target_scope)
            backups = self._counted(self._backup_count(result), "backup", "backups")
            self.notify_status(
                f"{label} already matches {into} — nothing was sent ({backups} on disk)"
            )
            return
        perfs = self._remember_written_bank(target, relocated, result, target_scope)
        overwrote = f"; overwrote {conflict}" if conflict else ""
        self.notify_status(
            f"imported {label} into {into}: {perfs} performances{overwrote}",
            warn=bool(conflict),
        )

    def _complete_source_file(self, path: str, bank: Any) -> tuple[Any, str]:
        """Auto-merge uniquely matching companion fragments and warn if short."""
        if not path:
            return bank, ""
        current = bank.source
        notes: list[str] = []
        for _ in range(4):
            audit = import_audit.audit_source(current)
            if audit.complete:
                break
            companions = unique_equivalent_companions(
                find_companions(path, current), path
            )
            if len(companions) > 1:
                names = ", ".join(os.path.basename(c.path) for c in companions[:3])
                notes.append(f"{audit.summary()}; multiple companions: {names}")
                break
            if not companions:
                notes.append(f"{audit.summary()}; no companion file found")
                break
            companion = companions[0]
            current = companion.merged
            notes.append(f"auto-merged {os.path.basename(companion.path)}")
        final = import_audit.audit_source(current)
        if notes and not final.complete:
            notes.append(final.summary())
        completed = SimpleNamespace(
            label=getattr(bank, "label", ""),
            scope=getattr(bank, "scope", ""),
            source=current,
        )
        return completed, "; ".join(notes)

    def _import_single_source_chosen(
        self, path: str, banks_list: Sequence[Any], index: Optional[int]
    ) -> None:
        if index is None:
            # Back to the file picker, not the browser.
            self._import_kind_chosen(1)
            return
        try:
            bank = banks_list[index]
        except (IndexError, TypeError):
            return
        if bank is None:
            return
        from kws import banks as _banks

        writable = _banks.writable_banks()
        if not writable:
            self.notify_status("no writable target banks", refused=True)
            return
        bank, note = self._complete_source_file(path, bank)
        self._open_single_target_chooser(path, bank, banks_list, writable, note)

    def _open_single_target_chooser(
        self,
        path: str,
        bank: Any,
        banks_list: Sequence[Any],
        writable: Sequence[Any],
        note: str = "",
    ) -> None:
        self.push_screen(
            TargetBankScreen("Target bank", [b.label for b in writable]),
            lambda ti: self._import_single_target_chosen(
                path, bank, banks_list, writable, ti, note
            ),
        )

    def _source_bank_number_for_scope(self, scope: str) -> Optional[int]:
        if scope.startswith("CARD/"):
            return banks.bank("CARD").number
        try:
            return banks.bank(scope).number
        except LookupError:
            return None

    def _unique_source_scope(self, base: str, sources: Mapping[str, Any]) -> str:
        if base not in sources:
            return base
        index = 2
        while f"{base}#{index}" in sources:
            index += 1
        return f"{base}#{index}"

    def _session_source(
        self, bank: Any, sources: Mapping[str, Any], path: str = ""
    ) -> tuple[Any, deps.Source]:
        """A source bank under a unique session key.

        Two RAM1 files can be assembled into one target. The key is a
        session alias; ``Source.bank_number`` carries the real bank number
        so the planner still knows which parts are local.
        """
        base = str(getattr(bank, "scope", ""))
        alias = self._unique_source_scope(base, sources)
        original = bank.source
        number = getattr(original, "bank_number", None)
        if number is None:
            number = self._source_bank_number_for_scope(base)
        aliased = deps.Source(
            scope_id=alias,
            performances=list(original.performances),
            patches=list(original.patches),
            waveseq=original.waveseq,
            bank_number=number,
        )
        label = getattr(bank, "label", base)
        if alias != base:
            label = f"{label} ({alias})"
        wrapped = SimpleNamespace(
            label=label, scope=alias, source=aliased, source_path=path
        )
        return wrapped, aliased

    def _import_single_target_chosen(
        self,
        path: str,
        bank: Any,
        banks_list: Sequence[Any],
        writable: Sequence[Any],
        target_index: Optional[int],
        note: str = "",
    ) -> None:
        if target_index is None:
            # Back to the source-bank preview.
            self._open_source_chooser(path, banks_list)
            return
        try:
            target = writable[target_index]
        except (IndexError, TypeError):
            return
        if target is None:
            return
        sources = dict(self._sources_with_store())
        target_source = sources.get(target.id)
        if target_source is None:
            try:
                target_source = self._read_source_offline(target.id)
            except Exception as exc:
                self.notify_status(
                    f"import: could not read {target.label}: {exc}", refused=True
                )
                return
            self._sources[target.id] = target_source
            sources = dict(self._sources_with_store())
        wrapped, aliased = self._session_source(bank, sources, path)
        sources[wrapped.scope] = aliased
        sources[target.id] = target_source
        self._import_single_sources = sources
        self._import_single_bank = bank
        screen = SingleImportScreen(
            path=path,
            banks=[wrapped],
            sources=sources,
            target_bank=target,
            target_source=target_source,
            gate=self._write_gate,
            on_source_request=self._single_source_menu,
            on_audition_request=self._audition_focused,
            initial_status=note,
        )
        self._begin_staging_prefetch(screen)
        self.push_screen(
            screen,
            lambda choice: self._import_single_chosen(
                choice, target, bank, sources, path, banks_list, writable
            ),
        )

    def _single_source_menu(self, screen: Any) -> None:
        choices = [
            f"{index:02d} {getattr(bank, 'label', '')}"
            for index, bank in enumerate(screen.source_banks)
        ]
        choices.append("Add another source file…")
        choices.append("Add unit bank (RAM/ROM/Card)…")
        self.push_screen(
            ImportMenuScreen("Source bank", choices, screen.source_index),
            lambda index: self._single_source_menu_chosen(screen, index),
        )

    def _select_for_audition(self, bank: Any, number: int) -> None:
        """Make the dump's destination the current performance first.

        A single performance dump fills the edit buffer, and what the unit
        shows and plays is the buffer on top of a current performance.
        Selecting the destination first -- Performance mode, Bank Select,
        Program Change, the same messages the browser's select uses --
        leaves the unit on the slot the audition is meant for instead of
        whatever the user happened to be on, and guarantees the unit is in
        Performance mode where a Program Change means a performance.
        """
        from kws import banks as _banks

        select = getattr(self.device, "select", None)
        if not callable(select):
            return
        try:
            select(_banks.Slot(bank=bank, kind=_banks.Kind.PERFORMANCE, number=number))
        except Exception:  # noqa: S110 - a unit that will not switch still hears
            pass

    def _audition_buffer(
        self,
        screen: Any,
        sources: Any,
        allocation: Any,
        target_bank: Any,
        target_slot: int,
    ) -> None:
        try:
            record = perfcopy.planned_performance_record(
                sources, allocation, target_bank
            )
            self._select_for_audition(target_bank, target_slot)
            self.device.send_performance_to_buffer(
                target_bank.number, target_slot, record
            )
            msg = (
                "auditioning — nothing written; play it on the unit; "
                "ctrl+s imports for real"
            )
            screen.set_status(msg)
            self.notify_status(msg)
        except Exception as exc:
            msg = f"audition failed: {exc}"
            screen.set_status(msg)
            self.notify_status(msg, refused=True)

    def _refuse_audition(self, screen: Any, msg: str) -> None:
        """Say no in both places: the screen's line and the app's."""
        screen.set_status(msg, warning=True)
        self.notify_status(msg, refused=True)

    def _preaudit_conflicting_slots(
        self,
        target_bank: Any,
        perf_slots: Any,
        patch_slots: Any,
    ) -> str:
        """What of the configured staging area an import is about to replace.

        The staging slots are the user's scratch, reserved by config; an
        import into that same bank will write them like any other slot.
        Naming them before the write is the difference between a surprise
        and a decision.
        """
        from kws import config as _cfg

        hits: list[str] = []
        perf_area = _cfg.load_preaudit_target(self._config_path)
        if (
            perf_area is not None
            and perf_area[0].id == target_bank.id
            and perf_area[1] in set(perf_slots)
        ):
            hits.append(f"performance {perf_area[1]:02d}")
        patch_area = _cfg.load_preaudit_patch_area(self._config_path)
        if patch_area is not None and patch_area[0].id == target_bank.id:
            overlap = sorted(set(patch_area[1]) & set(patch_slots))
            if overlap:
                hits.append("patch " + ", ".join(f"{n:02d}" for n in overlap))
        if not hits:
            return ""
        return (
            f"pre-audit staging {' and '.join(hits)} in {target_bank.label} "
            "(preaudit_target_perf / preaudit_target_patches)"
        )

    def _audition_staging_areas(
        self, screen: Any, fresh: Any
    ) -> Optional[Tuple[Any, Any]]:
        """The configured staging areas, or a refusal already shown."""
        from kws import config as _cfg

        perf_area = _cfg.load_preaudit_target(self._config_path)
        patch_area = _cfg.load_preaudit_patch_area(self._config_path)
        if perf_area is None or patch_area is None:
            self._refuse_audition(
                screen,
                "pre-audit staging not configured (see README: "
                "preaudit_target_perf / preaudit_target_patches)",
            )
            return None
        if len(fresh) > len(patch_area[1]):
            self._refuse_audition(
                screen,
                f"staging patch area too small: {len(fresh)} needed, "
                f"{len(patch_area[1])} configured",
            )
            return None
        if not self._write_gate.armed:
            self._refuse_audition(
                screen, "write gate locked (press w) — staging writes need it"
            )
            return None
        return perf_area, patch_area

    def _audition_staged(
        self,
        screen: Any,
        sources: Any,
        plan: Any,
        allocation: Any,
        target_bank: Any,
        target_slot: int,
        fresh: Any,
    ) -> None:
        """Stage what the plan needs, then put the record in the buffer.

        Fresh patch copies are written into the user-reserved staging
        slots one patch at a time -- 426 bytes and an EXECUTE_WRITE, read
        back to prove it landed -- because a whole patch bank is nearly
        30 kB on the wire for what is usually one copy. Wave sequences go
        to the target bank's sequence memory as its whole bank, the very
        bank the real import would install; there is no single-sequence
        write. A unit that does not take the single-patch write gets the
        whole bank instead, which is the path proven on the SR. Every
        write is fenced and backed up, and the staging is one undo record:
        `z` takes it back out. Reused patches need no write: they are
        already in the target bank, and the auditioned record points at
        them there.
        """
        areas = self._audition_staging_areas(screen, fresh)
        if areas is None:
            return
        perf_area, patch_area = areas
        planned_to_staging = {
            planned_slot: staging_slot
            for (_pid, planned_slot), staging_slot in zip(fresh, patch_area[1])
        }
        slot_override = {}
        for idx, s in allocation.part_patch_slots.items():
            if s in planned_to_staging:
                slot_override[idx] = planned_to_staging[s]
        try:
            built = perfcopy.build_slot_import(sources, plan)
            record = perfcopy.planned_performance_record(
                sources,
                allocation,
                target_bank,
                slot_override=slot_override,
                bank_override=patch_area[0].number,
            )
        except Exception as exc:
            msg = f"audition failed: {exc}"
            screen.set_status(msg)
            self.notify_status(msg, refused=True)
            return
        target_source = getattr(screen, "_target_source", None)
        stage_bank = patch_area[0].number
        buffer_slot = perf_area[1]
        staging = self._staging_cache_for(screen)
        from kws import config as _cfg2

        save_backup = _cfg2.load_preaudit_backup(self._config_path)

        def work() -> Any:
            event = getattr(screen, "_staging_prefetch", None)
            if event is not None:
                event.wait(180)
            undo_entries, seq_note, patch_path = self._stage_banks(
                target_source,
                plan,
                built,
                fresh,
                patch_area,
                target_bank,
                staging,
                save_backup,
            )
            self._select_for_audition(patch_area[0], buffer_slot)
            self.device.send_performance_to_buffer(stage_bank, buffer_slot, record)
            return undo_entries, seq_note, patch_path

        def done(result: Any, failure: BaseException | None) -> None:
            if failure is not None:
                msg = f"audition failed: {failure}"
                screen.set_status(msg)
                self.notify_status(msg, refused=True)
                return
            undo_entries, seq_note, patch_path = result
            if undo_entries:
                # The staging is destructive, so it is one undo record: `z`
                # puts both banks back the way they were.
                self._undo_log.push(
                    _undo.Record(
                        label="pre-audit staging",
                        payload={"backups": undo_entries},
                    )
                )
                self._pending_staging = True
            msg = self._staging_message(
                fresh,
                patch_area,
                undo_entries,
                seq_note,
                patch_path,
                save_backup,
            )
            screen.set_status(msg)
            self.notify_status(msg)

        self.notify_status("staging the audition...")
        self._device_worker(work, done, "auditioning from the staging area")

    def _begin_staging_prefetch(self, screen: Any) -> None:
        """Read the staging bank while the user is still picking rows.

        The whole-bank fallback needs the staging bank's content to keep
        the slots it is not staging, and that read is what the unit spends
        its time on. Started here, it overlaps with choosing assignments;
        `t` then usually finds the answer waiting. A read costs nothing on
        the unit's memory, so no write gate is involved.
        """
        import threading as _threading

        from kws import config as _cfg

        patch_area = _cfg.load_preaudit_patch_area(self._config_path)
        if patch_area is None:
            return
        staging = self._staging_cache_for(screen)
        if staging.get("bank") is not None:
            return
        bank = patch_area[0]
        event = _threading.Event()
        try:
            screen._staging_prefetch = event
        except Exception:
            return

        def work() -> Any:
            dump = self.device.read_bank(bank.id, banks.Kind.PATCH)
            return b"".join(dump.records)

        def done(result: Any, failure: BaseException | None) -> None:
            try:
                if failure is None and result is not None:
                    staging["bank"] = bytes(result)
            finally:
                event.set()

        self._device_worker(work, done, f"preparing {bank.label} for auditions")

    def _staging_cache_for(self, screen: Any) -> dict[str, Any]:
        """What this screen's staging slots hold, from the first read on.

        One import screen is one session: after the first audition this
        program is the only writer of those slots, so a slot is read once
        and then tracked here instead of re-read for every `t`. Undo drops
        the cache, because a restore is another writer.
        """
        staging = getattr(screen, "_staging_cache", None)
        if isinstance(staging, dict):
            return staging
        staging = {"slots": {}, "bank": None}
        try:
            screen._staging_cache = staging
        except Exception:  # noqa: S110 - the cache is an optimisation
            pass
        return staging

    def _stage_banks(
        self,
        target_source: Any,
        plan: Any,
        built: Any,
        fresh: Any,
        patch_area: Any,
        target_bank: Any,
        staging: Any = None,
        save_backup: bool = True,
    ) -> tuple[list[dict[str, Any]], str, str]:
        """Write the staging patch bank, then sequences if the plan needs them.

        Returns the undo entries, the sentence fragment that names the
        sequences in the status line, and which patch path was taken
        (``"single"`` or ``"bank"``).
        """
        undo_entries, patch_path = self._stage_patches(
            target_source, built, fresh, patch_area, staging, save_backup
        )
        seq_note = ""
        if plan.sequence_map and built.waveseq is not None:
            undo_entries += self._stage_one_bank(
                target_source, target_bank, "waveseq", bytes(built.waveseq)
            )
            seq_note = (
                f" and staged {len(plan.sequence_map)} wave sequence(s) "
                f"into {target_bank.label}"
            )
        return undo_entries, seq_note, patch_path

    def _stage_patches(
        self,
        target_source: Any,
        built: Any,
        fresh: Any,
        patch_area: Any,
        staging: Any = None,
        save_backup: bool = True,
    ) -> tuple[list[dict[str, Any]], str]:
        """Stage the fresh copies one patch at a time.

        A whole patch bank is nearly 30 kB on the wire -- about ten seconds
        each way -- for what is usually one or two patches. One patch is
        426 bytes and the unit's EXECUTE_WRITE commits it (see
        bridge.write_patch), so the copy is a fraction of a second.

        Only **one** read-back is spent on a whole audition. Verifying each
        patch meant a read after every write, and on the SR those reads are
        what the unit spends its time on -- a retry per patch made the
        audition slower than the bank dump it replaced. The last copy is
        read once; if it did not land, the whole bank is staged instead,
        which is the path proven on the SR.

        The previous content of every slot written is captured here (one
        small read per patch) because it is the undo's ``before`` and the
        bank fallback's restore point.
        """
        from kws import backup as _backup
        from kws import messages as _m

        bank = patch_area[0]
        if self._preaudit_single_writes is False:
            # This unit already showed it does not commit a single patch;
            # the writes and read-backs would only cost time.
            return (
                self._stage_patches_as_bank(
                    target_source,
                    built,
                    fresh,
                    patch_area,
                    {},
                    staging,
                    save_backup,
                ),
                "bank",
            )
        cache_slots: dict[int, bytes] = (
            staging["slots"] if isinstance(staging, dict) else {}
        )
        entries: list[dict[str, Any]] = []
        captured: dict[int, bytes] = {}
        written: list[tuple[int, bytes]] = []
        for (_pid, planned_slot), staging_slot in zip(fresh, patch_area[1]):
            src_at = planned_slot * rec.PATCH_SIZE
            data = bytes(built.patches[src_at : src_at + rec.PATCH_SIZE])
            before: Optional[bytes] = cache_slots.get(staging_slot)
            if save_backup and before is None:
                # One read per slot per import-screen session: after that
                # this program is the only writer of the staging slots, so
                # what it wrote last time is what is there now. With the
                # backup switched off, nothing is read at all.
                before = bytes(
                    self.device.read_patch(
                        bank.number,
                        staging_slot,
                        label=(
                            f"backing up {bank.label} staging slot {staging_slot:02d}"
                        ),
                    )
                )
                cache_slots[staging_slot] = before
            captured[staging_slot] = before if before is not None else b""
            try:
                result = self.device.write_patch(
                    bank.number,
                    staging_slot,
                    data,
                    backup_dir=_backup.default_dir(),
                    verify=False,
                    wait_status=False,
                    before=before,
                    save_backup=save_backup,
                )
            except Exception:
                return (
                    self._stage_patches_as_bank(
                        target_source,
                        built,
                        fresh,
                        patch_area,
                        captured,
                        staging,
                        save_backup,
                    ),
                    "bank",
                )
            if result.get("status") == "write error":
                return (
                    self._stage_patches_as_bank(
                        target_source,
                        built,
                        fresh,
                        patch_area,
                        captured,
                        staging,
                        save_backup,
                    ),
                    "bank",
                )
            written.append((staging_slot, data))
            cache_slots[staging_slot] = data
            if isinstance(staging, dict) and staging.get("bank") is not None:
                bank_bytes = bytearray(staging["bank"])
                at = staging_slot * rec.PATCH_SIZE
                bank_bytes[at : at + rec.PATCH_SIZE] = data
                staging["bank"] = bytes(bank_bytes)
            if save_backup and before is not None:
                entries.append(
                    {
                        "kind": "patch",
                        "bank": bank.number,
                        "slot": staging_slot,
                        "blocks": {
                            "dump": _m.single_patch_dump(
                                bank.sysex,
                                staging_slot,
                                before,
                                channel=self.device.channel,
                            )
                        },
                    }
                )
        if written and not self._staged_patch_landed(bank, *written[-1]):
            self._preaudit_single_writes = False
            return (
                self._stage_patches_as_bank(
                    target_source,
                    built,
                    fresh,
                    patch_area,
                    captured,
                    staging,
                    save_backup,
                ),
                "bank",
            )
        if written:
            self._preaudit_single_writes = True
        return entries, "single"

    def _staged_patch_landed(self, bank: Any, slot: int, expected: bytes) -> bool:
        """Whether the last staged copy is really in the unit's memory.

        One read for the whole audition, with one short retry: the unit may
        still have been committing when the first read was answered, and
        that must not be mistaken for a write it never performed. A read
        that fails is treated as "assume it landed": the copies are already
        sent, and turning a slow read into a whole-bank write on top would
        make the audition slower still.
        """
        try:
            after = bytes(self.device.read_patch(bank.number, slot))
            if after != expected:
                time.sleep(0.25)
                after = bytes(self.device.read_patch(bank.number, slot))
        except Exception:
            return True
        return after == expected

    def _stage_patches_as_bank(
        self,
        target_source: Any,
        built: Any,
        fresh: Any,
        patch_area: Any,
        captured: Any,
        staging: Any = None,
        save_backup: bool = True,
    ) -> list[dict[str, Any]]:
        """Stage the fresh copies as one whole patch bank.

        The slow path, kept because it is the one seen to work on an SR.
        The bank is read first so its untouched slots survive, and every
        slot this audition already tried to write gets its captured
        original put back -- a refused single write may still have landed,
        and the undo must put back what was there, not what a half-done
        try left.
        """
        cached = staging.get("bank") if isinstance(staging, dict) else None
        expected = banks.PATCHES_PER_BANK * rec.PATCH_SIZE
        if cached is not None and len(cached) == expected:
            before_bank = bytearray(cached)
        else:
            dump = self.device.read_bank(patch_area[0].id, banks.Kind.PATCH)
            before_bank = bytearray(b"".join(dump.records))
        for staging_slot, blob in captured.items():
            at = staging_slot * rec.PATCH_SIZE
            before_bank[at : at + rec.PATCH_SIZE] = blob
        staged_bank = bytearray(before_bank)
        for (_pid, planned_slot), staging_slot in zip(fresh, patch_area[1]):
            src_at = planned_slot * rec.PATCH_SIZE
            dst_at = staging_slot * rec.PATCH_SIZE
            staged_bank[dst_at : dst_at + rec.PATCH_SIZE] = built.patches[
                src_at : src_at + rec.PATCH_SIZE
            ]
        source = deps.Source(
            patch_area[0].id,
            patches=[
                bytes(before_bank[at : at + rec.PATCH_SIZE])
                for at in range(0, len(before_bank), rec.PATCH_SIZE)
            ],
        )
        result = self._stage_one_bank(
            target_source, patch_area[0], "patches", bytes(staged_bank), source
        )
        if not save_backup:
            result = []
        if isinstance(staging, dict):
            staging["bank"] = bytes(staged_bank)
            for (_pid, _planned_slot), staging_slot in zip(fresh, patch_area[1]):
                at = staging_slot * rec.PATCH_SIZE
                staging["slots"][staging_slot] = bytes(
                    staged_bank[at : at + rec.PATCH_SIZE]
                )
        return result

    def _stage_one_bank(
        self,
        target_source: Any,
        bank: Any,
        kind: str,
        data: bytes,
        source: Any = None,
    ) -> list[dict[str, Any]]:
        """Write one kind of one bank; return its undo entry, if any.

        ``source`` is the bank being written, as the app last read it;
        it defaults to the target source, which is right only when the
        bank is the target bank itself.
        """
        from kws import backup as _backup

        holder = target_source if source is None else source
        before = self._before_blocks(holder, bank)
        before_blocks = {kind: before[kind]} if kind in before else None
        if kind == "patches":
            result = self.device.write_bank_bundle(
                bank.number,
                patches=data,
                backup_dir=_backup.default_dir(),
                verify=False,
                before_blocks=before_blocks,
            )
        else:
            result = self.device.write_bank_bundle(
                bank.number,
                waveseq=data,
                backup_dir=_backup.default_dir(),
                verify=False,
                before_blocks=before_blocks,
            )
        verified = result.get("verified")
        status = result.get("status")
        if verified is False or status == "write error":
            raise RuntimeError(f"staging write failed: {status or 'not verified'}")
        if kind not in before:
            return []
        return [
            {"kind": "bank-bundle", "slot": bank.number, "blocks": {kind: before[kind]}}
        ]

    def _staging_message(
        self,
        fresh: Any,
        patch_area: Any,
        undo_entries: Any,
        seq_note: str,
        patch_path: str = "single",
        save_backup: bool = True,
    ) -> str:
        """The status line for a successful staged audition."""
        slots_text = ", ".join(f"{s:02d}" for s in patch_area[1][: len(fresh)])
        patch_note = (
            f"staged {len(fresh)} patch(es) into {patch_area[0].label} {slots_text}"
            if fresh
            else "no fresh patches needed"
        )
        how = "" if patch_path == "single" or not fresh else " (bank dump)"
        backed = "" if save_backup else "; not backed up (preaudit_backup = false)"
        return (
            f"{patch_note}{how}{seq_note}; auditioning — play it; "
            "ctrl+s imports for real"
            + ("; z undoes the staging" if undo_entries else "")
            + backed
        )

    def _audition_focused(self, screen: Any) -> None:
        """Send the focused row's planned performance to the edit buffer."""
        slot = screen._focused_source_slot()
        if slot is None:
            screen.set_status("focus a source row to audition")
            self.notify_status("focus a source row to audition", refused=True)
            return
        target_slot = screen._target_slot_for_source(slot)
        if target_slot is None:
            # Audition through a scratch slot; no assignment is committed.
            target_slot = screen._first_free_target_slot()
            if target_slot is None:
                msg = "no free target slot to audition into"
                screen.set_status(msg)
                self.notify_status(msg, refused=True)
                return
        sources = screen.sources
        # resolve target scope from screen state
        target_bank = screen._target_bank
        from kwsed.import_single import _target_scope as _ts

        target_scope = _ts(sources, target_bank, screen._target_source)
        plan = perfcopy.plan_slot_import(
            sources,
            target_scope,
            {target_slot: relations.PerfId(screen._source_scope(), slot)},
            destination_bank=target_bank,
            share_existing=screen._share_patches,
        )
        if not plan.fits or not plan.allocations:
            reasons = "; ".join(plan.rejected) if plan.rejected else "plan does not fit"
            screen.set_status(reasons)
            self.notify_status(reasons, refused=True)
            return
        allocation = plan.allocations[0]
        # "allocated" lists every placement; the reused ones are already in
        # the target bank, and only the rest need a staging write.
        reused = set(allocation.reused_patch_slots)
        fresh = [item for item in allocation.allocated_patches if item[1] not in reused]
        if not fresh and not plan.sequence_map:
            self._audition_buffer(screen, sources, allocation, target_bank, target_slot)
            return
        self._audition_staged(
            screen, sources, plan, allocation, target_bank, target_slot, fresh
        )

    def _single_source_menu_chosen(self, screen: Any, index: Optional[int]) -> None:
        if index is None:
            return
        if 0 <= index < len(screen.source_banks):
            screen.select_source(index)
            return
        extra_index = len(screen.source_banks)
        if index == extra_index:
            self._browse_extra_source(screen)
            return
        if index == extra_index + 1:
            self._browse_unit_source(screen)
            return

    def _browse_extra_source(self, screen: Any) -> None:
        picker = FilePickerScreen(
            start=self._import_folder or "",
            start_row=self._import_cursor,
        )

        def done(path: Optional[str]) -> None:
            self._import_folder = str(picker.folder)
            from kws import config as _cfg

            if self._config_path is not None:
                try:
                    _cfg.save_import_folder(str(picker.folder), self._config_path)
                except OSError:
                    pass
            self._import_cursor = picker.last_row
            if not path:
                return
            try:
                extra_banks = importers.read_file(path)
            except (importers.UnsupportedFormat, ValueError, OSError) as exc:
                self.notify_status(f"import: {exc}", refused=True)
                return
            if not extra_banks:
                self.notify_status("import: no banks found")
                return
            if len(extra_banks) == 1:
                self._add_extra_source(screen, extra_banks[0], path)
                return
            self.push_screen(
                BankImportScreen(path=path, banks=extra_banks),
                lambda choice: self._add_extra_source_choice(
                    screen, extra_banks, path, choice
                ),
            )

        self.push_screen(picker, done)

    def _browse_unit_source(self, screen: Any) -> None:
        choices = unit_bank_choices()
        active_scope = self._active_card_scope()
        labels: List[str] = []
        for choice in choices:
            if choice.scope == "CARD":
                labels.append(origin_for_unit(active_scope))
            else:
                labels.append(origin_for_unit(choice.scope))

        def chosen(index: Optional[int]) -> None:
            if index is None:
                return
            try:
                choice = choices[index]
            except (IndexError, TypeError):
                return
            self._add_unit_source(screen, choice)

        self.push_screen(ImportMenuScreen("Add unit bank", labels), chosen)

    def _add_unit_source(self, screen: Any, choice: Any) -> None:
        scope = (
            self._active_card_scope()
            if getattr(choice, "scope", "") == "CARD"
            else str(getattr(choice, "scope", ""))
        )
        origin = origin_for_unit(scope)
        label = origin
        source = self._load_unit_source(scope, label)
        if source is None:
            return
        entry: SourceEntry = make_source_entry(
            label=origin,
            scope=scope,
            source=source,
            origin=origin,
            existing=screen.sources,
            read_only=bool(getattr(choice, "read_only", False)),
            bank_number=None,
        )
        screen.add_source_bank(entry, entry.source)

    def _add_extra_source_choice(
        self,
        screen: Any,
        banks_list: Sequence[Any],
        path: str,
        index: Optional[int],
    ) -> None:
        if index is None:
            return
        try:
            bank = banks_list[index]
        except (IndexError, TypeError):
            return
        if bank is not None:
            self._add_extra_source(screen, bank, path)

    def _add_extra_source(self, screen: Any, bank: Any, path: str = "") -> None:
        bank, note = self._complete_source_file(path, bank)
        incoming = bank.source
        for index, existing in enumerate(screen.source_banks):
            if not same_source_bank(existing.source, incoming):
                continue
            merged = merge_source_components(existing.source, incoming)
            if merged is None:
                continue
            entry = SourceEntry(
                label=str(getattr(existing, "label", "source")),
                scope=str(getattr(existing, "scope", "source")),
                source=merged,
                origin=str(getattr(existing, "source_origin", "")),
                read_only=bool(getattr(existing, "read_only", False)),
            )
            screen.replace_source_bank(index, entry, merged)
            message = note or (
                f"merged {getattr(bank, 'label', path)} into "
                f"{entry.label}; assignments kept"
            )
            screen.set_status(message)
            self.notify_status(message)
            return
        wrapped, aliased = self._session_source(bank, screen.sources, path)
        screen.add_source_bank(wrapped, aliased)
        if note:
            screen.set_status(note)

    def _import_single_chosen(  # noqa: C901 - one flow, with fence and cache
        self,
        choice: Any,
        target: Any,
        bank: Any,
        sources: Any,
        path: str = "",
        banks_list: Sequence[Any] = (),
        writable: Sequence[Any] = (),
    ) -> None:
        if choice is None:
            if self._pending_staging:
                self.notify_status(
                    "an audition's staging is still on the unit; z restores "
                    "it (armed gate)",
                    warn=True,
                )
            elif path and writable:
                self._open_single_target_chooser(path, bank, banks_list, writable)
            return
        if not self._write_gate.armed:
            self.notify_status("write gate is locked", refused=True)
            return
        from kws import banks as _banks
        from kws import backup as _backup

        choice_sources = getattr(choice, "sources", None)
        if isinstance(choice_sources, Mapping) and choice_sources:
            sources = dict(choice_sources)
        target_bank = _banks.bank(getattr(choice, "target_bank", target.id))
        target_scope = target_bank.id
        assignments = getattr(choice, "assignments", {}) or {}
        if not assignments:
            self.notify_status("import: no assignments")
            return
        share_existing = bool(getattr(choice, "share_patches", True))
        try:
            built = perfcopy.copy_slots_into_bank(
                sources,
                target_scope,
                assignments,
                destination_bank=target_bank,
                share_existing=share_existing,
            )
        except (ValueError, LookupError) as exc:
            self.notify_status(f"import does not fit: {exc}", refused=True)
            return
        if getattr(built.plan, "rejected", None):
            self.notify_status(
                "import does not fit: " + "; ".join(built.plan.rejected),
                refused=True,
            )
            return
        patch_slots = {
            slot
            for alloc in built.plan.allocations
            for _pid, slot in alloc.allocated_patches
        }
        conflict = self._preaudit_conflicting_slots(
            target_bank, assignments.keys(), patch_slots
        )
        target_source = sources.get(target_scope)
        if target_source is None:
            target_source = sources.get(target.id)
        before = (
            self._before_blocks(target_source, target_bank)
            if target_source is not None
            else {}
        )

        def work() -> Any:
            return self.device.write_bank_bundle(
                target_bank.number,
                performances=built.performances,
                patches=built.patches,
                waveseq=built.waveseq,
                backup_dir=_backup.default_dir(),
                before_blocks=before if before else None,
            )

        def done(result: Any, failure: BaseException | None) -> None:
            if failure is not None:
                self.notify_status(f"import: {failure}", refused=True)
                return
            self._import_single_written(
                built, target_scope, target_bank, result, conflict
            )

        status = f"writing the import into {target_bank.label}..."
        if conflict:
            status = f"warning: this import overwrites {conflict} — writing anyway"
        self.notify_status(status, warn=bool(conflict))
        self._device_worker(work, done, f"importing into {target_bank.label}")

    def _import_single_written(
        self,
        built: Any,
        target_scope: str,
        target_bank: Any,
        result: Any,
        conflict: str = "",
    ) -> None:
        """Book-keeping after a single import has landed."""
        if hasattr(self, "_record_write"):
            self._record_write(result)
        new_source = self._source_from_built(target_scope, built)
        self._sources[target_scope] = new_source
        if self._bankstore_dir is not None:
            try:
                bankstore.save(
                    target_scope,
                    performances=built.performances,
                    patches=built.patches,
                    waveseq=built.waveseq,
                    directory=self._bankstore_dir,
                )
            except OSError:
                pass
        self._refresh_written_bank(target_scope, built)
        overwrote = f"; overwrote {conflict}" if conflict else ""
        self.notify_status(
            f"imported into {target_bank.label}{overwrote}; target refreshed "
            "only (S is a full scan, r re-reads the current bank)",
            warn=bool(conflict),
        )

    def _import_bank_chosen(
        self, kind: int, banks_list: Sequence[Any], index: Optional[int]
    ) -> None:
        if index is None:
            return
        try:
            bank = banks_list[index]
        except (IndexError, TypeError):
            return
        if bank is None:
            return
        if kind == 0:
            self._save_imported_bank(bank)
            return
        active = list(perfcopy.active_performances(bank.source))
        if not active:
            self.notify_status("import: no performances in this bank")
            return
        choices = []
        for slot in active:
            rec_bytes = bank.source.performances[slot]
            name = m.decode_name(rec_bytes)
            choices.append(f"{slot:02d} {name}")
        self.push_screen(
            ChoiceScreen("Import performance", choices),
            lambda sel: self._import_perf_chosen(bank, active, sel),
        )

    def _import_perf_chosen(
        self, bank: Any, active: Sequence[int], sel: Optional[int]
    ) -> None:
        if sel is None:
            return
        try:
            slot = active[sel]
        except (IndexError, TypeError):
            return
        name = m.decode_name(bank.source.performances[slot])
        self.push_screen(
            CopyPerformanceScreen(self, relations.PerfId(bank.scope, slot), name)
        )

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

    def action_toggle_patches(self) -> None:
        """Show or hide the patch rows in the bank list.

        A patch cannot be selected, auditioned or copied on its own; its
        rows exist for inspection. Hiding them halves the list without
        losing an action, and the dependency pane still shows the patches
        of whichever Performance is selected.
        """
        multi = self._current_multi
        pair = self._current_pair
        card = self._current_card
        physical = self._current_physical
        self._show_patches = not self._show_patches
        if not self._show_patches and pair[1] == banks.Kind.PATCH:
            pair = (pair[0], banks.Kind.PERFORMANCE)
        self._build_row_model()
        self._apply_row_model()
        table = self.query_one("#bank-table", DataTable)
        key = (
            f"multi:{multi:02d}"
            if multi is not None
            else self._row_key(pair, card, None, physical)
        )
        with contextlib.suppress(Exception):
            table.move_cursor(row=table.get_row_index(key))
        if multi is not None:
            self._refresh_deps_pane()
        elif physical is not None:
            self._fill_physical(physical, pair[1])
        else:
            self._fill(pair, card)
        self._remember_show_patches()
        self.notify_status(
            "showing patches" if self._show_patches else "hiding patches"
        )

    def action_toggle_deps_pane(self) -> None:
        """Show or hide the live dependency pane on the right."""
        self._deps_pane_visible = not self._deps_pane_visible
        with contextlib.suppress(NoMatches):
            self.query_one("#deps-pane").display = self._deps_pane_visible
        if self._deps_pane_visible:
            self._refresh_deps_pane()
        self._remember_deps_pane()
        self.notify_status(
            "dependency pane shown"
            if self._deps_pane_visible
            else "dependency pane hidden"
        )

    def _remember_show_patches(self) -> None:
        """Save the patch-row choice for the next run."""
        if self._config_path is None:
            return
        from kws import config as _ui_cfg

        try:
            _ui_cfg.save_show_patches(self._show_patches, self._config_path)
        except OSError:
            pass

    def _remember_deps_pane(self) -> None:
        """Save the dependency-pane choice for the next run."""
        if self._config_path is None:
            return
        from kws import config as _ui_cfg

        try:
            _ui_cfg.save_deps_pane(self._deps_pane_visible, self._config_path)
        except OSError:
            pass

    def _deps_target(self) -> "tuple[Optional[int], Optional[SlotLike]]":
        """The Multiset row under the bank cursor, or the slot under the list."""
        try:
            bank_table = self.query_one("#bank-table", DataTable)
            if bank_table.has_focus:
                row = bank_table.cursor_row
                if 0 <= row < len(self._row_multis):
                    chosen = self._row_multis[row]
                    if chosen is not None:
                        return chosen, None
        except Exception:  # noqa: S110 - a table mid-refill has no cursor
            pass
        return None, self._cursor_entry()

    def _sources_with_store(self) -> Dict[str, deps.Source]:
        """``self._sources`` plus whatever the cache holds that it lacks."""
        sources = dict(self._sources)
        if self._bankstore_dir is not None:
            try:
                for sid, src in bankstore.load_all(self._bankstore_dir).items():
                    if sid not in sources:
                        sources[sid] = src
            except Exception:  # noqa: S110 - a corrupt cache is not fatal
                pass
        return sources

    def _deps_sources(self) -> Dict[str, deps.Source]:
        """The merged source map for the live pane, cached until it changes.

        ``_sources_with_store`` reads the bank cache from disk; the pane
        refreshes on every cursor move, so that read happens once per
        change to ``_sources`` rather than once per key press.
        """
        key = tuple(sorted((scope, id(src)) for scope, src in self._sources.items()))
        if key == self._deps_sources_key and self._deps_sources_cache is not None:
            return self._deps_sources_cache
        sources = self._sources_with_store()
        self._deps_sources_key = key
        self._deps_sources_cache = sources
        return sources

    def _deps_graph_for_pane(self, sources: Dict[str, deps.Source]) -> Any:
        """One relation graph per loaded snapshot, so its own caches help."""
        key = (
            tuple(sorted((scope, id(src)) for scope, src in sources.items())),
            tuple(id(ms) for ms in self._multisets),
        )
        if key != self._deps_graph_key or self._deps_graph is None:
            self._deps_graph = relations.RelationGraph(
                sources,
                multisets=self._multisets,
                active_card_scope=self._active_card_scope(),
            )
            self._deps_graph_key = key
        return self._deps_graph

    def _deps_pane_lines(self) -> List[str]:
        """The ``d`` report for the current selection, without reading the unit."""
        multiset, entry = self._deps_target()
        sources = self._deps_sources()
        if multiset is not None:
            return self._deps_multiset_lines(multiset, sources)
        if entry is None:
            return ["(nothing selected)"]
        scope = self._resolve_card_scope(self._scoped_bank_id(entry))
        if scope not in sources:
            # This pane never reads the unit; it only shows what is cached.
            return [f"({scope} is not loaded — press d to read it)"]
        graph = self._deps_graph_for_pane(sources)
        try:
            if entry.kind == banks.Kind.PERFORMANCE:
                return self._deps_performance_lines(graph, sources, scope, entry.number)
            if entry.kind == banks.Kind.PATCH:
                return self._deps_patch_lines(graph, sources, scope, entry.number)
        except Exception as exc:
            return [f"(error: {exc})"]
        return [f"{scope} {entry.number} (unknown kind)"]

    def _refresh_deps_pane(self) -> None:
        """Rewrite the live pane, if it is showing. Never touches MIDI."""
        if not self._deps_pane_visible:
            return
        try:
            body = self.query_one("#deps-pane-body", Static)
        except NoMatches:
            return
        try:
            lines = self._deps_pane_lines()
        except Exception as exc:  # pragma: no cover - defensive
            lines = [f"(error: {exc})"]
        body.update("\n".join(lines))

    def _active_card_scope(self) -> str:
        """The archived scope that the one physical card slot currently holds.

        A Multiset part records the wire byte ``3`` for the card, which names
        the slot rather than which archived bank is in it. The dependency
        graph has to resolve that byte to the scope whose records are loaded
        (``CARD/07``, ``CARD/c1``), or every card channel looks unloaded.
        """
        if self._active_physical:
            try:
                return _physical.scope_id(self._active_physical)
            except Exception:  # noqa: S110 - fall back to the HCard
                pass
        if self._active_card is not None:
            try:
                return _cards.scope_id(self._active_card)
            except Exception:  # noqa: S110 - fall back to the bare slot
                pass
        return "CARD"

    def _resolve_card_scope(self, scope: str) -> str:
        """``"CARD"`` is the slot; the loaded records are under the active scope."""
        return self._active_card_scope() if scope == "CARD" else scope

    def _guard_card_slot(self, scope: str) -> None:
        """Refuse to read a card sub-bank the slot may not hold.

        The synth has one physical card slot, so a read of ``CARD/07`` is
        really a read of whatever bank is in that slot. Without this check
        the records would be filed under the requested scope even when the
        slot holds another bank -- and a write built from them would carry
        the wrong performance under the requested label.
        """
        if not scope.startswith("CARD/"):
            return
        active = self._active_card_scope()
        if active == scope:
            return
        if active == "CARD":
            raise ValueError(
                "the app has not confirmed which card bank is in the slot; "
                f"select {scope[5:]} on the HCard and press C once, then try again"
            )
        raise ValueError(
            f"{scope} is not the card bank in the slot; the slot holds {active}; "
            "select that bank on the HCard and press C to re-check"
        )

    def action_show_deps(self) -> None:
        multiset, entry = self._deps_target()
        if multiset is not None:
            self._show_deps_multiset(multiset)
            return
        if entry is None:
            self.notify_status("nothing under the cursor", refused=True)
            return
        scope = self._resolve_card_scope(self._scoped_bank_id(entry))
        sources = self._sources_with_store()
        if scope not in sources:
            # Only a *read* needs the slot to hold the card the row names;
            # a bank already in the archive or cache can be inspected even
            # when a different card is inserted.
            try:
                self._guard_card_slot(scope)
            except ValueError as exc:
                self.notify_status(str(exc), refused=True)
                return
            if not self._exchange.acquire(blocking=False):
                self.notify_status("busy", refused=True)
                return
            self._deps_worker(scope, entry.kind, entry.number, None)
            return
        self._show_deps(scope, entry.kind, entry.number, None, sources)

    @work(thread=True)
    def _copy_perf_load_worker(self, scopes: list[str], screen: Any) -> None:
        loaded: Dict[str, deps.Source] = {}
        failure: BaseException | None = None
        try:
            if not self._exchange.acquire(blocking=False):
                self.call_from_thread(screen._context_failed, "busy")
                return
            try:
                with self._busy_gate, self._lock:
                    for scope in scopes:
                        loaded[scope] = self._read_source_offline(scope)
            finally:
                try:
                    self._exchange.release()
                except RuntimeError:
                    pass
        except Exception as exc:
            failure = exc
        if failure is not None:
            self.call_from_thread(screen._context_failed, str(failure))
            return
        self.call_from_thread(self._copy_perf_context_loaded, loaded, screen)

    def _copy_perf_context_loaded(self, loaded: Any, screen: Any) -> None:
        for scope, src in loaded.items():
            self._sources[scope] = src
        if self._bankstore_dir is not None:
            for scope, src in loaded.items():
                try:
                    bankstore.save(
                        scope,
                        performances=b"".join(src.performances),
                        patches=b"".join(src.patches),
                        waveseq=src.waveseq,
                        directory=self._bankstore_dir,
                    )
                except OSError:
                    pass
        screen._context_loaded()

    @work(thread=True)
    def _deps_worker(
        self, scope: str, kind: str, number: int, multiset: Optional[int]
    ) -> None:
        failure: BaseException | None = None
        payload: Any = None
        try:
            with self._busy_gate, self._lock:
                bank = self._bank_for_scope(scope)
                perf_raw = self.device.read_raw(
                    bank.sysex, m.Function.ALL_PERFORMANCE_DUMP
                )
                patch_raw = self.device.read_raw(bank.sysex, m.Function.ALL_PATCH_DUMP)
                ws_raw = self.device.read_raw(bank.sysex, m.Function.ALL_WAVESEQ_DUMP)
            pp = m.parse(perf_raw)
            pa = m.parse(patch_raw)
            pw = m.parse(ws_raw)
            perf_bytes = pp.data() if pp is not None else b""
            patch_bytes = pa.data() if pa is not None else b""
            ws_bytes = pw.data() if pw is not None else b""
            if len(ws_bytes) > rec.WAVESEQ_BANK_SIZE:
                ws_bytes = ws_bytes[: rec.WAVESEQ_BANK_SIZE]
            perfs_list = [
                perf_bytes[i * rec.PERFORMANCE_SIZE : (i + 1) * rec.PERFORMANCE_SIZE]
                for i in range(len(perf_bytes) // rec.PERFORMANCE_SIZE)
            ]
            patches_list = [
                patch_bytes[i * rec.PATCH_SIZE : (i + 1) * rec.PATCH_SIZE]
                for i in range(len(patch_bytes) // rec.PATCH_SIZE)
            ]
            source = deps.Source(
                scope_id=scope,
                performances=perfs_list,
                patches=patches_list,
                waveseq=ws_bytes,
            )
            payload = (scope, kind, number, multiset, source)
        except Exception as exc:
            failure = exc
        finally:
            try:
                self._exchange.release()
            except RuntimeError:
                pass
        if failure is not None:
            self.call_from_thread(self.notify_status, f"deps: {failure}", refused=True)
            return
        self.call_from_thread(self._deps_loaded, payload)

    def _deps_loaded(self, payload: Any) -> None:
        scope, kind, number, multiset, source = payload
        self._sources[scope] = source
        if self._bankstore_dir is not None:
            try:
                bankstore.save(
                    scope,
                    performances=b"".join(source.performances),
                    patches=b"".join(source.patches),
                    waveseq=source.waveseq,
                    directory=self._bankstore_dir,
                )
            except OSError:
                pass
        sources = dict(self._sources)
        if self._bankstore_dir is not None:
            try:
                for sid, src in bankstore.load_all(self._bankstore_dir).items():
                    if sid not in sources:
                        sources[sid] = src
            except Exception:  # noqa: S110
                pass
        if multiset is not None:
            self._show_deps_multiset(multiset, sources)
        else:
            self._show_deps(scope, kind, number, multiset, sources)
        self._refresh_deps_pane()

    def _bank_for_scope(self, scope: str) -> Any:
        if scope.startswith("CARD") or scope == "CARD":
            return banks.bank("CARD")
        return banks.bank(scope)

    def _show_deps(
        self,
        scope: str,
        kind: str,
        number: int,
        multiset: Optional[int],
        sources: Dict[str, deps.Source],
    ) -> None:
        if multiset is not None:
            self._show_deps_multiset(multiset, sources)
            return
        graph = relations.RelationGraph(
            sources,
            multisets=self._multisets,
            active_card_scope=self._active_card_scope(),
        )
        try:
            if kind == banks.Kind.PERFORMANCE:
                body_lines = self._deps_performance_lines(graph, sources, scope, number)
            elif kind == banks.Kind.PATCH:
                body_lines = self._deps_patch_lines(graph, sources, scope, number)
            else:
                body_lines = [f"{scope} {number} (unknown kind)"]
        except Exception as exc:
            body_lines = [f"(error: {exc})"]
        self.push_screen(ReportScreen("Dependencies", "\n".join(body_lines)))

    @staticmethod
    def _where_used(graph: Any, patch: Any, current: Any) -> str:
        """Other Performances that use ``patch``, as ``SCOPE:NN``, no names.

        Only writable patch banks are reported: a ROM patch cannot be
        overwritten, so listing every factory ROM performance that also
        plays ``ROM11:16`` adds noise without warning of a shared-slot edit.
        """
        try:
            if (
                not patch.scope_id.startswith("CARD/")
                and not banks.bank(patch.scope_id).writable
            ):
                return ""
        except LookupError:
            pass
        others = [
            perf for perf in graph.performances_using_patch(patch) if perf != current
        ]
        return ", ".join(f"{perf.scope_id}:{perf.number:02d}" for perf in others)

    def _deps_performance_lines(
        self, graph: Any, sources: Dict[str, deps.Source], scope: str, number: int
    ) -> List[str]:
        """A Performance's parts, wave sequences, ownership and Multisets."""
        from kws import dumps as dumps_mod

        lines: List[str] = []
        pid = relations.PerfId(scope, number)
        rel = graph.performance_relation(pid)
        name = rel.name or ""
        src = sources.get(scope)
        if (
            not name
            and src is not None
            and src.performances
            and number < len(src.performances)
        ):
            try:
                name = dumps_mod.record_name(src.performances[number])
            except Exception:
                name = ""
        lines.append(f'{scope} {number:02d} "{name}"')
        for mapping in rel.parts:
            patch = mapping.source_patch
            pname = ""
            psrc = sources.get(patch.scope_id)
            if psrc is not None and psrc.patches and patch.number < len(psrc.patches):
                try:
                    pname = dumps_mod.record_name(psrc.patches[patch.number])
                except Exception:
                    pname = ""
            if not pname:
                try:
                    pname = self._scope_slot_name(
                        patch.scope_id, banks.Kind.PATCH, patch.number
                    )
                except Exception:
                    pname = ""
            used = self._where_used(graph, patch, pid)
            suffix = f"   (also {used})" if used else ""
            lines.append(
                f"  {mapping.part_index} -> {patch.scope_id}:{patch.number:02d} "
                f"{pname}{suffix}"
            )
        lines.append(
            "  unique patches: "
            + (
                ", ".join(f"{p.scope_id}:{p.number}" for p in rel.unique_patches)
                or "none"
            )
        )
        lines.append(f"  sequences: {len(rel.sequences)}")
        for seq_scope, wave_bank, sequence in rel.sequences:
            lines.append(f"    {seq_scope} {sequence} (wave bank {wave_bank})")
        lines.append(f"  step cost: {rel.step_cost}")
        shared = {
            pnum
            for pnum, perfs in graph.shared_patches_in_bank(scope).items()
            if number in perfs
        }
        lines.append("  patches: shared" if shared else "  patches: atomic")
        multisets = graph.multisets_using_performance(pid)
        lines.append(
            "  multisets: "
            + (", ".join(f"{num}.{ch}" for num, ch in multisets) or "none")
        )
        return lines

    def _deps_patch_lines(
        self, graph: Any, sources: Dict[str, deps.Source], scope: str, number: int
    ) -> List[str]:
        """A Patch's four oscillators and every Performance that uses it."""
        from kws import dumps as dumps_mod

        lines: List[str] = []
        src = sources.get(scope)
        record = (
            src.patches[number]
            if (src is not None and src.patches and number < len(src.patches))
            else None
        )
        pname = ""
        if record is not None:
            try:
                pname = dumps_mod.record_name(record)
            except Exception:
                pname = ""
        lines.append(f'{scope} {number:02d} "{pname}"')
        if record is None:
            lines.extend(f"  Osc {i} -> (not loaded)" for i in range(1, 5))
        else:
            for i, wave in enumerate(rec.patch_waves(record), start=1):
                if not wave.used:
                    lines.append(f"  Osc {i} -> (off)")
                    continue
                if wave.is_sequence:
                    seq_scope = relations.resolve_part_scope(wave.bank, scope)
                    lines.append(
                        f"  Osc {i} -> wave sequence {wave.wave} in {seq_scope}"
                    )
                else:
                    lines.append(f"  Osc {i} -> wave {wave.wave}")
        lines.append("  Where used:")
        for perf in graph.performances_using_patch(relations.PatchId(scope, number)):
            pname2 = ""
            psrc = sources.get(perf.scope_id)
            if (
                psrc is not None
                and psrc.performances
                and perf.number < len(psrc.performances)
            ):
                try:
                    pname2 = dumps_mod.record_name(psrc.performances[perf.number])
                except Exception:
                    pname2 = ""
            lines.append(f"    {perf.scope_id}:{perf.number:02d} {pname2}")
        return lines

    def _multiset_channel_line(
        self, part: Any, sources: Dict[str, deps.Source], graph: Any
    ) -> "tuple[str, str]":
        """One ``Ch NN -> Bank PP Name patches a,b`` line and its scope."""
        from kws import dumps as dumps_mod

        try:
            bank_obj = banks.by_sysex(part.bank)
        except LookupError:
            bank_obj = None
        scope = bank_obj.id if bank_obj is not None else f"bank{part.bank}"
        scope = self._resolve_card_scope(scope)
        src = sources.get(scope)
        name = ""
        if (
            src is not None
            and src.performances
            and part.performance < len(src.performances)
        ):
            try:
                name = dumps_mod.record_name(src.performances[part.performance])
            except Exception:
                name = ""
        patches: List[str] = []
        if src is not None:
            try:
                rel = graph.performance_relation(
                    relations.PerfId(scope, part.performance)
                )
                patches = [str(p.number) for p in rel.unique_patches]
            except Exception:
                patches = []
        patch_text = " patches " + ",".join(patches) if patches else ""
        line = (
            f"  Ch {part.channel:02d} -> {scope} "
            f"{part.performance:02d} {name}{patch_text}"
        )
        return line, scope

    def _deps_multiset_lines(
        self, ms_number: int, sources: Optional[Dict[str, deps.Source]] = None
    ) -> List[str]:
        """The 16 channels of one Multiset, as a list of report lines."""
        if sources is None:
            sources = dict(self._sources)
        lines: List[str] = []
        try:
            ms = self._multisets[ms_number]
        except Exception:
            ms = None
        if ms is None:
            return [f"Multiset {ms_number:02d}", "(not loaded)"]
        lines.append(f"Multiset {getattr(ms, 'number', ms_number):02d}")
        graph = relations.RelationGraph(
            sources,
            multisets=self._multisets,
            active_card_scope=self._active_card_scope(),
        )
        card_scopes: List[str] = []
        for part in getattr(ms, "parts", []):
            if getattr(part, "used", False):
                line, scope = self._multiset_channel_line(part, sources, graph)
                lines.append(line)
                card_scopes.append(scope)
            else:
                lines.append(f"  Ch {part.channel:02d} -> (unused)")
        distinct_card = {s for s in card_scopes if s.startswith("CARD/")}
        if len(distinct_card) > 1:
            lines.append("  card conflicts: " + ", ".join(sorted(distinct_card)))
        else:
            lines.append("  card conflicts: none")
        return lines

    def _show_deps_multiset(
        self, ms_number: int, sources: Optional[Dict[str, deps.Source]] = None
    ) -> None:
        self.push_screen(
            ReportScreen(
                "Dependencies", "\n".join(self._deps_multiset_lines(ms_number, sources))
            )
        )

    def _refill_keeping_place(self) -> None:
        if self._current_physical is not None:
            kind = self._current_physical_kind
            if kind is None:
                kind = self._current_pair[1]
            self._fill_physical(self._current_physical, kind)
        elif self._current_multi is not None:
            self._fill_multiset(self._current_multi)
        else:
            table = self.query_one("#slot-table", DataTable)
            row = table.cursor_row or 0
            self._fill(self._current_pair, self._current_card)
            if self._current:
                table.move_cursor(row=min(row, len(self._current) - 1))
        self._refresh_deps_pane()

    def _card_slot_name(self, card: int, kind: str, number: int) -> str:
        captured = self._archive.get(card)
        if captured is None:
            return ""
        return captured.names(kind).get(number, "")

    def _scope_label(self, bank_id: str) -> str:
        """A human label for a scoped favourite bank id."""
        if bank_id.startswith("CARD/"):
            key = bank_id[5:]
            if key.isdigit():
                return f"Card {int(key):02d}"
            card = self._physical.get(key)
            return (card.label or key) if card is not None else key
        try:
            return banks.bank(bank_id).label
        except LookupError:
            return bank_id

    def _scope_slot_name(self, bank_id: str, kind: str, number: int) -> str:
        """The stored name for a slot in a scoped bank id."""
        if bank_id.startswith("CARD/"):
            key = bank_id[5:]
            if key.isdigit():
                captured = self._archive.get(int(key))
                return captured.names(kind).get(number, "") if captured else ""
            card = self._physical.get(key)
            if card is None:
                return ""
            names = (
                card.performances if kind == banks.Kind.PERFORMANCE else card.patches
            )
            return names.get(number, "")
        return self.catalog.display_name(bank_id, number, kind)

    def _slot_shown_name(self, item: SlotLike, card: Optional[int]) -> str:
        scope = getattr(item, "bank_id", None)
        if scope is not None:
            stored = getattr(item, "name", "")
            if stored == "(unnamed)":
                stored = ""
            name = stored or self._scope_slot_name(scope, item.kind, item.number)
            return f"[b]{name}[/b]" if name else "[dim](unnamed)[/dim]"
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
        if isinstance(table, WarningTable):
            table.set_warning_rows(())
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
                scope = getattr(item, "bank_id", None)
                if scope is not None:
                    scoped = scope
                    where = self._scope_label(scope)
                else:
                    scoped = item.bank.id if card is None else _cards.scope_id(card)
                    where = f"Card {card:02d}" if card is not None else item.bank.label
                marked = self._favorite_keys(scoped, item.kind)
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
        self._refresh_deps_pane()

    def _multi_part_display(self, binding: Any, part: Any) -> tuple[str, str]:
        """The bank and name a Multiset channel points at, card scope included.

        The part stores only the wire byte ``3`` for the card, which names
        the slot rather than the card in it. The live catalog knows which
        card the Multiset was recorded against; fall back to the bank in the
        slot, then to the static catalog, when there is no record.
        """
        card_bank = banks.bank("CARD").sysex
        if part.bank == card_bank:
            if binding is not None and binding.card_scope:
                perf = f"{self._scope_label(binding.card_scope)} {part.performance:02d}"
                if binding.performance == part.performance and binding.name:
                    return perf, binding.name
                return perf, self._scope_slot_name(
                    binding.card_scope,
                    banks.Kind.PERFORMANCE,
                    part.performance,
                )
            scope = self._active_card_scope()
            if scope != "CARD":
                return (
                    f"{self._scope_label(scope)} {part.performance:02d}",
                    self._scope_slot_name(
                        scope, banks.Kind.PERFORMANCE, part.performance
                    ),
                )
        perf = f"{_bank_label(part.bank)} {part.performance:02d}"
        try:
            bank_entry = banks.by_sysex(part.bank)
            name = self.catalog.display_name(
                bank_entry.id, part.performance, banks.Kind.PERFORMANCE
            )
        except Exception:
            name = ""
        return perf, name

    def _multi_detail_text(self, number: int, name: str) -> str:
        """The detail line under the panes for one Multiset."""
        found = self._multi_mismatches.get(number, [])
        detail = f"[b]Multi {number:02d} {name}[/b]"
        if found:
            detail += f"\n[#f0a0a0]{found[0].text}[/#f0a0a0]"
            if len(found) > 1:
                detail += f"  [dim](+{len(found) - 1} more)[/dim]"
        return detail + "\n[dim]m reads the setup and e edits it[/dim]"

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
            marks = {
                mismatch.channel for mismatch in self._multi_mismatches.get(number, [])
            }
            recorded = self._multi_catalog.get(number)
            bindings = recorded.channels if recorded is not None else {}
            for part in ms.parts:
                if part.used:
                    perf, name = self._multi_part_display(
                        bindings.get(part.channel - 1), part
                    )
                    used = "used !" if (part.channel - 1) in marks else "used"
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
            if isinstance(table, WarningTable):
                table.set_warning_rows(
                    {f"multi:{number:02d}:{mark + 1:02d}" for mark in marks}
                )
        else:
            if isinstance(table, WarningTable):
                table.set_warning_rows(())
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
                self._multi_detail_text(number, ms_name)
            )
        self._refresh_deps_pane()

    def _fill_all_favourites(self, table: DataTable[Any]) -> None:
        """Rows for the every-favourite view, independent of the card row."""
        slots = self._all_favourites()
        self._current = slots
        if not slots:
            table.add_row(
                "",
                "00",
                "[dim]nothing favourited in any bank yet[/dim]",
                key="physical:all:000",
            )
        for fav in slots:
            fav_scope = fav.bank_id
            shown = self._slot_shown_name(fav, None)
            where = self._scope_label(fav_scope)
            marked = self._favorite_keys(fav_scope, fav.kind)
            table.add_row(
                "[b]♥[/b]" if fav.number in marked else "",
                f"{where} {fav.kind[:4]} {fav.number:02d}",
                shown,
                key=fav.key,
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
            if self.view_mode == VIEW_ALL_FAVOURITES:
                # The every-favourite view spans every bank, including the
                # internal ones, so it does not depend on a card being in
                # the archive or in the slot at all.
                self._fill_all_favourites(table)
            elif card_id in ("", None) or card_id not in self._physical:
                table.add_row(
                    "",
                    "00",
                    "[dim]insert a card and press C[/dim]",
                    key="physical:none:000",
                )
                self._current = []
            else:
                card = self._physical[card_id]
                scoped = _physical.scope_id(card_id)
                card_number = banks.bank("CARD").number
                slots: Sequence[SlotLike]
                # The bank favourites view and the rating filter apply here
                # as they do to a bank row; the keys are the card's scoped
                # ones, never the shared CARD key.
                plain: List[banks.Slot] = banks.slots(card_number, kind)
                if self.view_mode == VIEW_BANK_FAVOURITES:
                    marked = self._favorite_keys(scoped, kind)
                    plain = [slot for slot in plain if slot.number in marked]
                slots = self._by_rating(plain)
                self._current = slots
                for item in slots:
                    item_scope = getattr(item, "bank_id", None)
                    if item_scope is not None:
                        shown = self._slot_shown_name(item, None)
                        item_scoped = item_scope
                        where = self._scope_label(item_scope)
                    else:
                        names = (
                            card.performances
                            if item.kind == banks.Kind.PERFORMANCE
                            else card.patches
                        )
                        name = names.get(item.number, "")
                        shown = f"[b]{name}[/b]" if name else "[dim](unnamed)[/dim]"
                        item_scoped = scoped
                        where = ""
                    marked = self._favorite_keys(item_scoped, item.kind)
                    label = (
                        f"{where} {item.kind[:4]} {item.number:02d}"
                        if self.view_mode == VIEW_ALL_FAVOURITES
                        else f"{item.number:02d}"
                    )
                    table.add_row(
                        "[b]♥[/b]" if item.number in marked else "",
                        label,
                        shown,
                        key=(
                            item.key
                            if item_scope is not None
                            else f"physical:{card_id}:{item.kind}:{item.number:03d}"
                        ),
                    )
        finally:
            self._filling = False
        if self.view_mode == VIEW_ALL_FAVOURITES:
            self._detail(0)
            self._refresh_deps_pane()
            return
        # Detail
        if card_id in ("", None) or card_id not in self._physical:
            with contextlib.suppress(NoMatches):
                self.query_one("#detail", Static).update(
                    "[dim]insert a card and press C[/dim]"
                )
            self._refresh_deps_pane()
            return
        card = self._physical[card_id]
        active_marker = " • active" if self._active_physical == card_id else ""
        verified = "verified" if card.verified else "unverified"
        digest_prefix = card.digest[:12]
        cache_age = self._cache_age_text(_physical.scope_id(card_id))
        with contextlib.suppress(NoMatches):
            self.query_one("#detail", Static).update(
                f"[b]{card.label or card.id}[/b]{active_marker}\n"
                f"[dim]{verified} · {digest_prefix} · "
                f"C captures the inserted card; L labels this one[/dim]{cache_age}"
            )
        self._refresh_deps_pane()

    def _detail_scoped(self, item: SlotLike, scope: str) -> None:
        """The detail line for a row from the all-favourites view."""
        stored = getattr(item, "name", "")
        if stored == "(unnamed)":
            stored = ""
        name = stored or self._scope_slot_name(scope, item.kind, item.number)
        shown = f"[b]{name}[/b]" if name else "[dim](unnamed)[/dim]"
        cache_age = self._cache_age_text(scope)
        if scope.startswith("CARD/"):
            key = scope[5:]
            if key.isdigit():
                active = int(key) == self._active_card
            else:
                active = key == self._active_physical
            if active:
                extra = "[dim]active card — Enter auditions it[/dim]"
            else:
                extra = (
                    "[dim]favourite in another card — select that card on the "
                    "unit to audition it[/dim]"
                )
            title = f"{self._scope_label(scope)} {item.kind} {item.number:02d}"
        else:
            entry = banks.bank(scope)
            note = (
                "   [dim]writable[/dim]"
                if entry.writable
                else "   [dim]ROM — read only[/dim]"
            )
            extra = (
                f"[dim]Bank Select LSB[/dim] [b]{entry.midi_bank}[/b]   "
                f"[dim]Program Change[/dim] "
                f"[b]{entry.program_base + item.number}[/b]"
            )
            title = f"{entry.label} {item.kind} {item.number:02d}{note}"
        with contextlib.suppress(NoMatches):
            self.query_one("#detail", Static).update(
                f"[b]{title}[/b]  {shown}\n{extra}{cache_age}"
            )

    def _detail(self, row: int) -> None:  # noqa: C901
        if self.view_mode == VIEW_ALL_FAVOURITES and not self._current:
            with contextlib.suppress(NoMatches):
                self.query_one("#detail", Static).update(
                    "[dim]nothing favourited in any bank yet[/dim]"
                )
            return
        if row is not None and 0 <= row < len(self._current):
            item = self._current[row]
            scope = getattr(item, "bank_id", None)
            if scope is not None:
                self._detail_scoped(item, scope)
                return
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
                cache_age = self._cache_age_text(
                    _physical.scope_id(self._current_physical)
                )
                with contextlib.suppress(NoMatches):
                    self.query_one("#detail", Static).update(
                        f"[b]{card.label or card.id}[/b]\n"
                        f"[dim]C captures the inserted card; L labels this one[/dim]"
                        f"{cache_age}"
                    )
                return
            item = self._current[row]
            names = (
                card.performances
                if item.kind == banks.Kind.PERFORMANCE
                else card.patches
            )
            name = names.get(item.number, "")
            shown = f"[b]{name}[/b]" if name else "[dim](unnamed)[/dim]"
            cache_age = self._cache_age_text(_physical.scope_id(self._current_physical))
            with contextlib.suppress(NoMatches):
                self.query_one("#detail", Static).update(
                    f"[b]{card.label or card.id} {item.kind} {item.number:02d}[/b]  "
                    f"{shown}\n"
                    f"[dim]C captures the inserted card; L labels this one[/dim]"
                    f"{cache_age}"
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
        cache_age = self._cache_age_text(item.bank.id)
        with contextlib.suppress(NoMatches):
            self.query_one("#detail", Static).update(
                f"[b]{item.bank.label} {item.kind} {item.number:02d}[/b]  "
                f"{name}{note}\n"
                f"[dim]Bank Select LSB[/dim] [b]{item.bank.midi_bank}[/b]   "
                f"[dim]Program Change[/dim] "
                f"[b]{item.bank.program_base + item.number}[/b]"
                f"{cache_age}"
            )

    def _cache_age_text(self, scope_id: str) -> str:
        """Return a cache age suffix for the detail pane, or empty string."""
        if self._bankstore_dir is None:
            return ""
        try:
            age = bankstore.get_cache_age(scope_id, directory=self._bankstore_dir)
        except Exception:
            return ""
        if age is None:
            return ""
        if age < 60:
            return f"   [dim]cached {age:.0f}s ago[/dim]"
        if age < 3600:
            return f"   [dim]cached {age / 60:.0f}m ago[/dim]"
        if age < 86400:
            return f"   [dim]cached {age / 3600:.1f}h ago[/dim]"
        return f"   [dim]cached {age / 86400:.1f}d ago[/dim]"

    def _detail_card(self, item: SlotLike) -> None:
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
        cache_age = self._cache_age_text(_cards.scope_id(number))
        with contextlib.suppress(NoMatches):
            self.query_one("#detail", Static).update(
                f"[b]Card {number:02d} {item.kind} {item.number:02d}[/b]  "
                f"{shown}\n"
                f"{_cards.bank_title(captured, entry)}   [dim]{status}[/dim]{cache_age}"
            )

    def notify_status(
        self, message: str, *, refused: bool = False, warn: bool = False
    ) -> None:
        prefix = "[b]refused[/b] · " if refused else ""
        widget = self.query_one("#status", Static)
        widget.update(prefix + message)
        # A red-tinted line for anything that is a refusal or a warning, so
        # the eye finds it without reading; plain status clears the tint.
        if refused or warn:
            widget.add_class("warning")
        else:
            widget.remove_class("warning")

    # --- transfer progress --------------------------------------------------

    def _progress_report(self, label: str, fraction: Optional[float]) -> None:
        """Take one transfer event from the device, from any thread.

        The bridge calls this while a dump is on the wire. ``fraction`` is
        None when the transfer is over, however it ended.
        """
        if threading.current_thread() is threading.main_thread():
            self._progress_apply(label, fraction)
            return
        try:
            self.call_from_thread(self._progress_apply, label, fraction)
        except Exception:  # noqa: S110 - the app may be shutting down
            pass

    def _progress_apply(self, label: str, fraction: Optional[float]) -> None:
        if fraction is None:
            # A step inside a held operation is not the operation's end.
            if self._progress_hold == 0:
                self._progress_finish()
            return
        if not self._progress_active:
            self._progress_active = True
            self._progress_generation += 1
        if label and label != self._progress_label:
            self._progress_label = label
            self._progress_value = 0.0
        self._progress_value = max(
            self._progress_value, min(1.0, max(0.0, float(fraction)))
        )
        self._progress_paint()

    def _progress_finish(self) -> None:
        if not self._progress_active:
            return
        self._progress_value = 1.0
        self._progress_paint()
        generation = self._progress_generation
        try:
            self.set_timer(0.4, lambda: self._progress_hide(generation))
        except Exception:  # noqa: S110 - the app may be shutting down
            pass

    def _progress_hold_begin(self, label: str) -> None:
        """Show the bar for a whole operation, not only its messages.

        A staged audition of one patch is three 426-byte exchanges; each
        finishes inside a frame, so byte progress alone would never be
        visible. The worker holds the bar for its whole run and the
        bridge's own events refine the label and the fill.
        """
        self._progress_hold += 1
        if self._progress_hold == 1:
            self._progress_active = True
            self._progress_generation += 1
            self._progress_label = label
            self._progress_value = 0.0
            self._progress_paint()

    def _progress_hold_end(self) -> None:
        self._progress_hold = max(0, self._progress_hold - 1)
        if self._progress_hold == 0:
            self._progress_finish()

    def _progress_hide(self, generation: int) -> None:
        if generation != self._progress_generation:
            return
        self._progress_active = False
        self._progress_label = ""
        self._progress_value = 0.0
        self._progress_paint()

    def _progress_paint(self) -> None:
        text = self._progress_text()
        try:
            screen = self.screen
        except Exception:
            # A worker can outlive the screen stack during teardown.
            return
        setter = getattr(screen, "set_progress", None)
        try:
            own = self.query_one("#progress", Static)
        except Exception:
            own = None
        if callable(setter):
            # The screen covers the app's bar. Clear the app's own one, or
            # its last text stays there and reappears when the screen closes.
            if own is not None:
                own.update("")
                own.display = False
            setter(text)
            return
        if own is None:
            return
        own.update(text)
        own.display = bool(text)

    def _progress_text(self) -> str:
        if not self._progress_active:
            return ""
        width = 22
        filled = round(width * self._progress_value)
        bar = "█" * filled + "░" * (width - filled)
        percent = f"{self._progress_value * 100:3.0f}%"
        label = self._progress_label or "working"
        return f"{bar} {percent}  {label}"

    # --- navigation ---------------------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if self._filling:
            return
        if event.data_table.id == "bank-table":
            # A clear-and-refill queues a highlight for whatever row the
            # table selected first. Once the cursor has moved on, that
            # message is stale and must not overwrite the current pane.
            if event.cursor_row != event.data_table.cursor_row:
                return
            row = event.cursor_row
            if 0 <= row < len(self._row_multis):
                physical = (
                    self._row_physical[row] if row < len(self._row_physical) else None
                )
                multi = self._row_multis[row]
                if multi is not None and multi in self._multi_mismatches:
                    event.data_table.add_class("stale-cursor")
                else:
                    event.data_table.remove_class("stale-cursor")
                if physical is not None:
                    self._fill_physical(physical, self._pairs[row][1])
                elif multi is not None:
                    self._fill_multiset(multi)
                else:
                    self._fill(self._pairs[row], self._row_cards[row])
        elif event.data_table.id == "slot-table":
            self._detail(event.cursor_row)
            self._refresh_deps_pane()

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
        select_card = self._current_card
        scope = getattr(entry, "bank_id", None)
        if scope is not None:
            # An all-favourites row carries its own bank; the cursor's
            # bank row is not evidence about which card is in the slot.
            select_card = None
            if scope.startswith("CARD/"):
                key = scope[5:]
                if key.isdigit():
                    card_number = int(key)
                    if self._active_card is None:
                        self.notify_status(
                            "the app has not confirmed which HCard bank is "
                            f"active; select Card {card_number:02d} on the "
                            "HCard and press C once, then select again",
                            refused=True,
                        )
                        return
                    if card_number != self._active_card:
                        self.notify_status(
                            f"the HCard was on Card {self._active_card:02d} "
                            f"at the last capture; select Card {card_number:02d} "
                            "on the HCard first, then press C to re-check",
                            refused=True,
                        )
                        return
                    select_card = card_number
                elif key != self._active_physical:
                    self.notify_status(
                        "that card is not the one in the slot; insert it and "
                        "press C to activate",
                        refused=True,
                    )
                    return
        else:
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
                        f"the HCard first, then press C to re-check",
                        refused=True,
                    )
                    return
        if self._busy:
            self.notify_status("busy", refused=True)
            return
        if not self._exchange.acquire(blocking=False):
            self.notify_status("busy", refused=True)
            return
        self._select_worker(entry, card=select_card)

    @work(thread=True)
    def _select_worker(self, entry: SlotLike, card: Optional[int] = None) -> None:
        """**MIDI only.**"""
        failure: BaseException | None = None
        try:
            with self._busy_gate, self._lock:
                self.device.select(entry.slot if isinstance(entry, ViewSlot) else entry)
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
            f"{entry.bank.program_base + entry.number} sent for {entry} "
            "— switched the unit to performance (POLY) mode",
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

    def _auto_multi_read(self) -> None:
        """Read the Multisets once at launch, off the UI thread, quietly."""
        if not self._auto_read_multi_enabled:
            return
        if (
            self._busy
            or self._scanning
            or isinstance(self.screen, _scan.ScanOfferScreen)
        ):
            self.set_timer(1.0, self._auto_multi_read)
            return
        self.action_multi()

    def _auto_capture_card(self) -> None:
        """Startup probe: file or confirm the active card bank.

        The card is switched physically and the synth cannot report the
        bank, so a read at launch is the only way to know which one is in
        front of it. Silent and read-only; skipped while the scan offer is
        up or the browser is busy, and the user can always press C.
        """
        if self._card_mode != "hcard":
            # The launch probe confirms the active HCard bank. In cards mode
            # identifying a card is an explicit C: the content may need a
            # label or a picker, which must not appear at launch.
            return
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
                # Relabelled here rather than through kws.physical.set_label:
                # with no injected path that would read and rewrite the
                # user's real archive while leaving this one untouched.
                updated = _physical.PhysicalCard(
                    id=card.id,
                    label=(value or "").strip(),
                    digest=card.digest,
                    captured_at=card.captured_at,
                    performances=dict(card.performances),
                    patches=dict(card.patches),
                    verified=card.verified,
                )
                self._remember_physical(updated)
                self._build_row_model()
                self._apply_row_model()
                self._show_physical_row(card_id)
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
            if self._cards_path is None:
                # Memory only: kws.cards.set_label would fall back to the
                # user's real archive and file the label there instead.
                from dataclasses import replace as _replace

                updated = _replace(captured, label=(value or "").strip())
            else:
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
                perf_dump = self.device.read_bank("CARD", banks.Kind.PERFORMANCE)
                patch_dump = self.device.read_bank("CARD", banks.Kind.PATCH)
                performances = perf_dump.names()
                patches = patch_dump.names()
                digest = _cards.content_digest(perf_dump.records, patch_dump.records)
                waveseq_raw = None
                reader = getattr(self.device, "read_raw", None)
                if callable(reader):
                    try:
                        waveseq_raw = reader(
                            banks.bank("CARD").sysex, m.Function.ALL_WAVESEQ_DUMP
                        )
                    except _BRIDGE_ERRORS:
                        waveseq_raw = None
                decoded_waveseq = (
                    self._waveseq_bytes(waveseq_raw)
                    if waveseq_raw is not None
                    else None
                )
                bundle = {
                    "performances": b"".join(perf_dump.records),
                    "patches": b"".join(patch_dump.records),
                }
                if decoded_waveseq is not None:
                    bundle["waveseq"] = decoded_waveseq
            payload = (None, performances, patches, digest, bundle)
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
        name, performances, patches, digest, bundle = payload
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
            if startup and evidence_bank is None:
                # A launch probe has no user to ask. Filing unknown content
                # under the last remembered bank is how #39's content once
                # overwrote #24 and made #24 look active: leave the archive
                # alone and say the bank could not be identified.
                number = None
            elif evidence_bank is not None and evidence_bank != expected:
                # different bank
                number = evidence_bank
                verified = True
                message_suffix = (
                    f" — the card is Card {number:02d}, not "
                    f"{expected:02d} — filed under {number:02d}. "
                    f"Switch the HCard to {expected:02d} if that is the "
                    "bank you meant."
                )
            elif evidence_bank == expected:
                number = expected
                verified = True
                message_suffix = " — confirmed"
            else:
                # no evidence, explicit capture: file on the user's say-so
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
                # The card was read but its content is not in the archive,
                # so this session cannot confirm which bank is in front of
                # the unit. The persisted hint (the last confirmed bank) is
                # not evidence: stop marking it active rather than showing
                # #24 while #39 is the bank actually selected.
                previous = self._active_card
                if previous is not None:
                    self._active_card = None
                    self._refresh_card_row(previous)
                self._refresh_multi_checks()
                self.notify_status(
                    "the active Card bank is not in the archive yet; select "
                    "its row and press C to file it (the last remembered "
                    "bank is no longer marked active)"
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
        message += " (the card name is not readable over MIDI; press L to label it)"
        message += message_suffix
        self._remember_bank(_cards.scope_id(number), **bundle)
        self._refresh_multi_checks()
        stale = sum(len(items) for items in self._multi_mismatches.values())
        if stale:
            message += (
                f" — {stale} Multiset channel(s) no longer match their "
                "records; the affected Multi rows are red"
            )
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
                waveseq_raw = None
                reader = getattr(self.device, "read_raw", None)
                if callable(reader):
                    try:
                        waveseq_raw = reader(
                            banks.bank("CARD").sysex, m.Function.ALL_WAVESEQ_DUMP
                        )
                    except _BRIDGE_ERRORS:
                        waveseq_raw = None
                decoded_waveseq = (
                    self._waveseq_bytes(waveseq_raw)
                    if waveseq_raw is not None
                    else None
                )
                bundle = {
                    "performances": b"".join(perf_dump.records),
                    "patches": b"".join(patch_dump.records),
                }
                if decoded_waveseq is not None:
                    bundle["waveseq"] = decoded_waveseq
            payload = (perf_names, patch_names, digest, bundle)
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

    def _remember_physical(
        self, card: _physical.PhysicalCard, active: Optional[str] = None
    ) -> None:
        """Keep a captured card in memory; write it out only with a path.

        ``physical_path`` is None in tests and ``--demo``, and the kws
        helpers would then fall back to the user's real archive on disk.
        So the in-memory copy -- which is what the rows are built from --
        is updated either way, and the file is touched only when one was
        injected.
        """
        self._physical[card.id] = card
        if active is not None:
            self._active_physical = active
        if self._physical_path is None:
            return
        _physical.save(card, self._physical_path)
        if active is not None:
            _physical.set_active(active, self._physical_path)
        self._physical = _physical.load(self._physical_path)

    def _file_physical_capture(self, payload: Any, startup: bool = False) -> None:
        perf_names, patch_names, digest, bundle = payload
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
            # save and mark active: with no path this stays in memory only
            self._remember_physical(updated, updated.id)
            self._remember_bank(_physical.scope_id(updated.id), **bundle)
            self._build_row_model()
            self._apply_row_model()
            self._show_physical_row(updated.id)
            self._fill_physical(updated.id, banks.Kind.PERFORMANCE)
            self._refresh_multi_checks()
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
                self._remember_physical(card, card.id)
                self._remember_bank(_physical.scope_id(card.id), **bundle)
                self._build_row_model()
                self._apply_row_model()
                self._show_physical_row(card.id)
                self._fill_physical(card.id, banks.Kind.PERFORMANCE)
                self._refresh_multi_checks()
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
                    self._remember_physical(card, card.id)
                    self._remember_bank(_physical.scope_id(card.id), **bundle)
                    self._build_row_model()
                    self._apply_row_model()
                    self._show_physical_row(card.id)
                    self._fill_physical(card.id, banks.Kind.PERFORMANCE)
                    self._refresh_multi_checks()
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
            self._remember_physical(updated, updated.id)
            self._remember_bank(_physical.scope_id(updated.id), **bundle)
            self._build_row_model()
            self._apply_row_model()
            self._show_physical_row(updated.id)
            self._fill_physical(updated.id, banks.Kind.PERFORMANCE)
            self._refresh_multi_checks()
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

    def _refresh_bank_hearts(self) -> None:
        """Update the ♥ count column in #bank-table after a favourite changes."""
        table = self.query_one("#bank-table", DataTable)
        for pair, card, multi, physical in zip(
            self._pairs, self._row_cards, self._row_multis, self._row_physical
        ):
            try:
                cells = self._bank_row(pair, card, multi, physical)
                table.update_cell(
                    self._row_key(pair, card, multi, physical), "fav", cells[5]
                )
            except Exception:  # noqa: S110 - the row may be filtered away
                pass

    @work(thread=True)
    def _read_worker(self, bank_id: int, kind: str) -> None:
        """**MIDI only** -- the catalog is updated on the main thread."""
        failure: BaseException | None = None
        result_kind = kind
        result_names: Any = None
        bundle: Optional[Dict[str, bytes]] = None
        try:
            self.call_from_thread(
                self.notify_status,
                f"asking for bank {bank_id} {banks.plural(kind)} "
                "in one message (silent)...",
            )
            with self._busy_gate, self._lock:
                dump = self.device.read_bank(bank_id, kind)
                result_kind = dump.kind
                result_names = dump.names()
                blob = b"".join(dump.records)
                if kind == banks.Kind.PERFORMANCE:
                    bundle = {"performances": blob}
                else:
                    bundle = {"patches": blob}
        except _BRIDGE_ERRORS as exc:
            failure = exc
        finally:
            self._exchange.release()
        if failure is not None:
            self.call_from_thread(self.notify_status, f"read: {failure}", refused=True)
            return
        self.call_from_thread(self._adopt, bank_id, result_kind, result_names, bundle)

    def _adopt(
        self,
        bank_id: int,
        kind: str,
        names: Dict[int, str],
        bundle: Optional[Dict[str, bytes]] = None,
    ) -> None:
        entry = banks.bank(bank_id)
        for number, name in names.items():
            self.catalog.set_live_name(entry.id, number, name, kind)
        if bundle is not None:
            self._remember_bank(entry.id, **bundle)
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
                        dump = self.device.read_bank(number, kind)
                        result_kind = dump.kind
                        names = dump.names()
                        blob = b"".join(dump.records)
                        if kind == banks.Kind.PERFORMANCE:
                            bundle = {"performances": blob}
                        else:
                            bundle = {"patches": blob}
                except Exception as exc:
                    self.call_from_thread(
                        self.notify_status, f"scan {number}: {exc}", refused=True
                    )
                    continue
                self.call_from_thread(self._adopt, number, result_kind, names, bundle)
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
        # Do not put the empty-catalog offer on top of an import/dialog.
        if len(self.screen_stack) > 1:
            return
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

    def _search_hits(self, text: str) -> List[Tuple[str, str, int, str]]:
        """Every name matching ``text``: catalog, HCard and physical cards.

        The catalog carries the printed list and the live names of the
        resident banks. A captured card bank's names live in the archive
        instead, so searching only the catalog made a performance on a
        captured card unfindable by name. Archive rows are labelled with
        the card, and a slot the catalog already answered is not repeated.
        """
        rows: List[Tuple[str, str, int, str]] = list(self.catalog.search(text))
        # A slot the catalog already answered for the active card must not
        # be repeated under the archive's label; every other match is its
        # own bank's row, however similar the names are.
        seen = set(rows)
        for number in sorted(self._archive):
            captured = self._archive[number]
            for row in _cards.search_captured(
                captured, text, label=f"Card {number:02d}"
            ):
                if number == self._active_card and any(
                    bank_id == "CARD" and (kind, num, name) == (row[1], row[2], row[3])
                    for bank_id, kind, num, name in rows
                ):
                    continue
                if row in seen:
                    continue
                seen.add(row)
                rows.append(row)
        for card_id in sorted(self._physical):
            card = self._physical[card_id]
            label = getattr(card, "label", "") or card_id
            for row in _cards.search_captured(card, text, label=label):
                if row in seen:
                    continue
                seen.add(row)
                rows.append(row)
        return rows

    def action_search(self) -> None:
        def done(text: Optional[str]) -> None:
            if not text:
                return
            hits = self._search_hits(text)
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
            f"selected Multiset {number:02d} on the unit — switched it to MULTI mode",
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
                from rich.text import Text

                row_key = f"multi:{number:02d}"
                name = getattr(self._multisets[number], "name", "") or ""
                found = self._multi_mismatches.get(number, [])
                if found:
                    kinds = sorted({mismatch.kind for mismatch in found})
                    name = (name or "(unnamed)") + " ! " + "/".join(kinds)
                    try:
                        cells = list(table.get_row(row_key))
                    except Exception:
                        cells = []
                    for column, cell in zip(table.ordered_columns, cells):
                        table.update_cell(
                            row_key,
                            column.key,
                            Text.from_markup(
                                name if column.key == "name" else str(cell),
                                style=self.MULTI_WARNING_STYLE,
                            ),
                        )
                    continue
                table.update_cell(row_key, "name", name or "[dim]--[/dim]")
            except Exception:  # noqa: S110
                pass
        if isinstance(table, WarningTable):
            table.set_warning_rows(
                {f"multi:{number:02d}" for number in self._multi_mismatches}
            )
        self._update_stale_cursor()

    def _after_multi_write(self) -> None:
        """The editor stored a new setup: show what it wrote."""
        self._build_row_model()
        self._apply_row_model()
        self._refresh_multi_rows()
        if self._current_multi is not None:
            self._fill_multiset(self._current_multi)
        self._refresh_deps_pane()

    def _card_digest_for_scope(self, scope: Optional[str]) -> str:
        """The archive digest of a card scope, or "" when it is unknown."""
        if not scope or not scope.startswith("CARD/"):
            return ""
        key = scope[5:]
        if key.isdigit():
            captured = self._archive.get(int(key))
            return getattr(captured, "digest", "") or ""
        card = self._physical.get(key.lower())
        return getattr(card, "digest", "") or ""

    def _check_multi_snapshots(self, sets: Sequence[Any]) -> Dict[int, List[Any]]:
        """Compare the live multi catalog with the setup just read."""
        if not self._multi_catalog:
            return {}
        active_scope = self._active_card_scope()
        active_digest = self._card_digest_for_scope(active_scope)
        merged = self._sources_with_store()

        def name_at(bank_byte: int, number: int) -> Optional[str]:
            try:
                entry = banks.by_sysex(bank_byte)
            except LookupError:
                return None
            if entry.id == "CARD":
                return None
            if entry.id not in merged:
                return None  # not read: no evidence either way
            return self._scope_slot_name(entry.id, banks.Kind.PERFORMANCE, number) or ""

        out: Dict[int, List[Any]] = {}
        for number, snapshot in self._multi_catalog.items():
            if not (0 <= number < len(sets)):
                continue
            found = _multisets_mod.check(
                snapshot,
                active_card_scope=active_scope if active_scope != "CARD" else None,
                active_card_digest=active_digest,
                name_at=name_at,
                multiset=sets[number],
            )
            scopes = sorted(
                {
                    binding.card_scope
                    for binding in snapshot.channels.values()
                    if binding.card_scope
                }
            )
            if len(scopes) > 1:
                first = next(
                    binding.channel
                    for binding in snapshot.channels.values()
                    if binding.card_scope
                )
                found.insert(
                    0,
                    _multisets_mod.Mismatch(
                        multi=number,
                        channel=first,
                        kind="mixed",
                        text=(
                            "uses performances from "
                            + ", ".join(self._scope_label(scope) for scope in scopes)
                            + "; the unit can only use the active card — "
                            "press e, then s to stage them into RAM"
                        ),
                    ),
                )
            if found:
                out[number] = found
        return out

    def _refresh_multi_checks(self) -> None:
        """Re-run the live-catalog check after the card in the slot changes.

        The Multiset records name the card they were written against, so a
        different bank in the slot makes them stale even though the RAM
        setup itself has not moved. No MIDI: only the check and the views.
        """
        if not self._multisets:
            return
        fresh = self._check_multi_snapshots(self._multisets)
        self._multi_mismatches.clear()
        self._multi_mismatches.update(fresh)
        if not getattr(self, "_screen_stack", None):
            return
        self._build_row_model()
        self._apply_row_model()
        self._refresh_multi_rows()
        if self._current_multi is not None:
            self._fill_multiset(self._current_multi)
        self._refresh_deps_pane()

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
        self._multi_mismatches = self._check_multi_snapshots(sets)
        # Rebuild the Multi rows so the warning tint follows the check.
        self._build_row_model()
        self._apply_row_model()
        self._refresh_multi_rows()
        if self._multi_mismatches:
            total = sum(len(items) for items in self._multi_mismatches.values())
            self.notify_status(
                f"loaded {len(sets)} Multisets — {total} channel(s) no longer "
                "match their recording; e inspects the named Multiset",
                warn=True,
            )
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
        self._update_stale_cursor()
        self._fill_multiset(number)
        self._refresh_deps_pane()
        self.notify_status(
            f"read {len(sets)} Multisets — Enter selects one on the unit "
            "(MULTI mode), e edits"
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
                backup_dir=getattr(self, "_backup_dir", None),
                catalog=getattr(self, "catalog", None),
                sources=getattr(self, "_sources", None),
                archive=getattr(self, "_archive", None),
                physical=getattr(self, "_physical", None),
                on_edit_fx=self._edit_multiset_fx,
                favorites=getattr(self, "favorites", None),
                card_scope=self._picker_card_scope(),
                snapshots=self._multi_catalog,
                mismatches=self._multi_mismatches,
            )
        )

    def _record_multi_snapshot(
        self,
        snapshot: Any,
        multisets: Optional[Sequence[Any]] = None,
    ) -> None:
        """Put one snapshot in the live multi catalog and save it.

        With ``multisets`` (the setups just written) the check is re-run
        against those exact setups, so a mixed-card Multiset is still
        flagged immediately. Without it (the editor's plain ``r``), the
        record is the assertion and its own entry is cleared.
        """
        self._multi_catalog[snapshot.number] = snapshot
        if multisets is not None:
            fresh = self._check_multi_snapshots(multisets)
            self._multi_mismatches.clear()
            self._multi_mismatches.update(fresh)
        else:
            self._multi_mismatches.pop(snapshot.number, None)
        if self._multisets_path is not None:
            _multisets_mod.save(self._multi_catalog, self._multisets_path)
        if getattr(self, "_screen_stack", None) and self._multisets:
            self._build_row_model()
            self._apply_row_model()
            self._refresh_multi_rows()

    def _picker_card_scope(self) -> str:
        """The card scope whose favourites the performance picker should use.

        The active card when it is known; otherwise the card bank being
        viewed (a weaker hint, but better than the shared bare slot); else
        ``CARD``. The bank number cannot be read, so this is always a hint.
        """
        scope = self._active_card_scope()
        if scope != "CARD":
            return scope
        if self._current_card is not None:
            try:
                return _cards.scope_id(self._current_card)
            except Exception:  # noqa: S110 - fall back to the bare slot
                pass
        return scope

    def _edit_multiset_fx(self, multiset: Any) -> None:
        """Open the selected Multiset's own FX block."""
        if self._busy:
            self.notify_status("busy", refused=True)
            return

        def on_write(block: FxBlock, screen: Any) -> None:
            self._multiset_fx_write(multiset, block, screen)

        self.push_screen(
            FxScreen(
                multiset.fx,
                None,
                on_write,
                gate_armed=self._write_gate.armed,
                subject=f"Multiset {multiset.number:02d}",
                control_channel=multiset.control_channel,
            )
        )

    def _multiset_fx_write(self, multiset: Any, block: FxBlock, screen: Any) -> None:
        """Write one Multiset's FX through the whole-setup fence."""
        if not self._write_gate.armed:
            screen.write_failed("the write gate is locked (w on the browser)")
            return
        updated = [
            replace(entry, fx=block) if entry.number == multiset.number else entry
            for entry in getattr(self, "_multisets", [])
        ]
        if not updated:
            screen.write_failed("no Multisets are loaded; read them first (m)")
            return
        if self._busy:
            screen.write_failed("busy")
            return
        if not self._exchange.acquire(blocking=False):
            screen.write_failed("busy")
            return
        self._multi_edit_write_worker(updated, self.device, self._backup_dir, screen)

    def action_edit_fx(self) -> None:  # noqa: C901
        entry = self._cursor_entry()
        if entry is None:
            self.notify_status("nothing under the cursor", refused=True)
            return
        if entry.kind == banks.Kind.PATCH:
            self.notify_status(
                "select a performance; FX live in the performance, not its patches",
                refused=True,
            )
            return
        scope = self._scoped_bank_id(entry)
        try:
            bank = self._bank_for_scope(scope)
        except Exception as exc:
            self.notify_status(f"fx: {exc}", refused=True)
            return
        if not getattr(bank, "writable", True):
            self.notify_status(
                f"{bank.label} is ROM and cannot be edited", refused=True
            )
            return
        try:
            self._guard_card_slot(scope)
        except ValueError as exc:
            self.notify_status(str(exc), refused=True)
            return
        if getattr(self, "_busy", False):
            self.notify_status("busy", refused=True)
            return
        number = entry.number

        def work() -> Any:
            record = self.device.read_performance(bank.number, number)
            try:
                setup = self.device.read_system_setup()
            except Exception:
                setup = None
            return record, setup

        def done(result: Any, failure: Any) -> None:
            if failure is not None:
                self.notify_status(f"fx: {failure}", refused=True)
                return
            record, setup = result
            try:
                block = FxBlock.from_bytes(record[16:37])
            except Exception as exc:
                self.notify_status(f"fx: {exc}", refused=True)
                return
            mode: Any = None
            if setup is not None:
                try:
                    if len(setup) > 4:
                        mode = setup[4]
                except Exception:
                    mode = None

            def on_write(new_block: FxBlock, screen: Any) -> None:
                self._fx_write(bank, number, record, new_block, screen)

            self.push_screen(
                FxScreen(
                    block,
                    mode,
                    on_write,
                    gate_armed=self._write_gate.armed,
                )
            )

        self._device_worker(work, done, "reading FX")

    def _fx_write(
        self,
        bank: Any,
        number: int,
        record: bytes,
        block: FxBlock,
        screen: Any,
    ) -> None:
        """Write one performance's FX block through the ordinary fence."""
        if not self._write_gate.armed:
            screen.write_failed("the write gate is locked (w on the browser)")
            return
        new_record = bytearray(record)
        new_record[16:37] = block.to_bytes()

        def work() -> Any:
            from kws import backup as _backup

            where = self._backup_dir or _backup.default_dir()
            return self.device.write_performance(
                bank.number, number, bytes(new_record), backup_dir=where
            )

        def done(result: Any, failure: Any) -> None:
            if failure is not None:
                screen.write_failed(str(failure))
                self.notify_status(f"fx: {failure}", refused=True)
                return
            if not isinstance(result, dict) or result.get("verified") is not True:
                message = (
                    result.get("mismatch") if isinstance(result, dict) else str(result)
                )
                screen.write_failed(str(message))
                self.notify_status(f"fx: {message}", refused=True)
                return
            try:
                self._record_write(result)
            except Exception:  # noqa: S110
                pass
            self.notify_status(
                f"FX written to {bank.label} perf {number:02d}, verified"
            )
            screen.write_done(block)

        self._device_worker(work, done, "writing FX")

    @work(thread=True)
    def _multi_edit_write_worker(
        self,
        multisets: Sequence[Any],
        device: Any,
        backup_dir: Optional[str],
        screen: Any,
        staged: Optional[Any] = None,
        snapshot: Any = None,
    ) -> None:
        """**MIDI only** -- state, undo and the screen update on the main thread."""
        if staged is None:
            self._multi_write_plain(
                multisets, device, backup_dir, screen, snapshot=snapshot
            )
        else:
            self._multi_write_staged(multisets, device, backup_dir, screen, staged)

    def _multi_where(self, backup_dir: Optional[str]) -> str:
        from kws import backup as _backup

        return backup_dir if backup_dir is not None else _backup.default_dir()

    def _multi_release(self) -> None:
        try:
            self._exchange.release()
        except RuntimeError:
            pass

    def _multi_report_failure(self, screen: Any, failure: Any) -> None:
        text = str(failure)
        self.call_from_thread(self.notify_status, f"multi write: {text}", refused=True)
        try:
            self.call_from_thread(screen._write_failed, text)
        except Exception:  # noqa: S110
            pass

    def _multi_write_plain(
        self,
        multisets: Sequence[Any],
        device: Any,
        backup_dir: Optional[str],
        screen: Any,
        snapshot: Any = None,
    ) -> None:
        from kws import multi as multi_mod

        result: Any = None
        try:
            encoded = multi_mod.encode(multisets)
            with self._busy_gate, self._lock:
                annotations = (
                    _multisets_mod.to_dict(snapshot) if snapshot is not None else None
                )
                result = device.write_multi(
                    encoded,
                    backup_dir=self._multi_where(backup_dir),
                    verify=True,
                    annotations=annotations,
                )
        except BaseException as exc:
            self._multi_release()
            self._multi_report_failure(screen, exc)
            return
        self._multi_release()
        if not isinstance(result, dict) or result.get("verified") is not True:
            mismatch = (
                result.get("mismatch")
                if isinstance(result, dict) and result.get("mismatch") is not None
                else "the read-back did not confirm the write"
            )
            self._multi_report_failure(screen, mismatch)
            return
        self.call_from_thread(
            self._multi_plain_finish, multisets, result, screen, snapshot
        )

    def _multi_plain_finish(
        self,
        multisets: Sequence[Any],
        result: Any,
        screen: Any,
        snapshot: Any = None,
    ) -> None:
        """Main thread: remember the written setup and its one undo step."""
        try:
            if hasattr(self, "_record_write"):
                self._record_write(result)
            self._multi_sets = list(multisets)
            self._multisets = list(multisets)
            if snapshot is not None:
                self._multi_catalog[snapshot.number] = snapshot
                self._multi_mismatches.pop(snapshot.number, None)
                if self._multisets_path is not None:
                    _multisets_mod.save(self._multi_catalog, self._multisets_path)
                if self._multisets:
                    self._multi_mismatches = self._check_multi_snapshots(
                        self._multisets
                    )
        except Exception:  # noqa: S110
            pass
        try:
            screen._write_succeeded(result)
        except Exception:  # noqa: S110
            pass

    def _multi_write_staged(
        self,
        multisets: Sequence[Any],
        device: Any,
        backup_dir: Optional[str],
        screen: Any,
        staged: Any,
    ) -> None:
        composite: Any = None
        try:
            with self._busy_gate, self._lock:
                composite = self._multi_stage_write(
                    multisets, device, self._multi_where(backup_dir), staged
                )
        except BaseException as exc:
            self._multi_release()
            self._multi_report_failure(screen, exc)
            return
        self._multi_release()
        self.call_from_thread(
            self._multi_staged_finish, multisets, staged, composite, screen
        )

    def _multi_stage_write(
        self,
        multisets: Sequence[Any],
        device: Any,
        where: str,
        staged: Any,
    ) -> Dict[str, Any]:
        """Write every staged bank, then the remapped Multi Mode Setup."""
        from kws import banks as _banks
        from kws import multi as multi_mod

        backups: List[Any] = []
        for bank_id, built in staged.built_banks.items():
            dest = _banks.bank(bank_id)
            result = device.write_bank_bundle(
                dest.number,
                performances=built.performances,
                patches=built.patches,
                waveseq=built.waveseq,
                backup_dir=where,
            )
            if not isinstance(result, dict) or result.get("verified") is not True:
                mismatch = (
                    result.get("mismatch")
                    if isinstance(result, dict) and result.get("mismatch") is not None
                    else "the read-back did not confirm the write"
                )
                raise RuntimeError(f"{dest.label}: {mismatch}")
            backups.append(result.get("backup"))
        multi_result = device.write_multi(
            multi_mod.encode(multisets), backup_dir=where, verify=True
        )
        if (
            not isinstance(multi_result, dict)
            or multi_result.get("verified") is not True
        ):
            mismatch = (
                multi_result.get("mismatch")
                if isinstance(multi_result, dict)
                and multi_result.get("mismatch") is not None
                else "the read-back did not confirm the write"
            )
            raise RuntimeError(f"multi: {mismatch}")
        return {
            "backups": [path for path in backups if path],
            "multi": multi_result.get("backup"),
            "verified": True,
            "bank": sorted(staged.built_banks.keys()),
            "kind": "composite",
        }

    def _multi_staged_finish(
        self,
        multisets: Sequence[Any],
        staged: Any,
        composite: Any,
        screen: Any,
    ) -> None:
        """Main thread: merge the staged banks, record ONE undo step, dismiss."""
        for bank_id, built in staged.built_banks.items():
            self._sources[bank_id] = self._source_from_built(bank_id, built)
            self._refresh_written_bank(bank_id, built)
        self._multi_sets = list(multisets)
        self._multisets = list(multisets)
        if hasattr(self, "_record_write"):
            self._record_write(composite)
        if hasattr(self, "_after_multi_write"):
            self._after_multi_write()
        try:
            screen._write_succeeded(composite)
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
Two tables and a detail pane. Left: the banks, each listed once per kind --
once for its 50 performances, once for its 35 patches (`p` hides the patch
rows). Right: that bank-and-kind's slots. The third pane on the right
repeats the `d` report for the current selection; `D` hides it.

Shortcuts: I import/copy (file, RAM, ROM or Card) · y copy performance ·
x FX · B copy bank · d dependencies · D dependency pane · z undo · w write gate ·
? this help.

  RAM 1-3   writable; somebody's own work lives here.
  ROM 4-11  read-only.
  Card      the card slot -- what it shows depends on `card_mode`.

A performance combines up to eight parts, each pointing at a patch. A
patch is a sound: four oscillators playing waves or wave sequences. They
are different objects; performance 7 is not patch 7.

**Card modes.**  `card_mode` in config.toml (default `cards`):

  `none`    no Card rows; the card actions are off.
  `cards`   one or several ordinary Program Cards. `C` reads the inserted
            card, identifies it by its content, and files it in the
            physical archive; new content opens a picker (an existing
            card, or "New card"). `L` labels the selected card. Each card
            keeps its own names and favourites. Enter auditions only the
            card that is in the slot; other archive rows are read-only
            until that card is inserted.
  `hcard`   the HCard's forty banks, switched physically on the card.
            Every bank is listed; the one marked `•` is the last active
            bank. `C` captures the bank on the card, `X` sweeps 00-39,
            `L` labels. `show_card_defaults = true` shows the manual's
            pre-set names for banks not captured yet.

  C   capture what is in the slot: the HCard bank (`hcard`) or the
      inserted card (`cards`). SILENT; files its names.
  X   card sweep (`hcard` only): walk 00-39, asking you to select each
      bank on the card between reads.
  L   label the captured bank or card. This is kwsed's own label, not the
      card's name: the card name is unreadable over MIDI (TODO 23).
  ⏎   on a Card row, auditions only the bank/card that is in the slot;
      kwsed refuses a row that is not the active one.

A card's own name is not readable over MIDI. A first capture is filed
unverified; later captures are confirmed by a content digest, and an
HCard capture that is not the row you selected says so and asks you to
switch the HCard.

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
      ⏎ on a Multiset row makes it current on the unit and switches the unit
      to MULTI mode; ⏎ on a Performance switches it back to performance
      (POLY) mode. e edits the setup as a 16-channel table; in that editor
      ⏎ on a channel's performance opens the performance picker (favourites
      with F, rating filter with V), and x opens the Multiset's own FX
      block. Both writes are one fenced Multi Mode Setup write.
  f   favourite this slot; F steps all -> this bank's -> every favourite
  t   tags; n   note (both hang off a favourite, so press f first)
  V   filter the list by favourite rating
  [ ] step the send channel; c set it by number
  p   show or hide the patch rows in the bank list -- they cannot be
      selected or copied on their own, so a performer-only view hides them.
      The choice is remembered between runs.
  d   dependency inspector for a Performance, Patch or Multiset
  D   show or hide the dependency pane that follows the selection
      (remembered between runs)
  y   copy a Performance (with its patches) into a writable bank; opens the
      unified copy screen with that performance prefilled
  B   copy the whole bank under the cursor into another writable bank --
      wave sequences, patches and performances together, backed up first.
      The copy window's m chooses between kwsed's read/remap/write and the
      unit's own MOVE, which is still backed up and verified here.
  I   import a file, disk image, ZIP, RAM bank, ROM bank or the active Card:
      whole bank, or single/multiple performances into one writable target
      bank, with a target-slot map and one batch write.
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
    # Heal a settings file that an older build wrote UI state into: an
    # unquoted dotted key is a TOML table, and it would block the channel
    # save below as well as every later preference.
    cfg.migrate_ui_state(config_path)

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
        single_object_timeout = cfg.load_single_object_timeout(config_path)
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
                    single_object_timeout=single_object_timeout,
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
                    single_object_timeout=single_object_timeout,
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
    multisets_path = (
        None if getattr(args, "demo", False) else _multisets_mod.default_path()
    )
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
        bankstore_dir=None if args.demo else bankstore.default_dir(),
        multisets_path=multisets_path,
        auto_read_multi=True,
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
