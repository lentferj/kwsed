# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# kwsed is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version.
#
# kwsed is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.

"""The pre-audit audition flow through the app: buffer, staging, refusals.

A single performance dump lands in the unit's edit buffer and bypasses
memory protect, so `t` can make an imported performance audible before
anything is written. When fresh patch copies are needed, they are staged
into the user-reserved area first. Driven headlessly against the demo
device; the backup directory is redirected so a test cannot write into
the user's real envelopes.
"""

from __future__ import annotations

from typing import Any

import pytest
from textual.widgets import DataTable, Static

from kws import backup, banks, config, deps, relations
from kws import catalog as cat
from kws import records as r
from kwsed.app import KwsedApp
from kwsed.demo import DemoDevice
from kwsed.gate import EditGate
from kwsed.import_single import SingleImportChoice, SingleImportScreen
from tests.test_bankbuild import make_patch, make_performance, make_waveseq
from vinsynlib.ui.modals import ChoiceScreen

RAM3 = banks.bank("RAM3")
PERF_SIZE = r.PERFORMANCE_SIZE


async def _settle(pilot: Any, times: int = 8) -> None:
    for _ in range(times):
        await pilot.pause()


def _pcm_patch(name: str = "Kelp Forest") -> bytes:
    """A patch whose four oscillators play ROM waves, quoting no sequence."""
    return bytes(make_patch(name, ((0, 40), (0, 40), (0, 40), (0, 40))))


def _pcm_source(scope: str = "RAM1") -> deps.Source:
    """One performance whose two parts share a PCM-only patch 0."""
    patches = [bytes(r.PATCH_SIZE) for _ in range(banks.PATCHES_PER_BANK)]
    patches[0] = _pcm_patch()
    perfs = [bytes(PERF_SIZE) for _ in range(banks.PERFORMANCES_PER_BANK)]
    perfs[0] = make_performance("Glass Meridian", ((0, 0), (0, 0)))
    return deps.Source(
        scope,
        performances=perfs,
        patches=patches,
        waveseq=bytes(r.WAVESEQ_BANK_SIZE),
    )


def _seq_patch(name: str = "Kelp Forest") -> bytes:
    """A patch whose first oscillator plays wave sequence 0."""
    return bytes(make_patch(name, ((0, 0), (0, 40), (0, 40), (0, 40))))


def _seq_source(scope: str = "RAM1") -> deps.Source:
    """One performance whose patch quotes a wave sequence."""
    patches = [bytes(r.PATCH_SIZE) for _ in range(banks.PATCHES_PER_BANK)]
    patches[0] = _seq_patch()
    perfs = [bytes(PERF_SIZE) for _ in range(banks.PERFORMANCES_PER_BANK)]
    perfs[0] = make_performance("Glass Meridian", ((0, 0), (0, 0)))
    return deps.Source(
        scope,
        performances=perfs,
        patches=patches,
        waveseq=make_waveseq({0: [40, 41]})[0],
    )


def _target_source(
    *, keep: bool, patch: bytes | None = None, scope: str = "RAM3"
) -> deps.Source:
    """An empty target bank; `keep` adds one kept performance pinning patch 2."""
    number = banks.bank(scope).number
    perfs = [bytes(PERF_SIZE) for _ in range(banks.PERFORMANCES_PER_BANK)]
    patches = [bytes(r.PATCH_SIZE) for _ in range(banks.PATCHES_PER_BANK)]
    if keep:
        perfs[5] = make_performance("Kept", ((number, 2),))
        patches[2] = patch if patch is not None else _pcm_patch()
    return deps.Source(
        scope,
        performances=perfs,
        patches=patches,
        waveseq=bytes(r.WAVESEQ_BANK_SIZE),
    )


def _staging_config(tmp_path: Any, *, backup: bool | None = None) -> str:
    path = tmp_path / "config.toml"
    settings: dict[str, Any] = {
        "preaudit_target_perf": "RAM3:49",
        "preaudit_target_patches": "RAM3:27-34",
    }
    if backup is not None:
        settings["preaudit_backup"] = backup
    config._write_settings(str(path), settings)
    return str(path)


async def _open_import_screen(
    app: KwsedApp, pilot: Any, target: str = "RAM 3"
) -> SingleImportScreen:
    """Drive the `y` flow to the unified import screen for ``target``."""
    app._fill((0, banks.Kind.PERFORMANCE))
    app.query_one("#slot-table").focus()
    await _settle(pilot)
    await pilot.press("y")
    await _settle(pilot)
    chooser: Any = app.screen
    index = chooser._choices.index(target)
    chooser._current = index
    chooser.action_confirm()
    await _settle(pilot)
    assert isinstance(app.screen, SingleImportScreen)
    return app.screen


def _status(screen: Any) -> str:
    return str(screen.query_one("#status", Static).content)


@pytest.mark.asyncio
async def test_t_auditions_in_the_edit_buffer_when_nothing_is_new(tmp_path):
    """Case 1: the target already holds the patches; nothing is written."""
    device = DemoDevice()
    app = KwsedApp(device, catalog=cat.Catalog([]), config_path=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        app._sources["RAM1"] = _pcm_source()
        app._sources["RAM3"] = _target_source(keep=True)
        screen = await _open_import_screen(app, pilot)
        assigned = screen._target_slot_for_source(0)
        assert assigned is not None

        await pilot.press("t")
        await app.workers.wait_for_complete()
        await _settle(pilot)

        assert device.buffer_log == [("RAM 3", assigned)]
        selected = device.selected_log[-1]
        assert selected.kind == banks.Kind.PERFORMANCE
        assert selected.bank.id == "RAM3"
        assert selected.number == assigned, (
            "the unit is switched to the slot the dump names before it arrives"
        )
        assert device.patch_write_count == 0, "a buffer audition writes nothing"
        assert device.performance_write_count == 0
        assert len(app._undo_log) == 0, "auditioning is not an undo step"
        buffer = device.edit_buffer
        assert buffer is not None
        used = [part for part in r.performance_parts(buffer) if part.used]
        assert {part.bank for part in used} == {RAM3.number}
        assert {part.patch for part in used} == {2}, (
            "the planned record points at the reused patch slot"
        )
        assert "auditioning" in _status(screen)
        assert "nothing written" in _status(screen)


@pytest.mark.asyncio
async def test_t_auditions_an_unassigned_row_through_a_scratch_slot(tmp_path):
    """No assignment is needed: the first free slot borrows as scratch."""
    device = DemoDevice()
    app = KwsedApp(device, catalog=cat.Catalog([]), config_path=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        app._sources["RAM1"] = _pcm_source()
        app._sources["RAM3"] = _target_source(keep=True)
        screen = await _open_import_screen(app, pilot)
        await pilot.press("u")  # drop the assignment
        await _settle(pilot)
        assert screen._target_slot_for_source(0) is None

        await pilot.press("t")
        await app.workers.wait_for_complete()
        await _settle(pilot)

        assert device.buffer_log, "the audition still happened"
        assert device.patch_write_count == 0
        assert _status(screen) != "", _status(screen)
        assert "auditioning" in _status(screen)


@pytest.mark.asyncio
async def test_t_stages_fresh_patches_into_the_reserved_area(tmp_path, monkeypatch):
    """Case 2: fresh PCM patches are staged, then the record is auditioned."""
    monkeypatch.setattr(
        backup, "default_dir", lambda root=None: str(tmp_path / "backups")
    )
    device = DemoDevice()
    app = KwsedApp(
        device, catalog=cat.Catalog([]), config_path=_staging_config(tmp_path)
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._sources["RAM1"] = _pcm_source()
        app._sources["RAM3"] = _target_source(keep=False)
        screen = await _open_import_screen(app, pilot)
        # The device's staging bank is not the app's in-memory source: what
        # is in RAM 3:27 is what undo must put back.
        before_staged = device.read_patch("RAM3", 27)
        assert before_staged.startswith(b"Drifting Bell")
        await pilot.press("w")  # staging writes are destructive: gate first
        await _settle(pilot)

        await pilot.press("t")
        await app.workers.wait_for_complete()
        await _settle(pilot)

        assert device.patch_write_count == 1, (
            "one patch is staged as one single-patch write, not a whole bank"
        )
        assert device.bundle_write_count == 0, (
            "no whole-bank dump is needed for a single patch"
        )
        staged = device.read_patch("RAM3", 27)
        assert staged == _pcm_patch(), "the staging slot holds the source patch"
        buffer = device.edit_buffer
        assert buffer is not None
        used = [part for part in r.performance_parts(buffer) if part.used]
        assert {part.bank for part in used} == {RAM3.number}
        assert {part.patch for part in used} == {27}, (
            "the auditioned record points at the staging slot, not the plan's"
        )
        assert device.buffer_log == [("RAM 3", 49)]
        selected = device.selected_log[-1]
        assert selected.kind == banks.Kind.PERFORMANCE
        assert selected.bank.id == "RAM3"
        assert selected.number == 49, (
            "the configured pre-audit slot is made current before the dump"
        )
        assert "staged 1 patch" in _status(screen)
        assert "27" in _status(screen)
        assert len(app._undo_log) == 1

        # One `z` puts the staging bank back exactly as the unit had it --
        # the undo base is the bank that was written, not the target.
        await pilot.press("z")
        await app.workers.wait_for_complete()
        await _settle(pilot)
        assert device.read_patch("RAM3", 27) == before_staged


@pytest.mark.asyncio
async def test_a_refused_single_write_falls_back_to_the_bank_dump(
    tmp_path, monkeypatch
):
    """A unit that ignores EXECUTE_WRITE for patches still gets an audition."""
    device = DemoDevice()
    app = KwsedApp(
        device, catalog=cat.Catalog([]), config_path=_staging_config(tmp_path)
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._sources["RAM1"] = _pcm_source()
        app._sources["RAM3"] = _target_source(keep=False)
        screen = await _open_import_screen(app, pilot)
        await pilot.press("w")
        await _settle(pilot)

        def refused(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"verified": False, "status": None, "mismatch": "not stored"}

        monkeypatch.setattr(device, "write_patch", refused)

        await pilot.press("t")
        await app.workers.wait_for_complete()
        await _settle(pilot)

        assert device.bundle_write_count == 1, "the slow path still stages"
        assert device.read_patch("RAM3", 27) == _pcm_patch()
        assert "staged 1 patch" in _status(screen)


@pytest.mark.asyncio
async def test_staged_parts_name_the_staging_bank_not_the_target(tmp_path):
    """Import into RAM 2, stage in RAM 3: the record must follow the patches.

    With the destination's bank number the audition would point at RAM 2's
    slot 27 -- some other patch, usually an empty one -- and stay silent
    even though RAM 3:27 holds the right copy. This is the live bug.
    """
    device = DemoDevice()
    app = KwsedApp(
        device, catalog=cat.Catalog([]), config_path=_staging_config(tmp_path)
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._sources["RAM1"] = _pcm_source()
        app._sources["RAM2"] = _target_source(keep=False, scope="RAM2")
        await _open_import_screen(app, pilot, target="RAM 2")
        was = device.read_patch("RAM2", 27)
        await pilot.press("w")
        await _settle(pilot)

        await pilot.press("t")
        await app.workers.wait_for_complete()
        await _settle(pilot)

        assert device.read_patch("RAM3", 27) == _pcm_patch()
        buffer = device.edit_buffer
        assert buffer is not None
        used = [part for part in r.performance_parts(buffer) if part.used]
        assert {part.bank for part in used} == {RAM3.number}, (
            "the staged parts must name the staging bank"
        )
        assert {part.patch for part in used} == {27}
        assert device.read_patch("RAM2", 27) == was, (
            "and the target's slot 27 was not touched: it holds something "
            "else, which is why naming it was silent"
        )


@pytest.mark.asyncio
async def test_t_stages_wave_sequences_when_configured(tmp_path, monkeypatch):
    """A patch quoting a sequence stages the patch AND the sequence bank."""
    monkeypatch.setattr(
        backup, "default_dir", lambda root=None: str(tmp_path / "backups")
    )
    device = DemoDevice()
    app = KwsedApp(
        device, catalog=cat.Catalog([]), config_path=_staging_config(tmp_path)
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._sources["RAM1"] = _seq_source()
        app._sources["RAM3"] = _target_source(keep=False)
        screen = await _open_import_screen(app, pilot)
        await pilot.press("w")
        await _settle(pilot)

        await pilot.press("t")
        await app.workers.wait_for_complete()
        await _settle(pilot)

        assert device.bundle_write_count == 1, (
            "only the sequence bank needs a whole-bank dump"
        )
        assert device.patch_write_count == 1, "the patch itself is one write"
        staged = device.read_patch("RAM3", 27)
        seqs = [wave for wave in r.patch_waves(staged) if wave.is_sequence]
        assert len(seqs) == 1
        assert seqs[0].bank == RAM3.number, (
            "the staged patch quotes the target bank's sequence slots"
        )
        assert seqs[0].wave == 0, "the first free sequence slot is the planned one"
        stored = device._written.get((RAM3.id, "waveseq"))
        assert stored is not None, "the target's sequence memory was staged"
        assert stored != bytes(r.WAVESEQ_BANK_SIZE)
        buffer = device.edit_buffer
        assert buffer is not None
        used = [part for part in r.performance_parts(buffer) if part.used]
        assert {part.bank for part in used} == {RAM3.number}
        assert {part.patch for part in used} == {27}
        assert device.buffer_log == [("RAM 3", 49)]
        assert "wave sequence" in _status(screen)
        assert "z undoes the staging" in _status(screen)
        assert len(app._undo_log) == 1, "the sequence write is one undo step"


@pytest.mark.asyncio
async def test_t_refuses_when_a_sequence_is_needed_but_staging_is_unconfigured(
    tmp_path,
):
    device = DemoDevice()
    app = KwsedApp(device, catalog=cat.Catalog([]), config_path=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        app._sources["RAM1"] = _seq_source()
        app._sources["RAM3"] = _target_source(keep=False)
        screen = await _open_import_screen(app, pilot)
        await pilot.press("w")
        await _settle(pilot)

        await pilot.press("t")
        await app.workers.wait_for_complete()
        await _settle(pilot)

        assert "not configured" in _status(screen)
        assert device.buffer_log == []
        assert device.patch_write_count == 0


@pytest.mark.asyncio
async def test_t_stages_only_the_fresh_patch_of_a_mixed_plan(tmp_path, monkeypatch):
    """A reused patch keeps its planned slot; only fresh ones are staged."""
    monkeypatch.setattr(
        backup, "default_dir", lambda root=None: str(tmp_path / "backups")
    )
    shared = _pcm_patch("Shared")

    def _mixed_source(scope: str = "RAM1") -> deps.Source:
        patches = [bytes(r.PATCH_SIZE) for _ in range(banks.PATCHES_PER_BANK)]
        patches[0] = shared
        patches[1] = _pcm_patch("Other")
        perfs = [bytes(PERF_SIZE) for _ in range(banks.PERFORMANCES_PER_BANK)]
        perfs[0] = make_performance("Glass Meridian", ((0, 0), (0, 1)))
        return deps.Source(
            scope,
            performances=perfs,
            patches=patches,
            waveseq=bytes(r.WAVESEQ_BANK_SIZE),
        )

    device = DemoDevice()
    app = KwsedApp(
        device, catalog=cat.Catalog([]), config_path=_staging_config(tmp_path)
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._sources["RAM1"] = _mixed_source()
        app._sources["RAM3"] = _target_source(keep=True, patch=shared)
        screen = await _open_import_screen(app, pilot)
        await pilot.press("w")
        await _settle(pilot)

        await pilot.press("t")
        await app.workers.wait_for_complete()
        await _settle(pilot)

        assert device.patch_write_count == 1, "only the fresh copy is written"
        assert device.bundle_write_count == 0, "no bank dump for one patch"
        buffer = device.edit_buffer
        assert buffer is not None
        used = [part for part in r.performance_parts(buffer) if part.used]
        assert {part.patch for part in used} == {2, 27}, (
            "the reused part points at the target slot, the fresh one at staging"
        )
        assert device.buffer_log == [("RAM 3", 49)]
        assert "staged 1 patch" in _status(screen)


@pytest.mark.asyncio
async def test_t_refuses_when_staging_is_not_configured(tmp_path):
    device = DemoDevice()
    app = KwsedApp(device, catalog=cat.Catalog([]), config_path=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        app._sources["RAM1"] = _pcm_source()
        app._sources["RAM3"] = _target_source(keep=False)
        screen = await _open_import_screen(app, pilot)
        await pilot.press("w")
        await _settle(pilot)

        await pilot.press("t")
        await app.workers.wait_for_complete()
        await _settle(pilot)

        assert "not configured" in _status(screen)
        assert device.patch_write_count == 0
        assert device.buffer_log == []


@pytest.mark.asyncio
async def test_t_refuses_when_the_write_gate_is_locked(tmp_path):
    device = DemoDevice()
    app = KwsedApp(
        device, catalog=cat.Catalog([]), config_path=_staging_config(tmp_path)
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._sources["RAM1"] = _pcm_source()
        app._sources["RAM3"] = _target_source(keep=False)
        screen = await _open_import_screen(app, pilot)
        # No `w`: the gate stays locked.

        await pilot.press("t")
        await app.workers.wait_for_complete()
        await _settle(pilot)

        assert "write gate locked" in _status(screen)
        assert device.patch_write_count == 0
        assert device.buffer_log == []


@pytest.mark.asyncio
async def test_the_progress_bar_shows_a_transfer_then_hides(tmp_path):
    device = DemoDevice()
    app = KwsedApp(device, catalog=cat.Catalog([]), config_path=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one("#progress", Static)
        assert bar.display is False

        app._progress_report("receiving RAM 2 patches", 0.5)
        await pilot.pause()
        assert bar.display is True
        assert "50%" in str(bar.content)
        assert "RAM 2 patches" in str(bar.content)

        app._progress_report("receiving RAM 2 patches", None)
        await pilot.pause()
        await pilot.pause(0.5)
        assert bar.display is False


@pytest.mark.asyncio
async def test_the_import_screen_shows_the_bar_while_it_covers_the_app(tmp_path):
    """The screen is modal, so the app's own bar is underneath it."""
    device = DemoDevice()
    app = KwsedApp(
        device, catalog=cat.Catalog([]), config_path=_staging_config(tmp_path)
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._sources["RAM1"] = _pcm_source()
        app._sources["RAM3"] = _target_source(keep=False)
        screen = await _open_import_screen(app, pilot)

        # The report paints synchronously on this thread, so assert at once:
        # any pause lets a stale hide-timer from an earlier transfer win.
        app._progress_report("receiving RAM 3 patches", 0.25)
        bar = screen.query_one("#progress", Static)
        assert bar.display is True
        assert "25%" in str(bar.content)

        app._progress_report("receiving RAM 3 patches", None)
        await pilot.pause()
        await pilot.pause(0.5)
        assert bar.display is False


@pytest.mark.asyncio
async def test_a_held_operation_keeps_the_bar_up_between_steps(tmp_path):
    """`t` is several tiny exchanges; the bar must span the whole audition."""
    device = DemoDevice()
    app = KwsedApp(device, catalog=cat.Catalog([]), config_path=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one("#progress", Static)

        app._progress_hold_begin("auditioning from the staging area")
        app._progress_report("reading RAM 3 patch 27", 0.4)
        await pilot.pause()
        assert bar.display is True
        assert "40%" in str(bar.content)

        # One step ending must not take the bar down while the operation runs.
        app._progress_report("reading RAM 3 patch 27", None)
        await pilot.pause(0.5)
        assert bar.display is True

        app._progress_hold_end()
        await pilot.pause(0.5)
        assert bar.display is False


@pytest.mark.asyncio
async def test_a_second_audition_uses_the_session_cache(tmp_path):
    """The staging slots' backups are read once per screen, not per `t`."""
    device = DemoDevice()
    app = KwsedApp(
        device, catalog=cat.Catalog([]), config_path=_staging_config(tmp_path)
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._sources["RAM1"] = _pcm_source()
        app._sources["RAM3"] = _target_source(keep=False)
        await _open_import_screen(app, pilot)
        await pilot.press("w")
        await _settle(pilot)

        reads: list[Any] = []
        original = device.read_patch

        def counting(*args: Any, **kwargs: Any) -> Any:
            reads.append(args[1] if len(args) > 1 else kwargs.get("number"))
            return original(*args, **kwargs)

        device.read_patch = counting  # type: ignore[method-assign]
        await pilot.press("t")
        await app.workers.wait_for_complete()
        await _settle(pilot)
        assert 27 in reads, "the first audition reads the slot it overwrites"

        reads.clear()
        await pilot.press("t")
        await app.workers.wait_for_complete()
        await _settle(pilot)
        assert reads == [27], (
            "the second audition reuses the cached backup; only the one "
            "read-back that proves the write remains"
        )


@pytest.mark.asyncio
async def test_backups_can_be_switched_off_for_the_staging_slots(tmp_path):
    """`preaudit_backup = false`: no slot reads, nothing to undo."""
    device = DemoDevice()
    app = KwsedApp(
        device,
        catalog=cat.Catalog([]),
        config_path=_staging_config(tmp_path, backup=False),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._sources["RAM1"] = _pcm_source()
        app._sources["RAM3"] = _target_source(keep=False)
        screen = await _open_import_screen(app, pilot)
        reads: list[Any] = []
        original = device.read_patch

        def counting(*args: Any, **kwargs: Any) -> Any:
            reads.append(args[1] if len(args) > 1 else kwargs.get("number"))
            return original(*args, **kwargs)

        device.read_patch = counting  # type: ignore[method-assign]
        await pilot.press("w")
        await _settle(pilot)

        await pilot.press("t")
        await app.workers.wait_for_complete()
        await _settle(pilot)

        assert reads == [27], (
            "only the read-back that proves the write; no backup capture"
        )
        assert len(app._undo_log) == 0, "nothing captured, nothing to undo"
        assert "not backed up" in _status(screen)
        assert device.read_patch("RAM3", 27) == _pcm_patch()


@pytest.mark.asyncio
async def test_the_staging_bank_is_read_when_the_screen_opens(tmp_path, monkeypatch):
    """The fallback's base read is prefetched, not paid during `t`."""
    device = DemoDevice()
    app = KwsedApp(
        device, catalog=cat.Catalog([]), config_path=_staging_config(tmp_path)
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._sources["RAM1"] = _pcm_source()
        app._sources["RAM3"] = _target_source(keep=False)

        bank_reads: list[Any] = []
        original = device.read_bank

        def counting(*args: Any, **kwargs: Any) -> Any:
            bank_reads.append(args[0] if args else kwargs.get("bank"))
            return original(*args, **kwargs)

        device.read_bank = counting  # type: ignore[method-assign]
        await _open_import_screen(app, pilot)
        await app.workers.wait_for_complete()
        await _settle(pilot)
        assert bank_reads, "the staging bank is read while the user picks rows"

        bank_reads.clear()

        def refused(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"verified": False, "status": None}

        monkeypatch.setattr(device, "write_patch", refused)
        await pilot.press("w")
        await _settle(pilot)

        await pilot.press("t")
        await app.workers.wait_for_complete()
        await _settle(pilot)

        assert bank_reads == [], "the fallback reused the prefetched bank"
        assert device.bundle_write_count == 1
        assert device.read_patch("RAM3", 27) == _pcm_patch()


def test_the_conflict_warning_names_the_staging_slots(tmp_path):
    app = KwsedApp(
        DemoDevice(), catalog=cat.Catalog([]), config_path=_staging_config(tmp_path)
    )
    conflict = app._preaudit_conflicting_slots(banks.bank("RAM3"), {49}, {27, 28})
    assert "performance 49" in conflict
    assert "patch 27, 28" in conflict
    assert "RAM 3" in conflict
    assert app._preaudit_conflicting_slots(banks.bank("RAM2"), {49}, {27, 28}) == ""


@pytest.mark.asyncio
async def test_a_single_import_into_a_staging_slot_warns(tmp_path):
    device = DemoDevice()
    app = KwsedApp(
        device, catalog=cat.Catalog([]), config_path=_staging_config(tmp_path)
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._sources["RAM1"] = _pcm_source()
        app._sources["RAM3"] = _target_source(keep=False)
        target = banks.bank("RAM3")
        app._write_gate = EditGate(armed=True)
        seen: list[str] = []
        app.notify_status = lambda msg, *a, **k: seen.append(str(msg))  # type: ignore[method-assign]

        choice = SingleImportChoice(
            target_bank="RAM3", assignments={49: relations.PerfId("RAM1", 0)}
        )
        app._import_single_chosen(
            choice, target, app._sources["RAM1"], dict(app._sources)
        )
        await app.workers.wait_for_complete()
        await _settle(pilot)

        assert any("pre-audit" in msg and "49" in msg for msg in seen), seen


@pytest.mark.asyncio
async def test_warnings_tint_the_status_line(tmp_path):
    device = DemoDevice()
    app = KwsedApp(device, catalog=cat.Catalog([]), config_path=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        status = app.query_one("#status", Static)
        app.notify_status("plain")
        await pilot.pause()
        assert not status.has_class("warning")
        app.notify_status("nope", refused=True)
        await pilot.pause()
        assert status.has_class("warning")
        app.notify_status("plain again")
        await pilot.pause()
        assert not status.has_class("warning")


@pytest.mark.asyncio
async def test_auto_audition_plays_the_row_the_cursor_lands_on(tmp_path):
    device = DemoDevice()
    app = KwsedApp(device, catalog=cat.Catalog([]), config_path=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        source = _pcm_source()
        source.performances[1] = make_performance("Second", ((0, 0), (0, 0)))
        app._sources["RAM1"] = source
        app._sources["RAM3"] = _target_source(keep=True)
        screen = await _open_import_screen(app, pilot)
        table = screen.query_one("#source-table", DataTable)
        table.focus()
        await _settle(pilot)

        await pilot.press("a")
        await pilot.pause(0.2)
        await app.workers.wait_for_complete()
        await _settle(pilot)
        assert screen._auto_audition is True
        first = len(device.buffer_log)
        assert first >= 1, "turning it on plays the current row"

        table.move_cursor(row=1)
        await pilot.pause(0.9)
        await app.workers.wait_for_complete()
        await _settle(pilot)
        assert len(device.buffer_log) > first, "the next row plays too"


@pytest.mark.asyncio
async def test_leaving_the_screen_says_the_staging_is_still_on_the_unit(tmp_path):
    device = DemoDevice()
    app = KwsedApp(
        device, catalog=cat.Catalog([]), config_path=_staging_config(tmp_path)
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._sources["RAM1"] = _pcm_source()
        app._sources["RAM3"] = _target_source(keep=False)
        before = device.read_patch("RAM3", 27)
        await _open_import_screen(app, pilot)
        await pilot.press("w")
        await _settle(pilot)
        await pilot.press("t")
        await app.workers.wait_for_complete()
        await _settle(pilot)
        assert app._pending_staging is True

        await pilot.press("q")
        await _settle(pilot)
        status = app.query_one("#status", Static)
        assert "staging is still on the unit" in str(status.content)
        assert status.has_class("warning")
        bar = app.query_one("#progress", Static)
        assert bar.display is False, "the prefetch bar must not stay behind"

        await pilot.press("z")
        await app.workers.wait_for_complete()
        await _settle(pilot)
        assert app._pending_staging is False
        assert device.read_patch("RAM3", 27) == before


async def _stage_and_leave(app: KwsedApp, pilot: Any) -> Any:
    """Open the screen, stage the focused row, leave it again."""
    await _open_import_screen(app, pilot)
    await pilot.press("w")
    await _settle(pilot)
    await pilot.press("t")
    await app.workers.wait_for_complete()
    await _settle(pilot)
    assert app._pending_staging is True
    await pilot.press("q")
    await _settle(pilot)
    assert not isinstance(app.screen, SingleImportScreen)


@pytest.mark.asyncio
async def test_quitting_with_staging_asks_before_leaving(tmp_path):
    device = DemoDevice()
    app = KwsedApp(
        device, catalog=cat.Catalog([]), config_path=_staging_config(tmp_path)
    )
    exits: list[int] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app.exit = lambda *a, **k: exits.append(1)  # type: ignore[method-assign]
        app._sources["RAM1"] = _pcm_source()
        app._sources["RAM3"] = _target_source(keep=False)
        await _stage_and_leave(app, pilot)

        await pilot.press("q")
        await _settle(pilot)
        chooser = app.screen
        assert isinstance(chooser, ChoiceScreen), "quit must ask first"
        assert exits == []

        chooser._current = 2  # cancel
        chooser.action_confirm()
        await _settle(pilot)
        assert exits == []
        assert app._pending_staging is True

        await pilot.press("q")
        await _settle(pilot)
        chooser = app.screen
        assert isinstance(chooser, ChoiceScreen)
        chooser._current = 1  # quit and forget
        chooser.action_confirm()
        await _settle(pilot)
        assert exits == [1]
        assert app._pending_staging is False


@pytest.mark.asyncio
async def test_quitting_can_restore_the_staging_first(tmp_path):
    device = DemoDevice()
    app = KwsedApp(
        device, catalog=cat.Catalog([]), config_path=_staging_config(tmp_path)
    )
    exits: list[int] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app.exit = lambda *a, **k: exits.append(1)  # type: ignore[method-assign]
        app._sources["RAM1"] = _pcm_source()
        app._sources["RAM3"] = _target_source(keep=False)
        before = device.read_patch("RAM3", 27)
        await _stage_and_leave(app, pilot)
        assert device.read_patch("RAM3", 27) != before

        await pilot.press("q")
        await _settle(pilot)
        chooser = app.screen
        assert isinstance(chooser, ChoiceScreen)
        chooser._current = 0  # restore, then quit
        chooser.action_confirm()
        await app.workers.wait_for_complete()
        await _settle(pilot)

        assert exits == [1]
        assert app._pending_staging is False
        assert device.read_patch("RAM3", 27) == before
