# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# kwsed is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version.

"""The empty-catalog offer and a scan's progress: two screens and no MIDI."""

from __future__ import annotations

import contextlib
import threading
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Static

__all__ = ["SCAN_OFFER", "ScanOffer", "ScanOfferScreen", "ScanScreen"]

ScanOffer = str

SCAN_OFFER = (
    "There is no name catalog yet, so the programs show as numbers.\n\n"
    "Scanning reads the names back from the unit. Every bank is read "
    "silently, one bulk message each: nothing is selected and nothing "
    "sounds. This changes nothing on the unit."
)


class ScanOfferScreen(ModalScreen[ScanOffer]):
    DEFAULT_CSS = """
    ScanOfferScreen { align: center middle; }
    ScanOfferScreen > Vertical {
        width: 72; height: auto; border: thick $accent;
        background: $surface; padding: 1 2;
    }
    ScanOfferScreen .prompt { margin-bottom: 1; }
    ScanOfferScreen > Vertical > Horizontal {
        height: auto; align: center middle;
    }
    ScanOfferScreen Button { margin: 0 1; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("y", "scan", "Scan"),
        Binding("n", "later", "Not now"),
        Binding("d", "never", "Don't ask again"),
        Binding("escape", "later", "Not now"),
    ]  # type: ignore[assignment]

    def __init__(self, prompt: str = SCAN_OFFER) -> None:
        super().__init__()
        self.prompt_text = prompt

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.prompt_text, classes="prompt")
            with Horizontal():
                yield Button("Scan", variant="primary", id="scan")
                yield Button("Not now", id="later")
                yield Button("Don't ask again", id="never")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id or "later")

    def action_scan(self) -> None:
        self.dismiss("scan")

    def action_later(self) -> None:
        self.dismiss("later")

    def action_never(self) -> None:
        self.dismiss("never")


class ScanScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    ScanScreen { align: center middle; }
    ScanScreen > Vertical {
        width: 72; height: auto; border: thick $accent;
        background: $surface; padding: 1 2;
    }
    ScanScreen .title { margin-bottom: 1; }
    ScanScreen > Vertical > Horizontal {
        height: auto; align: center middle;
    }
    ScanScreen Button { margin: 0 1; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("q", "cancel", "Cancel"),
    ]  # type: ignore[assignment]

    def __init__(self, cancel: threading.Event) -> None:
        super().__init__()
        self._cancel = cancel
        self._cancelled = False
        self._line = "starting…"

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("[b]Scanning the Wavestation[/b]", classes="title")
            yield Static(self._line, id="scan-line")
            with Horizontal():
                yield Button("Cancel", id="cancel")

    def set_progress(self, line: str) -> None:
        self._line = line
        with contextlib.suppress(NoMatches):
            self.query_one("#scan-line", Static).update(line)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        if self._cancelled:
            return
        self._cancelled = True
        self._cancel.set()
        self.set_progress("cancelling — stopping after this bank…")
        with contextlib.suppress(NoMatches):
            self.query_one("#cancel", Button).disabled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled
