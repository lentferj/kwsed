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

"""Preview of a bank file: what is inside, and which bank to import.

The screen is a question, not a write. It shows the file the user picked,
a one-line summary of what was found, and one row per bank. It answers
the app with the chosen bank index (or ``None`` on cancel) and writes
nothing itself -- no cache, no unit, no worker. Importing touches the
local cache only, so there is no write gate here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Static

if TYPE_CHECKING:
    from kws.importers import ImportedBank

__all__ = ["BankImportScreen"]

#: What the bottom line says the keys do.
HINT = "ctrl+s import · esc / q cancel"

#: The note that explains there is no device write.
NOTE = "Import writes the local bank cache; nothing is sent to the unit."


def _escape(text: str) -> str:
    """Render ``text`` literally under Rich markup.

    A bank label comes from disk and may contain a square bracket; a
    table cell rendered as markup would read it as a tag. The backslash
    is Textual's own escape.
    """
    return text.replace("[", "\\[")


def _active_count(items: Sequence[Any]) -> int:
    """Count the non-empty records in ``items``."""
    total = 0
    for item in items:
        if item and any(item):
            total += 1
    return total


class BankImportScreen(ModalScreen[Optional[int]]):
    """Preview an imported file and answer the chosen bank index."""

    DEFAULT_CSS = """
    BankImportScreen { align: center middle; }
    BankImportScreen > Vertical {
        width: 92; height: 80%; max-height: 90%;
        border: thick $accent; background: $surface; padding: 1 2;
    }
    BankImportScreen .title { height: auto; text-style: bold; }
    BankImportScreen .path { height: auto; color: $text-muted; }
    BankImportScreen .summary { height: auto; }
    BankImportScreen DataTable { height: 1fr; }
    BankImportScreen .note { height: auto; color: $text-muted; }
    BankImportScreen .status { height: auto; color: $text; padding: 0 1; }
    BankImportScreen .hint { height: auto; color: $text-muted; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("q", "cancel", "Cancel"),
        Binding("enter", "confirm", "Import", priority=True),
        Binding("ctrl+s", "confirm", "Import"),
    ]

    TABLE_ID = "import-bank-table"
    PATH_ID = "import-bank-path"
    SUMMARY_ID = "import-bank-summary"
    STATUS_ID = "import-bank-status"

    def __init__(self, *, path: str, banks: Sequence["ImportedBank"]) -> None:
        super().__init__()
        self._path = path
        self._banks: list["ImportedBank"] = list(banks)

    @property
    def banks(self) -> list["ImportedBank"]:
        return self._banks

    def _summary_text(self) -> str:
        bank_count = len(self._banks)
        perfs = 0
        patches = 0
        has_ws = False
        has_ms = False
        for bank in self._banks:
            source = bank.source
            perfs += _active_count(getattr(source, "performances", ()))
            patches += _active_count(getattr(source, "patches", ()))
            if getattr(source, "waveseq", None):
                has_ws = True
            if getattr(bank, "multisets", ()):
                has_ms = True
        plural = "" if bank_count == 1 else "s"
        return (
            f"{bank_count} bank{plural} found · "
            f"{perfs} active performance(s) · "
            f"{patches} active patch(es) · "
            f"wave sequences: {'yes' if has_ws else 'no'} · "
            f"multisets: {'yes' if has_ms else 'no'}"
        )

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Import bank", classes="title")
            yield Static(self._path, id=self.PATH_ID, classes="path", markup=False)
            yield Static(
                self._summary_text(),
                id=self.SUMMARY_ID,
                classes="summary",
                markup=False,
            )
            yield DataTable(
                id=self.TABLE_ID,
                cursor_type="row",
                zebra_stripes=True,
            )
            yield Static(NOTE, classes="note", markup=False)
            yield Static("", id=self.STATUS_ID, classes="status", markup=False)
            yield Static(HINT, classes="hint", markup=False)

    def on_mount(self) -> None:
        table = self.query_one(f"#{self.TABLE_ID}", DataTable)
        table.add_column("bank", key="label")
        table.add_column("scope", key="scope")
        table.add_column("perfs", key="perfs")
        table.add_column("patches", key="patches")
        table.add_column("waveseq", key="waveseq")
        for index, bank in enumerate(self._banks):
            source = bank.source
            table.add_row(
                _escape(str(bank.label)),
                _escape(str(bank.scope)),
                str(_active_count(getattr(source, "performances", ()))),
                str(_active_count(getattr(source, "patches", ()))),
                "yes" if getattr(source, "waveseq", None) else "no",
                key=str(index),
            )
        if self._banks:
            table.focus()
        else:
            self._set_status("No banks found in this file.")

    def _set_status(self, message: str) -> None:
        try:
            self.query_one(f"#{self.STATUS_ID}", Static).update(message)
        except Exception:  # pragma: no cover - screen closed
            return

    def action_confirm(self) -> None:
        if not self._banks:
            return
        table = self.query_one(f"#{self.TABLE_ID}", DataTable)
        index = table.cursor_row
        if index is None or index < 0 or index >= len(self._banks):
            index = 0
        self.dismiss(index)

    def action_cancel(self) -> None:
        self.dismiss(None)
