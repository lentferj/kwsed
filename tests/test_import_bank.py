# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Sequence, Tuple, cast

import pytest
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from kws import banks as kws_banks
from kws import deps
from kws import records as r
from kws.perfcopy import ImportMode
from kwsed.app import KwsedApp
from kwsed.demo import DemoDevice
from kwsed.gate import EditGate
from kwsed.import_bank import (
    OVERFLOW_CARDS_ONLY,
    ImportPassState,
    WholeBankChoice,
    WholeBankImportScreen,
)
from kwsed.target_bank import ImportMenuScreen
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
async def test_plan_line_follows_mode_source_and_target() -> None:
    """One plain outcome sentence before the gate, and it tracks the plan.

    Each change of mode, target or source re-says where the material
    lands and whether a later pass is owed -- with fragments of the
    sentence, never the whole string.
    """
    banks_list = _containment_banks()
    screen = WholeBankImportScreen(path="foo.syx", banks=cast(Any, banks_list))
    app = KwsedApp(DemoDevice())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen)
        await settle(pilot)
        plan = _static_text(screen, "whole-bank-plan")
        assert "as-is into RAM 1" in plan
        assert "1 part keeps pointing at another bank (RAM 2)" in plan
        assert "nothing is left for a later pass" in plan

        await pilot.press("m")
        await settle(pilot)
        plan = _static_text(screen, "whole-bank-plan")
        assert "Make one self-contained bank in RAM 1" in plan
        assert "keep 2 performances in their own slots" in plan
        assert "1 skipped because they use another bank" in plan

        await pilot.press("m")
        await settle(pilot)
        plan = _static_text(screen, "whole-bank-plan")
        assert "2 performances packed into slots 0-1" in plan
        assert "at their own slots" not in plan

        await pilot.press("b")
        await settle(pilot)
        plan = _static_text(screen, "whole-bank-plan")
        assert "bank in RAM 2" in plan

        await pilot.press("s")
        await settle(pilot)
        await pilot.press("m")  # packed wraps back to as-is
        await settle(pilot)
        plan = _static_text(screen, "whole-bank-plan")
        # the second bank is empty of used parts
        assert "nothing points at another bank" in plan


@pytest.mark.asyncio
async def test_plan_line_names_the_card_and_remainder_per_overflow_kind() -> None:
    """A waiting split names the card and what is left, by ``o`` kind.

    Cards only and the two-bank split are the two kinds whose remainder
    waits on this screen; the sentence says which one it is, where the
    next group goes, and how much is still owed.
    """
    banks_list = _containment_banks()
    state = ImportPassState(
        session_key="cards.syx|RAM1|t",
        written={0: "Card", 1: "Card"},
        split_remaining=(2, 3, 4),
        split_target="CARD",
        split_kind=OVERFLOW_CARDS_ONLY,
        source_index=0,
    )
    screen = WholeBankImportScreen(
        path="foo.syx", banks=cast(Any, banks_list), pass_state=state
    )
    app = KwsedApp(DemoDevice())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen)
        await settle(pilot)
        plan = _static_text(screen, "whole-bank-plan")
        assert "Cards only" in plan
        assert "the card in the slot" in plan
        assert "remain for the card after" in plan
        assert "press n after switching the card" in plan

        split_state = replace(state, split_kind="split")
        screen2 = WholeBankImportScreen(
            path="foo.syx", banks=cast(Any, banks_list), pass_state=split_state
        )
        app.pop_screen()
        app.push_screen(screen2)
        await settle(pilot)
        plan2 = _static_text(screen2, "whole-bank-plan")
        assert "Self-contained split" in plan2
        assert "Cards only" not in plan2
        assert "fully self-contained" in plan2
        assert "remain for the next card" in plan2


@pytest.mark.asyncio
async def test_plan_line_reads_at_a_narrow_short_terminal() -> None:
    """60 columns and 20 rows: the promises wrap, nothing is clipped.

    The sentence, the gate state and the target label are the critical
    lines; the plan line takes the height its wrapped text needs instead
    of a fixed one, and the screen scrolls for the rest.
    """
    banks_list = _containment_banks()
    screen = WholeBankImportScreen(path="foo.syx", banks=cast(Any, banks_list))
    app = KwsedApp(DemoDevice())
    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        app.push_screen(screen)
        await settle(pilot)
        plan = screen.query_one("#whole-bank-plan", Static)
        sentence = str(plan.content)
        assert "as-is into RAM 1" in sentence
        # the whole sentence is there, and the widget grew to show it
        assert sentence.endswith(".")
        assert plan.outer_size.height >= 2
        assert "Write gate" in _static_text(screen, "whole-bank-gate")
        assert "RAM 1" in _static_text(screen, "whole-bank-target")
        container = screen.query_one("Vertical", Vertical)
        assert container.styles.overflow_y == "auto"
        await pilot.press("m")
        await settle(pilot)
        assert "keep 2 performances" in str(plan.content)


@pytest.mark.asyncio
async def test_escape_leaves_the_browser_focused() -> None:
    """esc backs out of a child modal and then out of the screen itself.

    The child menu's esc returns to the import screen with the table
    focused; the screen's own esc returns to the browser with the
    browser's table focused again.
    """
    banks_list = _containment_banks()
    screen = WholeBankImportScreen(path="foo.syx", banks=cast(Any, banks_list))
    app = KwsedApp(DemoDevice())
    async with app.run_test() as pilot:
        await pilot.pause()
        browser = app.screen.query_one("#bank-table", DataTable)
        browser.focus()
        app.push_screen(screen)
        await settle(pilot)
        assert screen.query_one("#whole-bank-table", DataTable).has_focus
        await pilot.press("o")
        await settle(pilot)
        assert isinstance(app.screen, ImportMenuScreen)
        await pilot.press("escape")
        await settle(pilot)
        current: Any = app.screen
        assert current is screen
        assert screen.query_one("#whole-bank-table", DataTable).has_focus
        await pilot.press("escape")
        await settle(pilot)
        assert app.screen is not screen
        assert browser.has_focus


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


@pytest.mark.asyncio
async def test_cards_only_choice_targets_the_physical_card() -> None:
    """Cards only dismisses with the card slot, not the selected RAM bank.

    The target selector still shows its default RAM 1: the kind has no
    RAM bank, so that selection must not leak into the choice.
    """
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
        assert screen._target_bank().id == "RAM1"
        await pilot.press("o")
        await settle(pilot)
        await pilot.press("down")
        await settle(pilot)
        await pilot.press("down")
        await settle(pilot)
        await pilot.press("enter")
        await settle(pilot)
    val = result.get("value")
    assert isinstance(val, WholeBankChoice)
    assert val.target_bank == "CARD"
    assert val.overflow_kind == OVERFLOW_CARDS_ONLY
    assert val.overflow_bank == ""


@pytest.mark.asyncio
async def test_cards_only_choice_ignores_an_explicit_ram_target() -> None:
    """Cycling ``b`` to RAM 3 first still writes the card, not RAM 3."""
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
        for _ in range(2):
            await pilot.press("b")
            await settle(pilot)
        assert screen._target_bank().id == "RAM3"
        await pilot.press("o")
        await settle(pilot)
        await pilot.press("down")
        await settle(pilot)
        await pilot.press("down")
        await settle(pilot)
        await pilot.press("enter")
        await settle(pilot)
    val = result.get("value")
    assert isinstance(val, WholeBankChoice)
    assert val.target_bank == "CARD"


@pytest.mark.asyncio
async def test_cards_only_without_a_card_bank_refuses(monkeypatch: Any) -> None:
    """No resolvable card: the screen says so and never falls back to RAM."""
    from kwsed import import_bank

    banks_list = _containment_banks()
    gate = EditGate(armed=True)
    screen = WholeBankImportScreen(
        path="foo.syx", banks=cast(Any, banks_list), gate=gate
    )
    app = KwsedApp(DemoDevice())
    result: dict[str, Any] = {}

    def on_dismiss(value: Any) -> None:
        result["value"] = value

    real_bank = kws_banks.bank

    def _no_card(which: Any) -> Any:
        if str(which) == "CARD":
            raise LookupError("no card bank")
        return real_bank(which)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen, callback=on_dismiss)
        await settle(pilot)
        monkeypatch.setattr(kws_banks, "bank", _no_card)
        await pilot.press("o")
        await settle(pilot)
        await pilot.press("down")
        await settle(pilot)
        await pilot.press("down")
        await settle(pilot)
        await pilot.press("enter")
        await settle(pilot)
        assert "value" not in result
        assert "no card bank" in _static_text(screen, "whole-bank-status")
