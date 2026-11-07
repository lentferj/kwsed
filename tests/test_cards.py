# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.

"""The HCard's forty-bank table, transcribed from docs/HCARD.md section 2."""

import pytest

from kws import banks, cards


class TestTheTable:
    def test_there_are_forty_banks_numbered_from_zero(self):
        assert len(cards.cards()) == 40
        assert cards.bank_numbers() == list(range(40))

    def test_the_ranges_are_the_manual_s(self):
        by_category: dict[str, list[int]] = {}
        for entry in cards.cards():
            by_category.setdefault(entry.category, []).append(entry.number)
        assert by_category[cards.Category.FACTORY] == [0]
        assert by_category[cards.Category.PRELOAD] == [1, 2, 3, 4]
        assert by_category[cards.Category.ROM_CARD] == list(range(5, 25))
        assert by_category[cards.Category.USER] == list(range(25, 40))

    def test_the_user_range_starts_at_25(self):
        user = [n for n in cards.bank_numbers() if cards.is_user(n)]
        assert user == list(range(25, 40))
        assert not cards.is_user(24)
        assert not cards.is_user(40)

    def test_the_card_names_the_manual_names_are_there(self):
        assert cards.card(0).name == "WS Internal ROM"
        assert cards.card(1).name == "Factory Preload 1"
        # Section 5's example name for bank 07.
        assert cards.card(7).name == 'WPC-13 "Performance 3"'
        assert cards.card(19).name == "MCR03-WS1"
        assert cards.card(22).name == "WSC-1S"
        assert cards.card(39).name == "User 15"

    def test_the_three_pcm_banks_are_flagged(self):
        flagged = {c.number: c.needs_pcm for c in cards.cards() if c.needs_pcm}
        assert flagged == {16: "WSC-01", 17: "WSC-02", 18: "WSC-03"}


class TestAccessors:
    def test_card_returns_one_bank(self):
        assert cards.card(7).number == 7
        with pytest.raises(LookupError):
            cards.card(40)

    def test_a_boolean_is_not_a_bank_number(self):
        with pytest.raises(LookupError):
            cards.card(True)

    def test_unknown_name_is_none(self):
        assert cards.card_by_name("not a bank") is None

    def test_name_lookup_ignores_case_and_padding(self):
        found = cards.card_by_name('  wpc-13 "PERFORMANCE 3" ')
        assert found is not None
        assert found.number == 7

    def test_counts_are_the_banks_module_s(self):
        assert cards.count_for(banks.Kind.PERFORMANCE) == 50
        assert cards.count_for(banks.Kind.PATCH) == 35
