# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors

"""Choice screens used by the import flow.

The shared ``ChoiceScreen`` binds Escape but not q, and draws no legend.
Import uses q as the alternate back key everywhere else, so these screens add
both without changing the library widget.
"""

from __future__ import annotations

from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Static

from vinsynlib.ui.modals import ChoiceScreen
from vinsynlib.ui.report import ReportScreen

__all__ = ["BackChoiceScreen", "ImportMenuScreen", "TargetBankScreen"]


class BackChoiceScreen(ChoiceScreen):
    """A choice list with ``q``/Escape and a visible back legend."""

    BINDINGS: ClassVar[list[Any]] = [
        *ChoiceScreen.BINDINGS,
        Binding("q", "cancel", "Cancel"),
        Binding("question_mark", "help", "Help"),
    ]

    HINT = "↑/↓ choose · Enter select · q / Esc back · ? help"
    HINT_ID = "choice-back-hint"
    HELP_TITLE = "Choose"
    HELP_BODY = "↑ / ↓  move\nEnter  select\nq / Esc  go back one step\n?  this help"

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield Static(self.HINT, id=self.HINT_ID, markup=False)

    def action_help(self) -> None:
        self.app.push_screen(ReportScreen(self.HELP_TITLE, self.HELP_BODY))


class ImportMenuScreen(BackChoiceScreen):
    """The Import menu and the in-session source-bank chooser."""

    HINT = "↑/↓ choose · Enter select · q / Esc back · ? help"
    HINT_ID = "import-menu-hint"
    HELP_TITLE = "Import"
    HELP_BODY = (
        "Whole bank writes every slot of one source bank into a destination.\n"
        "Single / multiple performances opens the two-pane target-slot map.\n\n"
        "↑ / ↓  move\n"
        "Enter  select\n"
        "q / Esc  go back one step\n"
        "?  this help"
    )


class TargetBankScreen(BackChoiceScreen):
    """Choose one destination bank."""

    HINT = "↑/↓ choose · Enter select · q / Esc back · ? help"
    HINT_ID = "target-bank-hint"
    HELP_TITLE = "Target bank"
    HELP_BODY = (
        "Choose the destination bank for this import.\n\n"
        "↑ / ↓  move\n"
        "Enter  select\n"
        "q / Esc  go back one step\n"
        "?  this help"
    )
