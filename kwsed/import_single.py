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

"""Two-pane multi-performance import screen."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Static

from kws import banks, dumps, perfcopy, relations
from kws import records as rec
from kwsed.gate import EditGate
from vinsynlib.ui.report import ReportScreen

__all__ = ["SingleImportChoice", "SingleImportScreen", "SlotPrompt"]

_HELP_LINES = [
    "Single import keys:",
    "Space  toggle auto-assign on the focused source row",
    "0-9  type the target slot directly into the target column",
    "     two digits assign immediately; Enter commits one digit",
    "Enter  commit the typed slot (no dialog opens)",
    "u / Backspace / Delete  unassign focused source",
    "A  assign all unassigned active sources to free/Init slots",
    "U  clear all assignments",
    "p  toggle shared/fresh patches (fresh = the import gets its own",
    "   patch copies, independent of the target bank)",
    "s  choose a source bank, add another file, or add a RAM/ROM/Card bank",
    "w  toggle write gate",
    "ctrl+s  import the current assignment",
    "r  rebuild",
    "?  this help",
    "Esc  cancel typed slot first, then leave the screen",
    "q  leave the screen",
    "",
    "Separate performance, patch and waveseq files for the same bank are",
    "merged when added through s, so a performance can bring its patches.",
    "",
    "The lower table's 'patches' column shows the destination patch",
    "slots a performance uses or will use.",
]


@dataclass(frozen=True)
class SingleImportChoice:
    target_bank: str
    assignments: Dict[int, relations.PerfId] = field(default_factory=dict)
    #: Every source bank in the session, including aliases for additional
    #: files and the target bank's current records.
    sources: Dict[str, Any] = field(default_factory=dict)
    share_patches: bool = True


def _escape(text: str) -> str:
    return text.replace("[", "\\[")


def _target_scope(
    sources: Mapping[str, Any], target_bank: banks.Bank, target_source: Any
) -> str:
    for scope, src in sources.items():
        if src is target_source:
            return scope
    for scope, _src in sources.items():
        if scope == target_bank.id:
            return scope
        if target_bank.id == "CARD" and scope.startswith("CARD/"):
            return scope
    for scope, src in sources.items():
        perfs = getattr(src, "performances", None)
        if perfs is not None and len(perfs) == banks.PERFORMANCES_PER_BANK:
            return scope
    return target_bank.id


class SlotPrompt(ModalScreen[Optional[int]]):
    DEFAULT_CSS = """
    SlotPrompt { align: center middle; }
    SlotPrompt > Vertical {
        width: 40; height: auto; border: thick $accent;
        background: $surface; padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "confirm", "OK", priority=True),
    ]

    def __init__(self, *, seed: str = "") -> None:
        super().__init__()
        self._seed = seed

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Target slot (00-49):", markup=False)
            yield Input(value=self._seed, id="slot-input", max_length=2)

    def on_mount(self) -> None:
        try:
            self.query_one("#slot-input", Input).focus()
        except Exception:
            return

    def on_input_changed(self, event: Input.Changed) -> None:
        value = event.value.strip()
        if len(value) == 2:
            try:
                number = int(value)
            except ValueError:
                return
            if 0 <= number <= 49:
                self.dismiss(number)

    def action_confirm(self) -> None:
        try:
            value = self.query_one("#slot-input", Input).value.strip()
        except Exception:
            self.dismiss(None)
            return
        try:
            number = int(value)
        except ValueError:
            self.dismiss(None)
            return
        if 0 <= number <= 49:
            self.dismiss(number)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SingleImportScreen(ModalScreen[Optional[SingleImportChoice]]):
    DEFAULT_CSS = """
    SingleImportScreen { align: center middle; }
    SingleImportScreen > Vertical {
        width: 100%; height: 100%; padding: 1 2;
        background: $surface;
    }
    SingleImportScreen DataTable { height: 1fr; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("q", "cancel", "Cancel"),
        Binding("space", "toggle_auto", "Toggle"),
        Binding("enter", "open_slot", "Slot"),
        Binding("u", "unassign", "Unassign"),
        Binding("backspace", "unassign", "Unassign"),
        Binding("delete", "unassign", "Unassign"),
        Binding("A", "assign_all", "All"),
        Binding("U", "clear_all", "Clear"),
        Binding("p", "toggle_patches", "Patches"),
        Binding("s", "cycle_source", "Source"),
        Binding("w", "toggle_gate", "Gate"),
        Binding("ctrl+s", "import_choice", "Import", priority=True),
        Binding("r", "rebuild", "Rebuild"),
        Binding("question_mark", "help", "Help"),
    ]

    SOURCE_TABLE_ID = "source-table"
    TARGET_TABLE_ID = "target-table"
    HEADER_ID = "header"
    BUDGET_ID = "budget"
    STATUS_ID = "status"
    GATE_ID = "gate"

    def __init__(
        self,
        *,
        path: str,
        banks: Sequence[Any],
        sources: Mapping[str, Any],
        target_bank: banks.Bank,
        target_source: Any,
        gate: Optional[EditGate] = None,
        on_source_request: Optional[Callable[["SingleImportScreen"], None]] = None,
        on_cancel: Optional[Callable[["SingleImportScreen"], None]] = None,
        initial_assignments: Optional[Mapping[int, relations.PerfId]] = None,
        initial_status: str = "",
    ) -> None:
        super().__init__()
        self._path = path
        self._banks: list[Any] = list(banks)
        self._sources: Dict[str, Any] = dict(sources)
        self._target_bank = target_bank
        self._target_source = target_source
        self._gate = gate
        self._on_source_request = on_source_request
        self._on_cancel = on_cancel
        self._target_scope = _target_scope(sources, target_bank, target_source)
        self._source_index = 0
        self._assignments: Dict[int, relations.PerfId] = (
            dict(initial_assignments) if initial_assignments else {}
        )
        self._active_sources: List[int] = []
        self._status_message = initial_status
        self._share_patches = True
        #: Target slot digits typed straight into the source row. No modal:
        #: the target column shows the digits while they are entered.
        self._slot_entry = ""
        self._slot_entry_source: Optional[int] = None
        self._source_path_width = 28

    @property
    def assignments(self) -> Dict[int, relations.PerfId]:
        return self._assignments

    @property
    def sources(self) -> Mapping[str, Any]:
        return self._sources

    @property
    def source_banks(self) -> List[Any]:
        return list(self._banks)

    @property
    def source_index(self) -> int:
        return self._source_index

    def select_source(self, index: int) -> None:
        if not 0 <= index < len(self._banks):
            return
        self._source_index = index
        self._clear_slot_entry()
        self._set_status(f"source bank {index + 1}/{len(self._banks)}")
        self._refresh()

    def replace_source_bank(
        self, index: int, bank: Any, source: Optional[Any] = None
    ) -> None:
        """Replace one session bank after merging components into it."""
        if not 0 <= index < len(self._banks):
            return
        self._banks[index] = bank
        scope = str(getattr(bank, "scope", ""))
        self._sources[scope] = (
            source if source is not None else getattr(bank, "source", None)
        )
        self._clear_slot_entry()
        self._set_status(
            f"merged into source {getattr(bank, 'label', scope)}; assignments kept"
        )
        self._refresh()

    def add_source_bank(self, bank: Any, source: Optional[Any] = None) -> None:
        """Add a source from another file without touching assignments."""
        self._banks.append(bank)
        scope = str(getattr(bank, "scope", ""))
        self._sources[scope] = (
            source if source is not None else getattr(bank, "source", None)
        )
        self._source_index = len(self._banks) - 1
        self._clear_slot_entry()
        self._set_status(
            f"added source {getattr(bank, 'label', scope)}; assignments kept"
        )
        self._refresh()

    def _source_bank(self) -> Any:
        if not self._banks:
            return None
        return self._banks[self._source_index]

    def _source_scope(self) -> str:
        bank = self._source_bank()
        return str(getattr(bank, "scope", "")) if bank is not None else ""

    def _active_source_slots(self) -> List[int]:
        bank = self._source_bank()
        if bank is None:
            return []
        source = getattr(bank, "source", None)
        if source is None:
            return []
        perfs = getattr(source, "performances", [])
        result: List[int] = []
        for slot in range(min(50, len(perfs))):
            if not perfcopy.is_empty_performance(perfs[slot]):
                result.append(slot)
        return result

    def _target_slot_for_source(self, source_slot: int) -> Optional[int]:
        perf_id = relations.PerfId(self._source_scope(), source_slot)
        for slot, pid in self._assignments.items():
            if pid == perf_id:
                return slot
        return None

    def _target_text(self, source_slot: int, target_slot: Optional[int]) -> str:
        if self._slot_entry_source == source_slot and self._slot_entry:
            return f"[bold cyan]{self._slot_entry}[/bold cyan]"
        if target_slot is None:
            return "--"
        return f"[bold yellow]{target_slot:02d}[/bold yellow]"

    @staticmethod
    def _compact_source_path(path: str) -> str:
        if not path:
            return ""
        parts = [part for part in path.replace("\\", "/").split("/") if part]
        if len(parts) >= 3:
            return ".../" + "/".join(parts[-3:])
        return "/".join(parts)

    def _source_path_for_scope(self, scope: str) -> str:
        for bank in self._banks:
            if getattr(bank, "scope", None) == scope:
                path = getattr(bank, "source_path", "")
                if path:
                    return str(path)
                origin = getattr(bank, "source_origin", "")
                if origin:
                    return str(origin)
                return ""
        return ""

    def _source_path_text(self, scope: str) -> str:
        raw = self._source_path_for_scope(scope)
        if not raw:
            return "--"
        width = max(3, self._source_path_width)
        # Unit origins (e.g. "RAM 2", "ROM 7", "Card 07") are not filesystem
        # paths: display them verbatim, only right-aligning/padding.
        if "/" not in raw and "\\" not in raw:
            return _escape(raw.rjust(width))
        compact = self._compact_source_path(raw)
        if not compact:
            return "--"
        if len(compact) > width:
            compact = "..." + compact[-(width - 3) :] if width > 3 else compact[-width:]
        # Right-align it by hand: DataTable cells are left-aligned by default,
        # and the right end is the file name we must never lose.
        return _escape(compact.rjust(width))

    def _target_path_for_slot(self, slot: int) -> str:
        assigned = self._assignments.get(slot)
        if assigned is None:
            return ""
        return self._source_path_text(assigned.scope_id)

    def _source_path_column_width(self) -> int:
        # slot 6 + state 8 + performance 20 + patches 14, plus frame/padding.
        available = max(0, int(getattr(self.size, "width", 0) or 80) - 52)
        return max(10, min(64, available))

    def _setup_target_columns(self) -> None:
        table = self.query_one(f"#{self.TARGET_TABLE_ID}", DataTable)
        table.clear(columns=True)
        self._source_path_width = self._source_path_column_width()
        table.add_column("slot", key="slot", width=6)
        table.add_column("state", key="state", width=8)
        table.add_column("performance", key="performance", width=20)
        table.add_column("patch slots", key="patches", width=14)
        table.add_column("source", key="source", width=self._source_path_width)

    def on_resize(self, event: Any) -> None:
        if not self.is_mounted:
            return
        try:
            self._setup_target_columns()
            self._refresh_target_table(self._plan())
        except Exception:
            return

    def _update_source_target_cell(self, source_slot: int) -> None:
        """Update one target cell without rebuilding the table or moving the cursor."""
        try:
            table = self.query_one(f"#{self.SOURCE_TABLE_ID}", DataTable)
            target_slot = self._target_slot_for_source(source_slot)
            table.update_cell(
                str(source_slot),
                "target",
                self._target_text(source_slot, target_slot),
            )
        except Exception:
            self._refresh_source_table()

    def _first_free_target_slot(self) -> Optional[int]:
        assigned = set(self._assignments)
        perfs = getattr(self._target_source, "performances", [])
        for slot in range(50):
            if slot in assigned:
                continue
            if slot >= len(perfs):
                continue
            if perfcopy.is_empty_performance(perfs[slot]):
                return slot
        return None

    def _plan(self) -> Any:
        return perfcopy.plan_slot_import(
            self._sources,
            self._target_scope,
            self._assignments,
            destination_bank=self._target_bank,
            share_existing=self._share_patches,
        )

    def _set_status(self, message: str) -> None:
        self._status_message = message
        with suppress(Exception):
            self.query_one(f"#{self.STATUS_ID}", Static).update(message)

    def set_status(self, message: str) -> None:
        """Show an app-generated source warning/merge note."""
        self._set_status(message)

    def _refresh(self) -> None:
        self._active_sources = self._active_source_slots()
        with suppress(Exception):
            bank = self._source_bank()
            label = getattr(bank, "label", "") if bank is not None else ""
            scope = getattr(bank, "scope", "") if bank is not None else ""
            header_text = (
                f"{self._path}  ·  source: {label} ({scope})  ·  "
                f"target: {self._target_bank.label}"
            )
            self.query_one(f"#{self.HEADER_ID}", Static).update(header_text)
        with suppress(Exception):
            if self._gate is None:
                gate_text = "Write gate: not held here · ctrl+s writes"
            elif self._gate.armed:
                gate_text = "Write gate: ARMED · w locks · ctrl+s writes"
            else:
                gate_text = "Write gate: locked (press w) · ctrl+s writes"
            self.query_one(f"#{self.GATE_ID}", Static).update(gate_text)
        plan = self._plan()
        self._refresh_source_table()
        self._refresh_target_table(plan)
        with suppress(Exception):
            patch_mode = "shared" if self._share_patches else "fresh"
            budget_text = (
                f"Assigned: {len(self._assignments)} · "
                f"Performances: {len(self._assignments)}/50 · "
                f"Patches: {plan.patch_count}/35 ({patch_mode}) · "
                f"Wave Seqs: {plan.sequence_count}/32 · "
                f"Steps: {plan.steps_used}/501"
            )
            self.query_one(f"#{self.BUDGET_ID}", Static).update(budget_text)
        if not plan.fits:
            rejected = plan.rejected[:3]
            suffix = ""
            if len(plan.rejected) > 3:
                suffix = f" (+{len(plan.rejected) - 3} more)"
            self._set_status("; ".join(rejected) + suffix)
        elif self._status_message:
            self._set_status(self._status_message)
        else:
            self._set_status("")

    def _refresh_source_table(self) -> None:
        try:
            table = self.query_one(f"#{self.SOURCE_TABLE_ID}", DataTable)
        except Exception:
            return
        cursor = table.cursor_row
        cursor_key = (
            str(self._active_sources[cursor])
            if cursor is not None and 0 <= cursor < len(self._active_sources)
            else None
        )
        table.clear()
        for slot in self._active_sources:
            bank = self._source_bank()
            source = getattr(bank, "source", None)
            perfs = getattr(source, "performances", [])
            record = perfs[slot] if slot < len(perfs) else b""
            name = dumps.record_name(record)
            parts = sum(1 for p in rec.performance_parts(record) if p.used)
            target_slot = self._target_slot_for_source(slot)
            target_text = self._target_text(slot, target_slot)
            table.add_row(
                f"{slot:02d}",
                target_text,
                _escape(str(name)),
                f"{parts}P",
                key=str(slot),
            )
        if self._active_sources:
            row = None
            if cursor_key is not None:
                with suppress(Exception):
                    row = table.get_row_index(cursor_key)
            if row is None:
                row = (
                    cursor
                    if cursor is not None and 0 <= cursor < len(self._active_sources)
                    else 0
                )
            with suppress(Exception):
                table.move_cursor(row=row)
            table.focus()

    def _refresh_target_table(self, plan: Any = None) -> None:
        try:
            table = self.query_one(f"#{self.TARGET_TABLE_ID}", DataTable)
        except Exception:
            return
        table.clear()
        alloc_by_slot = {}
        if plan is not None:
            alloc_by_slot = {
                allocation.target_perf_slot: allocation
                for allocation in plan.allocations
            }
        perfs = getattr(self._target_source, "performances", [])
        for slot in range(50):
            record = perfs[slot] if slot < len(perfs) else b""
            assigned_src = self._assignments.get(slot)
            if assigned_src is not None:
                state = f"← {assigned_src.number:02d}"
                allocation = alloc_by_slot.get(slot)
                patch_slots = (
                    sorted(set(allocation.part_patch_slots.values()))
                    if allocation is not None
                    else []
                )
            else:
                try:
                    empty = perfcopy.is_empty_performance(record)
                except Exception:
                    empty = False
                if empty:
                    name_low = dumps.record_name(record).strip().lower()
                    state = "Init" if name_low == "init" else "free"
                else:
                    state = "used"
                patch_slots = (
                    sorted(
                        {
                            part.patch
                            for part in rec.performance_parts(record)
                            if part.used and part.bank == self._target_bank.number
                        }
                    )
                    if not empty
                    else []
                )
            name = dumps.record_name(record)
            if assigned_src is not None:
                with suppress(Exception):
                    src_bank = self._sources.get(assigned_src.scope_id)
                    src_perfs = (
                        getattr(src_bank, "performances", [])
                        if src_bank is not None
                        else []
                    )
                    src_name = (
                        dumps.record_name(src_perfs[assigned_src.number])
                        if assigned_src.number < len(src_perfs)
                        else ""
                    )
                    name = f"{src_name} ← {name}"
            patch_text = ",".join(f"{number:02d}" for number in patch_slots) or "--"
            source_text = self._target_path_for_slot(slot) or "--"
            table.add_row(
                f"{slot:02d}",
                state,
                _escape(str(name)),
                _escape(patch_text),
                source_text,
                key=str(slot),
            )

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._path, id=self.HEADER_ID, classes="header", markup=False)
            hint = (
                "Space assign · type 00-49 + Enter exact slot (inline) · "
                "u unassign · A all · U clear\n"
                "s source banks/add · w gate · ctrl+s import · ? help · Esc cancel"
            )
            yield Static(hint, classes="hint", markup=False)
            yield DataTable(
                id=self.SOURCE_TABLE_ID, cursor_type="row", zebra_stripes=True
            )
            yield DataTable(
                id=self.TARGET_TABLE_ID, cursor_type="row", zebra_stripes=True
            )
            yield Static("", id=self.BUDGET_ID, classes="budget", markup=False)
            yield Static("", id=self.GATE_ID, classes="gate", markup=False)
            yield Static("", id=self.STATUS_ID, classes="status", markup=False)

    def on_mount(self) -> None:
        with suppress(Exception):
            src_table = self.query_one(f"#{self.SOURCE_TABLE_ID}", DataTable)
            src_table.add_column("#", key="num", width=4)
            src_table.add_column("target", key="target", width=8)
            src_table.add_column("name", key="name")
            src_table.add_column("deps", key="deps", width=6)
            self._setup_target_columns()
        self._refresh()

    def on_key(self, event: Any) -> None:
        key = getattr(event, "key", "")
        if key in "0123456789":
            event.stop()
            event.prevent_default()
            self._type_slot_digit(key)
            return
        if key == "backspace" and self._slot_entry:
            event.stop()
            event.prevent_default()
            source_slot = self._slot_entry_source
            self._slot_entry = self._slot_entry[:-1]
            if not self._slot_entry:
                self._slot_entry_source = None
                self._set_status("slot entry cancelled")
            else:
                self._set_status(
                    f"target slot {self._slot_entry}_ — "
                    "type the second digit or Enter; Esc cancels"
                )
            if source_slot is not None:
                self._update_source_target_cell(source_slot)
            return
        if key == "enter" and self._slot_entry:
            event.stop()
            event.prevent_default()
            self._commit_slot_entry()
            return
        if key == "escape" and self._slot_entry:
            event.stop()
            event.prevent_default()
            source_slot = self._slot_entry_source
            self._slot_entry = ""
            self._slot_entry_source = None
            self._set_status("slot entry cancelled")
            if source_slot is not None:
                self._update_source_target_cell(source_slot)

    def _type_slot_digit(self, digit: str) -> None:
        source_slot = self._focused_source_slot()
        if source_slot is None:
            self._set_status("select a source performance first")
            return
        if self._slot_entry_source != source_slot:
            self._slot_entry = ""
            self._slot_entry_source = source_slot
        self._slot_entry = (self._slot_entry + digit)[:2]
        if len(self._slot_entry) == 2:
            self._commit_slot_entry()
            return
        self._set_status(
            f"target slot {self._slot_entry}_ — "
            "type the second digit or Enter; Esc cancels"
        )
        self._update_source_target_cell(source_slot)

    def _commit_slot_entry(self) -> None:
        source_slot = self._slot_entry_source
        text = self._slot_entry
        if source_slot is None or not text:
            self._set_status("type a target slot 00-49 first")
            return
        try:
            target_slot = int(text)
        except ValueError:
            self._set_status("target slot must be 00-49")
            return
        if not 0 <= target_slot <= 49:
            self._set_status("target slot must be 00-49")
            self._update_source_target_cell(source_slot)
            return
        self._clear_slot_entry()
        self._assign_to_slot(source_slot, target_slot)

    def _clear_slot_entry(self) -> None:
        self._slot_entry = ""
        self._slot_entry_source = None

    def _focused_source_slot(self) -> Optional[int]:
        try:
            table = self.query_one(f"#{self.SOURCE_TABLE_ID}", DataTable)
        except Exception:
            return None
        row = table.cursor_row
        if row is None or row < 0 or row >= len(self._active_sources):
            return None
        return self._active_sources[row]

    def action_toggle_auto(self) -> None:
        self._clear_slot_entry()
        slot = self._focused_source_slot()
        if slot is None:
            return
        source_scope = self._source_scope()
        perf_id = relations.PerfId(source_scope, slot)
        target_slot = self._target_slot_for_source(slot)
        if target_slot is not None:
            new = dict(self._assignments)
            del new[target_slot]
            self._assignments = new
            self._set_status(f"unassigned {slot:02d}")
            self._refresh()
            return
        free = self._first_free_target_slot()
        if free is None:
            self._set_status("no free target slot")
            return
        new = dict(self._assignments)
        new[free] = perf_id
        plan = perfcopy.plan_slot_import(
            self._sources,
            self._target_scope,
            new,
            destination_bank=self._target_bank,
            share_existing=self._share_patches,
        )
        if not plan.fits:
            rejected = plan.rejected[:2]
            suffix = f" (+{len(plan.rejected) - 2})" if len(plan.rejected) > 2 else ""
            self._set_status("; ".join(rejected) + suffix)
            return
        self._assignments = new
        self._set_status(f"assigned {slot:02d} -> {free:02d}")
        self._refresh()
        with suppress(Exception):
            table = self.query_one(f"#{self.SOURCE_TABLE_ID}", DataTable)
            if table.cursor_row is not None and table.cursor_row + 1 < len(
                self._active_sources
            ):
                table.move_cursor(row=table.cursor_row + 1)

    def action_open_slot(self, seed: str = "") -> None:
        if self._slot_entry:
            self._commit_slot_entry()
            return
        if seed and seed in "0123456789":
            self._type_slot_digit(seed)
            return
        self._set_status(
            "type the target slot directly in the target column (00-49); "
            "two digits commit, Enter commits one"
        )

    def _assign_to_slot(self, source_slot: int, target_slot: int) -> None:
        self._clear_slot_entry()
        source_scope = self._source_scope()
        perf_id = relations.PerfId(source_scope, source_slot)
        existing = self._assignments.get(target_slot)
        if existing is not None and existing != perf_id:
            self._set_status(
                f"target slot {target_slot:02d} is already assigned to "
                f"{existing.scope_id}:{existing.number:02d}"
            )
            self._refresh()
            return
        new = dict(self._assignments)
        new[target_slot] = perf_id
        plan = perfcopy.plan_slot_import(
            self._sources,
            self._target_scope,
            new,
            destination_bank=self._target_bank,
            share_existing=self._share_patches,
        )
        if not plan.fits:
            rejected = plan.rejected[:2]
            suffix = f" (+{len(plan.rejected) - 2})" if len(plan.rejected) > 2 else ""
            self._set_status("; ".join(rejected) + suffix)
            self._refresh()
            return
        self._assignments = new
        self._set_status(f"assigned {source_slot:02d} -> {target_slot:02d}")
        self._refresh()

    def action_unassign(self) -> None:
        self._clear_slot_entry()
        slot = self._focused_source_slot()
        if slot is None:
            return
        target_slot = self._target_slot_for_source(slot)
        if target_slot is None:
            self._set_status(f"{slot:02d} not assigned")
            return
        new = dict(self._assignments)
        del new[target_slot]
        self._assignments = new
        self._set_status(f"unassigned {slot:02d}")
        self._refresh()

    def action_assign_all(self) -> None:
        self._clear_slot_entry()
        assigned = set(self._assignments)
        unassigned_sources = [
            s for s in self._active_sources if self._target_slot_for_source(s) is None
        ]
        count = 0
        stopped = None
        for src in unassigned_sources:
            candidate = None
            perfs = getattr(self._target_source, "performances", [])
            for ts in range(50):
                if ts in assigned:
                    continue
                if ts >= len(perfs):
                    continue
                if perfcopy.is_empty_performance(perfs[ts]):
                    candidate = ts
                    break
            if candidate is None:
                stopped = f"stopped at source {src:02d}: no free target slot"
                break
            source_scope = self._source_scope()
            perf_id = relations.PerfId(source_scope, src)
            new = dict(self._assignments)
            new[candidate] = perf_id
            plan = perfcopy.plan_slot_import(
                self._sources,
                self._target_scope,
                new,
                destination_bank=self._target_bank,
                share_existing=self._share_patches,
            )
            if not plan.fits:
                rejected = plan.rejected[:2]
                suffix = (
                    f" (+{len(plan.rejected) - 2})" if len(plan.rejected) > 2 else ""
                )
                stopped = (
                    f"stopped at source {src:02d}: " + "; ".join(rejected) + suffix
                )
                break
            self._assignments = new
            assigned.add(candidate)
            count += 1
        if stopped:
            self._set_status(f"assigned {count}; {stopped}")
        else:
            self._set_status(f"assigned {count}")
        self._refresh()

    def action_clear_all(self) -> None:
        self._clear_slot_entry()
        self._assignments.clear()
        self._set_status("cleared all assignments")
        self._refresh()

    def action_toggle_patches(self) -> None:
        self._clear_slot_entry()
        self._share_patches = not self._share_patches
        if self._share_patches:
            self._set_status(
                "patches: shared — byte-identical patches in the target bank are reused"
            )
        else:
            self._set_status(
                "patches: fresh — the import gets its own copies, "
                "independent of the target bank"
            )
        self._refresh()

    def action_cycle_source(self) -> None:
        self._clear_slot_entry()
        if self._on_source_request is not None:
            self._on_source_request(self)
            return
        if not self._banks:
            return
        self._source_index = (self._source_index + 1) % len(self._banks)
        # Switching among banks in one session must not throw away work
        # already assigned from the previous bank.
        self._set_status(
            f"source bank {self._source_index + 1}/{len(self._banks)}; assignments kept"
        )
        self._refresh()

    def action_toggle_gate(self) -> None:
        if self._gate is None:
            self._set_status("not held here")
            return
        self._gate.toggle()
        if self._gate.armed:
            self._set_status("armed — press ctrl+s to write")
        else:
            self._set_status("locked — press w to arm; ctrl+s writes")
        self._refresh()

    def action_import_choice(self) -> None:
        if not self._assignments:
            self._set_status("no assignments to import")
            return
        if self._gate is not None and not self._gate.armed:
            self._set_status("write gate locked (press w)")
            return
        plan = self._plan()
        if not plan.fits:
            rejected = plan.rejected[:2]
            suffix = f" (+{len(plan.rejected) - 2})" if len(plan.rejected) > 2 else ""
            self._set_status("; ".join(rejected) + suffix)
            return
        self.dismiss(
            SingleImportChoice(
                self._target_bank.id,
                dict(self._assignments),
                dict(self._sources),
                share_patches=self._share_patches,
            )
        )

    def action_rebuild(self) -> None:
        self._refresh()
        self._set_status("rebuilt")

    def action_help(self) -> None:
        self.app.push_screen(ReportScreen("Single import", "\n".join(_HELP_LINES)))

    def action_cancel(self) -> None:
        if self._slot_entry:
            source_slot = self._slot_entry_source
            self._clear_slot_entry()
            self._set_status("slot entry cancelled")
            if source_slot is not None:
                self._update_source_target_cell(source_slot)
            return
        if self._on_cancel is not None:
            self._on_cancel(self)
            return
        self.dismiss(None)
