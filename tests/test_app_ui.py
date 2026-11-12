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

from typing import Any, Mapping, Optional

import pytest
from textual.widgets import DataTable, Label, Static

from kws import backup, banks, deps, dumps
from kws import catalog as cat
from kws import messages as m
from kws import multi as multi_mod
from kws import records as rec
from kws.favorites import Favorites
from kwsed.app import (
    VIEW_ALL,
    VIEW_ALL_FAVOURITES,
    VIEW_BANK_FAVOURITES,
    FxScreen,
    KwsedApp,
    ViewSlot,
    favorite_bank_id,
)
from kwsed.demo import DemoDevice
from kwsed.gate import EditGate
from kwsed.import_single import SingleImportScreen
from kwsed.target_bank import TargetBankScreen
from kwsed.target_screen import TargetSlotScreen
from vinsynlib.ui.modals import RatingFilter
from vinsynlib.ui.report import ReportScreen


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
        from kws import cards as kws_cards

        async with app.run_test() as pilot:
            await pilot.pause()
            store.add(favorite_bank_id("RAM1", banks.Kind.PERFORMANCE), 1)
            store.add(favorite_bank_id(kws_cards.scope_id(7), banks.Kind.PATCH), 7)
            app._active_card = 7
            app.view_mode = VIEW_ALL_FAVOURITES
            app._fill(app._current_pair)
            # two, and from two different bank-and-kind pairs
            assert slot_row(app) == 2

    async def test_the_every_favourites_view_crosses_hcard_banks(self, app, store):
        from kws import cards as kws_cards

        async with app.run_test() as pilot:
            await pilot.pause()
            store.add(
                favorite_bank_id(kws_cards.scope_id(7), banks.Kind.PERFORMANCE),
                1,
                name="From Seven",
            )
            store.add(
                favorite_bank_id(kws_cards.scope_id(8), banks.Kind.PERFORMANCE),
                2,
                name="From Eight",
            )
            app.view_mode = VIEW_ALL_FAVOURITES
            app._fill(app._current_pair)
            assert slot_row(app) == 2
            table = app.query_one("#slot-table")
            labels = [str(table.get_row_at(i)[1]) for i in range(2)]
            assert any("Card 07" in label for label in labels)
            assert any("Card 08" in label for label in labels)

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
            "Card modes",
            "card_mode",
            "show_card_defaults",
            "capture what is in the slot",
            "label the captured bank or card",
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
    why the Card exists here; a captured one shows its label, or the printed
    table name when it has none.
    """

    def _app(self, tmp_path, *, seeded=True):
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
            DemoDevice(),
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

    async def test_c_files_the_selected_bank_on_your_say_so(self, tmp_path):
        """The card name is unreadable, so a selected Card row is evidence
        enough to file an uncaptured bank."""
        app = self._app(tmp_path, seeded=False)
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

    async def test_a_named_user_bank_keeps_its_printed_name_after_capture(
        self, tmp_path
    ):
        """A capture with no readable card name must not turn a bank whose
        table entry is a real printed name into "(unnamed)"."""
        app = self._app(tmp_path, seeded=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#bank-table")
            table.move_cursor(
                row=app._pairs.index(
                    (banks.bank("CARD").number, banks.Kind.PERFORMANCE)
                )
                + 25 * 2
            )
            await settle(pilot)
            assert app._current_card == 25
            app.action_capture_card()
            await settle(pilot, 30)
            assert 25 in app._archive
            assert app._archive[25].name == ""
            row = " ".join(str(cell) for cell in table.get_row("card:25:performance"))
            # The bank column clips the long printed name on purpose.
            assert "Pro-Rec WS1 Super WaveDan" in row
            assert "(unnamed)" not in row

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
        app = self._app(tmp_path, seeded=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._fill((banks.bank("CARD").number, banks.Kind.PERFORMANCE), 33)
            app.action_capture_card()
            await settle(pilot, 30)
            app._detail(0)
            assert "L to label" in panel_text(app, "#detail")

    async def test_a_capture_persists_the_active_bank(self, tmp_path):
        from kws import cards

        app = self._app(tmp_path, seeded=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._fill((banks.bank("CARD").number, banks.Kind.PERFORMANCE), 33)
            app.action_capture_card()
            await settle(pilot, 30)
            assert cards.load_active(app._cards_path) == 33

    async def test_labelling_a_bank_without_a_path_stays_in_memory(
        self, tmp_path, monkeypatch
    ):
        """A cards_path of None means memory only, as in ``--demo``.

        ``kws.cards.set_label`` would fall back to the user's real archive
        and write the label there instead of here.
        """
        from textual.widgets import Input

        from kws import cards
        from kwsed.app import PromptScreen

        forbidden = tmp_path / "never-written" / "cards.json"
        monkeypatch.setattr(cards, "default_path", lambda: str(forbidden))
        app = KwsedApp(DemoDevice(), catalog=cat.Catalog(), cards_path=None)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._archive[7] = cards.capture(
                7, "Invented Card", {0: "Opening"}, {0: "Bell"}
            )
            app._fill((banks.bank("CARD").number, banks.Kind.PERFORMANCE), 7)
            await settle(pilot)
            app.action_label_card()
            await settle(pilot, 5)
            assert isinstance(app.screen, PromptScreen)
            app.screen.query_one("#value", Input).value = "Renamed"
            await pilot.press("enter")
            await settle(pilot, 5)
            assert not forbidden.exists()
            assert app._archive[7].label == "Renamed"
            table = app.query_one("#bank-table", DataTable)
            cells = " ".join(str(cell) for cell in table.get_row("card:07:performance"))
            assert "Renamed" in cells

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
        app = self._app(tmp_path, seeded=False)
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
        app = self._app(tmp_path, seeded=True)
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
            DemoDevice(),
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
        app = self._app(tmp_path, seeded=False)
        recorded = []

        def spy(*args, **kwargs):
            recorded.append((args, kwargs))

        app.notify_status = spy
        async with app.run_test() as pilot:
            await pilot.pause()
            app._fill((banks.bank("CARD").number, banks.Kind.PERFORMANCE), 7)
            app.action_capture_card()
            await settle(pilot, 30)
        texts = [str(entry[0][0]) for entry in recorded if entry[0]]
        assert any("capturing the Card bank" in text for text in texts)
        assert any("captured Card" in text for text in texts)

    async def test_a_capture_with_no_evidence_is_refused_not_filed(self, tmp_path):
        app = self._app(tmp_path, seeded=False)
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
                "Card 29 on the HCard first, then press C to re-check",
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
            DemoDevice(),
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


class TestTheStartupCardProbeWithoutACard:
    """A launch probe that finds no card is normal, not a cable fault."""

    async def test_it_skips_when_nothing_was_captured(self, tmp_path):
        app = TestTheCardSubList()._app(tmp_path, seeded=False)
        calls = []

        def read(*args, **kwargs):
            calls.append(args)
            raise TimeoutError("no dump came back for Card.")

        app.device.read_bank = read
        async with app.run_test() as pilot:
            await pilot.pause()
            app._auto_capture_card()
            await settle(pilot, 5)
            assert calls == []
            assert app._active_card is None

    async def test_it_says_the_slot_may_be_empty(self, tmp_path):
        app = TestTheCardSubList()._app(tmp_path)  # archive has bank 7

        def read(*args, **kwargs):
            raise TimeoutError("no dump came back for Card. Program Card ...")

        app.device.read_bank = read
        async with app.run_test() as pilot:
            await pilot.pause()
            app._auto_capture_card()
            await settle(pilot, 30)
            status = panel_text(app, "#status")
            assert "card slot may be empty" in status
            assert "refused" not in status

    async def test_a_manual_capture_still_refuses(self, tmp_path):
        app = TestTheCardSubList()._app(tmp_path)

        def read(*args, **kwargs):
            raise TimeoutError("no dump came back for Card. Program Card ...")

        app.device.read_bank = read
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_capture_card()
            await settle(pilot, 30)
            status = panel_text(app, "#status")
            assert "refused" in status
            assert "no dump came back" in status


class TestCardModes:
    """The Card UI is gated by card_mode (docs/CARDS.md)."""

    def _app(
        self,
        tmp_path,
        mode,
        cards=None,
        with_favourites=False,
        show_card_defaults=False,
    ):
        from kws import physical

        pp = str(tmp_path / "physical-cards.json")
        for card in cards or []:
            physical.save(card, pp)
        store = None
        if with_favourites:
            store = Favorites(str(tmp_path / "fav.db"))
        app = KwsedApp(
            DemoDevice(),
            catalog=cat.Catalog(),
            favorites=store,
            card_mode=mode,
            show_card_defaults=show_card_defaults,
            physical_path=pp,
        )
        return app, pp

    def _card(self, cid, label, names):
        from kws import physical

        return physical.PhysicalCard(
            id=cid,
            label=label,
            digest=f"digest-{cid}",
            captured_at="2026-11-09T00:00:00+00:00",
            performances=names,
            patches={0: "Bell"},
            verified=True,
        )

    async def test_none_mode_has_no_card_rows(self, tmp_path):
        app, _ = self._app(tmp_path, "none")
        async with app.run_test() as pilot:
            await pilot.pause()
            assert len(app._pairs) == 22 + 32
            assert all(item is None for item in app._row_physical)
            app.action_capture_card()
            assert "card support is off" in panel_text(app, "#status")

    async def test_cards_mode_placeholder_when_empty(self, tmp_path):
        app, _ = self._app(tmp_path, "cards")
        async with app.run_test() as pilot:
            await pilot.pause()
            assert len(app._pairs) == 22 + 2 + 32
            table = app.query_one("#bank-table", DataTable)
            row = " ".join(
                str(cell) for cell in table.get_row("physical:none:performance")
            )
            assert "insert a card" in row
            table.move_cursor(row=app._row_physical.index(""))
            await settle(pilot)
            slots = [str(cell) for cell in app.query_one("#slot-table").get_row_at(0)]
            assert "insert a card and press C" in " ".join(slots)

    async def test_cards_mode_lists_archived_cards_and_scopes_favourites(
        self, tmp_path
    ):
        from kws import physical

        app, _ = self._app(
            tmp_path,
            "cards",
            cards=[
                self._card("c1", "Orchestra", {0: "Opening"}),
                self._card("c2", "Pads", {0: "Air"}),
            ],
            with_favourites=True,
        )
        assert len(app._pairs) == 22 + 4 + 32
        assert app.favorites is not None
        app.favorites.toggle(
            favorite_bank_id(physical.scope_id("c1"), "performance"),
            0,
            name="Opening",
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#bank-table", DataTable)
            c1 = [str(cell) for cell in table.get_row("physical:c1:performance")]
            c2 = [str(cell) for cell in table.get_row("physical:c2:performance")]
            assert "Orchestra" in " ".join(c1) and c1[-1] == "1"
            assert "Pads" in " ".join(c2) and c2[-1] == ""
            # Selecting c1 shows its names and its favourite.
            table.move_cursor(row=app._row_physical.index("c1"))
            await settle(pilot)
            first = " ".join(
                str(cell) for cell in app.query_one("#slot-table").get_row_at(0)
            )
            assert "Opening" in first and "♥" in first
            assert "c1" in app._current_physical

    async def test_hcard_mode_still_expands_to_forty(self, tmp_path):
        app, _ = self._app(tmp_path, "hcard")
        async with app.run_test() as pilot:
            await pilot.pause()
            assert len(app._pairs) == 22 + 80 + 32
            table = app.query_one("#bank-table", DataTable)
            assert table.get_row("card:07:performance")

    async def test_show_card_defaults_names_unread_banks(self, tmp_path):
        app, _ = self._app(tmp_path, "hcard", show_card_defaults=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#bank-table", DataTable)
            joined = " ".join(
                str(cell) for cell in table.get_row("card:07:performance")
            )
            assert 'Korg WPC-13 "Performance 3"' in joined
            assert "not captured" not in joined

    async def test_without_show_card_defaults_unread_banks_stay_plain(self, tmp_path):
        app, _ = self._app(tmp_path, "hcard", show_card_defaults=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#bank-table", DataTable)
            joined = " ".join(
                str(cell) for cell in table.get_row("card:07:performance")
            )
            assert "not captured" in joined
            assert 'Korg WPC-13 "Performance 3"' not in joined

    async def test_hcard_banks_scope_favourites_per_bank(self, tmp_path):
        from kws import cards as kws_cards

        app, _ = self._app(tmp_path, "hcard", with_favourites=True)
        assert app.favorites is not None
        app.favorites.toggle(
            favorite_bank_id(kws_cards.scope_id(7), "performance"),
            0,
            name="Opening",
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#bank-table", DataTable)
            seven = [str(cell) for cell in table.get_row("card:07:performance")]
            eight = [str(cell) for cell in table.get_row("card:08:performance")]
            assert seven[-1] == "1"
            assert eight[-1] == ""
            # Selecting Card 07 shows its own favourite on slot 0.
            table.move_cursor(row=app._row_cards.index(7))
            await settle(pilot)
            first = " ".join(
                str(cell) for cell in app.query_one("#slot-table").get_row_at(0)
            )
            assert "♥" in first

    async def test_toggling_on_an_hcard_slot_writes_the_bank_scope(self, tmp_path):
        from kws import cards as kws_cards

        app, _ = self._app(tmp_path, "hcard", with_favourites=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#bank-table", DataTable)
            table.move_cursor(row=app._row_cards.index(7))
            await settle(pilot)
            app.query_one("#slot-table").move_cursor(row=0)
            app.action_toggle_favorite()
            scoped = favorite_bank_id(kws_cards.scope_id(7), "performance")
            assert scoped.startswith("CARD/07:")
            assert app.favorites.get(scoped, 0) is not None
            assert app.favorites.get(favorite_bank_id("CARD", "performance"), 0) is None

    async def test_the_old_shared_card_favourite_is_discarded(self, tmp_path):
        from kws import cards as kws_cards

        app, _ = self._app(tmp_path, "hcard", with_favourites=True)
        assert app.favorites is not None
        app.favorites.toggle(
            favorite_bank_id("CARD", "performance"), 0, name="Old shared"
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#bank-table", DataTable)
            for number in kws_cards.bank_numbers():
                for kind in (banks.Kind.PERFORMANCE, banks.Kind.PATCH):
                    cells = [
                        str(cell) for cell in table.get_row(f"card:{number:02d}:{kind}")
                    ]
                    assert cells[-1] == ""

    async def test_all_favourites_does_not_need_a_confirmed_hcard_bank(self, tmp_path):
        from kws import cards as kws_cards

        app, _ = self._app(tmp_path, "hcard", with_favourites=True)
        assert app.favorites is not None
        app.favorites.toggle(
            favorite_bank_id(kws_cards.scope_id(7), "performance"),
            0,
            name="Opening",
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            # The favourites view is a catalogue of everything filed; it
            # does not depend on which bank the HCard is currently showing.
            app._active_card = None
            shown = app._all_favourites()
            assert any(
                item.bank_id == "CARD/07"
                and item.number == 0
                and item.kind == banks.Kind.PERFORMANCE
                for item in shown
            )


class TestPhysicalCardCapture:
    """cards mode identifies a physical card by content (docs/CARDS.md)."""

    def _app(self, tmp_path, *, seed=None, active=None, favourites=False):
        from kws import physical

        pp = str(tmp_path / "physical-cards.json")
        for card in seed or []:
            physical.save(card, pp)
        if active:
            physical.set_active(active, pp)
        store = Favorites(str(tmp_path / "fav.db")) if favourites else None
        app = KwsedApp(
            DemoDevice(),
            catalog=cat.Catalog(),
            favorites=store,
            card_mode="cards",
            physical_path=pp,
        )
        return app, pp

    def _card(self, cid, label, digest):
        from kws import physical

        return physical.PhysicalCard(
            id=cid,
            label=label,
            digest=digest,
            captured_at="2026-11-09T00:00:00+00:00",
            performances={0: "Opening"},
            patches={0: "Bell"},
            verified=True,
        )

    def _demo_digest(self):
        from kws import cards as kws_cards

        device = DemoDevice()
        perf = device.read_bank("CARD", banks.Kind.PERFORMANCE)
        patch = device.read_bank("CARD", banks.Kind.PATCH)
        return kws_cards.content_digest(perf.records, patch.records)

    async def test_an_empty_archive_prompts_for_a_name(self, tmp_path):
        from textual.widgets import Input

        from kws import physical
        from kwsed.app import PromptScreen

        app, pp = self._app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_capture_card()
            await settle(pilot, 30)
            assert isinstance(app.screen, PromptScreen)
            app.screen.query_one("#value", Input).value = "Orchestra"
            await pilot.press("enter")
            await settle(pilot, 10)
            cards = physical.load(pp)
            assert len(cards) == 1
            cid = next(iter(cards))
            assert cards[cid].label == "Orchestra"
            assert app._active_physical == cid
            row = " ".join(
                str(cell)
                for cell in app.query_one("#bank-table", DataTable).get_row(
                    f"physical:{cid}:performance"
                )
            )
            assert "Orchestra" in row

    async def test_a_capture_without_a_path_never_touches_the_disk(
        self, tmp_path, monkeypatch
    ):
        """``--demo`` and the tests inject no physical path.

        The kws helpers fall back to ``default_path()`` when handed None,
        which would file the capture in the user's real archive while the
        rows here still showed nothing. Nothing may be written, and the
        card must exist in memory with the active marker set.
        """
        from textual.widgets import Input

        from kws import physical
        from kwsed.app import PromptScreen

        forbidden = tmp_path / "never-written" / "physical-cards.json"
        monkeypatch.setattr(physical, "default_path", lambda: str(forbidden))
        app = KwsedApp(
            DemoDevice(),
            catalog=cat.Catalog(),
            card_mode="cards",
            physical_path=None,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_capture_card()
            await settle(pilot, 30)
            assert isinstance(app.screen, PromptScreen)
            app.screen.query_one("#value", Input).value = "Orchestra"
            await pilot.press("enter")
            await settle(pilot, 10)
            assert not forbidden.exists()
            assert len(app._physical) == 1
            cid = next(iter(app._physical))
            assert app._physical[cid].label == "Orchestra"
            assert app._active_physical == cid
            row = " ".join(
                str(cell)
                for cell in app.query_one("#bank-table", DataTable).get_row(
                    f"physical:{cid}:performance"
                )
            )
            assert "Orchestra" in row

    async def test_a_capture_leaves_the_bank_cursor_on_the_new_card(self, tmp_path):
        """C must not snap the browser back to the RAM row.

        Re-modelling the table leaves the cursor at row 0 (RAM 1) and queues
        a highlight for it. That message used to land after the capture had
        filled the card's slots and replace them with RAM's.
        """
        from textual.widgets import Input

        from kws import physical
        from kwsed.app import PromptScreen

        app, pp = self._app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_capture_card()
            await settle(pilot, 30)
            assert isinstance(app.screen, PromptScreen)
            app.screen.query_one("#value", Input).value = "Orchestra"
            await pilot.press("enter")
            await settle(pilot, 30)
            cid = next(iter(physical.load(pp)))
            table = app.query_one("#bank-table", DataTable)
            expected = next(
                index
                for index, (pair, physical_id) in enumerate(
                    zip(app._pairs, app._row_physical)
                )
                if physical_id == cid and pair[1] == banks.Kind.PERFORMANCE
            )
            assert expected != 0
            assert table.cursor_row == expected
            assert app._current_physical == cid
            slots = app.query_one("#slot-table", DataTable)
            card_number = banks.bank("CARD").number
            card_slots = banks.slots(card_number, banks.Kind.PERFORMANCE)
            assert slots.row_count == len(card_slots)
            assert app._current == card_slots

    async def test_a_matching_digest_confirms_without_a_prompt(self, tmp_path):
        from kws import physical

        app, pp = self._app(
            tmp_path,
            seed=[self._card("c1", "Orchestra", self._demo_digest())],
            active="c1",
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_capture_card()
            await settle(pilot, 30)
            assert len(app.screen_stack) == 1
            cards = physical.load(pp)
            assert list(cards) == ["c1"]
            assert cards["c1"].verified is True
            assert "confirmed" in panel_text(app, "#status")

    async def test_an_unknown_digest_opens_the_picker(self, tmp_path):
        from kws import physical
        from vinsynlib.ui.modals import ChoiceScreen

        app, pp = self._app(
            tmp_path, seed=[self._card("c1", "Old", "not-the-demo")], active="c1"
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_capture_card()
            await settle(pilot, 30)
            assert isinstance(app.screen, ChoiceScreen)
            await pilot.press("enter")  # the archived card is row 0
            await settle(pilot, 10)
            cards = physical.load(pp)
            assert list(cards) == ["c1"]
            assert cards["c1"].digest == self._demo_digest()
            assert cards["c1"].verified is False
            assert app._active_physical == "c1"

    async def test_the_picker_can_create_a_new_card(self, tmp_path):
        from textual.widgets import Input

        from kws import physical
        from kwsed.app import PromptScreen

        app, pp = self._app(
            tmp_path, seed=[self._card("c1", "Old", "not-the-demo")], active="c1"
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_capture_card()
            await settle(pilot, 30)
            await pilot.press("down")
            await pilot.press("enter")  # "New card"
            await settle(pilot, 5)
            assert isinstance(app.screen, PromptScreen)
            app.screen.query_one("#value", Input).value = "Second"
            await pilot.press("enter")
            await settle(pilot, 10)
            cards = physical.load(pp)
            assert len(cards) == 2
            assert "Second" in [card.label for card in cards.values()]

    async def test_favouriting_a_physical_slot_is_scoped_to_the_card(self, tmp_path):
        from kws import physical

        app, _pp = self._app(
            tmp_path,
            seed=[self._card("c1", "Orchestra", "d1")],
            active="c1",
            favourites=True,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#bank-table", DataTable)
            table.move_cursor(row=app._row_physical.index("c1"))
            await settle(pilot)
            app.query_one("#slot-table").move_cursor(row=0)
            app.action_toggle_favorite()
            scoped = favorite_bank_id(physical.scope_id("c1"), "performance")
            assert app.favorites.get(scoped, 0) is not None
            assert app.favorites.get(favorite_bank_id("CARD", "performance"), 0) is None

    async def test_l_labels_the_selected_card(self, tmp_path):
        from textual.widgets import Input

        from kws import physical
        from kwsed.app import PromptScreen

        app, pp = self._app(
            tmp_path, seed=[self._card("c1", "Orchestra", "d1")], active="c1"
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#bank-table", DataTable)
            table.move_cursor(row=app._row_physical.index("c1"))
            await settle(pilot)
            app.action_label_card()
            await settle(pilot, 5)
            assert isinstance(app.screen, PromptScreen)
            app.screen.query_one("#value", Input).value = "Renamed"
            await pilot.press("enter")
            await settle(pilot, 5)
            assert physical.load(pp)["c1"].label == "Renamed"

    async def test_select_is_only_allowed_for_the_active_card(self, tmp_path):
        app, _pp = self._app(
            tmp_path,
            seed=[
                self._card("c1", "One", "d1"),
                self._card("c2", "Two", "d2"),
            ],
            active="c1",
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#bank-table", DataTable)
            table.move_cursor(row=app._row_physical.index("c2"))
            await settle(pilot)
            app.action_select()
            assert app.device.selected_log == []
            assert "not the one in the slot" in panel_text(app, "#status")
            table.move_cursor(row=app._row_physical.index("c1"))
            await settle(pilot)
            app.query_one("#slot-table").move_cursor(row=0)
            app.action_select()
            await settle(pilot, 10)
            assert len(app.device.selected_log) == 1

    async def test_r_on_a_physical_row_refuses(self, tmp_path):
        app, _pp = self._app(
            tmp_path, seed=[self._card("c1", "Orchestra", "d1")], active="c1"
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#bank-table", DataTable)
            table.move_cursor(row=app._row_physical.index("c1"))
            await settle(pilot)
            app.action_read_names()
            assert "press C" in panel_text(app, "#status")


class TestCardModesFollowUps:
    def _app(self, tmp_path, *, mode, cards=None, favourites=False):
        from kws import physical

        pp = str(tmp_path / "physical-cards.json")
        for card in cards or []:
            physical.save(card, pp)
        if cards:
            physical.set_active(cards[0].id, pp)
        store = Favorites(str(tmp_path / "fav.db")) if favourites else None
        app = KwsedApp(
            DemoDevice(),
            catalog=cat.Catalog(),
            favorites=store,
            card_mode=mode,
            physical_path=pp,
        )
        return app, pp

    def _card(self, cid, label, digest):
        from kws import physical

        return physical.PhysicalCard(
            id=cid,
            label=label,
            digest=digest,
            captured_at="2026-11-09T00:00:00+00:00",
            performances={0: "Opening", 1: "Second"},
            patches={0: "Bell"},
            verified=True,
        )

    async def test_show_card_defaults_fits_the_pane(self):
        app = KwsedApp(
            DemoDevice(),
            catalog=cat.Catalog(),
            card_mode="hcard",
            show_card_defaults=True,
        )
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            table = app.query_one("#bank-table", DataTable)
            row = " ".join(str(cell) for cell in table.get_row("card:01:performance"))
            assert "Korg WPC-00PI RAM1" in row
            assert "not captured" not in row
            assert table.virtual_size.width <= table.size.width

    async def test_the_startup_probe_only_runs_in_hcard_mode(self, tmp_path):
        app, _ = self._app(
            tmp_path, mode="cards", cards=[self._card("c1", "One", "d1")]
        )
        calls = []
        app.device.read_bank = lambda *a, **k: calls.append(a)
        app._auto_capture_card()
        assert calls == []

    async def test_the_favourites_view_filters_a_physical_cards_slots(self, tmp_path):
        from kws import physical

        app, _ = self._app(
            tmp_path,
            mode="cards",
            cards=[self._card("c1", "Orchestra", "d1")],
            favourites=True,
        )
        assert app.favorites is not None
        app.favorites.toggle(
            favorite_bank_id(physical.scope_id("c1"), "performance"),
            0,
            name="Opening",
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#slot-table")
            app._fill_physical("c1", banks.Kind.PERFORMANCE)
            app.view_mode = VIEW_BANK_FAVOURITES
            app._refill_keeping_place()
            await settle(pilot)
            assert table.row_count == 1
            assert "Opening" in " ".join(str(cell) for cell in table.get_row_at(0))
            app.view_mode = VIEW_ALL_FAVOURITES
            app._refill_keeping_place()
            await settle(pilot)
            assert table.row_count >= 1
            assert "Opening" in " ".join(str(cell) for cell in table.get_row_at(0))

    async def test_all_favourites_shows_internal_banks_with_no_card(self, tmp_path):
        app, _ = self._app(tmp_path, mode="cards", favourites=True)
        assert app.favorites is not None
        app.favorites.add(favorite_bank_id("RAM1", banks.Kind.PERFORMANCE), 0)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#slot-table")
            app.view_mode = VIEW_ALL_FAVOURITES
            app._fill_physical("", banks.Kind.PERFORMANCE)
            await settle(pilot)
            assert table.row_count == 1
            row = " ".join(str(cell) for cell in table.get_row_at(0))
            assert "RAM 1" in row
            detail = panel_text(app, "#detail")
            assert "insert a card" not in detail
            assert "RAM 1" in detail

    async def test_all_favourites_with_nothing_favourited_does_not_prompt_for_a_card(
        self, tmp_path
    ):
        app, _ = self._app(tmp_path, mode="cards", favourites=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#slot-table")
            app.view_mode = VIEW_ALL_FAVOURITES
            app._fill_physical("", banks.Kind.PERFORMANCE)
            await settle(pilot)
            assert table.row_count == 1
            row = " ".join(str(cell) for cell in table.get_row_at(0))
            assert "nothing favourited" in row
            detail = panel_text(app, "#detail")
            assert "insert a card" not in detail
            assert "nothing favourited" in detail

    async def test_an_all_favourites_card_row_says_whether_it_is_active(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            slot = banks.slot(banks.bank("CARD").number, banks.Kind.PERFORMANCE, 1)
            item = ViewSlot(slot, "CARD/07", "")
            app._active_card = 7
            app._detail_scoped(item, "CARD/07")
            text = panel_text(app, "#detail")
            assert "active card" in text
            assert "another card" not in text
            app._active_card = 8
            app._detail_scoped(item, "CARD/07")
            text = panel_text(app, "#detail")
            assert "another card" in text
            assert "active card" not in text

    async def test_an_inactive_physical_row_keeps_the_refusal_wording(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            slot = banks.slot(banks.bank("CARD").number, banks.Kind.PERFORMANCE, 1)
            item = ViewSlot(slot, "CARD/c1", "")
            app._active_physical = "c1"
            app._detail_scoped(item, "CARD/c1")
            assert "active card" in panel_text(app, "#detail")
            app._active_physical = "c2"
            app._detail_scoped(item, "CARD/c1")
            text = panel_text(app, "#detail")
            assert "another card" in text
            assert "active card" not in text

    async def test_toggle_favorite_on_physical_card_stores_scoped_name(self, tmp_path):
        from kws import physical

        app, _ = self._app(
            tmp_path,
            mode="cards",
            cards=[self._card("c1", "Orchestra", "d1")],
            favourites=True,
        )
        assert app.favorites is not None
        async with app.run_test() as pilot:
            await pilot.pause()
            app._fill_physical("c1", banks.Kind.PERFORMANCE)
            await settle(pilot)
            app.action_toggle_favorite()
            await settle(pilot)
            key = favorite_bank_id(physical.scope_id("c1"), banks.Kind.PERFORMANCE)
            fav = app.favorites.get(key, 0)
            assert fav is not None
            assert fav.name == "Opening"
            assert fav.name != "(unnamed)"
            bank_table = app.query_one("#bank-table", DataTable)
            row = bank_table.get_row("physical:c1:performance")
            assert row[-1] == "1"


def _copied_source(scope: str = "RAM1") -> deps.Source:
    """One performance whose two parts share patch 0, whose osc is a sequence."""
    from tests.test_bankbuild import make_patch, make_performance, make_waveseq

    patch = make_patch("Kelp Forest", ((0, 0), (0, 40), (0, 40), (0, 40)))
    perf = make_performance("Glass Meridian", parts=((0, 0), (0, 0)))
    patches: list[bytes] = [
        bytes(rec.PATCH_SIZE) for _ in range(banks.PATCHES_PER_BANK)
    ]
    patches[0] = patch
    perfs: list[bytes] = [
        make_performance("", ()) for _ in range(banks.PERFORMANCES_PER_BANK)
    ]
    perfs[0] = perf
    waveseq = make_waveseq({0: [40, 41]})[0]
    return deps.Source(scope, performances=perfs, patches=patches, waveseq=waveseq)


def _shared_source(scope: str = "RAM1") -> deps.Source:
    """Two performances that both use patch 0, for where-used reports."""
    from tests.test_bankbuild import make_patch, make_performance, make_waveseq

    patch = make_patch("Kelp Forest", ((0, 0), (0, 40), (0, 40), (0, 40)))
    first = make_performance("Glass Meridian", parts=((0, 0), (0, 0)))
    second = make_performance("Amber Drift", parts=((0, 0),))
    patches: list[bytes] = [
        bytes(rec.PATCH_SIZE) for _ in range(banks.PATCHES_PER_BANK)
    ]
    patches[0] = patch
    perfs: list[bytes] = [
        make_performance("", ()) for _ in range(banks.PERFORMANCES_PER_BANK)
    ]
    perfs[0] = first
    perfs[1] = second
    waveseq = make_waveseq({0: [40, 41]})[0]
    return deps.Source(scope, performances=perfs, patches=patches, waveseq=waveseq)


def _empty_source(scope: str) -> deps.Source:
    from tests.test_bankbuild import make_performance

    perfs: list[bytes] = [
        make_performance("", ()) for _ in range(banks.PERFORMANCES_PER_BANK)
    ]
    patches: list[bytes] = [
        bytes(rec.PATCH_SIZE) for _ in range(banks.PATCHES_PER_BANK)
    ]
    return deps.Source(
        scope,
        performances=perfs,
        patches=patches,
        waveseq=bytes(rec.WAVESEQ_BANK_SIZE),
    )


class _RecordingDemo(DemoDevice):
    """A demo device whose bundle write leaves a real backup behind.

    DemoDevice keeps what it was given but writes no envelope, so undo has
    nothing to load. The real bridge always writes one; this adds the step
    the fake leaves out, without changing what the fake stores.
    """

    def __init__(self, directory: str) -> None:
        super().__init__()
        self._directory = directory

    def write_bank_bundle(
        self,
        bank: int | str,
        *,
        performances: Optional[bytes] = None,
        patches: Optional[bytes] = None,
        waveseq: Optional[bytes] = None,
        backup_dir: str,
        verify: bool = True,
        timeout: Optional[float] = None,
        before_blocks: Optional[Mapping[str, bytes]] = None,
    ) -> dict[str, Any]:
        result = super().write_bank_bundle(
            bank,
            performances=performances,
            patches=patches,
            waveseq=waveseq,
            backup_dir=backup_dir,
            verify=verify,
            timeout=timeout,
            before_blocks=before_blocks,
        )
        entry = banks.bank(bank)
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


class TestTheDependencyInspector:
    """`d` shows what a Performance, a Patch or a Multiset depends on."""

    async def test_d_on_a_performance_lists_parts_sequences_and_ownership(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app._sources["RAM1"] = _copied_source()
            app._fill((0, banks.Kind.PERFORMANCE))
            app.query_one("#slot-table").focus()
            await pilot.press("d")
            await settle(pilot)
            assert isinstance(app.screen, ReportScreen)
            body = app.screen.body
            assert 'RAM1 00 "Glass Meridian"' in body
            assert "0 -> RAM1:00 Kelp Forest" in body
            assert "step cost: 2" in body
            assert "patches: atomic" in body
            await pilot.press("escape")
            await settle(pilot)

    async def test_d_on_a_patch_lists_oscillators_and_where_used(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app._sources["RAM1"] = _copied_source()
            app._fill((0, banks.Kind.PATCH))
            app.query_one("#slot-table").focus()
            await pilot.press("d")
            await settle(pilot)
            assert isinstance(app.screen, ReportScreen)
            body = app.screen.body
            assert "Osc 1 -> wave sequence 0 in RAM1" in body
            assert "Where used:" in body
            assert "RAM1:00 Glass Meridian" in body
            await pilot.press("escape")
            await settle(pilot)

    async def test_d_on_a_multiset_lists_the_sixteen_channels(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app._multisets = multi_mod.parse(
                bytes(multi_mod.NAME_BASE + multi_mod.MULTISETS * multi_mod.NAME_SIZE)
            )
            table = app.query_one("#bank-table", DataTable)
            RowKey = table.get_row_index("multi:00")
            table.move_cursor(row=RowKey)
            table.focus()
            await pilot.press("d")
            await settle(pilot)
            assert isinstance(app.screen, ReportScreen)
            body = app.screen.body
            assert "Ch 01" in body
            assert "card conflicts: none" in body
            await pilot.press("escape")
            await settle(pilot)

    async def test_d_on_a_multiset_resolves_the_active_card(self, app):
        from dataclasses import replace

        async with app.run_test() as pilot:
            await pilot.pause()
            app._active_card = 7
            app._sources["CARD/07"] = _copied_source("CARD/07")
            sets = multi_mod.parse(
                bytes(multi_mod.NAME_BASE + multi_mod.MULTISETS * multi_mod.NAME_SIZE)
            )
            card = banks.bank("CARD").sysex
            first = replace(sets[0].parts[0], bank=card, performance=0)
            parts = [first] + [
                replace(part, performance=50) for part in sets[0].parts[1:]
            ]
            app._multisets = [replace(sets[0], parts=tuple(parts))] + list(sets[1:])
            table = app.query_one("#bank-table", DataTable)
            table.move_cursor(row=table.get_row_index("multi:00"))
            table.focus()
            await pilot.press("d")
            await settle(pilot)
            assert isinstance(app.screen, ReportScreen)
            assert "CARD/07 00 Glass Meridian" in app.screen.body
            await pilot.press("escape")
            await settle(pilot)

    async def test_d_on_a_card_row_when_another_bank_is_active_refuses(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app._active_physical = None
            app._active_card = 13
            app._fill((banks.bank("CARD").number, banks.Kind.PERFORMANCE), 7)
            app.query_one("#slot-table").focus()
            await pilot.press("d")
            await settle(pilot)
            assert not isinstance(app.screen, ReportScreen)
            text = panel_text(app, "#status")
            assert "CARD/07" in text
            assert "CARD/13" in text, "the status names the bank actually in the slot"
            assert "refused" in text

    async def test_d_on_a_cached_card_row_works_when_another_card_is_active(self, app):
        """Only a read needs the slot; cached or archived records are safe."""
        async with app.run_test() as pilot:
            await pilot.pause()
            app._active_physical = None
            app._active_card = 13
            app._sources["CARD/07"] = _copied_source("CARD/07")
            app._fill((banks.bank("CARD").number, banks.Kind.PERFORMANCE), 7)
            await settle(pilot)
            assert "Glass Meridian" in str(
                app.query_one("#deps-pane-body", Static).content
            )

            app.query_one("#slot-table").focus()
            await pilot.press("d")
            await settle(pilot)
            assert isinstance(app.screen, ReportScreen)
            assert "Glass Meridian" in app.screen.body
            await pilot.press("escape")
            await settle(pilot)

    async def test_d_names_the_other_performances_using_each_patch(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app._sources["RAM1"] = _shared_source()
            app._fill((0, banks.Kind.PERFORMANCE))
            app.query_one("#slot-table").focus()
            await pilot.press("d")
            await settle(pilot)
            assert isinstance(app.screen, ReportScreen)
            assert "(also RAM1:01)" in app.screen.body
            await pilot.press("escape")
            await settle(pilot)

            table = app.query_one("#slot-table", DataTable)
            table.move_cursor(row=1)
            await settle(pilot)
            await pilot.press("d")
            await settle(pilot)
            assert isinstance(app.screen, ReportScreen)
            assert "(also RAM1:00)" in app.screen.body
            await pilot.press("escape")
            await settle(pilot)


class TestTheBrowserPanes:
    """The patch rows and the live dependency pane are both optional."""

    async def test_p_hides_and_shows_the_patch_rows(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#bank-table", DataTable)
            before = len(app._pairs)
            assert any(kind == banks.Kind.PATCH for _number, kind in app._pairs)
            assert table.row_count == before

            await pilot.press("p")
            await settle(pilot)
            assert app._show_patches is False
            assert all(kind != banks.Kind.PATCH for _number, kind in app._pairs)
            assert len(app._pairs) < before
            assert table.row_count == len(app._pairs)
            assert "hiding patches" in panel_text(app, "#status")

            await pilot.press("p")
            await settle(pilot)
            assert app._show_patches is True
            assert len(app._pairs) == before

    async def test_hiding_patches_moves_off_a_patch_bank(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app._fill((0, banks.Kind.PATCH))
            assert app._current_pair[1] == banks.Kind.PATCH

            await pilot.press("p")
            await settle(pilot)
            assert app._current_pair[1] == banks.Kind.PERFORMANCE
            assert app._current
            assert all(item.kind == banks.Kind.PERFORMANCE for item in app._current)

    async def test_the_dependency_pane_follows_the_selection(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            body = app.query_one("#deps-pane-body", Static)
            assert "not loaded" in str(body.content)

            app._sources["RAM1"] = _shared_source()
            app._fill((0, banks.Kind.PERFORMANCE))
            await settle(pilot)
            text = str(body.content)
            assert "Glass Meridian" in text
            assert "(also RAM1:01)" in text

            table = app.query_one("#slot-table", DataTable)
            table.move_cursor(row=1)
            await settle(pilot)
            text = str(body.content)
            assert "Amber Drift" in text
            assert "(also RAM1:00)" in text

    async def test_D_hides_and_shows_the_dependency_pane(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            pane = app.query_one("#deps-pane")
            assert pane.display is True

            await pilot.press("D")
            await settle(pilot)
            assert pane.display is False
            assert "dependency pane hidden" in panel_text(app, "#status")

            await pilot.press("D")
            await settle(pilot)
            assert pane.display is True

    async def test_the_toggles_are_remembered_between_runs(self, tmp_path):
        config_path = str(tmp_path / "config.toml")
        first = KwsedApp(DemoDevice(), catalog=cat.Catalog([]), config_path=config_path)
        async with first.run_test() as pilot:
            await pilot.pause()
            assert first._show_patches is True
            assert first._deps_pane_visible is True
            first.action_toggle_patches()
            first.action_toggle_deps_pane()
            await pilot.pause()
            assert first._show_patches is False
            assert first._deps_pane_visible is False

        second = KwsedApp(
            DemoDevice(), catalog=cat.Catalog([]), config_path=config_path
        )
        async with second.run_test() as pilot:
            await pilot.pause()
            assert second._show_patches is False
            assert second._deps_pane_visible is False
            assert all(kind != banks.Kind.PATCH for _number, kind in second._pairs)
            assert second.query_one("#deps-pane").display is False

        third = KwsedApp(DemoDevice(), catalog=cat.Catalog([]), config_path=config_path)
        async with third.run_test() as pilot:
            await pilot.pause()
            third.action_toggle_patches()
            third.action_toggle_deps_pane()
            await pilot.pause()
        fourth = KwsedApp(
            DemoDevice(), catalog=cat.Catalog([]), config_path=config_path
        )
        async with fourth.run_test() as pilot:
            await pilot.pause()
            assert fourth._show_patches is True
            assert fourth._deps_pane_visible is True

    async def test_a_stale_settings_file_is_healed_on_startup(self, tmp_path):
        from kws import config as cfg

        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "# kwsed local config - gitignored, safe to delete.\n"
            "channel = 4\n"
            "browser.deps_pane = false\n",
            encoding="utf-8",
        )
        app = KwsedApp(
            DemoDevice(), catalog=cat.Catalog([]), config_path=str(config_path)
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._deps_pane_visible is False
            assert app.query_one("#deps-pane").display is False
        assert "browser" not in config_path.read_text(encoding="utf-8")
        # The stale table is gone, so ordinary settings can be saved again.
        cfg.save_channel(5, str(config_path))
        assert cfg.load_channel(str(config_path)) == 5


class TestCopyPerformance:
    """`y` copies a Performance and its private patches, arm then fire."""

    async def test_y_on_a_patch_is_refused_with_the_reason(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app._fill((0, banks.Kind.PATCH))
            app.query_one("#slot-table").focus()
            await pilot.press("y")
            await settle(pilot)
            text = panel_text(app, "#status")
            assert "select a Performance to copy" in text
            assert "refused" in text

    async def test_arm_then_fire_copies_atomically_into_ram3(self, tmp_path):
        device = _RecordingDemo(str(tmp_path / "backups"))
        app = KwsedApp(device, catalog=cat.Catalog([]))
        async with app.run_test() as pilot:
            await pilot.pause()
            app._sources["RAM1"] = _copied_source()
            app._sources["RAM3"] = _empty_source("RAM3")
            app._fill((0, banks.Kind.PERFORMANCE))
            app.query_one("#slot-table").focus()
            await pilot.press("y")
            await settle(pilot)
            assert isinstance(app.screen, TargetBankScreen)

            chooser = app.screen
            idx = chooser._choices.index("RAM 3")
            chooser._current = idx
            chooser.action_confirm()
            await settle(pilot)
            assert isinstance(app.screen, SingleImportScreen)
            screen = app.screen
            assert screen.assignments, "one assignment to the first free slot"

            # Nothing is written before the screen is armed.
            await pilot.press("w")
            await settle(pilot)
            assert device.bundle_write_count == 0
            assert "ARMED" in str(screen.query_one("#status", Static).content).upper()

            await pilot.press("ctrl+s")
            await settle(pilot, 40)

            assert device.bundle_write_count == 1
            assert len(app._undo_log) == 1, "one copy is one undo step"
            cached = app._sources["RAM3"]
            assert dumps.record_name(cached.performances[0]) == "Glass Meridian"
            assert dumps.record_name(cached.patches[0]) == "Kelp Forest"
            dump = device.read_bank("RAM3", banks.Kind.PERFORMANCE)
            used = [
                part for part in rec.performance_parts(dump.records[0]) if part.used
            ]
            assert used
            assert {part.bank for part in used} == {banks.bank("RAM3").number}
            assert {part.patch for part in used} == {0}, (
                "two parts sharing one source patch keep one private slot"
            )

    async def test_copy_target_chooser_labels_and_source_origin(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app._sources["RAM1"] = _copied_source()
            app._fill((0, banks.Kind.PERFORMANCE))
            app.query_one("#slot-table").focus()
            await pilot.press("y")
            await settle(pilot)
            chooser = app.screen
            assert isinstance(chooser, TargetBankScreen)
            assert chooser._choices == ["RAM 1", "RAM 2", "RAM 3", "Card"]

            idx = chooser._choices.index("RAM 1")
            chooser._current = idx
            chooser.action_confirm()
            await settle(pilot)
            screen = app.screen
            assert isinstance(screen, SingleImportScreen)
            assert "RAM 1" in screen._source_path_text(screen.source_banks[0].scope)

    async def test_init_slots_are_free(self, app):
        from tests.test_bankbuild import make_performance

        async with app.run_test() as pilot:
            await pilot.pause()
            app._sources["RAM1"] = _copied_source()
            app._sources["RAM3"] = deps.Source(
                "RAM3",
                performances=[
                    make_performance("Init", ((0, 0),))
                    for _ in range(banks.PERFORMANCES_PER_BANK)
                ],
                patches=[bytes(rec.PATCH_SIZE) for _ in range(banks.PATCHES_PER_BANK)],
                waveseq=bytes(rec.WAVESEQ_BANK_SIZE),
            )
            app._fill((0, banks.Kind.PERFORMANCE))
            app.query_one("#slot-table").focus()
            await pilot.press("y")
            await settle(pilot)
            chooser = app.screen
            assert isinstance(chooser, TargetBankScreen)
            idx = chooser._choices.index("RAM 3")
            chooser._current = idx
            chooser.action_confirm()
            await settle(pilot)
            screen = app.screen
            assert isinstance(screen, SingleImportScreen)
            assert list(screen.assignments.keys()) == [0]

    async def test_q_esc_from_chooser_returns_without_writing(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app._sources["RAM1"] = _copied_source()
            app._fill((0, banks.Kind.PERFORMANCE))
            app.query_one("#slot-table").focus()
            await pilot.press("y")
            await settle(pilot)
            assert isinstance(app.screen, TargetBankScreen)
            await pilot.press("q")
            await settle(pilot)
            assert not isinstance(app.screen, TargetBankScreen)
            assert app.device.bundle_write_count == 0

    async def test_q_esc_from_screen_returns_to_chooser(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app._sources["RAM1"] = _copied_source()
            app._sources["RAM3"] = _empty_source("RAM3")
            app._fill((0, banks.Kind.PERFORMANCE))
            app.query_one("#slot-table").focus()
            await pilot.press("y")
            await settle(pilot)
            chooser = app.screen
            idx = chooser._choices.index("RAM 3")
            chooser._current = idx
            chooser.action_confirm()
            await settle(pilot)
            assert isinstance(app.screen, SingleImportScreen)
            await pilot.press("q")
            await settle(pilot)
            assert isinstance(app.screen, TargetBankScreen)

    async def test_full_target_no_assignment(self, tmp_path):
        from tests.test_bankbuild import make_patch, make_performance

        def full_ram3():
            perfs = [make_performance(f"Filled {i:02d}", ((2, 0),)) for i in range(50)]
            patches = [make_patch(f"Patch {i:02d}") for i in range(35)]
            return deps.Source(
                "RAM3",
                performances=perfs,
                patches=patches,
                waveseq=bytes(rec.WAVESEQ_BANK_SIZE),
            )

        device = _RecordingDemo(str(tmp_path / "backups"))
        app = KwsedApp(device, catalog=cat.Catalog([]))
        async with app.run_test() as pilot:
            await pilot.pause()
            app._sources["RAM1"] = _copied_source()
            app._sources["RAM3"] = full_ram3()
            app._fill((0, banks.Kind.PERFORMANCE))
            app.query_one("#slot-table").focus()
            await pilot.press("y")
            await settle(pilot)
            chooser = app.screen
            assert isinstance(chooser, TargetBankScreen)
            idx = chooser._choices.index("RAM 3")
            chooser._current = idx
            chooser.action_confirm()
            await settle(pilot, 20)
            assert not isinstance(app.screen, SingleImportScreen)
            assert device.bundle_write_count == 0
            assert "no free slot" in panel_text(app, "#status")

    async def test_card_mismatch_refusal(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            app._active_physical = None
            app._active_card = 13
            app._sources["RAM1"] = _copied_source()
            app._fill((banks.bank("CARD").number, banks.Kind.PERFORMANCE), 7)
            app.query_one("#slot-table").focus()
            await pilot.press("y")
            await settle(pilot, 20)
            assert isinstance(app.screen, TargetBankScreen)
            chooser = app.screen
            chooser._current = 0
            chooser.action_confirm()
            await settle(pilot, 20)
            text = panel_text(app, "#status")
            assert "CARD/07" in text
            assert "CARD/13" in text
            assert "refused" in text
            assert not isinstance(app.screen, SingleImportScreen)
            assert app.device.bundle_write_count == 0


class TestFxScreen:
    """The read-only FX view: what it shows, and that it sends no write."""

    @staticmethod
    def _block() -> Any:
        from kws.fx import Effect, FxBlock

        return FxBlock(
            series=False,
            fx1=Effect(type=1, raw=bytes(8)),  # Small Hall
            fx2=Effect(type=0, raw=bytes(8)),  # Off
            mix=(0, 0),
            mod_source=(0, 0),
            mod_amount=(0, 0),
        )

    def test_the_banner_says_which_block_is_heard(self) -> None:
        block = self._block()
        assert "performance" in FxScreen(block, 0).mode_banner()
        assert "Multiset" in FxScreen(block, 1).mode_banner()
        assert "unknown" in FxScreen(block, None).mode_banner()

    async def test_it_shows_types_and_parameters(self, app: Any) -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(FxScreen(self._block(), 0))
            await settle(pilot, 4)
            table = app.screen.query_one("#fx-table", DataTable)
            rows = [
                [str(cell) for cell in table.get_row_at(index)]
                for index in range(table.row_count)
            ]
            text = " ".join(" ".join(row) for row in rows)
            assert "Small Hall" in text
            assert "Off" in text
            assert any("Dry/Wet" in row[0] for row in rows)
            banner = app.screen.query_one("#fx-mode-banner", Static)
            assert "performance FX" in str(banner.content)

    async def test_a_patch_row_is_refused(self, app: Any) -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            app._cursor_entry = lambda: banks.Slot(
                banks.bank("RAM1"), banks.Kind.PATCH, 0
            )
            statuses: list[str] = []
            app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))
            app.action_edit_fx()
            await pilot.pause()
            assert any("performance" in message for message in statuses)
            assert not isinstance(app.screen, FxScreen)

    async def test_a_performance_row_opens_without_writing(self, app: Any) -> None:
        writes: list[Any] = []
        app.device.write_performance = lambda *a, **k: writes.append(1)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._cursor_entry = lambda: banks.Slot(
                banks.bank("RAM1"), banks.Kind.PERFORMANCE, 0
            )
            app.action_edit_fx()
            await settle(pilot, 30)
            assert isinstance(app.screen, FxScreen)
            assert writes == []

    async def test_editing_a_value_changes_only_that_row(self, app: Any) -> None:
        block = self._block()
        rows = block.fx1.rows()
        target = next(row for row in rows if row.label == "Pre Delay")
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = FxScreen(block, 0)
            app.push_screen(screen)
            await settle(pilot, 3)
            screen._apply_param(0, target, "300")
            assert screen._block.fx1.display(target) == 300
            for row in rows:
                if row.key != target.key:
                    assert screen._block.fx1.display(row) == block.fx1.display(row)

    async def test_the_write_path_sends_one_performance(self, app: Any) -> None:
        calls: list[Any] = []

        def fake(bank: Any, number: int, data: bytes, *, backup_dir: str) -> Any:
            calls.append((bank, number, bytes(data)))
            return {
                "verified": True,
                "backup": "not-read.json",
                "kind": "performance",
                "bank": "RAM 1",
            }

        app.device.write_performance = fake
        bank = banks.bank("RAM1")
        record = bytes(app.device.read_performance(bank.sysex, 0))
        block = self._block()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._write_gate = EditGate(armed=True)
            screen = FxScreen(
                block,
                0,
                on_write=lambda new_block, target: app._fx_write(
                    bank, 0, record, new_block, target
                ),
                gate_armed=True,
            )
            app.push_screen(screen)
            await settle(pilot, 3)
            await pilot.press("a")
            await pilot.press("w")
            await settle(pilot, 25)
            assert len(calls) == 1
            assert calls[0][2][16:37] == block.to_bytes()
            assert not isinstance(app.screen, FxScreen)

    async def test_the_locked_gate_refuses_before_a_write(self, app: Any) -> None:
        calls: list[Any] = []
        app.device.write_performance = lambda *a, **k: calls.append(1)
        bank = banks.bank("RAM1")
        record = bytes(app.device.read_performance(bank.sysex, 0))
        block = self._block()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._write_gate = EditGate(armed=False)
            screen = FxScreen(
                block,
                0,
                on_write=lambda new_block, target: app._fx_write(
                    bank, 0, record, new_block, target
                ),
                gate_armed=False,
            )
            app.push_screen(screen)
            await settle(pilot, 3)
            await pilot.press("a")
            await settle(pilot, 2)
            state = str(screen.query_one("#fx-state", Static).content)
            assert "locked" in state
            await pilot.press("w")
            await settle(pilot, 10)
            assert calls == []
            assert isinstance(app.screen, FxScreen)

    async def test_a_failed_write_keeps_the_screen_open(self, app: Any) -> None:
        def bad(*a: Any, **k: Any) -> Any:
            raise RuntimeError("cable unplugged")

        app.device.write_performance = bad
        bank = banks.bank("RAM1")
        record = bytes(app.device.read_performance(bank.sysex, 0))
        block = self._block()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._write_gate = EditGate(armed=True)
            screen = FxScreen(
                block,
                0,
                on_write=lambda new_block, target: app._fx_write(
                    bank, 0, record, new_block, target
                ),
                gate_armed=True,
            )
            app.push_screen(screen)
            await settle(pilot, 3)
            await pilot.press("a")
            await pilot.press("w")
            await settle(pilot, 20)
            assert isinstance(app.screen, FxScreen)
            assert "cable unplugged" in str(
                screen.query_one("#fx-state", Static).content
            )
