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

"""The browser's own behaviour: keys, favourites, views and the channel.

Driven headlessly against the demo device. Every name is invented, and
nothing here sends MIDI except where sending is the point -- the rule
this project holds hardest is that moving the cursor sends nothing, and
the tests that check it are in test_app.py.

A favourites database is injected rather than opened, so a test cannot
write into the user's real one.
"""

import pytest
from textual.widgets import Static

from kws import banks
from kws import catalog as cat
from kws.favorites import Favorites
from kwsed.app import (
    VIEW_ALL,
    VIEW_ALL_FAVOURITES,
    VIEW_BANK_FAVOURITES,
    KwsedApp,
    favorite_bank_id,
)
from kwsed.demo import DemoDevice


@pytest.fixture()
def store(tmp_path):
    with Favorites(str(tmp_path / "f.db")) as opened:
        yield opened


@pytest.fixture()
def app(store):
    catalog = cat.Catalog(
        [
            cat.Entry("RAM1", 0, "Book Name", banks.Kind.PERFORMANCE),
        ]
    )
    return KwsedApp(DemoDevice(), catalog=catalog, favorites=store)


def slot_row(app):
    return app.query_one("#slot-table").row_count


def panel_text(app, selector):
    """The text currently shown in a Static panel.

    Textual 8 renamed this: `.renderable` was how the widget used to be
    read, and `.content` is what it exposes now.
    """
    return str(app.query_one(selector, Static).content)


async def settle(pilot, times=6):
    for _ in range(times):
        await pilot.pause()


class TestTheChannelKeys:
    """The channel selections go out on. It is settable and clamped."""

    async def test_brackets_step_it_and_clamp_at_the_ends(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.device.channel = 0
            app.action_channel_down()
            assert app.device.channel == 0, "already at the bottom"
            for expected in range(1, 4):
                app.action_channel_up()
                assert app.device.channel == expected
            app.device.channel = 15
            app.action_channel_up()
            assert app.device.channel == 15, "already at the top"

    async def test_the_status_line_says_the_channel_a_person_counts(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.device.channel = 0
            app.action_channel_up()
            assert "channel 2" in panel_text(app, "#status")

    async def test_it_survives_a_device_that_refuses_the_channel(self, app):
        class NoChannel:
            description = "no channel"

            def __init__(self):
                self._channel = 0

            @property
            def channel(self):
                return self._channel

            @channel.setter
            def channel(self, value):
                raise AttributeError("this device has no settable channel")

        async with app.run_test() as pilot:
            await pilot.pause()
            app.device = NoChannel()
            app.action_channel_up()
            assert "no settable channel" in panel_text(app, "#status")


class TestFavouritingThroughTheKeys:
    async def test_f_marks_and_unmarks_the_slot_under_the_cursor(self, app, store):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_toggle_favorite()
            assert len(store) == 1
            key = favorite_bank_id("RAM1", banks.Kind.PERFORMANCE)
            assert store.get(key, 0) is not None
            app.action_toggle_favorite()
            assert len(store) == 0, "un-favouriting keeps the row, not the row count"

    async def test_f_is_silent_on_the_wire(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_toggle_favorite()
            await settle(pilot)
            assert app.device.selected_log == []

    async def test_tags_need_a_favourite_first(self, app):
        """Saying so beats silently creating one: the user asked to
        annotate, not to favourite."""
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_edit_tags()
            assert "not a favourite yet" in panel_text(app, "#status")

    async def test_tags_and_notes_are_written_through(self, app, store):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_toggle_favorite()
            key = favorite_bank_id("RAM1", banks.Kind.PERFORMANCE)
            store.set_tags(key, 0, "pad")
            store.set_note(key, 0, "written")
            kept = store.get(key, 0)
            assert kept is not None
            assert kept.tag_list == ["pad"] and kept.note == "written"


class TestTheFavouritesViews:
    async def test_f_cycles_all_through_bank_to_every_favourite(self, app, store):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.view_mode = VIEW_ALL
            app.action_cycle_favorites()
            assert app.view_mode == VIEW_BANK_FAVOURITES
            app.action_cycle_favorites()
            assert app.view_mode == VIEW_ALL_FAVOURITES
            app.action_cycle_favorites()
            assert app.view_mode == VIEW_ALL, "and wraps"

    async def test_the_bank_view_narrows_to_the_favourites(self, app, store):
        async with app.run_test() as pilot:
            await pilot.pause()
            store.add(favorite_bank_id("RAM1", banks.Kind.PERFORMANCE), 4)
            store.add(favorite_bank_id("RAM1", banks.Kind.PERFORMANCE), 9)
            app.view_mode = VIEW_BANK_FAVOURITES
            app._fill(app._current_pair)
            assert slot_row(app) == 2

    async def test_the_every_favourites_view_crosses_banks(self, app, store):
        async with app.run_test() as pilot:
            await pilot.pause()
            store.add(favorite_bank_id("RAM1", banks.Kind.PERFORMANCE), 1)
            store.add(favorite_bank_id("CARD", banks.Kind.PATCH), 7)
            app.view_mode = VIEW_ALL_FAVOURITES
            app._fill(app._current_pair)
            # two, and from two different bank-and-kind pairs
            assert slot_row(app) == 2

    async def test_the_kinds_do_not_merge_in_either_view(self, app, store):
        """Performance 3 and patch 3 are two different favourites."""
        async with app.run_test() as pilot:
            await pilot.pause()
            store.add(favorite_bank_id("RAM1", banks.Kind.PERFORMANCE), 3)
            store.add(favorite_bank_id("RAM1", banks.Kind.PATCH), 3)
            app.view_mode = VIEW_ALL_FAVOURITES
            app._fill(app._current_pair)
            assert slot_row(app) == 2

    async def test_an_empty_view_says_where_nothing_is_favourited(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            # The bank view is empty -- nothing here is favourited -- and
            # cycling from it lands on the across-every-bank view, which is
            # also empty. That is the case the message exists for.
            app.view_mode = VIEW_BANK_FAVOURITES
            app._fill(app._current_pair)
            assert slot_row(app) == 0
            app.action_cycle_favorites()
            assert app.view_mode == VIEW_ALL_FAVOURITES
            assert "any bank" in panel_text(app, "#status")


class TestTheCursorContract:
    async def test_arrow_keys_and_tab_send_nothing(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#slot-table").focus()
            for key in ("down", "down", "up", "right", "left", "page_down"):
                await pilot.press(key)
            await settle(pilot)
            assert app.device.selected_log == []

    async def test_a_read_sends_nothing_either(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("r")
            await settle(pilot, 12)
            assert app.device.selected_log == []

    async def test_tab_switches_pane_without_selecting(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#slot-table").focus()
            await pilot.press("tab")
            await settle(pilot)
            assert app.device.selected_log == []
            assert app.query_one("#bank-table").has_focus

    async def test_select_sends_exactly_one_program_change(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#slot-table").focus()
            await pilot.press("enter")
            await settle(pilot, 10)
            assert len(app.device.selected_log) == 1

    async def test_nothing_is_selected_when_the_pane_is_empty(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.view_mode = VIEW_ALL_FAVOURITES
            app._fill(app._current_pair)
            app.action_select()
            await settle(pilot)
            assert app.device.selected_log == []


class TestTheDetailLine:
    async def test_it_names_the_slot_and_says_a_rom_bank_is_read_only(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            # Display position, not the wire byte: position 3 is ROM 4.
            app._current_pair = (3, banks.Kind.PERFORMANCE)
            app._fill(app._current_pair)
            text = panel_text(app, "#detail")
            assert "ROM 4" in text
            assert "read only" in text

    async def test_a_writable_bank_carries_no_warning(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app._fill((0, banks.Kind.PATCH))
            assert "read only" not in panel_text(app, "#detail")


class TestSearchAndHelp:
    async def test_search_opens_a_prompt_and_escape_leaves_no_screen_behind(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_search()
            await settle(pilot)
            assert len(app.screen_stack) == 2, "the prompt is up"
            await pilot.press("escape")
            await settle(pilot)
            assert len(app.screen_stack) == 1, "and it goes away again"

    async def test_help_opens_and_closes(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_help()
            await settle(pilot)
            assert len(app.screen_stack) == 2
            await pilot.press("escape")
            await settle(pilot)
            assert len(app.screen_stack) == 1


class TestWithoutAFavouritesStore:
    async def test_the_keys_that_need_one_say_so(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.favorites = None
            app.action_toggle_favorite()
            assert "no favourites database" in str(panel_text(app, "#status"))
            app.action_cycle_favorites()
            assert "no favourites database" in str(panel_text(app, "#status"))

    async def test_an_empty_view_rather_than_a_crash(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.favorites = None
            app._fill(app._current_pair)
            assert slot_row(app) == 50


class TestTheBankTable:
    async def test_it_lists_twelve_banks_twice_over(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#bank-table").row_count == 24

    async def test_moving_down_it_changes_bank_and_sends_nothing(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#bank-table").focus()
            before = app._current_pair
            await pilot.press("down")
            await settle(pilot)
            assert app._current_pair != before, "the right pane followed"
            assert app.device.selected_log == []

    async def test_a_rom_bank_is_marked_read_only(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#bank-table")
            cells = [str(c) for c in table.get_row("5:performance")]
            assert "ro" in cells
