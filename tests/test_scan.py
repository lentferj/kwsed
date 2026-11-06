# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# kwsed is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version.

"""The scan feature: offer, answers, persistence, results and cancel."""

import os

import pytest

from kws import banks
from kws import catalog as cat
from kws import config as cfg
from kwsed.app import KwsedApp
from kwsed.demo import DemoDevice


@pytest.fixture()
def tmp_config(tmp_path):
    return str(tmp_path / "config.json")


async def settle(pilot, times=20):
    for _ in range(times):
        await pilot.pause()


class TestScanOfferOnMount:
    async def test_timer_scheduled(self):
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


class TestScanOfferAnswers:
    async def test_scan_now_triggers_scan(self, tmp_path):
        live_path = str(tmp_path / "live.json")
        app = KwsedApp(DemoDevice(), catalog=cat.Catalog(), live_path=live_path)
        recorded = []
        app.action_scan_device = lambda: recorded.append("scan")  # type: ignore[method-assign]
        app._scan_offer_answered("scan")
        assert recorded == ["scan"]

    async def test_dismiss_does_nothing(self, tmp_path):
        app = KwsedApp(DemoDevice(), catalog=cat.Catalog())
        app._scan_offer_answered("later")
        assert app._scan_offer_dismissed is False

    async def test_never_sets_dismissed(self, tmp_path):
        config_path = str(tmp_path / "config.json")
        app = KwsedApp(DemoDevice(), catalog=cat.Catalog(), config_path=config_path)
        app._scan_offer_answered("never")
        assert app._scan_offer_dismissed is True
        assert cfg.load_scan_offer_dismissed(config_path) is True


class TestScanOfferPersistence:
    async def test_never_honoured_on_next_launch(self, tmp_path):
        config_path = str(tmp_path / "config.json")
        app = KwsedApp(DemoDevice(), catalog=cat.Catalog(), config_path=config_path)
        app._scan_offer_answered("never")
        app2 = KwsedApp(DemoDevice(), catalog=cat.Catalog(), config_path=config_path)
        assert app2._scan_offer_dismissed is True


class TestScannedBanksLandInCatalog:
    async def test_live_names_present_after_scan(self, tmp_path):
        live_path = str(tmp_path / "live.json")
        catalog = cat.Catalog([cat.Entry("RAM1", 0, "Printed", banks.Kind.PERFORMANCE)])
        app = KwsedApp(DemoDevice(), catalog=catalog, live_path=live_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_scan_device()
            await settle(pilot, 40)
        assert app.catalog.live_name("RAM1", 0, banks.Kind.PERFORMANCE) is not None
        assert app.catalog.differs("RAM1", 0, banks.Kind.PERFORMANCE)


class TestScanCancel:
    async def test_cancel_stops_worker(self, tmp_path):
        live_path = str(tmp_path / "live.json")
        app = KwsedApp(DemoDevice(), catalog=cat.Catalog(), live_path=live_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_scan_device()
            # cancel mid-flight
            await pilot.pause()
            if app._scanning:
                app._scan_cancel.set()
            await settle(pilot, 40)
        assert app._scanning is False
