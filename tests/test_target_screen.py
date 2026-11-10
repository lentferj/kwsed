# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors

"""Tests for the overwrite-target list."""

from __future__ import annotations

from typing import Any

from kws import banks
from kwsed.target_screen import TargetSlotScreen
from tests.test_bankbuild import make_performance


def _records() -> list[bytes]:
    records = [make_performance("", ()) for _ in range(banks.PERFORMANCES_PER_BANK)]
    records[0] = make_performance("Init", ((0, 0),))
    records[2] = make_performance("Kelp Forest", ((0, 0),))
    records[3] = make_performance("Amber Drift", ((0, 1),))
    return records


def test_it_lists_all_slots_and_marks_init_free() -> None:
    records = _records()
    screen = TargetSlotScreen(scope="RAM1", records=records)
    assert screen.slots == list(range(50))
    assert screen._state(records[0]) == "Init"
    assert screen._state(records[2]) == "used"


async def test_enter_answers_the_selected_slot(monkeypatch: Any) -> None:
    app = _App()
    async with app.run_test() as pilot:
        app.push_screen(TargetSlotScreen(scope="RAM1", records=_records(), current=3))
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == 3


async def test_escape_cancels() -> None:
    app = _App()
    async with app.run_test() as pilot:
        app.push_screen(TargetSlotScreen(scope="RAM1", records=_records()))
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.result is None


class _App:
    """Tiny Textual host; the screen itself owns the work."""

    def __init__(self) -> None:
        from textual.app import App

        class Host(App[Any]):
            def on_mount(self) -> None:
                pass

        self._app = Host()
        self.result: Any = "unset"

    def push_screen(self, screen: Any) -> None:
        self._app.push_screen(screen, self._done)

    def _done(self, result: Any) -> None:
        self.result = result

    def run_test(self) -> Any:
        return self._app.run_test()
