# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# kwsed is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 2 of the License, or (at your option)
# any later version.
#
# kwsed is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.

"""Preview one source bank for a whole-bank import."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional, Sequence, Tuple

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Static

from kws import banks as kws_banks
from kws import containment, dumps, records
from kws.perfcopy import (
    ImportMode,
    SplitGroup,
    SplitPartition,
    pack_self_contained_into,
    partition_self_contained_split,
)
from kwsed.gate import EditGate
from kwsed.target_bank import ImportMenuScreen, TargetBankScreen
from vinsynlib.ui.report import ReportScreen

if TYPE_CHECKING:
    from kws.importers import ImportedBank

__all__ = [
    "DanglingRefsScreen",
    "ImportPassState",
    "PreflightImportScreen",
    "SpilloverCompanion",
    "WholeBankChoice",
    "WholeBankImportScreen",
]

TABLE_ID = "whole-bank-table"
PATH_ID = "whole-bank-path"
SUMMARY_ID = "whole-bank-summary"
TARGET_ID = "whole-bank-target"
MODE_ID = "whole-bank-mode"
GATE_ID = "whole-bank-gate"
STATUS_ID = "whole-bank-status"
SOURCE_ID = "whole-bank-source"

#: The order ``m`` walks: today's byte-identical write first, then the two
#: self-contained slot policies (plan R2 a1 and a2).
MODE_ORDER: Tuple[ImportMode, ...] = (
    ImportMode.AS_IS,
    ImportMode.SELF_CONTAINED_1TO1,
    ImportMode.SELF_CONTAINED_PACKED,
)

MODE_LABELS: Dict[ImportMode, str] = {
    ImportMode.AS_IS: "as-is",
    ImportMode.SELF_CONTAINED_1TO1: "self-contained 1:1",
    ImportMode.SELF_CONTAINED_PACKED: "self-contained packed",
}

#: The three kinds of overflow ``o`` offers, before any bank is picked.
#: ``PATCHES`` keeps every performance on the target and sends only the
#: patches it cannot hold elsewhere; ``SELF_CONTAINED`` moves the
#: performances that point outside the target into the overflow bank,
#: with every patch they name, so both banks are self-contained.
#: ``CARDS_ONLY`` names no RAM bank at all: one self-contained group goes
#: onto the card in the slot per pass, however many cards that takes.
OVERFLOW_PATCHES = "patches"
OVERFLOW_SELF_CONTAINED = "self_contained"
OVERFLOW_CARDS_ONLY = "cards_only"

#: The menu ``o`` opens first; the RAM bank chooser follows only for the
#: first two kinds.
OVERFLOW_KIND_ORDER: Tuple[str, ...] = (
    OVERFLOW_PATCHES,
    OVERFLOW_SELF_CONTAINED,
    OVERFLOW_CARDS_ONLY,
)

OVERFLOW_KIND_CHOICES: Tuple[str, ...] = (
    "Only the patches it cannot hold (performances stay on the target)",
    "The performances that point outside it, with all their patches "
    "(both banks self-contained)",
    "Cards only: one self-contained group per card, no RAM bank",
)


@dataclass(frozen=True)
class WholeBankChoice:
    source_index: int
    target_bank: str
    write_to_unit: bool
    #: How the write treats performances that reference another bank. The
    #: default keeps every existing caller on the byte-identical as-is path
    #: (:func:`kws.perfcopy.relocate_bank`).
    mode: ImportMode = ImportMode.AS_IS
    #: Which import session this write belongs to: the source file and the
    #: source bank within it. The app keeps the per-pass written-slot state
    #: under this key, so a later ``n`` pass knows what is already placed.
    session_key: str = ""
    #: True when the write came from this screen's own gate keys, so the
    #: app re-offers the screen with the pass state once the write lands.
    #: Companion writes (``x``) leave it off: they are their own flow.
    continue_pass: bool = False
    #: Where the patches the target cannot hold go (option ``o``), or "" for
    #: a plain write. The app writes this bank first and repoints the
    #: target's parts at it.
    overflow_bank: str = ""
    #: Which kind of overflow to plan (option ``o``): ``"patches"`` keeps
    #: every performance on the target and sends the patches it cannot hold
    #: to ``overflow_bank``; ``"self_contained"`` moves the performances
    #: that point outside the target into it, patches included, so both
    #: banks are self-contained. The default keeps every existing caller on
    #: the patches-only split.
    overflow_kind: str = OVERFLOW_PATCHES
    #: True for a companion write (``x``, or the post-write popup's own
    #: write): the chosen bank's patches and wave sequences travel, and
    #: its performances stay as they are -- unless the unit's copy of the
    #: bank is empty of performances (all Init), where a whole-bank write
    #: carries nothing away. The app decides which of the two the bank's
    #: current contents allow.
    companion_only: bool = False
    #: The preflight answer this write already carries (``"both"`` or
    #: ``"only"``), or "" while the question has not been asked. A marked
    #: write never raises the modal again, and the post-write companion
    #: popup stays a fallback for writes nobody asked about.
    preflight: str = ""


@dataclass(frozen=True)
class ImportPassState:
    """What the earlier passes of one import session already placed.

    ``written`` maps each source performance slot to the target label the
    pass wrote it into. It is the session's memory between passes: the
    remainder a ``n`` pass offers is the contained slots *not* in here,
    and the status line echoes the whole map so nothing is dropped
    silently.

    A self-contained split that needed more banks than one pass offered
    continues under the same session: ``split_remaining`` holds the source
    slots still to place and ``split_target`` the bank the split writes
    into (the card slot -- the user swaps the card between passes).
    ``split_kind`` says which flow the remainder belongs to: ``"split"``
    for the two-bank partition, ``"cards_only"`` for one card per group
    with no RAM bank. ``source_index`` keeps the session's own source
    bank selected when the screen is re-offered.
    """

    session_key: str = ""
    written: Mapping[int, str] = field(default_factory=dict)
    #: Source slots the split still has to place, in source order.
    split_remaining: Tuple[int, ...] = ()
    #: The bank id the split writes into, or "" outside a split session.
    split_target: str = ""
    #: Which pass flow this is: ``"split"`` or ``"cards_only"``.
    split_kind: str = "split"
    #: The source bank index the session started from.
    source_index: int = 0

    @property
    def split_session(self) -> bool:
        """Whether this screen continues a self-contained split.

        True while the session carries a split target, even once nothing
        is left: the screen then says the split is complete instead of
        speaking about the contained-remainder flow.
        """
        return bool(self.split_remaining) or bool(self.split_target)


@dataclass(frozen=True)
class SpilloverCompanion:
    """One companion bank of the file, and the RAM bank it is aimed at.

    Plan R3 (option b): a file that holds more than one bank can also carry
    the banks the main source's performances reference. ``index`` is the
    companion's position in the file -- the same index a
    :class:`WholeBankChoice` carries. ``target`` is the suggested RAM bank,
    the bank the main source's parts name (plan D2), matched by bank
    number; the chooser lets the person replace it. ``named_parts`` is how
    many of the main source's parts point at that bank, so a companion the
    performances do not reference says so instead of implying otherwise.
    """

    index: int
    label: str
    bank_number: int
    target: str
    named_parts: int = 0


def _escape(text: str) -> str:
    return text.replace("[", "\\[")


def _active_count(items: Sequence[Any]) -> int:
    total = 0
    for item in items:
        if item and any(item):
            total += 1
    return total


def _is_empty_performance(record: Sequence[int]) -> bool:
    parts = records.performance_parts(record)
    if not any(p.used for p in parts):
        return True
    name = dumps.record_name(record).strip().lower()
    if name not in (
        "",
        "init",
        "init perf",
        "init perf.",
        "init performance",
        "unnamed",
    ):
        return False
    for part in parts:
        if part.used and (part.bank != 0 or part.patch != 0):
            return False
    return True


def _ram_banks() -> list[kws_banks.Bank]:
    """The writable RAM banks a companion can be spilled into.

    The Card is writable too, but plan R3 names a RAM bank for each
    companion; card targets belong to the HCard work, not here.
    """
    return [b for b in kws_banks.writable_banks() if b.id.startswith("RAM")]


def _entry_bank_number(entry: Any) -> Optional[int]:
    """The real bank number behind one loaded file bank, or ``None``.

    A file bank carries its scope (``RAM2``, ``CARD/import0``); a session
    alias carries the number on its ``Source`` instead. Anything else --
    a malformed fixture, an unknown scope -- has no number and is not
    offered as a companion.
    """
    source = getattr(entry, "source", None)
    number = getattr(source, "bank_number", None)
    if number is not None:
        try:
            return int(number)
        except (TypeError, ValueError):
            return None
    scope = getattr(entry, "scope", None)
    if not isinstance(scope, str):
        return None
    if scope == "CARD" or scope.startswith("CARD/"):
        return kws_banks.bank("CARD").number
    try:
        return kws_banks.bank(scope).number
    except LookupError:
        return None


def _entry_bank(entry: Any) -> Optional[kws_banks.Bank]:
    number = _entry_bank_number(entry)
    if number is None:
        return None
    try:
        return kws_banks.bank(number)
    except LookupError:
        return None


class DanglingRefsScreen(ModalScreen[Optional[str]]):
    """A popup: this write left parts pointing at a bank the file carries.

    An as-is write of one bank of a multi-bank file keeps every part
    pointing where the file points. When the file also carries the bank
    those parts name, the written performances play whatever that bank
    holds -- usually Init -- until the companion is written too. Saying so
    in the status line put it in a wall of text, so it is a modal with one
    sentence and the two keys that finish the job.
    """

    DEFAULT_CSS = """
    DanglingRefsScreen { align: center middle; }
    DanglingRefsScreen Vertical {
        width: 78; height: auto; border: thick $warning;
        padding: 1 2; background: $panel;
    }
    DanglingRefsScreen #dangling-title { text-style: bold; height: auto; }
    DanglingRefsScreen #dangling-body { height: auto; padding: 1 0; }
    DanglingRefsScreen #dangling-hint { height: auto; color: $text-muted; }
    """

    BINDINGS = [
        Binding("x", "write", "Write it"),
        Binding("o", "repoint", "Repoint"),
        Binding("escape", "cancel", "Leave"),
        Binding("q", "cancel", "Leave"),
    ]

    def __init__(self, body: str, *, write_label: str) -> None:
        super().__init__()
        self._body = body
        self._write_label = write_label

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Parts left pointing outside", id="dangling-title")
            yield Static(self._body, id="dangling-body", markup=False)
            yield Static(
                f"x  write {self._write_label}'s patches (its performances "
                "stay as they are)\n"
                "o  self-contained pass, repointing those parts\n"
                "esc  leave it as it is",
                id="dangling-hint",
                markup=False,
            )

    def action_write(self) -> None:
        self.dismiss("write")

    def action_repoint(self) -> None:
        self.dismiss("repoint")

    def action_cancel(self) -> None:
        self.dismiss(None)


class PreflightImportScreen(ModalScreen[Optional[str]]):
    """Asked before a write: the file carries the bank its parts name.

    An as-is write of one bank of a multi-bank file keeps every part
    pointing where the file points, and a self-contained pass leaves the
    performances that name another bank for later. When the file also
    carries that bank, the write can either fill it with the companion's
    patches -- leaving its performances alone -- or leave it as it is.
    Discovering this after the write came too late and offered no choice,
    so the question is asked while nothing has been sent.
    """

    DEFAULT_CSS = """
    PreflightImportScreen { align: center middle; }
    PreflightImportScreen Vertical {
        width: 84; height: auto; border: thick $accent;
        padding: 1 2; background: $panel;
    }
    PreflightImportScreen #preflight-title { text-style: bold; height: auto; }
    PreflightImportScreen #preflight-body { height: auto; padding: 1 0; }
    PreflightImportScreen #preflight-choices { height: auto; padding: 1 0; }
    PreflightImportScreen #preflight-hint { height: auto; color: $text-muted; }
    """

    BINDINGS = [
        Binding("1", "both", "Write both"),
        Binding("2", "only", "Only this bank"),
        Binding("3", "cancel", "Cancel"),
        Binding("escape", "cancel", "Cancel"),
        Binding("q", "cancel", "Cancel"),
    ]

    def __init__(self, body: str, *, choices: Sequence[str]) -> None:
        super().__init__()
        self._body = body
        self._choices = list(choices)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("This file carries a second bank", id="preflight-title")
            yield Static(self._body, id="preflight-body", markup=False)
            yield Static(
                "\n".join(
                    f"{index + 1}  {choice}"
                    for index, choice in enumerate(self._choices)
                ),
                id="preflight-choices",
                markup=False,
            )
            yield Static(
                "1 / 2 / 3 choose · esc cancel", id="preflight-hint", markup=False
            )

    def action_both(self) -> None:
        self.dismiss("both")

    def action_only(self) -> None:
        self.dismiss("only")

    def action_cancel(self) -> None:
        self.dismiss(None)


class WholeBankImportScreen(ModalScreen[Optional[WholeBankChoice]]):
    DEFAULT_CSS = """
    WholeBankImportScreen { align: center middle; }
    WholeBankImportScreen > Vertical {
        width: 92; height: 80%; max-height: 90%;
        border: thick $accent; background: $surface; padding: 1 2;
    }
    WholeBankImportScreen .title { height: auto; text-style: bold; }
    WholeBankImportScreen .path { height: auto; color: $text-muted; }
    WholeBankImportScreen .source { height: auto; }
    WholeBankImportScreen .summary { height: auto; }
    WholeBankImportScreen DataTable { height: 1fr; }
    WholeBankImportScreen .target { height: auto; }
    WholeBankImportScreen .mode { height: auto; }
    WholeBankImportScreen .gate { height: auto; }
    WholeBankImportScreen .status { height: auto; color: $text; padding: 0 1; }
    WholeBankImportScreen .hint { height: auto; color: $text-muted; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("q", "cancel", "Cancel"),
        Binding("s", "source", "Source"),
        Binding("b", "target", "Target"),
        Binding("m", "mode", "Mode"),
        Binding("n", "next_pass", "Next pass"),
        Binding("o", "overflow", "Overflow"),
        Binding("x", "spillover", "Companions"),
        Binding("w", "toggle_gate", "Gate"),
        Binding("ctrl+s", "write", "Write", priority=True),
        Binding("c", "cache", "Cache"),
        Binding("question_mark", "help", "Help"),
    ]

    def __init__(
        self,
        *,
        path: str,
        banks: Sequence["ImportedBank"],
        gate: Optional[EditGate] = None,
        card_mode: Optional[str] = None,
        hcard_banks: Optional[Sequence[Any]] = None,
        mode: Optional[ImportMode] = None,
        pass_state: Optional[ImportPassState] = None,
        notice: str = "",
    ) -> None:
        super().__init__()
        self._path = path
        self._banks: list["ImportedBank"] = list(banks)
        self._source_index = 0
        self._writable: list[Any] = list(kws_banks.writable_banks())
        self._target_index = 0
        self._gate = gate
        self._mode = ImportMode.AS_IS if mode is None else mode
        #: The session's written-slot state for this source (plan R4 c).
        self._pass_state = pass_state
        if pass_state is not None and pass_state.source_index:
            self._source_index = pass_state.source_index
        #: Unique per screen visit; see _session_key.
        self._session_token: Optional[str] = None
        #: One focused line that wins over the pass line on mount: the app
        #: re-opened the screen after a write that left work to finish here
        #: (a write whose parts name a bank the same file also carries).
        self._notice = notice
        self._audit: Optional[containment.ContainmentAudit] = None
        #: The bank the excess patches go to (option b), or "" for none.
        self._overflow_index: Optional[int] = None
        # The target list is the unit's four writable banks, and nothing
        # else. The card slot is one physical place: listing every archived
        # card bank here invited a write to "Card 07" that could only ever
        # land on whatever card is in the slot. Multi-pass imports continue
        # onto another card by switching the card on the unit, not by
        # naming a different archive.
        del card_mode, hcard_banks

    @property
    def banks(self) -> list["ImportedBank"]:
        return self._banks

    def _source_bank(self) -> "ImportedBank":
        if not self._banks:
            raise IndexError("no source banks")
        return self._banks[self._source_index]

    def _target_bank(self) -> Any:
        if not self._writable:
            raise IndexError("no writable banks")
        return self._writable[self._target_index]

    def _active_perf_slots(self) -> list[int]:
        try:
            source = self._source_bank().source
        except (IndexError, AttributeError):
            return []
        performances = getattr(source, "performances", ()) or ()
        out: list[int] = []
        for index, record in enumerate(performances):
            try:
                if not _is_empty_performance(record):
                    out.append(index)
            except (ValueError, TypeError):
                if record and any(record):
                    out.append(index)
        return out

    def _sources(self) -> Dict[str, Any]:
        """The loaded banks as the audit's source map, keyed by scope.

        Every bank in the file is offered, not just the selected one, so a
        part that points at a companion bank is judged through the alias the
        importer gave that bank.
        """
        out: Dict[str, Any] = {}
        for entry in self._banks:
            scope = getattr(entry, "scope", None)
            source = getattr(entry, "source", None)
            if isinstance(scope, str) and source is not None:
                out[scope] = source
        return out

    def _refresh_audit(self) -> None:
        """Re-audit the selected source bank for containment (plan D1).

        A file can hold a partial bank, and a test fixture can hold anything;
        an audit that cannot run leaves the block out rather than failing the
        screen. ``None`` means "not known", which the mode line says in
        words.
        """
        self._audit = None
        try:
            bank = self._source_bank()
            self._audit = containment.audit_containment(
                self._sources(), str(bank.scope)
            )
        except (IndexError, LookupError, ValueError, TypeError, AttributeError):
            self._audit = None

    def _containment_text(self) -> str:
        """``contained: N · other bank: M (banks …) · ROM: K``, or "".

        The three counts are all performance counts and add up to the bank's
        non-empty performances: contained includes the ROM-assisted ones
        (plan D1), and ROM is reported separately so its number is never
        hidden behind that total.
        """
        audit = self._audit
        if audit is None:
            return ""
        banks_text = ""
        if audit.parts_per_bank:
            labels = ", ".join(
                kws_banks.bank(number).label for number in sorted(audit.parts_per_bank)
            )
            banks_text = f" ({labels})"
        return (
            f"contained: {audit.contained} · other bank: {audit.other_bank}"
            f"{banks_text} · ROM: {audit.rom_assisted}"
        )

    def _written_slots(self) -> set[int]:
        """The source slots earlier passes already wrote, or an empty set."""
        if self._pass_state is None:
            return set()
        return {int(slot) for slot in self._pass_state.written}

    def _split_session(self) -> bool:
        """Whether this screen continues a self-contained split (plan R4 d)."""
        return self._pass_state is not None and self._pass_state.split_session

    def _cards_only_session(self) -> bool:
        """Whether the split this screen continues writes cards and no RAM bank."""
        return (
            self._pass_state is not None
            and self._pass_state.split_session
            and self._pass_state.split_kind == OVERFLOW_CARDS_ONLY
        )

    def _split_remaining(self) -> list[int]:
        """The source slots the split still has to place, in source order."""
        if self._pass_state is None:
            return []
        return [int(slot) for slot in self._pass_state.split_remaining]

    def _split_target_bank(self) -> Any:
        """The bank the split writes into: the card now in the slot.

        The session carries the bank the split started with -- the card
        slot. The user swaps the card between passes, so the target stays
        the slot, not whatever this screen's selector happens to show.
        """
        target_id = (
            str(self._pass_state.split_target) if self._pass_state is not None else ""
        )
        if target_id:
            try:
                return kws_banks.bank(target_id)
            except LookupError:
                pass
        return self._target_bank()

    def _split_next_group(
        self, remaining: Sequence[int], target: Any
    ) -> Optional[SplitGroup]:
        """The one group the next ``n`` pass would write, or ``None``.

        The same packing the app performs before writing, so the statement
        before the gate says what this pass places and nothing else.
        """
        try:
            bank = self._source_bank()
            scope = str(bank.scope)
            sources = dict(self._sources())
            sources[scope] = bank.source
            groups = pack_self_contained_into(
                sources, scope, remaining, target, mode=self._mode
            )
        except (IndexError, LookupError, ValueError, TypeError, AttributeError):
            return None
        return groups[0] if groups else None

    def _split_group_words(self, group: Optional[SplitGroup]) -> str:
        """``one group of 5 performances and 5 patches``, or a fallback."""
        if group is None:
            return "one group of the remaining performances"
        patches = 0 if group.plan is None else group.plan.patch_count
        return (
            f"one group of {len(group.performances)} performances and {patches} patches"
        )

    def _split_mode_line(self, label: str) -> str:
        """The mode line while a split remainder waits for the next card."""
        remaining = self._split_remaining()
        cards_only = self._cards_only_session()
        if not remaining:
            flow = "cards-only pass" if cards_only else "self-contained split"
            return f"Mode: {label} · the {flow} is complete: nothing is left to write"
        try:
            where = self._split_target_bank().label
        except IndexError:
            where = "the current target"
        group = self._split_next_group(remaining, self._split_target_bank())
        if cards_only:
            this_pass = 0 if group is None else len(group.performances)
            later = max(len(remaining) - this_pass, 0)
            return (
                f"Mode: {label} · cards-only: next pass writes "
                f"{self._split_group_words(group)} into {where}, and "
                f"{later} performances remain for the card after — switch "
                "the card on the unit (select it on the HCard and press C), "
                "then press n"
            )
        return (
            f"Mode: {label} · split continues: next pass writes "
            f"{self._split_group_words(group)} packed into the current "
            f"target ({where}) — switch the card on the unit (select it on "
            "the HCard and press C), then press n"
        )

    def _session_key(self) -> str:
        """The import session key: source file, source bank, screen visit.

        The last component is unique to this screen instance (inherited
        from the pass state on a re-offered screen), so a fresh import of
        the same file starts over instead of continuing a dead session's
        remainder. The app keys its per-pass written-slot state by this.
        """
        if self._pass_state is not None and self._pass_state.session_key:
            return self._pass_state.session_key
        if self._session_token is None:
            self._session_token = uuid.uuid4().hex
        try:
            scope = str(self._source_bank().scope)
        except (IndexError, AttributeError):
            scope = ""
        return f"{self._path}|{scope}|{self._session_token}"

    def _remaining_slots(self) -> list[int]:
        """The contained slots no earlier pass has written (plan R4 c)."""
        audit = self._audit
        if audit is None:
            return []
        return containment.remaining_performances(audit, self._written_slots())

    def _remainder_audit(self) -> Optional[containment.ContainmentAudit]:
        """The audit restricted to the not-yet-written contained slots."""
        audit = self._audit
        if audit is None:
            return None
        remaining = set(self._remaining_slots())
        return replace(
            audit,
            performances=[
                entry for entry in audit.performances if entry.slot in remaining
            ],
        )

    def _pass_counts(self) -> Optional[Tuple[int, int, int, int]]:
        """(this pass, whole bank, skipped, later passes) for the mode line.

        The pass size is the partition's first chunk over the remainder, so
        the statement before the gate says what *this* write will place.
        The skip count stays relative to the whole source bank: the
        non-contained performances are skipped in every pass, not only
        this one.
        """
        audit = self._audit
        if audit is None:
            return None
        total = audit.contained + audit.other_bank
        skipped = audit.other_bank
        remainder_audit = self._remainder_audit()
        if remainder_audit is None or not remainder_audit.performances:
            return (0, total, skipped, 0)
        try:
            passes = containment.partition_self_contained(
                self._sources(), remainder_audit
            )
        except (IndexError, LookupError, ValueError, TypeError, AttributeError):
            return (len(remainder_audit.performances), total, skipped, 0)
        first = passes[0] if passes else None
        if first is None:
            return (len(remainder_audit.performances), total, skipped, 0)
        later = len(remainder_audit.performances) - len(first.slots)
        return (len(first.slots), total, skipped, later)

    def _overflow_parts(self) -> int:
        """How many used parts name a writable bank other than the source."""
        audit = self._audit
        if audit is None:
            return 0
        return sum(audit.parts_per_bank.values())

    def _overflow_hint(self) -> str:
        """Suffix naming the overflow option, when there is something to send."""
        parts = self._overflow_parts()
        if not parts:
            return ""
        banks = self._overflow_banks()
        where = banks[0].label if banks else "a RAM bank"
        return (
            f" · {parts} parts name another bank: press o to send those "
            f"patches to {where} and repoint them, to move those "
            f"performances with all their patches there, or to write all of "
            f"them one self-contained group per card, with no RAM bank at all"
        )

    def _overflow_banks(self) -> list[Any]:
        """The writable RAM banks this target can overflow into, best first.

        A part naming RAM 2 is best served by RAM 2 (plan D2), so the banks
        the source's own parts name come first; the rest follow. The target
        bank itself is never offered.
        """
        target = self._target_bank()
        audit = self._audit
        named = [] if audit is None else list(audit.parts_per_bank)
        candidates = [
            entry
            for entry in kws_banks.writable_banks()
            if entry.number != target.number and entry.id.startswith("RAM")
        ]
        candidates.sort(
            key=lambda entry: (
                named.index(entry.number) if entry.number in named else len(named),
                entry.number,
            )
        )
        return candidates

    def action_overflow(self) -> None:
        """Offer the three kinds of overflow; the two RAM kinds ask for a bank."""
        if not self._overflow_parts():
            self._set_status(
                "this bank's parts name no other bank, so there is nothing to overflow"
            )
            return
        self.app.push_screen(
            ImportMenuScreen("Overflow kind", list(OVERFLOW_KIND_CHOICES)),
            self._overflow_kind_chosen,
        )

    def _overflow_kind_chosen(self, index: Optional[int]) -> None:
        """After the kind, ask for the RAM bank only the RAM kinds need."""
        if index is None:
            return
        try:
            kind = OVERFLOW_KIND_ORDER[index]
        except (IndexError, TypeError):
            return
        if kind == OVERFLOW_CARDS_ONLY:
            self._cards_only_chosen()
            return
        banks = self._overflow_banks()
        if not banks:
            self._set_status("no RAM bank is free to overflow into")
            return
        self.app.push_screen(
            TargetBankScreen("Overflow bank", [b.label for b in banks]),
            lambda picked: self._overflow_bank_chosen(banks, kind, picked),
        )

    def _cards_only_chosen(self) -> None:
        """Dismiss into a cards-only split: no RAM bank, one card per group.

        The kind names no overflow bank at all, so there is no bank step:
        the app packs every non-empty performance, writes the first group
        into the card in the slot, and carries the rest to the next card.
        """
        if self._gate is not None and not self._gate.armed:
            self._set_status("write gate is locked")
            return
        self.dismiss(
            WholeBankChoice(
                self._source_index,
                str(self._target_bank().id),
                True,
                self._mode,
                self._session_key(),
                True,
                "",
                OVERFLOW_CARDS_ONLY,
            )
        )

    def _overflow_bank_chosen(
        self, banks: Sequence[Any], kind: str, picked: Optional[int]
    ) -> None:
        """After the bank, dismiss with the kind that was picked."""
        if picked is None:
            return
        try:
            overflow = banks[picked]
        except (IndexError, TypeError):
            return
        self._dismiss_overflow(overflow, kind)

    def _split_preview(self, overflow: Any) -> Optional[SplitPartition]:
        """The self-contained split over the target plus this overflow bank.

        The same call the app makes before writing, so a refusal here is
        the refusal the write would give -- stated while the screen is
        still open, not after a dismissal that could drop performances.
        """
        try:
            bank = self._source_bank()
            scope = str(bank.scope)
            sources = dict(self._sources())
            sources[scope] = bank.source
            return partition_self_contained_split(
                sources,
                scope,
                [self._target_bank(), overflow],
                mode=self._mode,
            )
        except (IndexError, LookupError, ValueError, TypeError, AttributeError):
            return None

    def _split_refusal(self, overflow: Any, partition: SplitPartition) -> str:
        """Why the split needs more banks than the one offered, in words.

        The engine's own rejections name the bank and the count (for
        example ``contained performances need 2 banks``), so they are
        repeated verbatim instead of being re-derived here.
        """
        reasons = "; ".join(partition.rejected)
        return (
            f"self-contained split does not fit {self._target_bank().label} "
            f"plus {overflow.label}: {reasons} — nothing is written; split "
            f"it with x, one bank per write, or carry the remainder with n"
        )

    def _split_plannable(self, overflow: Any) -> bool:
        """Whether the offered banks hold a first pass of this split.

        A split needing more banks than the two offered still travels one
        pass per card -- but only when every group, written or left for
        the next card, plans cleanly. A patch that is not loaded, or one
        performance alone bigger than a bank, is a refusal instead: the
        fence must not put a broken half of the split onto the unit.
        """
        audit = self._audit
        if audit is None:
            return False
        try:
            bank = self._source_bank()
            scope = str(bank.scope)
            sources = dict(self._sources())
            sources[scope] = bank.source
            contained = [e.slot for e in audit.performances if e.contained]
            non_contained = [e.slot for e in audit.performances if not e.contained]
            packed: list[SplitGroup] = []
            if contained:
                packed.extend(
                    pack_self_contained_into(
                        sources, scope, contained, self._target_bank(), mode=self._mode
                    )
                )
            if non_contained:
                packed.extend(
                    pack_self_contained_into(
                        sources, scope, non_contained, overflow, mode=self._mode
                    )
                )
        except (IndexError, LookupError, ValueError, TypeError, AttributeError):
            return False
        return all(group.plan is None or not group.plan.rejected for group in packed)

    def _dismiss_overflow(self, overflow: Any, kind: str) -> None:
        """Dismiss with the overflow bank and the kind that was picked."""
        if self._gate is not None and not self._gate.armed:
            self._set_status("write gate is locked")
            return
        if kind == OVERFLOW_SELF_CONTAINED:
            partition = self._split_preview(overflow)
            if partition is None:
                self._set_status(
                    "containment is not known for this bank, so the "
                    "self-contained split cannot be planned"
                )
                return
            if not partition.fits and not self._split_plannable(overflow):
                # Never dismiss on a split that would drop performances.
                self._set_status(self._split_refusal(overflow, partition))
                return
        self.dismiss(
            WholeBankChoice(
                self._source_index,
                str(self._target_bank().id),
                True,
                self._mode,
                self._session_key(),
                True,
                str(overflow.id),
                kind,
            )
        )

    def _mode_line(self) -> str:
        """What the selected mode writes and what it leaves out.

        Shown above the gate and repeated in the status line when ``m`` is
        pressed, so the choice is stated before the gate can be armed (R1).
        With pass state on the screen the line names the continuation
        pass: the remaining performances, with the skip count still
        relative to the whole source bank. While a split remainder waits,
        the line names the next card instead.
        """
        label = MODE_LABELS[self._mode]
        if self._split_session():
            return self._split_mode_line(label)
        if self._mode is ImportMode.AS_IS:
            return (
                f"Mode: {label} · writes the whole bank (every performance, "
                f"patch and wave sequence), skipping nothing; self-references "
                f"move to the target, references to other writable banks and "
                f"to ROM stay where they point{self._overflow_hint()}"
            )
        counts = self._pass_counts()
        if counts is None:
            return (
                f"Mode: {label} · containment is not known for this bank, so "
                f"the number of performances left out cannot be stated"
            )
        this_pass, total, skipped, later = counts
        continuation = bool(self._written_slots())
        if self._mode is ImportMode.SELF_CONTAINED_1TO1 and not continuation:
            placement = "at their own slots"
        else:
            placement = f"packed into slots 0-{max(this_pass - 1, 0)}"
        prefix = "next pass: writes " if continuation else "writes "
        tail = f"; {later} more remain for later passes (n)" if later else ""
        return (
            f"Mode: {label} · {prefix}{this_pass} of {total} "
            f"performances {placement}, skipping {skipped} that reference "
            f"another writable bank; ROM references stay in ROM{tail}"
        )

    def _pass_line(self) -> str:
        """The status-line echo of what earlier passes already placed."""
        state = self._pass_state
        if state is None or not state.written:
            return ""
        seen: Dict[str, int] = {}
        for target in state.written.values():
            seen[target] = seen.get(target, 0) + 1
        groups = ", ".join(f"{target}: {count}" for target, count in seen.items())
        head = f"pass so far: {len(state.written)} written ({groups})"
        if state.split_session:
            remaining = self._split_remaining()
            if remaining:
                what = (
                    "remain for the next card"
                    if self._cards_only_session()
                    else "remain"
                )
                return (
                    f"{head} · {len(remaining)} performances {what}: switch "
                    "the card on the unit (select it on the HCard and press "
                    "C), then press n"
                )
            return f"{head} · nothing left to write: the split is complete"
        return f"{head} · next pass: {len(self._remaining_slots())} remaining"

    def _summary_text(self) -> str:
        try:
            bank = self._source_bank()
        except IndexError:
            return "0 active performances · 0 patches · waveseq no"
        source = bank.source
        performances = getattr(source, "performances", ()) or ()
        patches = getattr(source, "patches", ()) or ()
        perf_count = sum(
            1 for record in performances if not _is_empty_performance(record)
        )
        patch_count = _active_count(patches)
        has_ws = bool(getattr(source, "waveseq", None))
        counts = (
            f"{perf_count} active performances · "
            f"{patch_count} patches · "
            f"waveseq {'yes' if has_ws else 'no'}"
        )
        block = self._containment_text()
        return f"{counts}\n{block}" if block else counts

    def _target_line(self) -> str:
        try:
            target = self._target_bank()
        except IndexError:
            return "Target bank: (none)"
        return f"Target bank: {target.label}  ·  b changes it"

    def _gate_line(self) -> str:
        if self._gate is None:
            return "Write gate: not held here"
        return (
            "Write gate: ARMED" if self._gate.armed else "Write gate: locked (press w)"
        )

    def _set_status(self, message: str) -> None:
        try:
            self.query_one(f"#{STATUS_ID}", Static).update(message)
        except Exception:
            return

    def _update_source(self) -> None:
        try:
            bank = self._source_bank()
        except IndexError:
            return
        try:
            self.query_one(f"#{SOURCE_ID}", Static).update(
                f"{_escape(str(bank.label))}  ·  {_escape(str(bank.scope))}"
            )
        except Exception:
            _ = None
        try:
            self.query_one(f"#{SUMMARY_ID}", Static).update(self._summary_text())
        except Exception:
            _ = None

    def _rebuild_table(self) -> None:
        try:
            table = self.query_one(f"#{TABLE_ID}", DataTable)
        except Exception:
            return
        table.clear()
        try:
            source = self._source_bank().source
        except IndexError:
            return
        performances = getattr(source, "performances", ()) or ()
        for slot in self._active_perf_slots():
            if slot >= len(performances):
                continue
            record = performances[slot]
            try:
                parts = [p for p in records.performance_parts(record) if p.used]
            except (ValueError, TypeError):
                parts = []
            try:
                from kws import deps as _deps

                dep_items = _deps.performance_dependencies(record)
            except Exception:
                dep_items = []
            table.add_row(
                f"{slot:02d}",
                _escape(str(dumps.record_name(record)).strip()),
                str(len(parts)),
                str(len(dep_items)),
                key=str(slot),
            )

    def _update_target(self) -> None:
        try:
            self.query_one(f"#{TARGET_ID}", Static).update(self._target_line())
        except Exception:
            _ = None

    def _update_gate(self) -> None:
        try:
            self.query_one(f"#{GATE_ID}", Static).update(self._gate_line())
        except Exception:
            _ = None

    def _update_mode(self) -> None:
        try:
            self.query_one(f"#{MODE_ID}", Static).update(self._mode_line())
        except Exception:
            _ = None

    def _rebuild_all(self) -> None:
        self._refresh_audit()
        self._update_source()
        self._rebuild_table()
        self._update_target()
        self._update_mode()
        self._update_gate()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Whole bank import", classes="title")
            yield Static(self._path, id=PATH_ID, classes="path", markup=False)
            yield Static("", id=SOURCE_ID, classes="source", markup=False)
            yield Static("", id=SUMMARY_ID, classes="summary", markup=False)
            yield DataTable(
                id=TABLE_ID,
                cursor_type="row",
                zebra_stripes=True,
            )
            yield Static("", id=TARGET_ID, classes="target", markup=False)
            yield Static("", id=MODE_ID, classes="mode", markup=False)
            yield Static("", id=GATE_ID, classes="gate", markup=False)
            yield Static("", id=STATUS_ID, classes="status", markup=False)
            yield Static(
                "s source · b target · m mode · o overflow (patches only, a "
                "self-contained split of both banks, or cards only with no "
                "RAM bank) · n next pass · "
                "x companions · "
                "w gate · ctrl+s write · c cache · ? help · esc/q cancel",
                classes="hint",
                markup=False,
            )

    def on_mount(self) -> None:
        table = self.query_one(f"#{TABLE_ID}", DataTable)
        table.add_column("#", key="num", width=4)
        table.add_column("name", key="name")
        table.add_column("parts", key="parts", width=6)
        table.add_column("deps", key="deps", width=6)
        self._rebuild_all()
        slots = self._active_perf_slots()
        if slots:
            try:
                table.focus()
            except Exception:
                _ = None
        if self._notice:
            # The notice wins over the pass line once: this visit exists to
            # finish a write, and that one line is the whole point of it.
            self._set_status(self._notice)
            return
        if slots:
            pass_line = self._pass_line()
            if pass_line:
                self._set_status(pass_line)
        else:
            self._set_status("No active performances in this bank.")

    def action_source(self) -> None:
        if len(self._banks) <= 1:
            self._set_status("only one source bank in this file")
            return
        self._source_index = (self._source_index + 1) % len(self._banks)
        self._rebuild_all()

    def action_target(self) -> None:
        if not self._writable:
            self._set_status("no writable target banks")
            return
        self._target_index = (self._target_index + 1) % len(self._writable)
        self._update_target()

    def action_toggle_gate(self) -> None:
        if self._gate is None:
            self._set_status("not held here")
            return
        self._gate.toggle()
        self._update_gate()
        self._set_status(self._gate.describe())

    def action_mode(self) -> None:
        index = MODE_ORDER.index(self._mode)
        self._mode = MODE_ORDER[(index + 1) % len(MODE_ORDER)]
        self._update_mode()
        self._set_status(self._mode_line())

    def action_next_pass(self) -> None:
        """Offer the not-yet-written performances (plan R4 c and d)."""
        if self._split_session():
            self._next_split_pass()
            return
        if self._mode is ImportMode.AS_IS:
            self._set_status("next pass needs a self-contained mode (press m)")
            return
        if self._audit is None:
            self._set_status("containment is not known for this bank")
            return
        remaining = self._remaining_slots()
        if not remaining:
            self._set_status("no remaining performances")
            return
        if not self._writable:
            self._set_status("no writable target banks")
            return
        self.app.push_screen(
            TargetBankScreen(
                "Next pass target", [str(w.label) for w in self._writable]
            ),
            self._next_pass_target_chosen,
        )

    def _next_split_pass(self) -> None:
        """Continue the split onto the card now in the slot (plan R4 d).

        The card has been swapped; the user selects it on the HCard and
        presses C first, so the next pass replaces what the slot holds
        with one more self-contained group of the same source bank.
        """
        remaining = self._split_remaining()
        if not remaining:
            flow = (
                "cards-only pass"
                if self._cards_only_session()
                else "self-contained split"
            )
            self._set_status(f"nothing left to write: the {flow} is complete")
            return
        try:
            target = self._split_target_bank()
        except IndexError:
            self._set_status("no writable target banks")
            return
        group = self._split_next_group(remaining, target)
        if group is None:
            self._set_status(f"the next group cannot be planned for {target.label}")
            return
        if self._cards_only_session():
            later = max(len(remaining) - len(group.performances), 0)
            self._set_status(
                f"next pass: writes {self._split_group_words(group)} into "
                f"{target.label}, leaving {later} for the card after — "
                "switch the card on the unit (select it on the HCard and "
                "press C), then press n"
            )
        else:
            self._set_status(
                f"next pass: writes {self._split_group_words(group)} packed into "
                f"{target.label} — switch the card on the unit (select it on "
                "the HCard and press C), then press n"
            )
        if self._gate is not None and not self._gate.armed:
            self._set_status("write gate is locked")
            return
        self.dismiss(
            WholeBankChoice(
                self._source_index,
                str(target.id),
                True,
                self._mode,
                self._session_key(),
                True,
                "",
                OVERFLOW_SELF_CONTAINED,
            )
        )

    def _next_pass_target_chosen(self, picked: Optional[int]) -> None:
        """Dismiss with the next pass's write into the chosen target."""
        if picked is None:
            return
        try:
            target = self._writable[picked]
        except (IndexError, TypeError):
            return
        if self._gate is not None and not self._gate.armed:
            self._set_status("write gate is locked")
            return
        self.dismiss(
            WholeBankChoice(
                self._source_index,
                str(target.id),
                True,
                self._mode,
                self._session_key(),
                True,
            )
        )

    def _spillover_companions(self) -> list[SpilloverCompanion]:
        """The file's other RAM banks, each with the RAM bank it would fill.

        The main source is the bank selected now; its containment audit is
        what says which banks its parts name (plan D2). A ROM companion is
        left out -- it cannot be written -- and so is the Card, because
        spill targets are RAM banks. The main source itself is not a
        companion: ``ctrl+s`` is how it is written.
        """
        audit = self._audit
        out: list[SpilloverCompanion] = []
        for index, entry in enumerate(self._banks):
            if index == self._source_index:
                continue
            bank = _entry_bank(entry)
            if bank is None or not bank.writable or not bank.id.startswith("RAM"):
                continue
            named = 0 if audit is None else audit.parts_per_bank.get(bank.number, 0)
            out.append(
                SpilloverCompanion(
                    index=index,
                    label=str(getattr(entry, "label", bank.label)),
                    bank_number=bank.number,
                    target=bank.id,
                    named_parts=named,
                )
            )
        return out

    @staticmethod
    def _spillover_choices(companions: Sequence[SpilloverCompanion]) -> list[str]:
        """One line per companion: what it is, and the RAM bank it fills."""
        out: list[str] = []
        for companion in companions:
            target = kws_banks.bank(companion.target).label
            named = companion.named_parts
            if named == 0:
                note = "no part names it"
            elif named == 1:
                note = "1 part names it"
            else:
                note = f"{named} parts name it"
            out.append(f"{_escape(companion.label)} → {target} · {note}")
        return out

    def action_spillover(self) -> None:
        """Offer the file's companion banks, one explicit write each (R3)."""
        if len(self._banks) <= 1:
            self._set_status("only one source bank in this file")
            return
        companions = self._spillover_companions()
        if not companions:
            self._set_status("no writable RAM companion bank in this file")
            return
        self.app.push_screen(
            ImportMenuScreen("Companion bank", self._spillover_choices(companions)),
            lambda index: self._companion_chosen(companions, index),
        )

    def _companion_chosen(
        self, companions: Sequence[SpilloverCompanion], index: Optional[int]
    ) -> None:
        """After the companion is picked, offer its RAM target (D2 default)."""
        if index is None:
            return
        try:
            companion = companions[index]
        except (IndexError, TypeError):
            return
        suggested = kws_banks.bank(companion.target)
        targets = [suggested] + [b for b in _ram_banks() if b.id != suggested.id]
        self.app.push_screen(
            TargetBankScreen("Spill target", [b.label for b in targets]),
            lambda picked: self._spill_target_chosen(companion, targets, picked),
        )

    def _spill_target_chosen(
        self,
        companion: SpilloverCompanion,
        targets: Sequence[kws_banks.Bank],
        picked: Optional[int],
    ) -> None:
        """Dismiss with the companion's own whole-bank write, gate first."""
        if picked is None:
            return
        try:
            target = targets[picked]
        except (IndexError, TypeError):
            return
        if self._gate is not None and not self._gate.armed:
            self._set_status("write gate is locked")
            return
        # A companion write fills the patch slots the parts name; it does
        # not exist to replace the performances that RAM bank already
        # holds. The app still writes the bank whole when the unit's copy
        # of it is empty of performances (all Init) -- there is nothing
        # there to destroy.
        self.dismiss(
            WholeBankChoice(
                companion.index, target.id, True, self._mode, companion_only=True
            )
        )

    def action_write(self) -> None:
        if self._gate is not None and not self._gate.armed:
            self._set_status("write gate is locked")
            return
        self.dismiss(
            WholeBankChoice(
                self._source_index,
                self._target_bank().id,
                True,
                self._mode,
                self._session_key(),
                True,
            )
        )

    def action_cache(self) -> None:
        self.dismiss(
            WholeBankChoice(
                self._source_index, self._target_bank().id, False, self._mode
            )
        )

    def action_help(self) -> None:
        text = (
            "Whole-bank import keys:\n"
            "s  switch to the next source bank in the file\n"
            "b  switch the target bank (RAM 1/2/3, Card)\n"
            "m  cycle the import mode: as-is, self-contained 1:1, "
            "self-contained packed\n"
            "o  choose what the target cannot hold, in one of three kinds:\n"
            "   only the patches its parts name in another bank, repointed at\n"
            "   a RAM bank while every performance stays on the target; or the\n"
            "   performances that point outside it, with all their patches,\n"
            "   so the target and the RAM bank are each self-contained\n"
            "   (a split needing more than the one offered bank is refused;\n"
            "   x sequences it one bank per write); or cards only, one\n"
            "   self-contained group per card and no RAM bank at all, however\n"
            "   many cards the whole bank needs\n"
            "n  offer the next pass: the contained performances no earlier\n"
            "   pass has written, into a fresh target (another card bank\n"
            "   after a switch, or a RAM bank); each pass is its own gated\n"
            "   write, and with nothing left it says so and writes nothing.\n"
            "   When a self-contained split needed more banks than one\n"
            "   pass offered, or the kind was cards only, n instead writes\n"
            "   one more group of the remainder into the card slot: switch\n"
            "   the card on the unit, select it on the HCard and press C,\n"
            "   then press n.\n"
            "x  offer the file's companion banks, each writing into the RAM "
            "bank the source's parts name (one gated write per bank): its "
            "patches and wave sequences travel, and its performances stay "
            "as they are -- only a bank with no performances at all is "
            "written whole\n"
            "w  arm/lock the write gate\n"
            "c  save the bank to the local cache only (no unit write)\n"
            "ctrl+s  write the whole bank to the target (gate must be armed)\n"
            "?  this help\n"
            "Esc / q  cancel\n\n"
            "Whole-bank writes move only self-references to the target.\n"
            "References to other writable banks stay where they point, and\n"
            "ROM references stay in ROM.\n\n"
            "The self-contained modes write only the performances whose parts\n"
            "are all self or ROM: 1:1 keeps their own slots, packed re-slots\n"
            "them 0..n-1 and defragments the patches beneath them. A bank\n"
            "that cannot hold them all splits into passes: each pass fills\n"
            "one target, and n carries the remainder into the next one.\n"
            "Cards only packs every performance, contained or not, one\n"
            "self-contained group per card; no RAM bank is written.\n\n"
            "A companion write is the same fenced write as ctrl+s, minus\n"
            "the performances: the chosen bank's patches and wave\n"
            "sequences are replaced, and what it holds as performances\n"
            "stays. Only a bank that is all Init is written whole.\n"
            "It is one bank at a time: picking a companion writes that\n"
            "bank only, never the first one with it, and the target may\n"
            "be changed from the suggested bank. Nothing is repointed\n"
            "automatically."
        )
        self.app.push_screen(ReportScreen("Whole bank import", text))

    def action_cancel(self) -> None:
        self.dismiss(None)
