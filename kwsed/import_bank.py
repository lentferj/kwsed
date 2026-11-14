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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional, Sequence, Tuple

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Static

from kws import banks as kws_banks
from kws import containment, dumps, records
from kws.perfcopy import ImportMode
from kwsed.gate import EditGate
from vinsynlib.ui.report import ReportScreen

if TYPE_CHECKING:
    from kws.importers import ImportedBank

__all__ = ["WholeBankChoice", "WholeBankImportScreen"]

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


@dataclass(frozen=True)
class WholeBankChoice:
    source_index: int
    target_bank: str
    write_to_unit: bool
    #: How the write treats performances that reference another bank. The
    #: default keeps every existing caller on the byte-identical as-is path
    #: (:func:`kws.perfcopy.relocate_bank`).
    mode: ImportMode = ImportMode.AS_IS


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
    ) -> None:
        super().__init__()
        self._path = path
        self._banks: list["ImportedBank"] = list(banks)
        self._source_index = 0
        self._writable = kws_banks.writable_banks()
        self._target_index = 0
        self._gate = gate
        self._mode = ImportMode.AS_IS
        self._audit: Optional[containment.ContainmentAudit] = None

    @property
    def banks(self) -> list["ImportedBank"]:
        return self._banks

    def _source_bank(self) -> "ImportedBank":
        if not self._banks:
            raise IndexError("no source banks")
        return self._banks[self._source_index]

    def _target_bank(self) -> kws_banks.Bank:
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

    def _self_contained_counts(self) -> Optional[Tuple[int, int]]:
        """(written, skipped) performances for a self-contained mode."""
        if self._audit is None:
            return None
        return (self._audit.contained, self._audit.other_bank)

    def _mode_line(self) -> str:
        """What the selected mode writes and what it leaves out.

        Shown above the gate and repeated in the status line when ``m`` is
        pressed, so the choice is stated before the gate can be armed (R1).
        """
        label = MODE_LABELS[self._mode]
        if self._mode is ImportMode.AS_IS:
            return (
                f"Mode: {label} · writes the whole bank (every performance, "
                f"patch and wave sequence), skipping nothing; self-references "
                f"move to the target, references to other writable banks and "
                f"to ROM stay where they point"
            )
        counts = self._self_contained_counts()
        if counts is None:
            return (
                f"Mode: {label} · containment is not known for this bank, so "
                f"the number of performances left out cannot be stated"
            )
        written, skipped = counts
        if self._mode is ImportMode.SELF_CONTAINED_1TO1:
            placement = "at their own slots"
        else:
            placement = f"packed into slots 0-{max(written - 1, 0)}"
        return (
            f"Mode: {label} · writes {written} of {written + skipped} "
            f"performances {placement}, skipping {skipped} that reference "
            f"another writable bank; ROM references stay in ROM"
        )

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
                "s source · b target · m mode · w gate · ctrl+s write · c cache · "
                "? help · esc/q cancel",
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

    def action_write(self) -> None:
        if self._gate is not None and not self._gate.armed:
            self._set_status("write gate is locked")
            return
        self.dismiss(
            WholeBankChoice(
                self._source_index, self._target_bank().id, True, self._mode
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
            "them 0..n-1 and defragments the patches beneath them."
        )
        self.app.push_screen(ReportScreen("Whole bank import", text))

    def action_cancel(self) -> None:
        self.dismiss(None)
