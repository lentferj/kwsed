# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors

"""Multi Mode Setup edit: round trip and the write fence."""

import pytest

from kws import multi as multi_mod
from kwsed.app import MultiEditScreen


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
