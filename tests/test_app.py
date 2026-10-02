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

pytestmark = pytest.mark.asyncio


@pytest.fixture
def app():
    catalog = cat.Catalog([cat.Entry("RAM1", banks.Kind.PERFORMANCE, 0, "Book Name")])
    return KwsedApp(DemoDevice(), catalog=catalog)


class TestStartup:
    async def test_both_panes_fill(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            # twelve banks x two kinds
            assert app.query_one("#bank-table").row_count == 24
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
            assert app.catalog.is_live("RAM1", banks.Kind.PERFORMANCE, 0)

    async def test_a_difference_from_the_catalog_is_flagged(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("r")
            for _ in range(10):
                await pilot.pause()
            assert app.catalog.differs("RAM1", banks.Kind.PERFORMANCE, 0)

    async def test_the_request_names_the_kind(self, app):
        """Each kind has its own request; the reply is filed under it."""
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("r")
            for _ in range(10):
                await pilot.pause()
            # The pane is showing performances, so performances come back --
            # never the other kind filed under this pane's numbers.
            assert app.catalog.is_live("RAM1", banks.Kind.PERFORMANCE, 0)
            assert not app.catalog.is_live("RAM1", banks.Kind.PATCH, 0)


class TestBothKindsAreListed:
    async def test_a_bank_appears_once_per_kind(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            kinds = {kind for _number, kind in app._pairs}
            assert kinds == {banks.Kind.PERFORMANCE, banks.Kind.PATCH}
            assert len(app._pairs) == 24


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
