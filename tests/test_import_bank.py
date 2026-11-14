# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Sequence, Tuple, cast

import pytest
from textual.widgets import DataTable, Static

from kws import deps
from kws import records as r
from kws.perfcopy import ImportMode
from kwsed.app import KwsedApp
from kwsed.demo import DemoDevice
from kwsed.gate import EditGate
from kwsed.import_bank import WholeBankChoice, WholeBankImportScreen
from vinsynlib.ui.report import ReportScreen


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


def _performance(name: str, parts: Sequence[Tuple[int, int]]) -> bytes:
    """A performance record with ``parts`` as (bank number, patch slot).

    Every name here is invented.
    """
    record = bytearray(r.PERFORMANCE_SIZE)
    record[0 : len(name)] = name.encode()
    for index in range(r.PARTS_PER_PERFORMANCE):
        r.set_performance_part(record, index, bank=0, patch=r.UNUSED_PATCH)
    for index, (bank, patch) in enumerate(parts):
        r.set_performance_part(record, index, bank=bank, patch=patch)
    return bytes(record)


def _containment_banks() -> list[SimpleNamespace]:
    """A two-bank file for the audit: two contained, one crossing to RAM 2.

    Slot 0 is self only, slot 1 is self plus ROM 6, slot 2 names RAM 2, and
    slot 3 is an unused placeholder -- not a performance and not a failure.
    """
    performances = [
        _performance("Tidal Marker", ((0, 1), (0, 2))),
        _performance("Harbour Lattice", ((0, 3), (5, 9))),
        _performance("Salt Ferry", ((1, 4),)),
        _performance("Empty Placeholder", ()),
    ]
    source1 = deps.Source(
        scope_id="RAM1",
        performances=performances,
        patches=[bytes([2]) * r.PATCH_SIZE] * 5,
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
    source2 = deps.Source(
        scope_id="CARD/import0",
        performances=[],
        patches=[],
        waveseq=bytes(r.WAVESEQ_BANK_SIZE),
    )
    bank2 = SimpleNamespace(
        label="Companion bank",
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
    assert val.mode is ImportMode.AS_IS


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


def _static_text(screen: WholeBankImportScreen, widget_id: str) -> str:
    return str(screen.query_one(f"#{widget_id}", Static).content)


@pytest.mark.asyncio
async def test_m_cycles_modes_in_order() -> None:
    banks_list = _containment_banks()
    screen = WholeBankImportScreen(path="foo.syx", banks=cast(Any, banks_list))
    app = KwsedApp(DemoDevice())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen)
        await settle(pilot)
        assert _static_text(screen, "whole-bank-mode").startswith("Mode: as-is")

        await pilot.press("m")
        await settle(pilot)
        assert _static_text(screen, "whole-bank-mode").startswith(
            "Mode: self-contained 1:1"
        )

        await pilot.press("m")
        await settle(pilot)
        assert _static_text(screen, "whole-bank-mode").startswith(
            "Mode: self-contained packed"
        )

        await pilot.press("m")
        await settle(pilot)
        assert _static_text(screen, "whole-bank-mode").startswith("Mode: as-is")


@pytest.mark.asyncio
async def test_mode_line_states_what_is_written_and_skipped() -> None:
    banks_list = _containment_banks()
    screen = WholeBankImportScreen(path="foo.syx", banks=cast(Any, banks_list))
    app = KwsedApp(DemoDevice())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen)
        await settle(pilot)

        await pilot.press("m")
        await settle(pilot)
        line = _static_text(screen, "whole-bank-mode")
        assert "writes 2 of 3 performances at their own slots" in line
        assert "skipping 1" in line
        # the status line repeats it, before the gate can be armed
        assert "writes 2 of 3 performances" in _static_text(screen, "whole-bank-status")

        await pilot.press("m")
        await settle(pilot)
        line = _static_text(screen, "whole-bank-mode")
        assert "packed into slots 0-1" in line
        assert "skipping 1" in line


@pytest.mark.asyncio
async def test_choice_carries_the_mode() -> None:
    banks_list = _containment_banks()
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
        await pilot.press("m")
        await settle(pilot)
        await pilot.press("ctrl+s")
        await settle(pilot)
    val = result.get("value")
    assert isinstance(val, WholeBankChoice)
    assert val.write_to_unit is True
    assert val.mode is ImportMode.SELF_CONTAINED_1TO1


@pytest.mark.asyncio
async def test_containment_block_for_a_two_bank_source() -> None:
    banks_list = _containment_banks()
    screen = WholeBankImportScreen(path="foo.syx", banks=cast(Any, banks_list))
    app = KwsedApp(DemoDevice())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen)
        await settle(pilot)
        summary = _static_text(screen, "whole-bank-summary")
        assert "contained: 2" in summary
        assert "other bank: 1 (RAM 2)" in summary
        assert "ROM: 1" in summary

        # The audit follows the source, not the file: the companion bank is
        # empty, so its block says so rather than keeping the first one.
        await pilot.press("s")
        await settle(pilot)
        summary = _static_text(screen, "whole-bank-summary")
        assert "contained: 0" in summary
        assert "other bank: 0" in summary


@pytest.mark.asyncio
async def test_help_names_all_three_reference_classes() -> None:
    banks_list = _containment_banks()
    screen = WholeBankImportScreen(path="foo.syx", banks=cast(Any, banks_list))
    app = KwsedApp(DemoDevice())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen)
        await settle(pilot)
        await pilot.press("question_mark")
        await settle(pilot)
        assert isinstance(app.screen, ReportScreen)
        body = app.screen.body
        assert "self-references" in body
        assert "References to other writable banks stay where they point" in body
        assert "ROM references stay in ROM" in body
