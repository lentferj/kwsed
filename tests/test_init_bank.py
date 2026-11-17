# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# kwsed is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version.

"""Init Bank: menu and fenced writes (synthetic, no MIDI)."""

from __future__ import annotations

from typing import Any, cast

import pytest
from textual.widgets import OptionList, Static

from kws import banks, deps, initbank
from kws import records as rec
from kwsed.app import KwsedApp
from kwsed.demo import DemoDevice
from kwsed.init_bank import InitBankScreen, selection_summary
from vinsynlib.ui.confirm import ConfirmScreen

pytestmark = pytest.mark.asyncio


def _static_text(screen: Any, selector: str) -> str:
    widget = screen.query_one(selector, Static)
    content = getattr(widget, "content", None)
    if content is not None:
        return str(content)
    renderable = getattr(widget, "renderable", None)
    return str(renderable) if renderable is not None else str(widget.render())


async def _settle(pilot: Any, times: int = 8) -> None:
    for _ in range(times):
        await pilot.pause()


def _make_source(kind: str, token: bytes) -> deps.Source:
    perf = (token * rec.PERFORMANCE_SIZE)[: rec.PERFORMANCE_SIZE]
    patch = (token * rec.PATCH_SIZE)[: rec.PATCH_SIZE]
    seq = (token * rec.WAVESEQ_BANK_SIZE)[: rec.WAVESEQ_BANK_SIZE]
    return deps.Source(
        scope_id=kind,
        performances=[perf] * banks.PERFORMANCES_PER_BANK,
        patches=[patch] * banks.PATCHES_PER_BANK,
        waveseq=seq,
    )


class TestInitBankScreen:
    async def test_renders_three_banks_and_kinds_card_absent(self) -> None:
        app = KwsedApp(DemoDevice())
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(InitBankScreen(gate=app._write_gate))
            await _settle(pilot)
            screen = app.screen
            assert isinstance(screen, InitBankScreen)
            options = screen.query_one("#init-list", OptionList)
            assert options.option_count == 9
            texts = [str(options.get_option_at_index(i).prompt) for i in range(9)]
            joined = "\n".join(texts)
            for label in ("RAM 1", "RAM 2", "RAM 3"):
                assert label in joined
            for kind in ("Performances", "Patches", "Wave sequences"):
                assert joined.count(kind) == 3
            assert "Card" not in _static_text(screen, "#init-title")
            note = _static_text(screen, "#init-card-note")
            assert "Card is not listed" in note
            assert "FORMAT CARD" in note

    async def test_space_toggles_and_esc_cancels(self) -> None:
        app = KwsedApp(DemoDevice())
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(InitBankScreen(gate=app._write_gate))
            await _settle(pilot)
            screen = app.screen
            # RAM 1 Performances
            await pilot.press("space")
            await _settle(pilot)
            assert cast(Any, screen)._selection()["RAM1"] == {"performances"}
            await pilot.press("space")
            await _settle(pilot)
            assert cast(Any, screen)._selection() == {}
            await pilot.press("space")
            await _settle(pilot)
            assert "RAM1" in cast(Any, screen)._selection()
            await pilot.press("escape")
            await _settle(pilot)
            assert not isinstance(app.screen, InitBankScreen)
            assert app.device.bundle_write_count == 0

    async def test_enter_with_nothing_ticked_writes_nothing(self) -> None:
        app = KwsedApp(DemoDevice())
        async with app.run_test() as pilot:
            await pilot.pause()
            app._write_gate.armed = True
            app.push_screen(InitBankScreen(gate=app._write_gate))
            await _settle(pilot)
            await pilot.press("enter")
            await _settle(pilot)
            assert isinstance(app.screen, InitBankScreen)
            status = _static_text(app.screen, "#init-status")
            assert "nothing" in status.lower()
            assert app.device.bundle_write_count == 0

    async def test_locked_gate_refuses(self) -> None:
        app = KwsedApp(DemoDevice())
        async with app.run_test() as pilot:
            await pilot.pause()
            app._write_gate.armed = False
            app.push_screen(InitBankScreen(gate=app._write_gate))
            await _settle(pilot)
            await pilot.press("space")  # tick RAM1 performances
            await _settle(pilot)
            await pilot.press("enter")
            await _settle(pilot)
            assert isinstance(app.screen, InitBankScreen)
            status = _static_text(app.screen, "#init-status")
            assert "locked" in status.lower() or "gate" in status.lower()
            assert app.device.bundle_write_count == 0

    async def test_confirmation_text_names_selected_kinds(self) -> None:
        app = KwsedApp(DemoDevice())
        async with app.run_test() as pilot:
            await pilot.pause()
            app._write_gate.armed = True
            app.push_screen(InitBankScreen(gate=app._write_gate))
            await _settle(pilot)
            # RAM 1 performances (index 0) and patches (index 1)
            await pilot.press("space")
            await pilot.press("down")
            await pilot.press("space")
            # RAM 2 wave sequences: down to index 5
            for _ in range(4):
                await pilot.press("down")
            await pilot.press("space")
            await _settle(pilot)
            assert selection_summary(cast(Any, app.screen)._selection()) == (
                "RAM 1: performances, patches · RAM 2: wave sequences"
            )
            await pilot.press("enter")
            await _settle(pilot)
            assert isinstance(app.screen, ConfirmScreen)
            prompt = str(getattr(app.screen, "_prompt", ""))
            # ConfirmScreen stores prompt text; be flexible in attribute name
            if not prompt:
                prompt = str(getattr(app.screen, "prompt", ""))
            if not prompt:
                # fall back to rendered statics
                prompt = "\n".join(
                    str(getattr(w, "content", getattr(w, "renderable", "")))
                    for w in app.screen.query(Static)
                )
            assert "RAM 1: performances, patches" in prompt
            assert "RAM 2: wave sequences" in prompt
            assert "ARMED" in prompt or "Gate" in prompt


class TestInitBankWrites:
    async def test_single_bank_patches_only(self) -> None:
        device = DemoDevice()
        app = KwsedApp(device)
        async with app.run_test() as pilot:
            await _settle(pilot)
            # Preload RAM2 with distinct non-init data
            app._sources["RAM2"] = _make_source("RAM2", b"\x33")
            before_perf = list(app._sources["RAM2"].performances)
            before_seq = app._sources["RAM2"].waveseq
            app._write_gate.armed = True

            async def drive() -> None:
                await _settle(pilot)
                # down to RAM2 row (3 options per bank) -> patches is index 4
                for _ in range(4):
                    await pilot.press("down")
                await pilot.press("space")  # RAM2 patches
                await pilot.press("enter")
                await _settle(pilot)
                if isinstance(app.screen, ConfirmScreen):
                    await pilot.press("enter")  # confirm
                await _settle(pilot, 12)

            await pilot.press("N")
            await drive()

            assert device.bundle_write_count == 1
            write = device.bundle_writes[0]
            assert write["bank"] == banks.bank("RAM2").label
            assert write["kinds"] == ["patches"]
            # RAM2 performances unchanged in session source and names untouched
            assert app._sources["RAM2"].performances == before_perf
            assert app._sources["RAM2"].waveseq == before_seq
            # patches became init
            init_patch = initbank.init_patches()
            assert b"".join(app._sources["RAM2"].patches) == init_patch
            # live names for written kind are Init
            for n in range(banks.PATCHES_PER_BANK):
                assert app.catalog.display_name("RAM2", n, banks.Kind.PATCH) == "Init"
            # untouched performances keep previous (no live name forced)
            assert not app.catalog.is_live("RAM2", 0, banks.Kind.PERFORMANCE)

    async def test_two_banks_in_order_and_second_failure(self) -> None:
        device = DemoDevice()
        app = KwsedApp(device)
        async with app.run_test() as pilot:
            await _settle(pilot)
            app._write_gate.armed = True
            calls: list[int] = []

            def fail_second(*args: Any, **kwargs: Any) -> Any:
                bank = args[0]
                calls.append(int(bank))
                if len(calls) == 2:
                    raise RuntimeError("write refused")
                return DemoDevice.write_bank_bundle(device, *args, **kwargs)

            device.write_bank_bundle = fail_second  # type: ignore[method-assign]

            await pilot.press("N")
            await _settle(pilot)
            # RAM1 performances
            await pilot.press("space")
            # RAM3 performances: indices 0..8, RAM3 perf is 6
            for _ in range(6):
                await pilot.press("down")
            await pilot.press("space")
            await pilot.press("enter")
            await _settle(pilot)
            if isinstance(app.screen, ConfirmScreen):
                await pilot.press("enter")
            await _settle(pilot, 12)

            assert calls == [0, 2]  # RAM1 then RAM3 in order
            try:
                status = str(app.query_one("#status", Static).content)
            except Exception:
                status = str(getattr(app, "_status_text", ""))
            assert "RAM 1" in status or "RAM1" in status or "done" in status.lower()
            assert "failed" in status.lower() or "RAM 3" in status or "RAM3" in status
            # first recorded: RAM1 performances are init in session
            expected = initbank.init_performances()
            assert b"".join(app._sources["RAM1"].performances) == expected
            assert app.catalog.display_name("RAM1", 0, banks.Kind.PERFORMANCE) == "Init"
