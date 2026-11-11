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

"""Pick a destination performance slot.

The copy screen offers three ways to place a performance: append to the first
free performance slot, replace the whole bank, or overwrite one named slot.
This modal lists the destination bank's 50 slots, marks free/``Init`` slots as
available, and answers one slot number.  The copy screen decides whether the
answer is an append (only the first free slot is exact) or an overwrite.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Label, Static

from kws import banks, dumps, perfcopy

__all__ = ["TargetSlotScreen"]


def _literal(text: str) -> str:
    return text.replace("[", "\\[")


class TargetSlotScreen(ModalScreen[Optional[int]]):
    """List all slots; ``enter`` answers the selected slot number."""

    DEFAULT_CSS = """
    TargetSlotScreen { align: center middle; }
    TargetSlotScreen > Vertical {
        width: 82; height: 82%; max-height: 90%;
        border: thick $accent; background: $surface; padding: 1 2;
    }
    TargetSlotScreen .title { height: auto; text-style: bold; }
    TargetSlotScreen .hint { height: auto; color: $text-muted; }
    TargetSlotScreen DataTable { height: 1fr; }
    TargetSlotScreen .note { height: auto; color: $text-muted; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("q", "cancel", "Cancel"),
        Binding("enter", "confirm", "Overwrite", priority=True),
    ]

    TABLE_ID = "target-table"

    def __init__(
        self,
        *,
        scope: str,
        records: Sequence[bytes],
        current: int = 0,
    ) -> None:
        super().__init__()
        self._scope = scope
        self._slots = list(enumerate(records))
        self._current = current

    @property
    def slots(self) -> List[int]:
        return [number for number, _record in self._slots]

    def _slot_label(self) -> str:
        try:
            return banks.bank(self._scope).label
        except LookupError:
            return self._scope

    def _state(self, record: bytes) -> str:
        if perfcopy.is_empty_performance(record):
            name = dumps.record_name(record).strip().lower()
            return "Init" if name == "init" else "free"
        return "used"

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(
                f"Choose a target performance in {self._slot_label()}",
                classes="title",
            )
            yield Static(
                "Free and Init slots are available to append; an occupied "
                "slot is overwritten when selected. Only the first free slot "
                "is exact for append.",
                classes="hint",
                markup=False,
            )
            yield DataTable(
                id=self.TABLE_ID,
                cursor_type="row",
                zebra_stripes=True,
            )
            yield Static(
                "enter overwrite · esc / q cancel",
                classes="hint",
                markup=False,
            )

    def on_mount(self) -> None:
        table = self.query_one(f"#{self.TABLE_ID}", DataTable)
        table.add_column("#", key="number", width=4)
        table.add_column("state", key="state", width=6)
        table.add_column("name", key="name")
        for number, record in self._slots:
            table.add_row(
                f"{number:02d}",
                self._state(record),
                _literal(dumps.record_name(record)),
            )
        if self._slots:
            index = next(
                (
                    i
                    for i, (number, _record) in enumerate(self._slots)
                    if number == self._current
                ),
                0,
            )
            table.move_cursor(row=index)
            table.focus()

    def action_confirm(self) -> None:
        if not self._slots:
            return
        table = self.query_one(f"#{self.TABLE_ID}", DataTable)
        row = table.cursor_row
        if row is None or not 0 <= row < len(self._slots):
            return
        self.dismiss(self._slots[row][0])

    def action_cancel(self) -> None:
        self.dismiss(None)
