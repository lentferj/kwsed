# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from textual.widgets import DataTable, Static

from kwsed.app import KwsedApp
from kwsed.demo import DemoDevice
from kwsed.gate import EditGate
from kwsed.import_bank import WholeBankChoice, WholeBankImportScreen


def _fake_banks() -> list[SimpleNamespace]:
    source1 = SimpleNamespace(
        performances=[bytes([1]) * 181, bytes(181)],
        patches=[bytes([2]) * 426],
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
        performances=[bytes(181)],
        patches=[],
        waveseq=bytes(8784),
    )
    bank2 = SimpleNamespace(
        label="Second bank",
        scope="CARD/import0",
        format="sysex",
        source=source2,
        multisets=(),
        notes="",
    )
    return [bank1, bank2]


async def settle(pilot: Any, times: int = 20) -> None:
    for _ in range(times):
        await pilot.pause()


@pytest.mark.asyncio
async def test_mount_rows() -> None:
    banks_list = _fake_banks()
    screen = WholeBankImportScreen(path="foo.syx", banks=cast(Any, banks_list))
    app = KwsedApp(DemoDevice())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen)
        await settle(pilot)
        table = screen.query_one("#whole-bank-table", DataTable)
        assert table.row_count == 1


@pytest.mark.asyncio
async def test_s_cycles() -> None:
    banks_list = _fake_banks()
    screen = WholeBankImportScreen(path="foo.syx", banks=cast(Any, banks_list))
    app = KwsedApp(DemoDevice())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen)
        await settle(pilot)
        await pilot.press("s")
        await settle(pilot)
        summary = screen.query_one("#whole-bank-summary", Static)
        assert "waveseq yes" in str(summary.content)


@pytest.mark.asyncio
async def test_b_and_ctrl_s() -> None:
    banks_list = _fake_banks()
    gate = EditGate(armed=True)
    screen = WholeBankImportScreen(
        path="foo.syx", banks=cast(Any, banks_list), gate=gate
    )
    app = KwsedApp(DemoDevice())
    result: dict[str, Any] = {}

    def on_dismiss(value: Any) -> None:
        result["value"] = value

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen, callback=on_dismiss)
        await settle(pilot)
        await pilot.press("b")
        await settle(pilot)
        await pilot.press("ctrl+s")
        await settle(pilot)
    val = result.get("value")
    assert isinstance(val, WholeBankChoice)
    assert val.write_to_unit is True


@pytest.mark.asyncio
async def test_locked_gate() -> None:
    banks_list = _fake_banks()
    gate = EditGate(armed=False)
    screen = WholeBankImportScreen(
        path="foo.syx", banks=cast(Any, banks_list), gate=gate
    )
    app = KwsedApp(DemoDevice())
    result: dict[str, Any] = {}

    def on_dismiss(value: Any) -> None:
        result["value"] = value

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen, callback=on_dismiss)
        await settle(pilot)
        await pilot.press("ctrl+s")
        await settle(pilot)
    assert "value" not in result


@pytest.mark.asyncio
async def test_cache_and_escape() -> None:
    banks_list = _fake_banks()
    gate = EditGate(armed=False)
    screen = WholeBankImportScreen(
        path="foo.syx", banks=cast(Any, banks_list), gate=gate
    )
    app = KwsedApp(DemoDevice())
    result: dict[str, Any] = {}

    def on_dismiss(value: Any) -> None:
        result["value"] = value

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen, callback=on_dismiss)
        await settle(pilot)
        await pilot.press("c")
        await settle(pilot)
    val = result.get("value")
    assert isinstance(val, WholeBankChoice)
    assert val.write_to_unit is False

    screen2 = WholeBankImportScreen(path="foo.syx", banks=cast(Any, banks_list))
    result2: dict[str, Any] = {}

    def on_dismiss2(value: Any) -> None:
        result2["value"] = value

    app2 = KwsedApp(DemoDevice())
    async with app2.run_test() as pilot:
        await pilot.pause()
        app2.push_screen(screen2, callback=on_dismiss2)
        await settle(pilot)
        await pilot.press("escape")
        await settle(pilot)
    assert result2.get("value") is None
