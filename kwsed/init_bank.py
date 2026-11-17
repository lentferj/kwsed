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

"""Initialise selected kinds of selected RAM banks from Init bytes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList, Static
from textual.widgets.option_list import Option

from kws import banks
from kwsed.gate import EditGate

__all__ = ["InitBankChoice", "InitBankScreen", "selection_summary"]

#: The kinds the menu offers, in display order, as (key, label).
KINDS: Tuple[Tuple[str, str], ...] = (
    ("performances", "Performances"),
    ("patches", "Patches"),
    ("waveseq", "Wave sequences"),
)

#: The banks the menu offers, in order. The Card is never listed.
RAM_BANKS: Tuple[str, ...] = ("RAM1", "RAM2", "RAM3")

CARD_NOTE = "The Card is not listed: format it on the unit (FORMAT CARD), then import."


@dataclass(frozen=True)
class InitBankChoice:
    """The selected kinds per bank, as ``{bank_id: {"performances", ...}}``."""

    banks: Dict[str, Set[str]]


def selection_summary(selection: Dict[str, Set[str]]) -> str:
    """Human confirmation line, e.g. ``RAM 1: performances, patches``."""
    labels = {key: label.lower() for key, label in KINDS}
    parts: List[str] = []
    for bank_id in RAM_BANKS:
        kinds = selection.get(bank_id, set())
        if not kinds:
            continue
        entry = banks.bank(bank_id)
        chosen = [labels[k] for k, _ in KINDS if k in kinds]
        parts.append(f"{entry.label}: {', '.join(chosen)}")
    return " · ".join(parts)


class InitBankScreen(ModalScreen[Optional[InitBankChoice]]):
    """Multi-choice Init menu for RAM 1-3."""

    DEFAULT_CSS = """
    InitBankScreen { align: center middle; }
    InitBankScreen > Vertical {
        width: 72; height: auto; max-height: 90%;
        border: thick $accent; background: $surface; padding: 1 2;
    }
    InitBankScreen #init-title { text-style: bold; }
    InitBankScreen #init-list { height: auto; max-height: 14; margin: 1 0; }
    InitBankScreen .muted { color: $text-muted; }
    InitBankScreen #init-status { color: $warning; min-height: 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("q", "cancel", "Cancel"),
        Binding("space", "toggle_mark", "Toggle", show=False),
        Binding("w", "toggle_gate", "Gate"),
        Binding("enter", "apply", "Apply", priority=True),
    ]

    def __init__(self, *, gate: EditGate) -> None:
        super().__init__()
        self._gate = gate
        self._checked: Dict[Tuple[str, str], bool] = {
            (bank_id, kind): False for bank_id in RAM_BANKS for kind, _ in KINDS
        }

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(
                "Init bank contents                     (space toggles, enter applies)",
                id="init-title",
            )
            yield OptionList(id="init-list")
            yield Static(CARD_NOTE, id="init-card-note", classes="muted")
            yield Static(self._gate_line(), id="init-gate", classes="muted")
            yield Static("", id="init-status")
            yield Static(
                "↑/↓ move · space toggles · enter applies · esc cancels",
                classes="muted",
            )

    def _gate_line(self) -> str:
        state = "ARMED" if self._gate.armed else "LOCKED"
        return f"Gate: {state} — press w to arm        esc cancels"

    def _row_label(self, bank_id: str, kind: str, kind_label: str) -> str:
        mark = "x" if self._checked[(bank_id, kind)] else " "
        return f"[{mark}] {kind_label}"

    def _bank_prefix(self, index: int, bank_id: str) -> str:
        if index == 0:
            return banks.bank(bank_id).label + "   "
        return "        "

    def _option_text(self, option_index: int) -> str:
        bank_id = RAM_BANKS[option_index // len(KINDS)]
        kind_index = option_index % len(KINDS)
        kind, kind_label = KINDS[kind_index]
        prefix = self._bank_prefix(kind_index, bank_id)
        return prefix + self._row_label(bank_id, kind, kind_label)

    def _rebuild(self) -> None:
        options = self.query_one("#init-list", OptionList)
        highlighted = options.highlighted
        options.clear_options()
        for idx in range(len(RAM_BANKS) * len(KINDS)):
            options.add_option(Option(self._option_text(idx), id=str(idx)))
        if highlighted is not None and options.option_count:
            options.highlighted = min(highlighted, options.option_count - 1)
        elif options.option_count:
            options.highlighted = 0

    def on_mount(self) -> None:
        self._rebuild()
        self.query_one("#init-list", OptionList).focus()

    def _current(self) -> Optional[Tuple[str, str]]:
        options = self.query_one("#init-list", OptionList)
        idx = options.highlighted
        if idx is None:
            return None
        return (RAM_BANKS[idx // len(KINDS)], KINDS[idx % len(KINDS)][0])

    def action_toggle_mark(self) -> None:
        current = self._current()
        if current is None:
            return
        self._checked[current] = not self._checked[current]
        self._rebuild()

    def action_toggle_gate(self) -> None:
        if self._gate is None:
            self._status("not held here")
            return
        self._gate.toggle()
        self._update_gate()
        self._status(self._gate.describe())

    def _update_gate(self) -> None:
        try:
            self.query_one("#init-gate", Static).update(self._gate_line())
        except Exception:
            return

    def _selection(self) -> Dict[str, Set[str]]:
        selected: Dict[str, Set[str]] = {bank_id: set() for bank_id in RAM_BANKS}
        for (bank_id, kind), checked in self._checked.items():
            if checked:
                selected[bank_id].add(kind)
        return {bank_id: kinds for bank_id, kinds in selected.items() if kinds}

    def _status(self, text: str) -> None:
        self.query_one("#init-status", Static).update(text)

    def action_apply(self) -> None:
        selection = self._selection()
        if not selection:
            self._status("nothing selected — nothing written")
            return
        if not self._gate.armed:
            self._status(self._gate.refuse("init"))
            return
        from vinsynlib.ui.confirm import ConfirmScreen

        summary = selection_summary(selection)
        prompt = (
            f"Initialise: {summary}\n"
            f"Gate: {'ARMED' if self._gate.armed else 'LOCKED'} — press w to arm\n\n"
            "Write the selected Init contents?"
        )
        self.app.push_screen(
            ConfirmScreen(prompt, confirm="Write", cancel="Cancel", danger=True),
            lambda confirmed: self._confirmed(selection, confirmed),
        )

    def _confirmed(
        self, selection: Dict[str, Set[str]], confirmed: Optional[bool]
    ) -> None:
        if not confirmed:
            self._status("")
            return
        self.dismiss(InitBankChoice(banks=selection))

    def action_cancel(self) -> None:
        self.dismiss(None)
