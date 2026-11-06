# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors

"""Tests for kws.undo bindings and app undo."""

import pytest

from kws import banks, undo
from kws import catalog as _catalog
from kwsed.app import KwsedApp
from kwsed.demo import DemoDevice
from vinsynlib.undo import Record


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
