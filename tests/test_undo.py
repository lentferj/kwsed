# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors

"""Tests for kws.undo bindings and app undo."""

from typing import Any, Dict

import pytest

from kws import backup, banks, undo
from kws import catalog as _catalog
from kws import messages as m
from kws import records as r
from kwsed.app import KwsedApp
from kwsed.demo import DemoDevice
from vinsynlib.undo import Record

CARD_SYSEX = banks.bank("CARD").sysex


class TestUndoBindings:
    def test_push_and_undo(self):
        seen = []

        def apply_hook(record):
            seen.append(record)
            return True

        log = undo.make_log(apply_hook=apply_hook)
        log.push(Record(label="first", payload=1))
        assert len(log) == 1
        result = log.undo()
        assert result == "undid 1"
        assert len(log) == 0
        assert seen[0].payload == 1

    def test_gate_refusal(self):
        log = undo.make_log(apply_hook=lambda r: True, gate_hook=lambda: "locked")
        log.push(Record(label="x"))
        result = log.undo()
        assert result == "refused: locked"
        assert len(log) == 1

    def test_plan_settle(self):
        log = undo.make_log(apply_hook=lambda r: True)
        log.push(Record(label="a"))
        log.push(Record(label="b"))
        plan = log.plan(2)
        assert len(plan) == 2
        assert len(log) == 0
        result = log.settle([True, False])
        assert "undid 1" in result
        assert "1 failed" in result

    def test_empty_log_undo(self):
        log = undo.make_log(apply_hook=lambda r: True)
        result = log.undo()
        assert result == "nothing to undo"


class TestAppUndo:
    @pytest.mark.asyncio
    async def test_empty_log_z_is_noop(self):
        app = KwsedApp(
            DemoDevice(),
            catalog=_catalog.empty(),
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            before = len(app._undo_log)
            app.action_undo()
            await pilot.pause()
            assert len(app._undo_log) == before

    @pytest.mark.asyncio
    async def test_refused_write_keeps_entry(self):
        class FailingDevice:
            def __init__(self):
                self.channel = 0

            def write_bank(self, bank, kind, data, **kw):
                raise RuntimeError("refused")

        app = KwsedApp(
            FailingDevice(),
            catalog=_catalog.empty(),
            allow_write=True,
        )
        app._record_write(11, banks.Kind.PATCH, b"\x00" * 10)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_undo()
            await pilot.pause()
            await pilot.pause()
            assert len(app._undo_log) == 1


def _bank_bytes(size: int, marker: bytes) -> bytes:
    data = bytearray(size)
    data[0 : len(marker)] = marker
    return bytes(data)


class BundleFixture:
    """The Card's three banks, as bytes, and as one backup envelope."""

    def __init__(self, marker: str) -> None:
        tag = marker.encode()
        self.bytes: Dict[str, bytes] = {
            "performances": _bank_bytes(
                banks.PERFORMANCES_PER_BANK * r.PERFORMANCE_SIZE, b"Was" + tag
            ),
            "patches": _bank_bytes(banks.PATCHES_PER_BANK * r.PATCH_SIZE, b"Was" + tag),
            "waveseq": _bank_bytes(r.WAVESEQ_BANK_SIZE, b"Was" + tag),
        }

    def framed_blocks(self, channel: int) -> Dict[str, bytes]:
        """The three banks as the framed dumps a bridge backup holds."""
        functions: Dict[str, int] = {
            "performances": m.Function.ALL_PERFORMANCE_DUMP,
            "patches": m.Function.ALL_PATCH_DUMP,
            "waveseq": m.Function.ALL_WAVESEQ_DUMP,
        }
        blocks: Dict[str, bytes] = {}
        for name, function in functions.items():
            data = self.bytes[name]
            blocks[name] = bytes(
                m.frame_bulk(function, CARD_SYSEX, data, channel=channel)
            )
        return blocks

    def envelope(self, directory: str, channel: int) -> str:
        return backup.save(
            directory,
            kind="bank-bundle",
            slot="Card",
            name="Card",
            device_id="0",
            source="kwsed",
            blocks=self.framed_blocks(channel),
        )

    def write(self, device: Any, backup_dir: str) -> None:
        device.write_bank_bundle(
            "CARD",
            performances=self.bytes["performances"],
            patches=self.bytes["patches"],
            waveseq=self.bytes["waveseq"],
            backup_dir=backup_dir,
        )

    def read_back(self, device: Any, name: str) -> bytes:
        """One kind's bank as the device now holds it."""
        if name == "waveseq":
            packet = m.parse(device.read_raw(CARD_SYSEX, m.Function.ALL_WAVESEQ_DUMP))
            assert packet is not None
            return bytes(packet.data())
        kind = banks.Kind.PERFORMANCE if name == "performances" else banks.Kind.PATCH
        dump = device.read_bank("CARD", kind)
        return bytes(b"".join(dump.records))


class TestSingleKindUndo:
    """A plain bank write is one record too, and it must go back decoded."""

    @pytest.mark.asyncio
    async def test_a_patch_write_is_restored_from_its_framed_backup(self, tmp_path):
        from tests.test_write import PATCH_BANK, Unit

        old = bytearray(PATCH_BANK)
        old[0:9] = b"Glass Mer"
        unit = Unit(contents=bytes(old))
        fresh = bytearray(PATCH_BANK)
        fresh[0:9] = b"Kelp Fore"
        result = unit.bridge.write_bank(
            11, banks.Kind.PATCH, bytes(fresh), backup_dir=str(tmp_path)
        )

        app = KwsedApp(unit.bridge, catalog=_catalog.empty(), allow_write=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._record_write(result)
            assert len(app._undo_log) == 1
            app.action_undo()
            for _ in range(10):
                await pilot.pause()
                if len(app._undo_log) == 0:
                    break
            assert len(app._undo_log) == 0, "one `z` must consume the record"
            after = m.parse(unit.bridge.read_raw(CARD_SYSEX, m.Function.ALL_PATCH_DUMP))
            assert after is not None
            assert after.data()[: len(PATCH_BANK)] == bytes(old)


class TestBundleUndo:
    """One bundle write, one backup envelope, one `z` -- whole."""

    @pytest.mark.asyncio
    async def test_one_undo_restores_a_whole_bundle(self, tmp_path):
        device = DemoDevice()
        old = BundleFixture("Old")
        new = BundleFixture("New")
        demo_dir = str(tmp_path)
        # the pre-write state, and then the bundle write itself
        old.write(device, demo_dir)
        backup_path = old.envelope(str(tmp_path / "store"), device.channel)
        new.write(device, demo_dir)
        bundles_before = device.bundle_write_count

        app = KwsedApp(device, catalog=_catalog.empty(), allow_write=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._record_write(
                {
                    "bank": "Card",
                    "kind": "bank-bundle",
                    "kinds": ["waveseq", "patches", "performances"],
                    "backup": backup_path,
                    "verified": True,
                    "mismatch": None,
                }
            )
            assert len(app._undo_log) == 1, "a bundle is one undo record"
            app.action_undo()
            for _ in range(10):
                await pilot.pause()
                if len(app._undo_log) == 0:
                    break
            assert len(app._undo_log) == 0, "one `z` must consume the record"
            assert device.bundle_write_count == bundles_before + 1, (
                "the undo must go through the bundle write, not three plain ones"
            )
            for name in ("performances", "patches", "waveseq"):
                assert old.read_back(device, name) == old.bytes[name], (
                    f"{name} was not restored"
                )
            assert new.read_back(device, "patches") != new.bytes["patches"], (
                "nothing was undone -- the write's own bytes are still there"
            )

    @pytest.mark.asyncio
    async def test_one_undo_restores_a_composite_write(self, tmp_path):
        device = DemoDevice()
        old = BundleFixture("Old")
        new = BundleFixture("New")
        demo_dir = str(tmp_path)
        old.write(device, demo_dir)
        backup_path = old.envelope(str(tmp_path / "store"), device.channel)
        # an older Multi Mode Setup, framed the way a bridge dump is
        from kws import multi as multi_mod

        parts: list[tuple[int, int]] = [(0, 0), (1, 9), (2, 100)]
        multi_old = bytearray(
            multi_mod.NAME_BASE + multi_mod.MULTISETS * multi_mod.NAME_SIZE
        )
        for at, value in parts:
            multi_old[at] = value
        header: list[int] = [0, 1]
        nibbles: list[int] = list(m.to_nibbles(bytes(multi_old)))
        check = m.checksum([*header, *nibbles])
        multi_frame = bytes(
            m.frame(
                m.Function.MULTISET_DUMP,
                [*header, *nibbles, check],
                channel=device.channel,
            )
        )
        multi_path = backup.save(
            str(tmp_path / "store"),
            kind="multi",
            slot="multi",
            name="Multi Mode Setup",
            device_id="0",
            source="kwsed",
            blocks={"dump": multi_frame},
        )

        app = KwsedApp(device, catalog=_catalog.empty(), allow_write=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            # the staged write replaced the bundle and the multisets
            new.write(device, demo_dir)
            device.write_multi(_bank_bytes(64, b"Multi New"), backup_dir=demo_dir)
            app._record_write({"backups": [backup_path], "multi": multi_path})
            assert len(app._undo_log) == 1, "a composite write is one record"
            app.action_undo()
            for _ in range(10):
                await pilot.pause()
                if len(app._undo_log) == 0:
                    break
            assert len(app._undo_log) == 0
            for name in ("performances", "patches", "waveseq"):
                assert old.read_back(device, name) == old.bytes[name], (
                    f"{name} was not restored"
                )
            assert device.multi_written is not None
            assert bytes(device.multi_written) == multi_frame, (
                "the multisets must come back too"
            )


class TestPlainDemoUndo:
    @pytest.mark.asyncio
    async def test_a_plain_demo_write_records_one_undo_step(self, tmp_path):
        device = DemoDevice()
        old = BundleFixture("Old")
        new = BundleFixture("New")
        old.write(device, str(tmp_path))
        result_b = device.write_bank_bundle(
            "CARD",
            performances=new.bytes["performances"],
            patches=new.bytes["patches"],
            waveseq=new.bytes["waveseq"],
            backup_dir=str(tmp_path),
        )

        app = KwsedApp(
            device,
            catalog=_catalog.empty(),
            allow_write=True,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app._record_write(result_b)
            assert len(app._undo_log) == 1
            app.action_undo()
            for _ in range(10):
                await pilot.pause()
                if len(app._undo_log) == 0:
                    break
            assert len(app._undo_log) == 0
            for name in ("performances", "patches", "waveseq"):
                assert old.read_back(device, name) == old.bytes[name]
