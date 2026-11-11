# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#

"""Tests for the single import screen."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest
from textual.widgets import DataTable, Static

from kws import banks, deps, importers, perfcopy, relations
from kws import records as rec
from kwsed.gate import EditGate
from kwsed.import_single import SingleImportChoice, SingleImportScreen
from tests.test_bankbuild import make_performance, make_waveseq


def _host() -> Any:
    from textual.app import App

    class Host(App[Any]):
        def on_mount(self) -> None:
            pass

    host = cast(Any, Host())
    host.result = "unset"

    def done(value: Any) -> None:
        host.result = value

    host._done = done
    host.push_screen_cb = lambda screen: host.push_screen(screen, done)
    return host


async def settle(pilot: Any, times: int = 25) -> None:
    for _ in range(times):
        await pilot.pause()


def _source_bank(scope: str, active_slots: list[int]) -> importers.ImportedBank:
    perfs = [bytes(rec.PERFORMANCE_SIZE) for _ in range(50)]
    for slot in active_slots:
        perfs[slot] = make_performance(f"Active{slot}", ((0, 0),))
    patches = [bytes(rec.PATCH_SIZE) for _ in range(35)]
    from tests.test_bankbuild import make_patch

    patches[0] = make_patch()
    source = deps.Source(
        bank_id=scope,
        performances=perfs,
        patches=patches,
        waveseq=make_waveseq({0: [40]})[0],
    )
    return importers.ImportedBank(
        label=f"Bank {scope}", scope=scope, format="sysex", source=source
    )


def _make_perfs(slots: list[int]) -> list[bytes]:
    perfs = [bytes(rec.PERFORMANCE_SIZE) for _ in range(50)]
    for slot in slots:
        perfs[slot] = make_performance(f"Active{slot}", ((0, 0),))
    return perfs


def _screen(
    *,
    active_slots: list[int] | None = None,
    two_sources: bool = False,
    target_used_patches: int = 0,
    gate_armed: bool = True,
    initial_assignments: Any = None,
    initial_status: str = "",
) -> SingleImportScreen:
    if active_slots is None:
        active_slots = [0, 1]
    bank = _source_bank("RAM1", active_slots)
    banks_list = [bank]
    if two_sources:
        bank2 = _source_bank("RAM3", [2, 3])
        banks_list.append(bank2)
    target_bank = banks.bank("RAM2")
    target_perfs = [bytes(rec.PERFORMANCE_SIZE) for _ in range(50)]
    for i in range(5):
        target_perfs[i] = make_performance("Init", ((0, 0),))
    for i in range(target_used_patches):
        target_perfs[5 + i] = make_performance(f"Used{i}", ((1, i),))
    target_patches = [bytes(rec.PATCH_SIZE) for _ in range(35)]
    target_source = deps.Source(
        bank_id=target_bank.id,
        performances=target_perfs,
        patches=target_patches,
        waveseq=None,
    )
    sources: dict[str, Any] = {bank.scope: bank.source, target_bank.id: target_source}
    if two_sources:
        sources[bank2.scope] = bank2.source
    return SingleImportScreen(
        path="x.syx",
        banks=banks_list,
        sources=sources,
        target_bank=target_bank,
        target_source=target_source,
        gate=EditGate(armed=gate_armed),
        initial_assignments=initial_assignments,
        initial_status=initial_status,
    )


@pytest.mark.asyncio
async def test_mount() -> None:
    screen = _screen(active_slots=[0, 1])
    host = _host()
    async with host.run_test() as pilot:
        host.push_screen_cb(screen)
        await settle(pilot)
        src = screen.query_one("#source-table", DataTable)
        assert src.row_count == 2
        tgt = screen.query_one("#target-table", DataTable)
        assert tgt.row_count == 50
        row0 = [str(c) for c in tgt.get_row_at(0)]
        assert row0[1] == "Init"


@pytest.mark.asyncio
async def test_initial_status_is_visible_and_survives_refresh() -> None:
    screen = _screen(active_slots=[0], initial_status="warning: missing patches")
    host = _host()
    async with host.run_test() as pilot:
        host.push_screen_cb(screen)
        await settle(pilot)
        status = screen.query_one("#status", Static)
        assert "missing patches" in str(status.content)
        screen._refresh()
        await settle(pilot)
        assert "missing patches" in str(status.content)


@pytest.mark.asyncio
async def test_space_assigns_and_unassigns() -> None:
    screen = _screen(active_slots=[0, 1])
    host = _host()
    async with host.run_test() as pilot:
        host.push_screen_cb(screen)
        await settle(pilot)
        src = screen.query_one("#source-table", DataTable)
        src.move_cursor(row=0)
        await settle(pilot)
        await pilot.press("space")
        await settle(pilot)
        assert 0 in screen.assignments
        assert screen.assignments[0].number == 0
        src.move_cursor(row=0)
        await settle(pilot)
        await pilot.press("space")
        await settle(pilot)
        assert 0 not in screen.assignments
        src.move_cursor(row=1)
        await settle(pilot)
        await pilot.press("space")
        await settle(pilot)
        assert 0 in screen.assignments
        assert screen.assignments[0].number == 1


@pytest.mark.asyncio
async def test_inline_two_digits_assign_without_dialog() -> None:
    screen = _screen(active_slots=[0, 1])
    host = _host()
    async with host.run_test() as pilot:
        host.push_screen_cb(screen)
        await settle(pilot)
        src = screen.query_one("#source-table", DataTable)
        src.move_cursor(row=0)
        await settle(pilot)
        await pilot.press("0")
        await settle(pilot)
        assert screen.assignments == {}
        assert screen._slot_entry == "0"
        await pilot.press("5")
        await settle(pilot)
        assert screen.assignments.get(5) is not None
        assert screen.assignments[5].number == 0
        assert screen._slot_entry == ""
        # no SlotPrompt modal was pushed
        assert host.screen is screen


@pytest.mark.asyncio
async def test_inline_one_digit_enter_assigns() -> None:
    screen = _screen(active_slots=[0, 1])
    host = _host()
    async with host.run_test() as pilot:
        host.push_screen_cb(screen)
        await settle(pilot)
        src = screen.query_one("#source-table", DataTable)
        src.move_cursor(row=1)
        await settle(pilot)
        await pilot.press("1")
        await settle(pilot)
        assert screen.assignments == {}
        assert src.cursor_row == 1
        await pilot.press("enter")
        await settle(pilot)
        assert screen.assignments.get(1) is not None
        assert screen.assignments[1].number == 1
        assert src.cursor_row == 1


@pytest.mark.asyncio
async def test_inline_enter_keeps_a_lower_source_row_selected() -> None:
    screen = _screen(active_slots=[0, 1, 2])
    host = _host()
    async with host.run_test() as pilot:
        host.push_screen_cb(screen)
        await settle(pilot)
        src = screen.query_one("#source-table", DataTable)
        src.move_cursor(row=2)
        await settle(pilot)
        await pilot.press("0")
        await pilot.press("5")
        await settle(pilot)
        assert screen.assignments.get(5) is not None
        assert src.cursor_row == 2


@pytest.mark.asyncio
async def test_inline_escape_cancels_entry_then_screen() -> None:
    screen = _screen(active_slots=[0])
    host = _host()
    async with host.run_test() as pilot:
        host.push_screen_cb(screen)
        await settle(pilot)
        src = screen.query_one("#source-table", DataTable)
        src.move_cursor(row=0)
        await settle(pilot)
        await pilot.press("2")
        await settle(pilot)
        await pilot.press("escape")
        await settle(pilot)
        assert screen._slot_entry == ""
        assert screen.assignments == {}
        assert host.result == "unset"
        await pilot.press("escape")
        await settle(pilot)
        assert host.result is None


@pytest.mark.asyncio
async def test_target_patch_slots_column() -> None:
    screen = _screen(active_slots=[0], target_used_patches=1)
    host = _host()
    async with host.run_test() as pilot:
        host.push_screen_cb(screen)
        await settle(pilot)
        tgt = screen.query_one("#target-table", DataTable)
        # slot 5 is a used target performance with local part (1, 0)
        assert str(tgt.get_row_at(5)[3]) == "00"
        src = screen.query_one("#source-table", DataTable)
        src.move_cursor(row=0)
        await settle(pilot)
        await pilot.press("0")
        await pilot.press("6")
        await settle(pilot)
        # patch 00 is pinned by the used target performance; the source gets 01
        assert str(tgt.get_row_at(6)[3]) == "01"


@pytest.mark.asyncio
async def test_target_source_path_column_is_right_trimmed() -> None:
    screen = _screen(active_slots=[0])
    original = screen._banks[0]
    screen._banks[0] = SimpleNamespace(
        label=original.label,
        scope=original.scope,
        source=original.source,
        source_path="/a/b/WS_ALL.syx",
    )
    host = _host()
    async with host.run_test() as pilot:
        host.push_screen_cb(screen)
        await settle(pilot)
        src = screen.query_one("#source-table", DataTable)
        src.move_cursor(row=0)
        await settle(pilot)
        await pilot.press("space")
        await settle(pilot)
        tgt = screen.query_one("#target-table", DataTable)
        shown = str(tgt.get_row_at(0)[4])
        assert "WS_ALL.syx" in shown
        assert shown.startswith(" "), "the path is right-aligned"
        long_bank = screen._banks[0]
        screen._banks[0] = SimpleNamespace(
            label=long_bank.label,
            scope=long_bank.scope,
            source=long_bank.source,
            source_path="/home/me/Monotanz/Sounds/LIBRA18.SYX",
        )
        screen._refresh()
        await settle(pilot)
        shown = str(tgt.get_row_at(0)[4])
        assert shown.endswith("LIBRA18.SYX")


@pytest.mark.asyncio
async def test_direct_assign_free_and_duplicate() -> None:
    screen = _screen(active_slots=[0, 1])
    host = _host()
    async with host.run_test() as pilot:
        host.push_screen_cb(screen)
        await settle(pilot)
        screen._assign_to_slot(0, 5)
        await settle(pilot)
        assert screen.assignments.get(5) is not None
        assert screen.assignments[5].number == 0
        screen._assign_to_slot(1, 6)
        await settle(pilot)
        assert screen.assignments[6].number == 1
        before = dict(screen.assignments)
        screen._assign_to_slot(1, 5)
        await settle(pilot)
        assert screen.assignments == before


@pytest.mark.asyncio
async def test_budget_refusal() -> None:
    screen = _screen(active_slots=[0], target_used_patches=35)
    host = _host()
    async with host.run_test() as pilot:
        host.push_screen_cb(screen)
        await settle(pilot)
        src = screen.query_one("#source-table", DataTable)
        src.move_cursor(row=0)
        await settle(pilot)
        before = dict(screen.assignments)
        await pilot.press("space")
        await settle(pilot)
        assert screen.assignments == before
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert host.result == "unset"


@pytest.mark.asyncio
async def test_unassign_and_assign_all_clear() -> None:
    screen = _screen(active_slots=[0, 1, 2])
    host = _host()
    async with host.run_test() as pilot:
        host.push_screen_cb(screen)
        await settle(pilot)
        src = screen.query_one("#source-table", DataTable)
        src.move_cursor(row=0)
        await settle(pilot)
        await pilot.press("space")
        await settle(pilot)
        assert len(screen.assignments) == 1
        src.move_cursor(row=0)
        await settle(pilot)
        await pilot.press("u")
        await settle(pilot)
        assert len(screen.assignments) == 0
        await pilot.press("A")
        await settle(pilot)
        assert len(screen.assignments) >= 2
        await pilot.press("U")
        await settle(pilot)
        assert len(screen.assignments) == 0
        src.move_cursor(row=0)
        await settle(pilot)
        await pilot.press("space")
        await settle(pilot)
        assert len(screen.assignments) == 1
        src.move_cursor(row=0)
        await settle(pilot)
        await pilot.press("backspace")
        await settle(pilot)
        assert len(screen.assignments) == 0
        # delete key
        src.move_cursor(row=0)
        await settle(pilot)
        await pilot.press("space")
        await settle(pilot)
        assert len(screen.assignments) == 1
        src.move_cursor(row=0)
        await settle(pilot)
        await pilot.press("delete")
        await settle(pilot)
        assert len(screen.assignments) == 0


@pytest.mark.asyncio
async def test_cycle_source() -> None:
    screen = _screen(active_slots=[0], two_sources=True)
    host = _host()
    async with host.run_test() as pilot:
        host.push_screen_cb(screen)
        await settle(pilot)
        src = screen.query_one("#source-table", DataTable)
        src.move_cursor(row=0)
        await settle(pilot)
        await pilot.press("space")
        await settle(pilot)
        assert len(screen.assignments) >= 1
        before_scope = screen._source_scope()
        before_assignments = dict(screen.assignments)
        await pilot.press("s")
        await settle(pilot)
        assert screen._source_scope() != before_scope
        # A source-bank switch keeps work already assigned from the first bank.
        assert screen.assignments == before_assignments
        src2 = screen.query_one("#source-table", DataTable)
        assert src2.row_count >= 1
        tgt = screen.query_one("#target-table", DataTable)
        assert tgt.row_count == 50


@pytest.mark.asyncio
async def test_source_request_keeps_assignments() -> None:
    screen = _screen(active_slots=[0, 1], two_sources=True)
    calls: list[SingleImportScreen] = []
    screen._on_source_request = calls.append
    host = _host()
    async with host.run_test() as pilot:
        host.push_screen_cb(screen)
        await settle(pilot)
        src = screen.query_one("#source-table", DataTable)
        src.move_cursor(row=0)
        await settle(pilot)
        await pilot.press("space")
        await settle(pilot)
        before = dict(screen.assignments)
        await pilot.press("s")
        await settle(pilot)
        assert calls == [screen]
        assert screen.assignments == before
        assert host.screen is screen


@pytest.mark.asyncio
async def test_add_source_bank_keeps_assignments_and_carries_sources() -> None:
    screen = _screen(active_slots=[0, 1])
    host = _host()
    async with host.run_test() as pilot:
        host.push_screen_cb(screen)
        await settle(pilot)
        src = screen.query_one("#source-table", DataTable)
        src.move_cursor(row=0)
        await settle(pilot)
        await pilot.press("space")
        await settle(pilot)
        before = dict(screen.assignments)
        extra = _source_bank("RAM1#2", [3])
        extra = replace(
            extra,
            source=deps.Source(
                bank_id="RAM1#2",
                performances=extra.source.performances,
                patches=extra.source.patches,
                waveseq=extra.source.waveseq,
                bank_number=0,
            ),
        )
        screen.add_source_bank(extra, extra.source)
        await settle(pilot)
        assert len(screen.source_banks) == 2
        assert screen.source_index == 1
        assert screen.assignments == before
        # Assign the new source to the other free slot.
        screen._assign_to_slot(3, 1)
        await settle(pilot)
        assert screen.assignments[1].scope_id == "RAM1#2"
        assert "RAM1#2" in screen.sources
        await pilot.press("ctrl+s")
        await settle(pilot)
        result = host.result
        assert isinstance(result, SingleImportChoice)
        assert "RAM1#2" in result.sources


@pytest.mark.asyncio
async def test_gate_arming_shows_ctrl_s_and_does_not_duplicate_line() -> None:
    screen = _screen(active_slots=[0], gate_armed=False)
    host = _host()
    async with host.run_test() as pilot:
        host.push_screen_cb(screen)
        await settle(pilot)
        await pilot.press("w")
        await settle(pilot)
        gate = str(screen.query_one("#gate", Static).content)
        status = str(screen.query_one("#status", Static).content)
        assert "ARMED" in gate
        assert "ctrl+s writes" in gate
        assert "ctrl+s" in status
        assert gate != status


@pytest.mark.asyncio
async def test_gate_and_empty() -> None:
    screen = _screen(active_slots=[0], gate_armed=False)
    host = _host()
    async with host.run_test() as pilot:
        host.push_screen_cb(screen)
        await settle(pilot)
        src = screen.query_one("#source-table", DataTable)
        src.move_cursor(row=0)
        await settle(pilot)
        await pilot.press("space")
        await settle(pilot)
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert host.result == "unset"
        assert "locked" in screen._status_message.lower()
    screen2 = _screen(active_slots=[0], gate_armed=True)
    host2 = _host()
    async with host2.run_test() as pilot:
        host2.push_screen_cb(screen2)
        await settle(pilot)
        src = screen2.query_one("#source-table", DataTable)
        src.move_cursor(row=0)
        await settle(pilot)
        await pilot.press("space")
        await settle(pilot)
        expected = dict(screen2.assignments)
        await pilot.press("ctrl+s")
        await settle(pilot)
        res = host2.result
        assert isinstance(res, SingleImportChoice)
        assert res.target_bank == screen2._target_bank.id
        assert res.assignments == expected
    screen3 = _screen(active_slots=[0], gate_armed=True)
    host3 = _host()
    async with host3.run_test() as pilot:
        host3.push_screen_cb(screen3)
        await settle(pilot)
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert not isinstance(host3.result, SingleImportChoice)


@pytest.mark.asyncio
async def test_escape_q() -> None:
    screen = _screen(active_slots=[0])
    host = _host()
    async with host.run_test() as pilot:
        host.push_screen_cb(screen)
        await settle(pilot)
        await pilot.press("escape")
        await settle(pilot)
        assert host.result is None
    screen2 = _screen(active_slots=[0])
    host2 = _host()
    async with host2.run_test() as pilot:
        host2.push_screen_cb(screen2)
        await settle(pilot)
        await pilot.press("q")
        await settle(pilot)
        assert host2.result is None


@pytest.mark.asyncio
async def test_no_active_performances() -> None:
    screen = _screen(active_slots=[])
    host = _host()
    async with host.run_test() as pilot:
        host.push_screen_cb(screen)
        await settle(pilot)
        src = screen.query_one("#source-table", DataTable)
        assert src.row_count == 0
        await pilot.press("space")
        await settle(pilot)
        assert len(screen.assignments) == 0
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert not isinstance(host.result, SingleImportChoice)


@pytest.mark.asyncio
async def test_initial_assignments_immediate_and_ctrls() -> None:
    screen = _screen(
        active_slots=[0, 1],
        initial_assignments={0: relations.PerfId("RAM1", 0)},
    )
    host = _host()
    async with host.run_test() as pilot:
        host.push_screen_cb(screen)
        await settle(pilot)
        assert screen.assignments == {0: relations.PerfId("RAM1", 0)}
        tgt = screen.query_one("#target-table", DataTable)
        row0 = [str(c) for c in tgt.get_row_at(0)]
        assert row0[1] == "← 00"
        budget = screen.query_one("#budget", Static)
        assert "Assigned: 1" in str(budget.content)
        await pilot.press("ctrl+s")
        await settle(pilot)
        result = host.result
        assert isinstance(result, SingleImportChoice)
        assert result.assignments == {0: relations.PerfId("RAM1", 0)}


@pytest.mark.asyncio
async def test_source_origin_ram2_displays() -> None:
    screen = _screen(active_slots=[0])
    original = screen._banks[0]
    screen._banks[0] = SimpleNamespace(
        label=original.label,
        scope=original.scope,
        source=original.source,
        source_origin="RAM 2",
    )
    host = _host()
    async with host.run_test() as pilot:
        host.push_screen_cb(screen)
        await settle(pilot)
        src = screen.query_one("#source-table", DataTable)
        src.move_cursor(row=0)
        await settle(pilot)
        await pilot.press("space")
        await settle(pilot)
        tgt = screen.query_one("#target-table", DataTable)
        shown = str(tgt.get_row_at(0)[4])
        assert shown.strip() == "RAM 2"


@pytest.mark.asyncio
async def test_source_origin_rom7_and_long_path() -> None:
    screen = _screen(active_slots=[0])
    original = screen._banks[0]
    screen._banks[0] = SimpleNamespace(
        label=original.label,
        scope=original.scope,
        source=original.source,
        source_origin="ROM 7",
    )
    host = _host()
    async with host.run_test() as pilot:
        host.push_screen_cb(screen)
        await settle(pilot)
        src = screen.query_one("#source-table", DataTable)
        src.move_cursor(row=0)
        await settle(pilot)
        await pilot.press("space")
        await settle(pilot)
        tgt = screen.query_one("#target-table", DataTable)
        shown = str(tgt.get_row_at(0)[4])
        assert shown.strip() == "ROM 7"
        long_bank = screen._banks[0]
        screen._banks[0] = SimpleNamespace(
            label=long_bank.label,
            scope=long_bank.scope,
            source=long_bank.source,
            source_path="/home/me/Monotanz/Sounds/LIBRA18.SYX",
        )
        screen._refresh()
        await settle(pilot)
        shown = str(tgt.get_row_at(0)[4])
        assert shown.endswith("LIBRA18.SYX")


@pytest.mark.asyncio
async def test_initial_assignments_survive_add_and_switch() -> None:
    screen = _screen(
        active_slots=[0, 1],
        initial_assignments={0: relations.PerfId("RAM1", 0)},
    )
    host = _host()
    async with host.run_test() as pilot:
        host.push_screen_cb(screen)
        await settle(pilot)
        before = dict(screen.assignments)
        extra = _source_bank("RAM3", [2, 3])
        screen.add_source_bank(extra, extra.source)
        await settle(pilot)
        assert screen.assignments == before
        screen.select_source(0)
        await settle(pilot)
        assert screen.assignments == before
        screen.select_source(1)
        await settle(pilot)
        assert screen.assignments == before
