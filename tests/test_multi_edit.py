# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors

"""Multi Mode Setup edit: round trip and the write fence."""

from pathlib import Path

import pytest

from kws import backup, deps, dumps
from kws import banks as banks_mod
from kws import catalog as cat
from kws import messages as m
from kws import multi as multi_mod
from kws import records as rec
from kwsed.app import KwsedApp, MultiEditScreen
from kwsed.demo import DemoDevice
from tests.test_bankbuild import make_patch, make_performance


def test_decode_edit_encode_round_trip():
    # Build a minimal valid dump, parse, edit a field, encode, re-parse.
    needed = multi_mod.NAME_BASE + multi_mod.MULTISETS * multi_mod.NAME_SIZE
    data = bytearray(needed)
    # name slot for multiset 0
    data[multi_mod.NAME_BASE : multi_mod.NAME_BASE + 4] = b"ABCD"
    sets = multi_mod.parse(bytes(data))
    # edit via replace (dataclass frozen) -> construct new
    from dataclasses import replace

    new_part = replace(sets[0].parts[0], bank=7, performance=3, level=100, flag=1)
    new_parts = (new_part,) + sets[0].parts[1:]
    new_set = replace(sets[0], name="Edited", parts=new_parts)
    sets[0] = new_set
    out = multi_mod.encode(sets)
    reparsed = multi_mod.parse(out)
    assert reparsed[0].name == "Edited"
    assert reparsed[0].parts[0].bank == 7
    assert reparsed[0].parts[0].performance == 3
    assert reparsed[0].parts[0].level == 100


class FakeDevice:
    def __init__(self):
        self.channel = 0
        self.calls = []

    def write_multi(self, data, **kw):
        self.calls.append((data, kw))
        return {"backup": "x", "verified": True, "mismatch": None}


@pytest.mark.asyncio
async def test_multi_edit_arm_then_fire_calls_write():
    sets = multi_mod.parse(
        bytes(multi_mod.NAME_BASE + multi_mod.MULTISETS * multi_mod.NAME_SIZE)
    )
    # need length; parse requires full length
    data = bytes(multi_mod.NAME_BASE + multi_mod.MULTISETS * multi_mod.NAME_SIZE)
    sets = multi_mod.parse(data)
    dev = FakeDevice()
    screen = MultiEditScreen(multisets=sets, device=dev, backup_dir=None)
    from kwsed.app import KwsedApp
    from kwsed.demo import DemoDevice

    app = KwsedApp(DemoDevice(), allow_write=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen)
        await pilot.pause()
        screen.action_arm()
        await pilot.pause()
        screen.action_fire()
        await pilot.pause()
        assert len(dev.calls) == 1
        assert dev.calls[0][1].get("verify") is True


@pytest.mark.asyncio
async def test_multi_edit_refused_without_arm():
    data = bytes(multi_mod.NAME_BASE + multi_mod.MULTISETS * multi_mod.NAME_SIZE)
    sets = multi_mod.parse(data)
    dev = FakeDevice()
    screen = MultiEditScreen(multisets=sets, device=dev, backup_dir=None)
    from kwsed.app import KwsedApp
    from kwsed.demo import DemoDevice

    app = KwsedApp(DemoDevice(), allow_write=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen)
        await pilot.pause()
        screen.action_fire()
        await pilot.pause()
        assert len(dev.calls) == 0


def test_round_trip_byte_identical():
    needed = multi_mod.NAME_BASE + multi_mod.MULTISETS * multi_mod.NAME_SIZE
    data = bytearray(needed)
    for i in range(multi_mod.PART_BASE):
        data[i] = (i + 1) & 0x7F
    data[multi_mod.NAME_BASE : multi_mod.NAME_BASE + 4] = b"ABCD"
    sets = multi_mod.parse(bytes(data))
    out = multi_mod.encode(sets)
    assert out == bytes(data)


def test_editing_one_field_changes_only_that_field():
    needed = multi_mod.NAME_BASE + multi_mod.MULTISETS * multi_mod.NAME_SIZE
    data = bytearray(needed)
    for i in range(multi_mod.PART_BASE):
        data[i] = (i + 1) & 0x7F
    sets = multi_mod.parse(bytes(data))
    from dataclasses import replace

    new_part = replace(sets[0].parts[0], bank=9)
    new_parts = (new_part,) + sets[0].parts[1:]
    new_set = replace(sets[0], parts=new_parts)
    sets[0] = new_set
    out = multi_mod.encode(sets)
    diffs = [i for i in range(needed) if out[i] != data[i]]
    assert diffs == [multi_mod.PART_BASE]


@pytest.mark.asyncio
async def test_no_write_while_busy():
    data = bytes(multi_mod.NAME_BASE + multi_mod.MULTISETS * multi_mod.NAME_SIZE)
    sets = multi_mod.parse(data)
    dev = FakeDevice()
    screen = MultiEditScreen(multisets=sets, device=dev, backup_dir=None)
    from kwsed.app import KwsedApp
    from kwsed.demo import DemoDevice

    app = KwsedApp(DemoDevice(), allow_write=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen)
        await pilot.pause()
        screen.action_arm()
        await pilot.pause()
        app._exchange.acquire()
        screen.action_fire()
        await pilot.pause()
        assert len(dev.calls) == 0
        app._exchange.release()


class _RecordingDemo(DemoDevice):
    def __init__(self, directory: str) -> None:
        super().__init__()
        self._directory = directory

    def write_bank_bundle(
        self,
        bank: int | str,
        *,
        performances: bytes | None = None,
        patches: bytes | None = None,
        waveseq: bytes | None = None,
        backup_dir: str,
        verify: bool = True,
        timeout: float | None = None,
    ) -> dict[str, object]:
        result = super().write_bank_bundle(
            bank,
            performances=performances,
            patches=patches,
            waveseq=waveseq,
            backup_dir=backup_dir,
            verify=verify,
            timeout=timeout,
        )
        entry = banks_mod.bank(bank)
        blocks: dict[str, bytes] = {}
        for name, function, data in (
            ("performances", m.Function.ALL_PERFORMANCE_DUMP, performances),
            ("patches", m.Function.ALL_PATCH_DUMP, patches),
            ("waveseq", m.Function.ALL_WAVESEQ_DUMP, waveseq),
        ):
            if data is not None:
                blocks[name] = bytes(
                    m.frame_bulk(function, entry.sysex, data, channel=self.channel)
                )
        result["backup"] = backup.save(
            self._directory,
            kind="bank-bundle",
            slot=entry.label,
            name=entry.label,
            device_id=str(self.channel),
            source="kwsed",
            blocks=blocks,
        )
        return result

    def write_multi(
        self,
        data: bytes,
        *,
        backup_dir: str,
        verify: bool = True,
        timeout: float | None = None,
    ) -> dict[str, object]:
        result = super().write_multi(
            data, backup_dir=backup_dir, verify=verify, timeout=timeout
        )
        result["backup"] = backup.save(
            self._directory,
            kind="multi",
            slot="multi",
            name="multi",
            device_id=str(self.channel),
            source="kwsed",
            blocks={"dump": bytes(data)},
        )
        return result


def _make_source(scope: str, perf_name: str, patch_name: str) -> deps.Source:
    """One card bank: performance 0 uses one patch, part bank is CARD."""
    perf = make_performance(perf_name, parts=((banks_mod.bank("CARD").number, 0),))
    patches = [make_patch(patch_name)] + [bytes(rec.PATCH_SIZE) for _ in range(34)]
    perfs = [perf] + [bytes(rec.PERFORMANCE_SIZE) for _ in range(49)]
    return deps.Source(
        bank_id=scope,
        performances=perfs,
        patches=patches,
        waveseq=bytes(rec.WAVESEQ_BANK_SIZE),
    )


def _make_sets() -> list[multi_mod.Multiset]:
    needed = multi_mod.NAME_BASE + multi_mod.MULTISETS * multi_mod.NAME_SIZE
    data = bytearray(needed)
    sets = multi_mod.parse(bytes(data))
    from dataclasses import replace

    # part 1 and 2 -> CARD bank, perf 0
    card_sysex = banks_mod.bank("CARD").sysex
    new_part0 = replace(sets[0].parts[0], bank=card_sysex, performance=0)
    new_part1 = replace(sets[0].parts[1], bank=card_sysex, performance=0)
    # channels 3..16 -> perf 50 (unused)
    parts = list(sets[0].parts)
    parts[0] = new_part0
    parts[1] = new_part1
    for i in range(2, 16):
        parts[i] = replace(parts[i], performance=50)
    new_set = replace(sets[0], parts=tuple(parts))
    sets[0] = new_set
    return list(sets)


@pytest.mark.asyncio
async def test_multi_edit_stage(tmp_path: Path) -> None:
    src07 = _make_source("CARD/07", "Glass Meridian", "Kelp Forest")
    src13 = _make_source("CARD/13", "Amber Drift", "Amber Drift")
    sets = _make_sets()
    device = _RecordingDemo(str(tmp_path / "backups"))
    app = KwsedApp(device, catalog=cat.Catalog([]))
    screen = MultiEditScreen(
        multisets=sets,
        selected=0,
        device=app.device,
        backup_dir=str(tmp_path / "backups"),
        sources={"CARD/07": src07, "CARD/13": src13},
        archive={},
        physical={},
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen)
        await pilot.pause()
        screen._desired_scopes[0] = "CARD/07"
        screen._desired_scopes[1] = "CARD/13"
        screen.action_stage()
        await pilot.pause()
        plan = screen._staged_plan
        assert plan is not None
        from textual.widgets import Static

        state = screen.query_one("#multi-edit-state", Static)
        text = str(state.content)
        assert "staged 2 card perf(s)" in text
        assert "RAM 3" in text
        staged = [channel for channel in plan.channels if channel.staged]
        assert len(staged) == 2
        assert {channel.target_bank.id for channel in staged} == {"RAM3"}
        assert sorted(channel.target_perf_slot for channel in staged) == [0, 1]
        assert sorted(
            {slot for channel in staged for slot in channel.target_patch_slots}
        ) == [0, 1]
        updated = screen._ms()
        ram3_sysex = banks_mod.bank("RAM3").sysex
        assert updated.parts[0].bank == ram3_sysex
        assert updated.parts[1].bank == ram3_sysex
        assert sorted((updated.parts[0].performance, updated.parts[1].performance)) == [
            0,
            1,
        ]


@pytest.mark.asyncio
async def test_multi_edit_stage_write(tmp_path: Path) -> None:
    src07 = _make_source("CARD/07", "Glass Meridian", "Kelp Forest")
    src13 = _make_source("CARD/13", "Amber Drift", "Amber Drift")
    sets = _make_sets()
    device = _RecordingDemo(str(tmp_path / "backups"))
    app = KwsedApp(device, catalog=cat.Catalog([]))
    screen = MultiEditScreen(
        multisets=sets,
        selected=0,
        device=app.device,
        backup_dir=str(tmp_path / "backups"),
        sources={"CARD/07": src07, "CARD/13": src13},
        archive={},
        physical={},
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen)
        await pilot.pause()
        screen._desired_scopes[0] = "CARD/07"
        screen._desired_scopes[1] = "CARD/13"
        screen.action_arm()
        await pilot.pause()
        screen.action_fire()
        for _ in range(40):
            await pilot.pause()
        assert device.bundle_write_count == 1
        assert device.multi_write_count == 1
        assert len(app._undo_log) == 1, "one staged write is one undo step"

        ram3 = banks_mod.bank("RAM3")
        perf_dump = device.read_bank("RAM3", banks_mod.Kind.PERFORMANCE)
        first = perf_dump.records[0]
        second = perf_dump.records[1]
        assert dumps.record_name(first) == "Glass Meridian"
        assert dumps.record_name(second) == "Amber Drift"
        first_parts = [p for p in rec.performance_parts(first) if p.used]
        second_parts = [p for p in rec.performance_parts(second) if p.used]
        assert first_parts and second_parts
        assert {p.bank for p in first_parts + second_parts} == {ram3.number}
        assert {p.patch for p in first_parts} == {0}
        assert {p.patch for p in second_parts} == {1}, (
            "two performances must never share a target patch slot"
        )

        patch_dump = device.read_bank("RAM3", banks_mod.Kind.PATCH)
        assert dumps.record_name(patch_dump.records[0]) == "Kelp Forest"
        assert dumps.record_name(patch_dump.records[1]) == "Amber Drift"

        assert device.multi_written is not None
        written = multi_mod.parse(device.multi_written)
        assert written[0].parts[0].bank == ram3.sysex
        assert written[0].parts[1].bank == ram3.sysex
        assert written[0].parts[0].performance == 0
        assert written[0].parts[1].performance == 1
