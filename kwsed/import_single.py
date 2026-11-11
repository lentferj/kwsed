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
from typing import Any, Dict, List, Mapping, Optional, Sequence

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
    "Enter / 0-9  assign focused source to a chosen target slot",
    "u / Backspace / Delete  unassign focused source",
    "A  assign all unassigned active sources to free/Init slots",
    "U  clear all assignments",
    "s  cycle source bank (clears assignments)",
    "w  toggle write gate",
    "ctrl+s  import the current assignment",
    "r  rebuild",
    "?  this help",
    "Esc / q  cancel",
]


@dataclass(frozen=True)
class SingleImportChoice:
    target_bank: str
    assignments: Dict[int, relations.PerfId] = field(default_factory=dict)


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
    ) -> None:
        super().__init__()
        self._path = path
        self._banks: list[Any] = list(banks)
        self._sources = sources
        self._target_bank = target_bank
        self._target_source = target_source
        self._gate = gate
        self._target_scope = _target_scope(sources, target_bank, target_source)
        self._source_index = 0
        self._assignments: Dict[int, relations.PerfId] = {}
        self._active_sources: List[int] = []
        self._status_message = ""

    @property
    def assignments(self) -> Dict[int, relations.PerfId]:
        return self._assignments

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
        )

    def _set_status(self, message: str) -> None:
        self._status_message = message
        with suppress(Exception):
            self.query_one(f"#{self.STATUS_ID}", Static).update(message)

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
                gate_text = "Write gate: not held here"
            elif self._gate.armed:
                gate_text = "Write gate: ARMED"
            else:
                gate_text = "Write gate: locked (press w)"
            self.query_one(f"#{self.GATE_ID}", Static).update(gate_text)
        self._refresh_source_table()
        self._refresh_target_table()
        plan = self._plan()
        with suppress(Exception):
            budget_text = (
                f"Assigned: {len(self._assignments)} · "
                f"Performances: {len(self._assignments)}/50 · "
                f"Patches: {plan.patch_count}/35 · "
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
        elif not self._status_message:
            self._set_status("")

    def _refresh_source_table(self) -> None:
        try:
            table = self.query_one(f"#{self.SOURCE_TABLE_ID}", DataTable)
        except Exception:
            return
        table.clear()
        cursor = table.cursor_row
        for slot in self._active_sources:
            bank = self._source_bank()
            source = getattr(bank, "source", None)
            perfs = getattr(source, "performances", [])
            record = perfs[slot] if slot < len(perfs) else b""
            name = dumps.record_name(record)
            parts = sum(1 for p in rec.performance_parts(record) if p.used)
            target_slot = self._target_slot_for_source(slot)
            target_text = "--" if target_slot is None else f"{target_slot:02d}"
            if target_slot is not None:
                target_text = f"[bold yellow]{target_text}[/bold yellow]"
            table.add_row(
                f"{slot:02d}",
                target_text,
                _escape(str(name)),
                f"{parts}P",
                key=str(slot),
            )
        if self._active_sources:
            if cursor is None or cursor < 0 or cursor >= len(self._active_sources):
                cursor = 0
            with suppress(Exception):
                table.move_cursor(row=cursor)
            table.focus()

    def _refresh_target_table(self) -> None:
        try:
            table = self.query_one(f"#{self.TARGET_TABLE_ID}", DataTable)
        except Exception:
            return
        table.clear()
        perfs = getattr(self._target_source, "performances", [])
        for slot in range(50):
            record = perfs[slot] if slot < len(perfs) else b""
            assigned_src = self._assignments.get(slot)
            if assigned_src is not None:
                state = f"← {assigned_src.number:02d}"
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
            table.add_row(f"{slot:02d}", state, _escape(str(name)), key=str(slot))

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._path, id=self.HEADER_ID, classes="header", markup=False)
            hint = "s source bank · w gate · ctrl+s import · ? help · esc cancel"
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
            tgt_table = self.query_one(f"#{self.TARGET_TABLE_ID}", DataTable)
            tgt_table.add_column("slot", key="slot", width=6)
            tgt_table.add_column("state", key="state", width=8)
            tgt_table.add_column("name", key="name")
        self._refresh()

    def on_key(self, event: Any) -> None:
        key = getattr(event, "key", "")
        if key in "0123456789":
            event.stop()
            event.prevent_default()
            self.action_open_slot(key)

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
            self._sources, self._target_scope, new, destination_bank=self._target_bank
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
        slot = self._focused_source_slot()
        if slot is None:
            return

        def on_result(result: Optional[int]) -> None:
            if result is None:
                return
            self._assign_to_slot(slot, result)

        self.app.push_screen(SlotPrompt(seed=seed), on_result)

    def _assign_to_slot(self, source_slot: int, target_slot: int) -> None:
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
            self._sources, self._target_scope, new, destination_bank=self._target_bank
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
        self._assignments.clear()
        self._set_status("cleared all assignments")
        self._refresh()

    def action_cycle_source(self) -> None:
        if not self._banks:
            return
        self._source_index = (self._source_index + 1) % len(self._banks)
        self._assignments.clear()
        self._set_status("switched source bank")
        self._refresh()

    def action_toggle_gate(self) -> None:
        if self._gate is None:
            self._set_status("not held here")
            return
        self._gate.toggle()
        if self._gate.armed:
            self._set_status("write gate ARMED")
        else:
            self._set_status("write gate locked (press w)")
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
        self.dismiss(SingleImportChoice(self._target_bank.id, dict(self._assignments)))

    def action_rebuild(self) -> None:
        self._refresh()
        self._set_status("rebuilt")

    def action_help(self) -> None:
        self.app.push_screen(ReportScreen("Single import", "\n".join(_HELP_LINES)))

    def action_cancel(self) -> None:
        self.dismiss(None)
