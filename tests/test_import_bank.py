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
from kwsed import import_intent
from kwsed.app import KwsedApp
from kwsed.demo import DemoDevice
from kwsed.gate import EditGate
from kwsed.import_bank import (
    OVERFLOW_CARDS_ONLY,
    ImportPassState,
    WholeBankChoice,
    WholeBankImportScreen,
)
from kwsed.import_intent import approval_matches, build_intent
from kwsed.target_bank import ImportMenuScreen
from tests.test_bankbuild import make_patch, make_performance
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


def _overflow_file_banks() -> list[SimpleNamespace]:
    """A two-bank file: the main bank's second performance names RAM 2.

    Slot 0 uses its own bank's patch 0; slot 1 names RAM 2's patch 0,
    which the file carries. Every name here is invented.
    """
    performances = [bytes(r.PERFORMANCE_SIZE)] * 8
    performances[0] = make_performance("Harbour Belle", ((0, 0),))
    performances[1] = make_performance("Crossing Deck", ((1, 0),))
    source1 = deps.Source(
        scope_id="RAM1",
        performances=performances,
        patches=[make_patch("Ashgrove Tone"), make_patch("Inlet Basis")],
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
        scope_id="RAM2",
        performances=[],
        patches=[make_patch("Quay Slate")],
        waveseq=None,
    )
    bank2 = SimpleNamespace(
        label="Companion bank",
        scope="RAM2",
        format="sysex",
        source=source2,
        multisets=(),
        notes="",
    )
    return [bank1, bank2]


def _file_sources(banks_list: Sequence[Any]) -> dict[str, deps.Source]:
    """The file's banks as the planners see them, keyed by scope."""
    out: dict[str, deps.Source] = {}
    for entry in banks_list:
        out[str(entry.scope)] = cast(deps.Source, entry.source)
    return out


def _rebuild_intent(
    banks_list: Sequence[Any], choice: WholeBankChoice, *, pass_slots: Any = ()
) -> Any:
    """The intent the shared builder produces for a dismissed choice.

    The same inputs the screen minted the approval from: the P3 contract
    is that the app rebuilds exactly this and compares fingerprints.
    """
    scope = str(banks_list[choice.source_index].scope)
    return build_intent(
        source_id=f"{CHOICE_PATH}|{scope}",
        source_scope=scope,
        sources=_file_sources(banks_list),
        source=banks_list[choice.source_index].source,
        target=kws_banks.bank(choice.target_bank),
        mode=choice.mode,
        overflow_kind=choice.overflow_kind,
        overflow_bank=choice.overflow_bank,
        preflight_answer=choice.preflight,
        session_id=choice.session_key,
        pass_slots=tuple(int(slot) for slot in pass_slots),
        companion_only=choice.companion_only,
    )


CHOICE_PATH = "choice.syx"


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


# ---------------------------------------------------------------------------
# P2: the screen finalizes the plan and mints a one-shot approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ctrl_s_mints_an_approval_for_the_dismissed_plan() -> None:
    """ctrl+s dismisses with a choice whose approval names exactly its plan.

    The approval is bound to the intent the shared builder produces for
    the same choice; a different plan does not match it.
    """
    banks_list = _fake_banks()
    gate = EditGate(armed=True)
    screen = WholeBankImportScreen(
        path=CHOICE_PATH, banks=cast(Any, banks_list), gate=gate
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
    choice = result.get("value")
    assert isinstance(choice, WholeBankChoice)
    assert isinstance(choice.approval, import_intent.PlanApproval)
    intent = _rebuild_intent(banks_list, choice)
    assert approval_matches(choice.approval, intent)
    other = WholeBankChoice(0, "RAM2", True, ImportMode.AS_IS, choice.session_key, True)
    assert choice.approval.fingerprint != import_intent.fingerprint(
        _rebuild_intent(banks_list, other)
    )


@pytest.mark.asyncio
async def test_overflow_patches_kind_mints_an_approval() -> None:
    """``o`` → patches names its bank, and the dismissal carries a token."""
    banks_list = _overflow_file_banks()
    gate = EditGate(armed=True)
    screen = WholeBankImportScreen(
        path=CHOICE_PATH, banks=cast(Any, banks_list), gate=gate
    )
    app = KwsedApp(DemoDevice())
    result: dict[str, Any] = {}

    def on_dismiss(value: Any) -> None:
        result["value"] = value

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen, callback=on_dismiss)
        await settle(pilot)
        await pilot.press("o")
        await settle(pilot)
        await pilot.press("enter")  # the patches-only kind
        await settle(pilot)
        await pilot.press("enter")  # the first overflow bank: RAM 2
        await settle(pilot)
    choice = result.get("value")
    assert isinstance(choice, WholeBankChoice)
    assert choice.overflow_kind == "patches"
    assert choice.overflow_bank == "RAM2"
    assert isinstance(choice.approval, import_intent.PlanApproval)
    intent = _rebuild_intent(banks_list, choice)
    assert approval_matches(choice.approval, intent)
    # The overflow bank is written first, then the target.
    assert [write.bank_id for write in intent.writes] == ["RAM2", "RAM1"]


@pytest.mark.asyncio
async def test_overflow_self_contained_kind_mints_an_approval() -> None:
    """``o`` → self-contained: target first, overflow second, token minted."""
    banks_list = _overflow_file_banks()
    gate = EditGate(armed=True)
    screen = WholeBankImportScreen(
        path=CHOICE_PATH, banks=cast(Any, banks_list), gate=gate
    )
    app = KwsedApp(DemoDevice())
    result: dict[str, Any] = {}

    def on_dismiss(value: Any) -> None:
        result["value"] = value

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen, callback=on_dismiss)
        await settle(pilot)
        await pilot.press("o")
        await settle(pilot)
        await pilot.press("down")
        await settle(pilot)
        await pilot.press("enter")  # the self-contained kind
        await settle(pilot)
        await pilot.press("enter")  # the overflow bank: RAM 2
        await settle(pilot)
    choice = result.get("value")
    assert isinstance(choice, WholeBankChoice)
    assert choice.overflow_kind == "self_contained"
    assert choice.overflow_bank == "RAM2"
    assert isinstance(choice.approval, import_intent.PlanApproval)
    intent = _rebuild_intent(banks_list, choice)
    assert approval_matches(choice.approval, intent)
    # The contained performance goes to the target, the crossing one to
    # the overflow bank, target first.
    assert [write.bank_id for write in intent.writes] == ["RAM1", "RAM2"]
    assert intent.writes[0].source_slots == (0,)
    assert intent.writes[1].source_slots == (1,)


@pytest.mark.asyncio
async def test_overflow_cards_only_mints_an_approval() -> None:
    """``o`` → cards only dismisses with the card slot and a token."""
    banks_list = _overflow_file_banks()
    gate = EditGate(armed=True)
    screen = WholeBankImportScreen(
        path=CHOICE_PATH, banks=cast(Any, banks_list), gate=gate
    )
    app = KwsedApp(DemoDevice())
    result: dict[str, Any] = {}

    def on_dismiss(value: Any) -> None:
        result["value"] = value

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen, callback=on_dismiss)
        await settle(pilot)
        await pilot.press("o")
        await settle(pilot)
        await pilot.press("down")
        await settle(pilot)
        await pilot.press("down")
        await settle(pilot)
        await pilot.press("enter")  # cards only: no bank step
        await settle(pilot)
    choice = result.get("value")
    assert isinstance(choice, WholeBankChoice)
    assert choice.overflow_kind == OVERFLOW_CARDS_ONLY
    assert choice.target_bank == "CARD"
    assert choice.overflow_bank == ""
    assert isinstance(choice.approval, import_intent.PlanApproval)
    intent = _rebuild_intent(banks_list, choice)
    assert approval_matches(choice.approval, intent)
    assert [write.bank_id for write in intent.writes] == ["CARD"]


@pytest.mark.asyncio
async def test_next_pass_mints_an_approval_for_its_own_slots() -> None:
    """``n`` writes one pass and mints a token naming that pass's slots."""
    banks_list = _containment_banks()
    gate = EditGate(armed=True)
    screen = WholeBankImportScreen(
        path=CHOICE_PATH, banks=cast(Any, banks_list), gate=gate
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
        await pilot.press("m")  # self-contained packed
        await settle(pilot)
        await pilot.press("n")
        await settle(pilot)
        await pilot.press("enter")  # the first target offered
        await settle(pilot)
    choice = result.get("value")
    assert isinstance(choice, WholeBankChoice)
    assert choice.continue_pass is True
    assert isinstance(choice.approval, import_intent.PlanApproval)
    intent = _rebuild_intent(
        banks_list, choice, pass_slots=tuple(screen._remaining_slots())
    )
    assert approval_matches(choice.approval, intent)
    assert intent.pass_slots == (0, 1)


@pytest.mark.asyncio
async def test_split_continuation_mints_an_approval_for_the_remainder() -> None:
    """``n`` on a split session writes the next group and mints its token."""
    banks_list = _overflow_file_banks()
    state = ImportPassState(
        session_key="split.syx|RAM1|t",
        written={0: "Card"},
        split_remaining=(2,),
        split_target="CARD",
        split_kind=OVERFLOW_CARDS_ONLY,
        source_index=0,
    )
    gate = EditGate(armed=True)
    screen = WholeBankImportScreen(
        path=CHOICE_PATH, banks=cast(Any, banks_list), gate=gate, pass_state=state
    )
    app = KwsedApp(DemoDevice())
    result: dict[str, Any] = {}

    def on_dismiss(value: Any) -> None:
        result["value"] = value

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen, callback=on_dismiss)
        await settle(pilot)
        await pilot.press("n")
        await settle(pilot)
    choice = result.get("value")
    assert isinstance(choice, WholeBankChoice)
    assert choice.target_bank == "CARD"
    assert choice.session_key == "split.syx|RAM1|t"
    assert isinstance(choice.approval, import_intent.PlanApproval)
    intent = _rebuild_intent(banks_list, choice, pass_slots=(2,))
    assert approval_matches(choice.approval, intent)
    assert intent.pass_slots == (2,)
    assert [write.bank_id for write in intent.writes] == ["CARD"]


@pytest.mark.asyncio
async def test_companion_mints_an_approval_for_its_own_bank() -> None:
    """``x`` writes one companion bank and mints a token for it alone."""
    banks_list = _overflow_file_banks()
    gate = EditGate(armed=True)
    screen = WholeBankImportScreen(
        path=CHOICE_PATH, banks=cast(Any, banks_list), gate=gate
    )
    app = KwsedApp(DemoDevice())
    result: dict[str, Any] = {}

    def on_dismiss(value: Any) -> None:
        result["value"] = value

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen, callback=on_dismiss)
        await settle(pilot)
        await pilot.press("x")
        await settle(pilot)
        await pilot.press("enter")  # the file's RAM 2 companion
        await settle(pilot)
        await pilot.press("enter")  # the suggested spill target
        await settle(pilot)
    choice = result.get("value")
    assert isinstance(choice, WholeBankChoice)
    assert choice.source_index == 1
    assert choice.companion_only is True
    assert isinstance(choice.approval, import_intent.PlanApproval)
    intent = _rebuild_intent(banks_list, choice)
    assert approval_matches(choice.approval, intent)
    # Patches travel; the companion has no wave bank, and no performance
    # is placed.
    assert [write.kinds for write in intent.writes] == [("patches",)]
    assert intent.writes[0].source_slots == ()


@pytest.mark.asyncio
async def test_cache_only_mints_an_approval_without_the_gate() -> None:
    """``c`` caches only; it still carries the plan it was taken under."""
    banks_list = _fake_banks()
    gate = EditGate(armed=False)
    screen = WholeBankImportScreen(
        path=CHOICE_PATH, banks=cast(Any, banks_list), gate=gate
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
    choice = result.get("value")
    assert isinstance(choice, WholeBankChoice)
    assert choice.write_to_unit is False
    assert isinstance(choice.approval, import_intent.PlanApproval)
    assert approval_matches(choice.approval, _rebuild_intent(banks_list, choice))


@pytest.mark.asyncio
async def test_a_locked_gate_mints_nothing() -> None:
    """A locked gate refuses both ctrl+s and the ``o`` flow, tokenless."""
    banks_list = _overflow_file_banks()
    gate = EditGate(armed=False)
    screen = WholeBankImportScreen(
        path=CHOICE_PATH, banks=cast(Any, banks_list), gate=gate
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
        await pilot.press("o")
        await settle(pilot)
        await pilot.press("down")
        await settle(pilot)
        await pilot.press("down")
        await settle(pilot)
        await pilot.press("enter")  # cards only
        await settle(pilot)
        assert "value" not in result
        assert "write gate is locked" in _static_text(screen, "whole-bank-status")
    assert result.get("value") is None


@pytest.mark.asyncio
async def test_change_after_arming_disarms_and_rearms_for_the_new_plan() -> None:
    """``b`` under an armed gate locks it; the next dismissal mints anew.

    The new token belongs to the new plan and matches nothing else.
    """
    banks_list = _fake_banks()
    gate = EditGate(armed=False)
    screen = WholeBankImportScreen(
        path=CHOICE_PATH, banks=cast(Any, banks_list), gate=gate
    )
    app = KwsedApp(DemoDevice())
    result: dict[str, Any] = {}

    def on_dismiss(value: Any) -> None:
        result["value"] = value

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen, callback=on_dismiss)
        await settle(pilot)
        await pilot.press("w")
        await settle(pilot)
        assert gate.armed
        assert screen._armed_fingerprint == screen._plan_fingerprint()
        await pilot.press("b")
        await settle(pilot)
        assert not gate.armed
        await pilot.press("w")
        await settle(pilot)
        assert gate.armed
        await pilot.press("ctrl+s")
        await settle(pilot)
    choice = result.get("value")
    assert isinstance(choice, WholeBankChoice)
    assert choice.target_bank == "RAM2"
    assert isinstance(choice.approval, import_intent.PlanApproval)
    new_plan = _rebuild_intent(banks_list, choice)
    assert approval_matches(choice.approval, new_plan)
    old_target = WholeBankChoice(
        0, "RAM1", True, ImportMode.AS_IS, choice.session_key, True
    )
    assert choice.approval.fingerprint != import_intent.fingerprint(
        _rebuild_intent(banks_list, old_target)
    )


@pytest.mark.asyncio
async def test_an_inherited_gate_alone_mints_nothing() -> None:
    """A gate armed before the screen opened approves no plan by itself.

    No key is pressed: nothing is dismissed, no token is minted, and the
    screen holds no plan fingerprint for the inherited gate.
    """
    banks_list = _fake_banks()
    app = KwsedApp(DemoDevice(), allow_write=True)
    result: dict[str, Any] = {}

    def on_dismiss(value: Any) -> None:
        result["value"] = value

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = WholeBankImportScreen(
            path=CHOICE_PATH,
            banks=cast(Any, banks_list),
            gate=app._write_gate,
        )
        app.push_screen(screen, callback=on_dismiss)
        await settle(pilot)
        assert app._write_gate.armed
        assert "ARMED" in _static_text(screen, "whole-bank-gate")
        assert screen._armed_fingerprint is None
        assert screen._pending_overflow is None
    assert result.get("value") is None


def test_a_positional_choice_defaults_to_no_approval() -> None:
    """Older call sites construct the choice positionally; no token."""
    choice = WholeBankChoice(0, "RAM1", True)
    assert choice.approval is None
    assert choice.preflight == ""


@pytest.mark.asyncio
async def test_the_pending_overflow_is_named_before_it_is_approved() -> None:
    """The plan line names the banks and kinds the token would cover."""
    banks_list = _overflow_file_banks()
    screen = WholeBankImportScreen(
        path=CHOICE_PATH, banks=cast(Any, banks_list), gate=EditGate(armed=False)
    )
    app = KwsedApp(DemoDevice())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen)
        await settle(pilot)
        screen._pending_overflow = ("patches", "RAM2")
        statement = screen._overflow_statement()
        assert "RAM 2" in statement
        assert "patches" in statement
        assert "RAM 1" in statement  # the target
        assert "first" in statement  # the order is said
        screen._pending_overflow = (OVERFLOW_CARDS_ONLY, "")
        cards_statement = screen._overflow_statement()
        assert "card in the slot" in cards_statement
        assert "no RAM bank" in cards_statement


@pytest.mark.asyncio
async def test_arming_after_choosing_approves_the_chosen_plan() -> None:
    """A blocked ``o`` choice waits; arming then ctrl+s dismisses with it.

    The pending choice is part of the fingerprint from the moment it is
    chosen, so arming after choosing sees the same plan the dismissal
    carries.
    """
    banks_list = _overflow_file_banks()
    gate = EditGate(armed=False)
    screen = WholeBankImportScreen(
        path=CHOICE_PATH, banks=cast(Any, banks_list), gate=gate
    )
    app = KwsedApp(DemoDevice())
    result: dict[str, Any] = {}

    def on_dismiss(value: Any) -> None:
        result["value"] = value

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen, callback=on_dismiss)
        await settle(pilot)
        await pilot.press("o")
        await settle(pilot)
        await pilot.press("down")
        await settle(pilot)
        await pilot.press("down")
        await settle(pilot)
        await pilot.press("enter")  # cards only, gate locked
        await settle(pilot)
        assert "value" not in result
        assert screen._pending_overflow == (OVERFLOW_CARDS_ONLY, "")
        assert "cards only" in _static_text(screen, "whole-bank-status")
        await pilot.press("w")
        await settle(pilot)
        assert gate.armed
        assert screen._armed_fingerprint == screen._plan_fingerprint()
        await pilot.press("ctrl+s")
        await settle(pilot)
    choice = result.get("value")
    assert isinstance(choice, WholeBankChoice)
    assert choice.overflow_kind == OVERFLOW_CARDS_ONLY
    assert choice.target_bank == "CARD"
    assert isinstance(choice.approval, import_intent.PlanApproval)
    assert approval_matches(choice.approval, _rebuild_intent(banks_list, choice))
