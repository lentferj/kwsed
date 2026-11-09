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
from textual.widgets import DataTable, Label, Static

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
from vinsynlib.ui.modals import RatingFilter


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

    async def test_select_refuses_a_patch_row_and_sends_nothing(self, app):
        """The SR has no patch mode: Enter picks a Performance, and a
        patch row is refused before anything is sent (cf8be55 had the
        bridge refuse it; the browser now says so itself, first)."""
        async with app.run_test() as pilot:
            await pilot.pause()
            app._fill((0, banks.Kind.PATCH))
            app.query_one("#slot-table").focus()
            await pilot.press("enter")
            await settle(pilot)
            assert app.device.selected_log == []
            assert "select a Performance instead" in panel_text(app, "#status")
            assert "refused" in panel_text(app, "#status")

    async def test_select_still_proceeds_for_a_performance_row(self, app):
        """Same key, performance bank: the refusal is about the kind."""
        async with app.run_test() as pilot:
            await pilot.pause()
            app._fill((0, banks.Kind.PERFORMANCE))
            app.query_one("#slot-table").focus()
            await pilot.press("enter")
            await settle(pilot, 10)
            assert len(app.device.selected_log) == 1
            assert "heard through a Performance" not in panel_text(app, "#status")

    async def test_select_on_a_card_row_sends_the_card_bank(self, app):
        """The HCard's active bank is the SR's CARD bank: Enter on a card
        sub-bank auditions the slot from it. The exact bytes (CC32=1,
        PC 50+N) are pinned in test_bridge; this pins the wiring."""
        async with app.run_test() as pilot:
            await pilot.pause()
            app._active_card = 17
            app._fill((banks.bank("CARD").number, banks.Kind.PERFORMANCE), 17)
            table = app.query_one("#slot-table")
            table.focus()
            table.move_cursor(row=2)
            await pilot.press("enter")
            await settle(pilot, 10)
            assert len(app.device.selected_log) == 1
            chosen = app.device.selected_log[-1]
            assert chosen.bank.id == "CARD"
            assert chosen.number == 2
            assert "HCard bank active" in panel_text(app, "#status")

    async def test_select_refuses_a_card_patch_row(self, app):
        """A card PATCH row is still refused as a patch: the SR has no
        patch mode, whatever bank the row belongs to."""
        async with app.run_test() as pilot:
            await pilot.pause()
            app._fill((banks.bank("CARD").number, banks.Kind.PATCH), 17)
            app.query_one("#slot-table").focus()
            await pilot.press("enter")
            await settle(pilot)
            assert app.device.selected_log == []
            assert "select a Performance instead" in panel_text(app, "#status")

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


class TestWhatADifferenceMeans:
    """The same `*` has two meanings, and the detail line says which.

    In a writable bank a differing name is somebody's own work, saved over
    the factory preset, which is not an error at all; in a ROM bank it
    cannot mean that -- ROM does not change -- so the printed list is the
    wrong one. Both former renderings agreed that a slot differs and
    stopped there; the wording is the family's, via
    `kws.catalog.disagreement_words`.
    """

    async def test_a_writable_difference_is_saved_work(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.catalog.set_live_name("RAM1", 0, "Unit Name", banks.Kind.PERFORMANCE)
            app._fill((0, banks.Kind.PERFORMANCE))
            assert "saved over the factory preset" in panel_text(app, "#detail")
            assert "the printed list is wrong" not in panel_text(app, "#detail")

    async def test_a_rom_difference_means_the_catalog_is_wrong(self, store):
        catalog = cat.Catalog([cat.Entry("ROM4", 0, "Printed Name", banks.Kind.PATCH)])
        app = KwsedApp(DemoDevice(), catalog=catalog, favorites=store)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.catalog.set_live_name("ROM4", 0, "Unit Name", banks.Kind.PATCH)
            # Display position: 3 is ROM 4, which is not writable.
            app._fill((3, banks.Kind.PATCH))
            text = panel_text(app, "#detail")
            assert "the printed list is wrong; the unit is right" in text
            assert "saved over the factory preset" not in text
            assert "read only" not in text, "the disagreement says it instead"

    async def test_a_writable_read_reports_its_names_as_saved_work(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("r")
            for _ in range(10):
                await pilot.pause()
            assert "saved over the factory preset" in panel_text(app, "#status")

    async def test_a_rom_read_reports_the_catalog_as_the_wrong_one(self, store):
        catalog = cat.Catalog([cat.Entry("ROM4", 0, "Printed Name", banks.Kind.PATCH)])
        app = KwsedApp(DemoDevice(), catalog=catalog, favorites=store)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._adopt(3, banks.Kind.PATCH, {0: "Unit Name"})
            await settle(pilot)
            status = panel_text(app, "#status")
            assert "1 differ: the printed list is wrong; the unit is right" in status


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

    def test_help_covers_the_hcard_and_the_current_keys(self):
        """The help is the full reference: the legend is capped, and the
        stale 'Bank Select not established' note is gone."""
        from kwsed.app import _HELP

        for needle in (
            "HCard",
            "capture the bank",
            "label a captured bank",
            "card sweep",
            "Multiset",
            "Bank Select is established",
            "undo",
            "write gate",
            "scan the ten internal banks",
        ):
            assert needle in _HELP, needle
        assert "not established" not in _HELP


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
    async def test_it_lists_ten_banks_twice_and_the_card_forty_ways(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#bank-table").row_count == 22 + 80 + 32

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


class TestRatingFilterKeysOnKindQualifiedBankId:
    def test_rated_entry_visible_unrated_not(self, app, store):
        bank = banks.bank(0)
        kind = banks.Kind.PERFORMANCE
        store.set_rating(favorite_bank_id(bank.id, kind), 0, 3)
        entries = [banks.slot(0, kind, 0), banks.slot(0, kind, 1)]
        app.rating_filter = RatingFilter(stars=3, at_least=True)
        out = app._by_rating(entries)
        assert [e.number for e in out] == [0]


class TestTheCardSubList:
    """The Card row expands into the HCard's forty banks (decision 4-A1).

    An unread bank is greyed rather than hidden, because the forty rows are
    why the Card exists here; a captured one shows the name it reported and
    a `*` where that differs from the printed table.
    """

    def _app(self, tmp_path, *, card_name=None, seeded=True):
        from kws import cards

        path = str(tmp_path / "cards.json")
        if seeded:
            cards.save(
                cards.capture(
                    7,
                    "Invented Card",
                    {0: "Opening"},
                    {0: "Bell"},
                    captured_at="2026-11-07T12:00:00+00:00",
                ),
                path,
            )
        return KwsedApp(
            DemoDevice(card_name=card_name),
            catalog=cat.Catalog(),
            cards_path=path,
        )

    async def test_unread_banks_are_greyed_not_captured(self, tmp_path):
        app = self._app(tmp_path, seeded=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#bank-table")
            cells = [str(cell) for cell in table.get_row("card:08:performance")]
            assert "not captured" in " ".join(cells)

    async def test_a_captured_bank_shows_its_name(self, tmp_path):
        app = self._app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#bank-table")
            cells = [str(cell) for cell in table.get_row("card:07:performance")]
            assert "Invented Card" in " ".join(cells)

    async def test_the_right_pane_shows_archive_names(self, tmp_path):
        app = self._app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#bank-table")
            table.move_cursor(
                row=app._pairs.index((banks.bank("CARD").number, "performance")) + 7 * 2
            )
            await settle(pilot)
            assert slot_row(app) == 50
            assert "Opening" in str(app.query_one("#slot-table").get_row_at(0))

    async def test_c_captures_the_active_bank(self, tmp_path):
        from kws import cards

        app = self._app(tmp_path, card_name='Korg WPC-13 "Performance 3"', seeded=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_capture_card()
            await settle(pilot, 30)
            assert 7 in app._archive
            assert cards.load(app._cards_path)[7].name == 'Korg WPC-13 "Performance 3"'
            assert "captured Card 07" in str(app.query_one("#status").content)

    async def test_c_files_the_selected_bank_on_your_say_so(self, tmp_path):
        """The real unit's card-name field is unconfirmed, so a selected
        Card row is evidence enough to file an uncaptured bank."""
        app = self._app(tmp_path, card_name=None, seeded=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#bank-table")
            table.move_cursor(
                row=app._pairs.index(
                    (banks.bank("CARD").number, banks.Kind.PERFORMANCE)
                )
                + 33 * 2
            )
            await settle(pilot)
            assert app._current_card == 33
            app.action_capture_card()
            await settle(pilot, 30)
            assert 33 in app._archive
            assert app._archive[33].name == ""
            assert app._archive[33].verified is False
            assert app._active_card == 33
            status = panel_text(app, "#status")
            assert "captured Card 33" in status
            assert "unverified" in status
            assert "filed as bank 33 on your say-so" in status
            # The slot list and the Card row were refreshed from the capture.
            assert app._current_card == 33
            slot = " ".join(
                str(cell) for cell in app.query_one("#slot-table").get_row_at(0)
            )
            assert "Velvet Stack" in slot
            row = " ".join(str(cell) for cell in table.get_row("card:33:performance"))
            assert "(unnamed)" in row

    @staticmethod
    def _demo_digest():
        """The digest of the demo device's fixed CARD content."""
        from kws import cards

        device = DemoDevice()
        perf = device.read_bank("CARD", banks.Kind.PERFORMANCE)
        patch = device.read_bank("CARD", banks.Kind.PATCH)
        return cards.content_digest(perf.records, patch.records)

    def _seeded_with_digest(self, tmp_path, digest):
        from kws import cards

        path = str(tmp_path / "cards.json")
        cards.save(
            cards.capture(
                7,
                "Invented Card",
                {0: "Opening"},
                {0: "Bell"},
                captured_at="2026-11-07T12:00:00+00:00",
                digest=digest,
                verified=True,
            ),
            path,
        )
        return KwsedApp(DemoDevice(), catalog=cat.Catalog(), cards_path=path)

    async def test_capture_confirms_the_bank_by_content_digest(self, tmp_path):
        """The same content as the archive's Card 07 confirms bank 07."""
        app = self._seeded_with_digest(tmp_path, self._demo_digest())
        async with app.run_test() as pilot:
            await pilot.pause()
            app._fill((banks.bank("CARD").number, banks.Kind.PERFORMANCE), 7)
            app.action_capture_card()
            await settle(pilot, 30)
            assert app._active_card == 7
            assert app._archive[7].verified is True
            assert "confirmed" in panel_text(app, "#status")
            row = " ".join(
                str(cell)
                for cell in app.query_one("#bank-table").get_row("card:07:performance")
            )
            assert "• " in row

    async def test_capture_of_another_banks_content_files_under_the_evidence(
        self, tmp_path
    ):
        """Content that is Card 07, captured from the Card 33 row, files
        under 07 and says to switch the HCard if 33 was meant."""
        app = self._seeded_with_digest(tmp_path, self._demo_digest())
        async with app.run_test() as pilot:
            await pilot.pause()
            app._fill((banks.bank("CARD").number, banks.Kind.PERFORMANCE), 33)
            app.action_capture_card()
            await settle(pilot, 30)
            assert app._active_card == 7
            assert 33 not in app._archive
            status = panel_text(app, "#status")
            assert "filed under 07" in status
            assert "Switch the HCard to 33" in status

    async def test_select_refuses_a_card_row_that_is_not_the_active_bank(
        self, tmp_path
    ):
        app = self._seeded_with_digest(tmp_path, self._demo_digest())
        async with app.run_test() as pilot:
            await pilot.pause()
            app._active_card = 7
            app._fill((banks.bank("CARD").number, banks.Kind.PERFORMANCE), 33)
            app.query_one("#slot-table").focus()
            await pilot.press("enter")
            await settle(pilot)
            assert app.device.selected_log == []
            assert "was on Card 07" in panel_text(app, "#status")

    async def test_select_refuses_a_card_row_when_the_active_bank_is_unknown(
        self, tmp_path
    ):
        """A fresh session that has never captured cannot tell which bank
        the card is on, so it refuses rather than auditioning the wrong
        bank silently."""
        app = self._app(tmp_path, seeded=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._active_card is None
            app._fill((banks.bank("CARD").number, banks.Kind.PERFORMANCE), 33)
            app.query_one("#slot-table").focus()
            await pilot.press("enter")
            await settle(pilot)
            assert app.device.selected_log == []
            assert "has not confirmed" in panel_text(app, "#status")

    async def test_the_active_bank_survives_a_restart(self, tmp_path):
        """The last confirmed bank is persisted, so a new session warns
        before auditioning from a different one."""
        from kws import cards

        path = str(tmp_path / "cards.json")
        cards.set_active(28, path)
        assert cards.load_active(path) == 28
        # Capturing another bank must not clear the active hint.
        cards.save(cards.capture(7, "x", {0: "a"}, {0: "b"}), path)
        assert cards.load_active(path) == 28

        app = KwsedApp(DemoDevice(), catalog=cat.Catalog(), cards_path=path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._active_card == 28
            app._fill((banks.bank("CARD").number, banks.Kind.PERFORMANCE), 29)
            app.query_one("#slot-table").focus()
            await pilot.press("enter")
            await settle(pilot)
            assert app.device.selected_log == []
            assert "was on Card 28" in panel_text(app, "#status")

    async def test_an_unlabelled_captured_bank_says_press_l(self, tmp_path):
        app = self._app(tmp_path, card_name=None, seeded=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._fill((banks.bank("CARD").number, banks.Kind.PERFORMANCE), 33)
            app.action_capture_card()
            await settle(pilot, 30)
            app._detail(0)
            assert "L to label" in panel_text(app, "#detail")

    async def test_a_capture_persists_the_active_bank(self, tmp_path):
        from kws import cards

        app = self._app(tmp_path, card_name=None, seeded=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._fill((banks.bank("CARD").number, banks.Kind.PERFORMANCE), 33)
            app.action_capture_card()
            await settle(pilot, 30)
            assert cards.load_active(app._cards_path) == 33

    async def test_inactive_card_rows_are_greyed_and_italic(self, tmp_path):
        app = self._app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._active_card = 7
            app._refresh_card_row(7)
            app._refresh_card_row(8)
            table = app.query_one("#bank-table", DataTable)
            active = " ".join(
                str(cell) for cell in table.get_row("card:07:performance")
            )
            other = " ".join(str(cell) for cell in table.get_row("card:08:performance"))
            assert "• " in active
            assert "[dim][i]" not in active
            assert "[dim][i]" in other

    async def test_a_capture_moves_the_active_marker(self, tmp_path):
        app = self._app(tmp_path, card_name=None, seeded=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._active_card = 7
            app._fill((banks.bank("CARD").number, banks.Kind.PERFORMANCE), 8)
            app.action_capture_card()
            await settle(pilot, 30)
            assert app._active_card == 8
            table = app.query_one("#bank-table", DataTable)
            new = " ".join(str(cell) for cell in table.get_row("card:08:performance"))
            old = " ".join(str(cell) for cell in table.get_row("card:07:performance"))
            assert "• " in new
            assert "[dim][i]" in old

    async def test_the_startup_probe_confirms_the_active_bank(self, tmp_path):
        app = self._seeded_with_digest(tmp_path, self._demo_digest())
        async with app.run_test() as pilot:
            await pilot.pause()
            app._auto_capture_card()
            await settle(pilot, 30)
            assert app._active_card == 7
            assert app._archive[7].verified is True
            assert "confirmed" in panel_text(app, "#status")

    async def test_the_startup_probe_reports_an_unknown_bank_as_information(
        self, tmp_path
    ):
        app = self._app(tmp_path, card_name=None, seeded=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._auto_capture_card()
            await settle(pilot, 30)
            assert app._active_card is None
            status = panel_text(app, "#status")
            assert "not in the archive yet" in status
            assert "refused" not in status

    async def test_the_startup_probe_skips_without_a_cards_path(self):
        app = KwsedApp(DemoDevice(), catalog=cat.Catalog())
        async with app.run_test() as pilot:
            await pilot.pause()
            app._auto_capture_card()
            await settle(pilot)
            assert app._active_card is None
            assert app._archive == {}

    def test_the_startup_probe_is_off_by_default(self, tmp_path):
        app = self._app(tmp_path)
        assert app._auto_capture is False

    async def test_no_selected_card_still_resolves_from_contents(self, tmp_path):
        """With no Card row selected, a captured fingerprint still files."""
        from kws import cards
        from kwsed.demo import demo_name

        card = banks.bank("CARD")
        path = str(tmp_path / "cards.json")
        cards.save(
            cards.capture(
                7,
                "Invented Card",
                {0: demo_name(card.id, banks.Kind.PERFORMANCE, 0)},
                {0: demo_name(card.id, banks.Kind.PATCH, 0)},
                captured_at="2026-11-07T12:00:00+00:00",
            ),
            path,
        )
        app = KwsedApp(
            DemoDevice(card_name=None),
            catalog=cat.Catalog(),
            cards_path=path,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._current_card is None
            app.action_capture_card()
            await settle(pilot, 30)
            assert 7 in app._archive
            assert "captured Card 07" in panel_text(app, "#status")

    async def test_c_notifies_the_progress_line_before_the_read(self, tmp_path):
        from kws import cards

        app = self._app(tmp_path, card_name='Korg WPC-13 "Performance 3"', seeded=False)
        recorded = []

        def spy(*args, **kwargs):
            recorded.append((args, kwargs))

        app.notify_status = spy
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_capture_card()
            await settle(pilot, 30)
        texts = [str(entry[0][0]) for entry in recorded if entry[0]]
        assert any("capturing the Card bank" in text for text in texts)
        assert any("captured Card" in text for text in texts)
        assert cards.load(app._cards_path)[7].name == 'Korg WPC-13 "Performance 3"'

    async def test_an_unknown_card_name_is_refused_not_filed(self, tmp_path):
        app = self._app(tmp_path, card_name="Not In The Table", seeded=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._current_card is None
            app.action_capture_card()
            await settle(pilot, 30)
            assert app._archive == {}
            assert "could not tell" in str(app.query_one("#status").content)

    async def test_r_on_a_card_row_points_at_capture(self, tmp_path):
        app = self._app(tmp_path, seeded=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._fill((banks.bank("CARD").number, "performance"), 7)
            app.action_read_names()
            assert "press C" in str(app.query_one("#status").content)

    async def test_a_long_status_is_not_clipped(self, app):
        """The status is height:auto up to 3 lines; a 110-character refusal
        used to be cut to one line."""
        async with app.run_test() as pilot:
            await pilot.pause()
            app.notify_status(
                "refused · the HCard is on Card 28 (last confirmed); select "
                "Card 29 on the HCard first, or press C to re-check",
                refused=True,
            )
            await settle(pilot)
            assert app.query_one("#status").size.height >= 2

    async def test_a_long_card_label_is_clipped_in_the_row(self, tmp_path):
        """A long label is shortened with an ellipsis, and the bank table
        no longer overflows its pane sideways."""
        from kws import cards

        path = str(tmp_path / "cards.json")
        cards.save(
            cards.capture(
                29,
                "",
                {0: "Calliosynth"},
                {0: "Bell"},
                label="A Very Long HCard Bank Label For Testing",
            ),
            path,
        )
        app = KwsedApp(DemoDevice(), catalog=cat.Catalog(), cards_path=path)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            table = app.query_one("#bank-table", DataTable)
            cells = [str(cell) for cell in table.get_row("card:29:performance")]
            assert any(cell.endswith("…") for cell in cells)
            assert table.virtual_size.width <= table.size.width

    async def test_the_card_detail_fits_its_pane(self, tmp_path):
        app = self._app(tmp_path)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            app._fill((banks.bank("CARD").number, "performance"), 7)
            await settle(pilot)
            assert app.query_one("#detail").size.height >= 3

    async def test_the_generic_detail_names_the_real_program_change(self, app):
        """The Bank Select mapping is established now, so the detail says
        which CC32/PC were sent instead of the old 'not established' note."""
        async with app.run_test() as pilot:
            await pilot.pause()
            app._fill((banks.bank("ROM4").number, banks.Kind.PERFORMANCE))
            app.query_one("#slot-table").move_cursor(row=7)
            await settle(pilot)
            detail = panel_text(app, "#detail")
            assert "Bank Select LSB" in detail
            assert "[b]2[/b]" in detail
            assert "[b]57[/b]" in detail
            assert "not established" not in detail


class TestCardSweep:
    """Walking the forty banks with a prompt between each.

    The HCard cannot be switched remotely, so the sweep is only as good as
    its pause: it must file the bank it asked for, and stop between banks
    rather than mid-read.
    """

    async def test_it_files_the_prompted_bank_and_stops_on_request(self, tmp_path):
        screens = []
        app = KwsedApp(
            DemoDevice(card_name='WPC-11 "Performance 1"'),
            # A non-empty catalog keeps the scan offer from firing and
            # pushing a screen this fake would also have to answer.
            catalog=cat.Catalog(
                [cat.Entry("RAM1", 0, "Book Name", banks.Kind.PERFORMANCE)]
            ),
            cards_path=str(tmp_path / "cards.json"),
        )
        original_push = app.push_screen

        def fake_push(screen, callback=None):
            screens.append(screen)
            if callback is not None:
                callback(len(screens) == 1)

        app.push_screen = fake_push  # type: ignore[assignment]
        try:
            async with app.run_test() as pilot:
                await pilot.pause()
                app.action_card_sweep()
                for _ in range(60):
                    await pilot.pause()
                    if not app._card_sweeping:
                        break
                status = str(app.query_one("#status", Static).content)
        finally:
            app.push_screen = original_push  # type: ignore[method-assign]

        assert len(screens) == 2, "one bank captured, then the stop screen"
        assert screens[0].number == 0
        assert screens[1].number == 1
        assert set(app._archive) == {0}
        assert not app._exchange.locked()
        assert "stopped between banks" in status

    async def test_it_refuses_while_busy(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app._busy = True
            app.action_card_sweep()
            app._busy = False
            assert "busy" in str(app.query_one("#status").content)


class TestCardPcmWording:
    """Banks 16-18 need a WSC PCM card; the detail line says so.

    A missing PCM card is silent on the unit -- the patch plays the wrong
    sound rather than failing -- so the browser has to be the place that
    says it, in both states: captured and not.
    """

    def _app(self, tmp_path, *, seeded=False):
        from kws import cards

        path = str(tmp_path / "cards.json")
        if seeded:
            cards.save(
                cards.capture(
                    16,
                    'WPC-22 "Performance 12"',
                    {0: "Needs PCM"},
                    {0: "Wave"},
                    captured_at="2026-11-07T12:00:00+00:00",
                ),
                path,
            )
        return KwsedApp(
            DemoDevice(),
            catalog=cat.Catalog(),
            cards_path=path,
        )

    async def test_an_uncaptured_pcm_bank_says_what_it_needs(self, tmp_path):
        app = self._app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._fill((banks.bank("CARD").number, banks.Kind.PERFORMANCE), 16)
            detail = panel_text(app, "#detail")
            assert "not captured" in detail
            assert "needs WSC-01" in detail

    async def test_a_captured_pcm_bank_still_says_what_it_needs(self, tmp_path):
        app = self._app(tmp_path, seeded=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._fill((banks.bank("CARD").number, banks.Kind.PERFORMANCE), 16)
            detail = panel_text(app, "#detail")
            assert "captured" in detail
            assert "needs WSC-01" in detail

    async def test_the_list_marks_an_uncaptured_pcm_bank(self, tmp_path):
        app = self._app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#bank-table")
            cells = [str(cell) for cell in table.get_row("card:16:performance")]
            joined = " ".join(cells)
            assert "not captured" in joined
            assert "needs WSC-01" in joined


class TestTheMultisetBrowser:
    """The 32 Multisets are browser rows, not a modal."""

    async def test_m_reads_and_focuses_the_multiset_rows(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_multi()
            await settle(pilot, 30)
            assert len(app._multisets) == 32
            assert len(app.screen_stack) == 1
            assert app._current_multi is not None
            assert app.query_one("#slot-table").row_count == 16
            table = app.query_one("#bank-table", DataTable)
            row = " ".join(str(cell) for cell in table.get_row("multi:03"))
            assert "Demo Set 03" in row

    async def test_a_used_channel_says_used_not_the_flag_wording(self, app):
        """The demo dump leaves every flag byte 0 while every part is
        used: the channel must read from Part.used, not from the
        unidentified flag (a flag-based on/off showed every demo
        channel as -- or off).
        """
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_multi()
            await settle(pilot, 30)
            assert app._current_multi is not None
            ms = app._multisets[3]
            assert all(part.used for part in ms.parts)
            assert all(part.flag == 0 for part in ms.parts)
            app._fill_multiset(3)
            await settle(pilot)
            table = app.query_one("#slot-table", DataTable)
            cells = [str(cell) for cell in table.get_row("multi:03:01")]
            assert cells[-1] == "used"
            app._detail(0)
            detail = panel_text(app, "#detail")
            assert "· used" in detail
            assert "off" not in detail
            assert "· on" not in detail

    async def test_an_unused_channel_says_unused_and_a_dash(self, app):
        """A part with no assignment (performance 50 is out of range)
        renders as -- in the column and unused in the detail.
        """
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_multi()
            await settle(pilot, 30)
            assert app._current_multi is not None
            from dataclasses import replace as _replace

            ms = app._multisets[3]
            parts = list(ms.parts)
            parts[0] = _replace(parts[0], performance=50)
            app._multisets[3] = _replace(ms, parts=tuple(parts))
            app._fill_multiset(3)
            await settle(pilot)
            table = app.query_one("#slot-table", DataTable)
            cells = [str(cell) for cell in table.get_row("multi:03:01")]
            assert cells[1] == "--"
            assert cells[-1] == "--"
            app._detail(0)
            detail = panel_text(app, "#detail")
            assert "· unused" in detail
            assert "· used" not in detail
            assert "off" not in detail

    async def test_enter_on_a_multiset_row_selects_it(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_multi()
            await settle(pilot, 30)
            table = app.query_one("#bank-table", DataTable)
            table.move_cursor(row=app._row_multis.index(3))
            await settle(pilot)
            assert app._current_multi == 3
            table.focus()
            await pilot.press("enter")
            await settle(pilot, 10)
            assert app.device.selected_multi == 3
            assert "Multiset 03" in panel_text(app, "#status")

    async def test_e_before_m_refuses(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_edit_multi()
            assert "read multi first" in panel_text(app, "#status")

    async def test_r_on_a_multiset_row_refuses_the_name_read(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_multi()
            await settle(pilot, 30)
            assert app._current_multi is not None
            current = app._current_multi
            calls = []

            def recorder(*args, **kwargs):
                calls.append((args, kwargs))

            app.device.read_bank_names = recorder
            app.action_read_names()
            await settle(pilot)
            assert calls == []
            assert "no name list" in panel_text(app, "#status")
            assert app._current_multi == current

    def test_the_demo_answers_a_multi_dump(self):
        from kws import messages as msgs
        from kws import multi as multi_mod

        dump = DemoDevice().read_multi()
        packet = msgs.parse(dump)
        assert packet is not None
        sets = multi_mod.parse(packet.data())
        assert len(sets) == 32
        assert sets[3].name == "Demo Set 03"


class TestTheMultisetEditor:
    async def _open(self, app, pilot):
        from kwsed.app import MultiEditScreen

        await pilot.pause()
        app.action_multi()
        await settle(pilot, 30)
        table = app.query_one("#bank-table", DataTable)
        table.move_cursor(row=app._row_multis.index(3))
        await settle(pilot)
        app.action_edit_multi()
        await settle(pilot, 6)
        assert isinstance(app.screen, MultiEditScreen)
        return app.screen

    async def test_it_shows_the_sixteen_channels(self, app):
        async with app.run_test() as pilot:
            screen = await self._open(app, pilot)
            table = screen.query_one("#multi-edit-table", DataTable)
            assert table.row_count == 16
            assert "Multiset 03" in str(
                screen.query_one("#multi-edit-title", Label).content
            )

    async def test_w_before_arm_refuses_and_writes_nothing(self, app):
        async with app.run_test() as pilot:
            screen = await self._open(app, pilot)
            screen.action_fire()
            assert app.device.multi_write_count == 0
            assert "not armed" in panel_text(screen, "#multi-edit-state")

    async def test_arm_then_write_stores_the_edited_setup(self, app):
        from dataclasses import replace

        from kws import multi as multi_mod

        async with app.run_test() as pilot:
            screen = await self._open(app, pilot)
            part = screen._ms().parts[0]
            screen._set_part(0, replace(part, level=42))
            screen.action_arm()
            screen.action_fire()
            await settle(pilot, 30)
            assert app.device.multi_write_count == 1
            written = multi_mod.parse(app.device.multi_written)
            assert written[3].parts[0].level == 42
            # The browser keeps what the write put there.
            assert app._multisets[3].parts[0].level == 42

    async def test_unverified_write_keeps_editor_open(self, app):
        from dataclasses import replace

        async with app.run_test() as pilot:
            screen = await self._open(app, pilot)
            original_level = screen._ms().parts[0].level
            part = screen._ms().parts[0]
            screen._set_part(0, replace(part, level=42))
            app.device.write_multi = lambda encoded, **kw: {
                "verified": False,
                "mismatch": "byte 3 differs",
            }
            before_undo = len(app._undo_log)
            screen.action_arm()
            screen.action_fire()
            await settle(pilot, 30)
            from kwsed.app import MultiEditScreen

            assert isinstance(app.screen, MultiEditScreen)
            state_text = panel_text(app.screen, "#multi-edit-state")
            status_text = panel_text(app, "#status")
            assert "byte 3 differs" in state_text or "byte 3 differs" in status_text
            assert app._multisets[3].parts[0].level == original_level
            assert len(app._undo_log) == before_undo

    async def test_cancel_leaves_the_original_unchanged(self, app):
        from dataclasses import replace

        async with app.run_test() as pilot:
            screen = await self._open(app, pilot)
            part = screen._ms().parts[0]
            screen._set_part(0, replace(part, level=42))
            await pilot.press("escape")
            await settle(pilot)
            assert app._multisets[3].parts[0].level == part.level
