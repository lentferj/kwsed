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

"""The browser, driven headlessly against the demo device."""

import pytest

from kws import banks
from kws import catalog as cat
from kwsed.app import KwsedApp
from kwsed.demo import DemoDevice
from vinsynlib.ui.report import ReportScreen as SharedReportScreen
from vinsynlib.ui.work import BusyGate

pytestmark = pytest.mark.asyncio


@pytest.fixture
def app():
    catalog = cat.Catalog([cat.Entry("RAM1", 0, "Book Name", banks.Kind.PERFORMANCE)])
    return KwsedApp(DemoDevice(), catalog=catalog)


class TestStartup:
    async def test_both_panes_fill(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            # ten physical banks x two kinds, plus the Card's forty
            # sub-banks x two kinds (decision 4-A1)
            assert app.query_one("#bank-table").row_count == 22 + 80 + 32
            assert app.query_one("#slot-table").row_count == 50


class TestCursorIsSilent:
    async def test_moving_sends_nothing(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#slot-table").focus()
            for _ in range(10):
                await pilot.press("down")
            await pilot.pause()
            assert app.device.selected_log == []

    async def test_changing_bank_sends_nothing(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(4):
                await pilot.press("down")
            await pilot.pause()
            assert app.device.selected_log == []

    async def test_enter_is_what_selects(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#slot-table").focus()
            await pilot.press("enter")
            for _ in range(6):
                await pilot.pause()
            assert len(app.device.selected_log) == 1


class TestReadingIsSilent:
    async def test_r_reads_without_selecting(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("r")
            for _ in range(10):
                await pilot.pause()
            assert app.device.selected_log == []
            assert app.catalog.is_live("RAM1", 0, banks.Kind.PERFORMANCE)

    async def test_a_difference_from_the_catalog_is_flagged(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("r")
            for _ in range(10):
                await pilot.pause()
            assert app.catalog.differs("RAM1", 0, banks.Kind.PERFORMANCE)

    async def test_the_request_names_the_kind(self, app):
        """Each kind has its own request; the reply is filed under it."""
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("r")
            for _ in range(10):
                await pilot.pause()
            # The pane is showing performances, so performances come back --
            # never the other kind filed under this pane's numbers.
            assert app.catalog.is_live("RAM1", 0, banks.Kind.PERFORMANCE)
            assert not app.catalog.is_live("RAM1", 0, banks.Kind.PATCH)


class TestBothKindsAreListed:
    async def test_a_bank_appears_once_per_kind(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            kinds = {kind for _number, kind in app._pairs}
            assert {banks.Kind.PERFORMANCE, banks.Kind.PATCH}.issubset(kinds)
            assert len(app._real_pairs) == 22
            assert len(app._pairs) == 22 + 80 + 32


class TestDeviceInfoIsGated:
    """`_info_worker` must respect `_busy` like the other workers.

    `action_device_info` used to skip the check entirely, so pressing `i`
    while a read was in flight ran `identify()` concurrently through the
    same bridge -- which interleaves with the in-flight read and can steal
    its reply.
    """

    async def test_pressing_i_while_busy_is_refused(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app._busy = True
            before = app.device.identify_calls
            app.action_device_info()
            for _ in range(5):
                await pilot.pause()
            assert app.device.identify_calls == before, (
                "identify must not run while the bridge is busy"
            )
            app._busy = False


class TestSharedUiAdoption:
    async def test_the_busy_gate_is_the_shared_one(self, app):
        assert isinstance(app._busy_gate, BusyGate)

    async def test_help_uses_the_shared_report_frame(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_help()
            for _ in range(5):
                await pilot.pause()
            assert isinstance(app.screen, SharedReportScreen)
            await pilot.press("escape")
            for _ in range(5):
                await pilot.pause()


class TestScanOfferScheduling:
    async def test_on_mount_schedules_scan_offer_timer(self):
        from kwsed.app import KwsedApp

        calls = []
        original = KwsedApp.set_timer

        def spy(self, delay, callback, *args, **kwargs):
            calls.append((delay, getattr(callback, "__name__", str(callback))))
            return original(self, delay, callback, *args, **kwargs)

        KwsedApp.set_timer = spy  # type: ignore[assignment]
        try:
            app = KwsedApp(DemoDevice(), catalog=cat.Catalog())
            async with app.run_test() as pilot:
                await pilot.pause()
        finally:
            KwsedApp.set_timer = original  # type: ignore[method-assign]
        assert any(d == 0.6 and name == "_maybe_offer_scan" for d, name in calls)


class TestCachedNamesAnySlot:
    def test_unnamed_slot_zero_but_named_elsewhere(self):
        from kwsed.app import KwsedApp

        catalog = cat.Catalog()
        # slot 0 unnamed, slot 1 live
        catalog.set_live_name("RAM1", 1, "Cached", banks.Kind.PERFORMANCE)
        app = KwsedApp(DemoDevice(), catalog=catalog)
        assert app._has_cached_names() is True

    def test_no_live_names_anywhere(self):
        from kwsed.app import KwsedApp

        app = KwsedApp(DemoDevice(), catalog=cat.Catalog())
        assert app._has_cached_names() is False


class TestScanOfferBusyRefusal:
    def test_scan_answer_refuses_when_busy(self):
        from kwsed.app import KwsedApp

        app = KwsedApp(DemoDevice(), catalog=cat.Catalog())
        recorded = []
        app.notify_status = lambda *a, **k: recorded.append((a, k))  # type: ignore[method-assign]
        app._busy = True
        app._scan_offer_answered("scan")
        app._busy = False
        assert any("busy" in str(a) for a, _ in recorded)


class TestSingleNotifyOnUndoSettle:
    def test_undo_settle_notifies_once(self):
        from kwsed.app import KwsedApp

        app = KwsedApp(DemoDevice(), catalog=cat.Catalog())
        count = {"n": 0}

        def spy(*a, **k):
            count["n"] += 1

        app.notify_status = spy  # type: ignore[method-assign]
        # keep status_hook pointing at spy too
        app._undo_log.status_hook = spy
        app._undo_settle([True])
        assert count["n"] == 1


class TestConfigDirOnFirstRun:
    def test_main_creates_config_dir(self, tmp_path, monkeypatch):
        import os

        # config_dir is per-platform (XDG on Linux, Application Support
        # on macOS, LOCALAPPDATA on Windows), so point it at the tmp dir
        # instead of relying on XDG_CONFIG_HOME, which only Linux reads.
        target = str(tmp_path / "cfg" / "kwsed")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        from kws import config as cfgmod

        monkeypatch.setattr(cfgmod, "config_dir", lambda tool: target)
        from kwsed import app as appmod

        monkeypatch.setattr(appmod.KwsedApp, "run", lambda self: None)
        rc = appmod.main(["--demo"])
        assert rc == 0
        assert os.path.isdir(target)
