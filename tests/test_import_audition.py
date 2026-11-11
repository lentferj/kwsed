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
from textual.widgets import Static

from kws import backup, banks, config, deps
from kws import catalog as cat
from kws import records as r
from kwsed.app import KwsedApp
from kwsed.demo import DemoDevice
from kwsed.import_single import SingleImportScreen
from tests.test_bankbuild import make_patch, make_performance, make_waveseq

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


def _staging_config(tmp_path: Any) -> str:
    path = tmp_path / "config.toml"
    config._write_settings(
        str(path),
        {
            "preaudit_target_perf": "RAM3:49",
            "preaudit_target_patches": "RAM3:27-34",
        },
    )
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
        await _settle(pilot)

        assert device.buffer_log == [("RAM 3", assigned)]
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
        await _settle(pilot)

        assert device.bundle_write_count == 1, (
            "the staged patches go out as one whole-bank dump"
        )
        assert device.patch_write_count == 0, (
            "the single-patch write command pops the unit's SAVE PATCH prompt"
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
        await _settle(pilot)

        assert device.bundle_write_count == 2, (
            "one dump for the staging bank's patches, one for the sequences"
        )
        assert device.patch_write_count == 0
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
        await _settle(pilot)

        assert device.bundle_write_count == 1, "only the staging bank is written"
        assert device.patch_write_count == 0, "the reused patch is not rewritten"
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
        await _settle(pilot)

        assert "write gate locked" in _status(screen)
        assert device.patch_write_count == 0
        assert device.buffer_log == []
