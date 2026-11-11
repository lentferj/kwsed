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

"""Tests for the bank import preview screen."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from textual.widgets import DataTable, Static

from kwsed.app import KwsedApp
from kwsed.demo import DemoDevice
from kwsed.import_screen import BankImportScreen


def _fake_banks() -> list[SimpleNamespace]:
    source1 = SimpleNamespace(
        performances=[bytes([1]), bytes([2]), b""],
        patches=[bytes([3])],
        waveseq=None,
    )
    bank1 = SimpleNamespace(
        label="Bank [A]",
        scope="RAM1",
        format="sysex",
        source=source1,
        multisets=(),
        notes="",
    )
    source2 = SimpleNamespace(
        performances=[bytes(4)],
        patches=[],
        waveseq=bytes(8784),
    )
    bank2 = SimpleNamespace(
        label="Second bank",
        scope="CARD/import0",
        format="sysex",
        source=source2,
        multisets=(SimpleNamespace(),),
        notes="",
    )
    return [bank1, bank2]


async def settle(pilot: Any, times: int = 20) -> None:
    for _ in range(times):
        await pilot.pause()


@pytest.mark.asyncio
async def test_mount_rows() -> None:
    banks = _fake_banks()
    screen = BankImportScreen(path="foo.syx", banks=cast(Any, banks))
    app = KwsedApp(DemoDevice())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen)
        await settle(pilot)
        table = screen.query_one("#import-bank-table", DataTable)
        assert table.row_count == 2
        row0 = [str(c) for c in table.get_row_at(0)]
        assert "Bank" in " ".join(row0)
        assert "[" in " ".join(row0)
        assert row0[1] == "RAM1"
        assert row0[2] == "2"
        assert row0[3] == "1"
        assert row0[4] == "no"
        row1 = [str(c) for c in table.get_row_at(1)]
        assert row1[0] == "Second bank"
        assert row1[1] == "CARD/import0"
        assert row1[2] == "0"
        assert row1[3] == "0"
        assert row1[4] == "yes"


@pytest.mark.asyncio
async def test_ctrl_s_selects_second() -> None:
    banks = _fake_banks()
    screen = BankImportScreen(path="foo.syx", banks=cast(Any, banks))
    app = KwsedApp(DemoDevice())
    result: dict[str, Any] = {}

    def on_dismiss(value: Any) -> None:
        result["value"] = value

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen, callback=on_dismiss)
        await settle(pilot)
        table = screen.query_one("#import-bank-table", DataTable)
        table.move_cursor(row=1)
        await settle(pilot)
        await pilot.press("ctrl+s")
        await settle(pilot)
    assert result.get("value") == 1


@pytest.mark.asyncio
async def test_escape_cancels() -> None:
    banks = _fake_banks()
    screen = BankImportScreen(path="foo.syx", banks=cast(Any, banks))
    app = KwsedApp(DemoDevice())
    result: dict[str, Any] = {}

    def on_dismiss(value: Any) -> None:
        result["value"] = value

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen, callback=on_dismiss)
        await settle(pilot)
        await pilot.press("escape")
        await settle(pilot)
    assert result.get("value") is None


@pytest.mark.asyncio
async def test_q_cancels() -> None:
    banks = _fake_banks()
    screen = BankImportScreen(path="foo.syx", banks=cast(Any, banks))
    app = KwsedApp(DemoDevice())
    result: dict[str, Any] = {}

    def on_dismiss(value: Any) -> None:
        result["value"] = value

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen, callback=on_dismiss)
        await settle(pilot)
        await pilot.press("q")
        await settle(pilot)
    assert result.get("value") is None


@pytest.mark.asyncio
async def test_empty_stays() -> None:
    screen = BankImportScreen(path="empty.syx", banks=cast(Any, []))
    app = KwsedApp(DemoDevice())
    result: dict[str, Any] = {"value": "unset"}

    def on_dismiss(value: Any) -> None:
        result["value"] = value

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen, callback=on_dismiss)
        await settle(pilot)
        await pilot.press("ctrl+s")
        await settle(pilot)
        status = str(screen.query_one("#import-bank-status", Static).content)
    assert result.get("value") == "unset"
    assert status != ""


@pytest.mark.asyncio
async def test_enter_answers() -> None:
    banks = _fake_banks()
    screen = BankImportScreen(path="foo.syx", banks=cast(Any, banks))
    app = KwsedApp(DemoDevice())
    result: dict[str, Any] = {}

    def on_dismiss(value: Any) -> None:
        result["value"] = value

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen, callback=on_dismiss)
        await settle(pilot)
        table = screen.query_one("#import-bank-table", DataTable)
        table.move_cursor(row=1)
        await settle(pilot)
        await pilot.press("enter")
        await settle(pilot)
    assert result.get("value") == 1
