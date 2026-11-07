# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.

"""The HCard's forty-bank table and its per-bank archive."""

import json
import os

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


class TestTheArchive:
    def _captured(self, number=7, name='WPC-13 "Performance 3"'):
        return cards.capture(
            number,
            name,
            {0: "First Performance", 1: "Second"},
            {0: "First Patch"},
            captured_at="2026-11-07T12:00:00+00:00",
        )

    def test_round_trip_save_load(self, tmp_path):
        path = str(tmp_path / "cards.json")
        bank = self._captured()
        cards.save(bank, path)
        assert cards.load(path) == {7: bank}

    def test_the_schema_is_versioned_and_keyed_by_two_digits(self, tmp_path):
        path = tmp_path / "cards.json"
        cards.save(self._captured(), str(path))
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["version"] == 1
        assert set(raw["banks"]) == {"07"}
        assert raw["banks"]["07"]["name"] == 'WPC-13 "Performance 3"'
        assert raw["banks"]["07"]["performances"] == {
            "0": "First Performance",
            "1": "Second",
        }
        assert raw["banks"]["07"]["patches"] == {"0": "First Patch"}

    def test_a_second_save_keeps_the_first_bank(self, tmp_path):
        path = str(tmp_path / "cards.json")
        cards.save(self._captured(7), path)
        cards.save(self._captured(8, "User 01"), path)
        loaded = cards.load(path)
        assert set(loaded) == {7, 8}
        assert loaded[8].name == "User 01"

    def test_a_missing_archive_is_not_an_error(self, tmp_path):
        assert cards.load(str(tmp_path / "absent.json")) == {}

    def test_a_damaged_archive_is_ignored_with_a_warning(self, tmp_path, capsys):
        path = tmp_path / "cards.json"
        path.write_text("{not json", encoding="utf-8")
        assert cards.load(str(path)) == {}
        assert "ignoring" in capsys.readouterr().err

    def test_a_future_version_is_ignored(self, tmp_path):
        path = tmp_path / "cards.json"
        path.write_text(json.dumps({"version": 99, "banks": {}}), encoding="utf-8")
        assert cards.load(str(path)) == {}

    def test_malformed_entries_are_dropped_not_guessed(self, tmp_path):
        path = tmp_path / "cards.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "banks": {
                        "07": {"name": "Good", "captured_at": "now"},
                        "x": "not a bank",
                        "08": {"name": 3, "captured_at": "now"},
                    },
                }
            ),
            encoding="utf-8",
        )
        loaded = cards.load(str(path))
        assert set(loaded) == {7}
        assert loaded[7].performances == {}

    def test_the_directory_is_created_and_no_temporary_file_is_left(self, tmp_path):
        path = tmp_path / "nested" / "deeper" / "cards.json"
        cards.save(self._captured(7), str(path))
        cards.save(self._captured(8, "User 01"), str(path))
        assert sorted(p.name for p in path.parent.iterdir()) == ["cards.json"]

    def test_default_path_is_beside_the_favourites_database(
        self, tmp_path, monkeypatch
    ):
        from kws import favorites

        monkeypatch.setattr(favorites, "data_dir", lambda: str(tmp_path))
        path = cards.default_path()
        assert os.path.basename(path) == "cards.json"
        assert os.path.dirname(path) == str(tmp_path)

    def test_names_are_selected_by_kind(self):
        bank = self._captured()
        assert bank.names(banks.Kind.PERFORMANCE) == {
            0: "First Performance",
            1: "Second",
        }
        assert bank.names(banks.Kind.PATCH) == {0: "First Patch"}
        with pytest.raises(LookupError):
            bank.names("waveseq")
