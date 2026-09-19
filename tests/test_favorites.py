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

"""Favourites. Names here are invented."""

import pytest

from kws.favorites import Favorites
from kwsed.app import (KwsedApp, VIEW_ALL, VIEW_ALL_FAVOURITES, VIEW_BANK_FAVOURITES,
                      VIEW_CYCLE, KEY_HINTS)


class TestStore:
    def test_the_slot_is_the_identity_not_the_name(self, tmp_path):
        db = str(tmp_path / "f.db")
        with Favorites(db) as store:
            store.add("A", 7, name="Glass Meridian")
        with Favorites(db) as store:
            store.add("A", 7, name="renamed entirely")
            assert len(store) == 1

    def test_the_same_number_in_two_banks_is_two_favourites(self, tmp_path):
        with Favorites(str(tmp_path / "f.db")) as store:
            store.add("A", 7)
            store.add("B", 7)
            assert len(store) == 2


class TestTheCycle:
    def test_three_steps_starting_at_all(self):
        assert len(VIEW_CYCLE) == 3
        assert VIEW_CYCLE[0] == VIEW_ALL
        assert set(VIEW_CYCLE) == {VIEW_ALL, VIEW_BANK_FAVOURITES,
                                   VIEW_ALL_FAVOURITES}

    def test_it_wraps(self):
        seen, mode = [], VIEW_ALL
        for _ in range(4):
            seen.append(mode)
            mode = VIEW_CYCLE[(VIEW_CYCLE.index(mode) + 1) % len(VIEW_CYCLE)]
        assert seen == list(VIEW_CYCLE) + [VIEW_ALL]

    def test_the_keys_are_in_the_legend(self):
        legend = " ".join(KEY_HINTS)
        assert "f favourite" in legend
        assert "F favourites" in legend


class TestKindIsPartOfTheKey:
    """A bank holds 50 performances and 35 patches, and both begin with P.

    The collision that once merged them in Slot.key would merge a
    favourited performance with the patch of the same number.
    """

    def test_performance_and_patch_are_different_favourites(self, tmp_path):
        from kwsed.app import favorite_bank_id
        perf = favorite_bank_id("RAM1", "performance")
        patch = favorite_bank_id("RAM1", "patch")
        assert perf != patch
        with Favorites(str(tmp_path / "f.db")) as store:
            store.add(perf, 7, name="a performance")
            store.add(patch, 7, name="a patch")
            assert len(store) == 2
            assert store.get(perf, 7).name == "a performance"
            assert store.get(patch, 7).name == "a patch"


class TestTheChannelIsRemembered:
    """The channel set last time should be the default next time.

    rxved had the save function from its first commit and never called it,
    so it loaded a channel from a file nothing ever wrote. The round trip is
    what makes that impossible to repeat quietly.
    """

    def test_saving_then_loading_round_trips(self, tmp_path):
        from kws import config
        path = str(tmp_path / "config.toml")
        config.save_channel(9, path)
        assert config.load_channel(path) == 9

    def test_another_setting_does_not_drop_it(self, tmp_path):
        from kws import config
        path = str(tmp_path / "config.toml")
        config.save_channel(5, path)
        config.save_last_port("some port", path)
        assert config.load_channel(path) == 5
        assert config.load_last_port(path) == "some port"

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        from kws import config
        assert config.load_channel(str(tmp_path / "nope.toml")) is None

    def test_an_out_of_range_channel_is_ignored(self, tmp_path):
        """A hand-edited file must not put the app on channel 99."""
        from kws import config
        path = str(tmp_path / "config.toml")
        path_obj = tmp_path / "config.toml"
        path_obj.write_text("channel = 99\n", encoding="utf-8")
        assert config.load_channel(path) is None

    def test_an_unparsable_file_is_not_fatal(self, tmp_path):
        from kws import config
        path_obj = tmp_path / "config.toml"
        path_obj.write_text("this is not toml = = =\n", encoding="utf-8")
        assert config.load_channel(str(path_obj)) is None


class TestTheLegendIsComplete:
    """A legend that omits a key teaches the user the key does not exist."""

    #: Textual key names are not what a legend prints.
    SHOWN_AS = {"left_square_bracket": "[", "right_square_bracket": "]",
                "slash": "/", "question_mark": "?"}
    IMPLIED = {"enter", "tab"}

    def test_every_binding_appears(self):
        legend = " ".join(KEY_HINTS)
        missing = []
        for binding in KwsedApp.BINDINGS:
            if binding.key in self.IMPLIED:
                continue
            shown = self.SHOWN_AS.get(binding.key, binding.key)
            if f"{shown} " not in legend:
                missing.append(binding.key)
        assert not missing, f"bound but missing from the legend: {missing}"
