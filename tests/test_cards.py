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
        assert cards.card(0).name == "Korg WS Internal ROM"
        assert cards.card(1).name == "Korg WPC-00PI RAM1 factory preload"
        # Section 5's example name for bank 07.
        assert cards.card(7).name == 'Korg WPC-13 "Performance 3"'
        assert cards.card(19).name == 'MCR03-WS "Legendary Prophet-VS Sounds"'
        assert cards.card(24).name == 'MCR03 "Vector World"'
        assert cards.card(39).name == "User 15"

    def test_every_korg_rom_card_name_is_prefixed(self):
        for entry in cards.cards():
            if entry.category == cards.Category.ROM_CARD and entry.number <= 18:
                assert entry.name.startswith("Korg "), entry

    def test_names_are_unique_case_insensitively(self):
        names = [entry.name.casefold() for entry in cards.cards()]
        assert len(names) == len(set(names))

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

    def test_a_float_is_not_a_bank_number(self):
        with pytest.raises(LookupError):
            cards.card(3.7)  # type: ignore[arg-type]

    def test_a_string_is_not_a_bank_number(self):
        with pytest.raises(LookupError):
            cards.card("07")  # type: ignore[arg-type]

    def test_unknown_name_is_none(self):
        assert cards.card_by_name("not a bank") is None

    def test_name_lookup_ignores_case_and_padding(self):
        found = cards.card_by_name('  Korg WPC-13 "PERFORMANCE 3" ')
        assert found is not None
        assert found.number == 7

    def test_counts_are_the_banks_module_s(self):
        assert cards.count_for(banks.Kind.PERFORMANCE) == 50
        assert cards.count_for(banks.Kind.PATCH) == 35

    def test_scope_id_is_the_per_bank_key_space(self):
        assert cards.scope_id(7) == "CARD/07"
        assert cards.scope_id(39) == "CARD/39"


class TestTheArchive:
    def _captured(self, number=7, name='Korg WPC-13 "Performance 3"'):
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
        assert raw["banks"]["07"]["name"] == 'Korg WPC-13 "Performance 3"'
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

    def test_an_old_user_placeholder_is_not_a_captured_name(self, tmp_path):
        path = str(tmp_path / "cards.json")
        cards.save(self._captured(25, "User 01"), path)
        assert cards.load(path)[25].name == ""
        cards.save(self._captured(25, "Pro-Rec WS1 Super WaveDance"), path)
        assert cards.load(path)[25].name == "Pro-Rec WS1 Super WaveDance"

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

    def test_the_active_bank_is_persisted_and_survives_a_save(self, tmp_path):
        path = str(tmp_path / "cards.json")
        cards.set_active(28, path)
        assert cards.load_active(path) == 28
        # Capturing a bank must not clear the active-bank hint.
        cards.save(self._captured(7), path)
        assert cards.load_active(path) == 28
        assert set(cards.load(path)) == {7}

    def test_the_active_bank_is_persisted_and_survives_a_label(self, tmp_path):
        path = str(tmp_path / "cards.json")
        cards.save(self._captured(7), path)
        cards.set_active(28, path)
        assert cards.load_active(path) == 28
        # Relabelling a bank must not clear the active-bank hint.
        updated = cards.set_label(7, "My Label", path)
        assert updated.label == "My Label"
        assert cards.load_active(path) == 28
        raw = json.loads((tmp_path / "cards.json").read_text(encoding="utf-8"))
        assert raw["active"] == 28
        assert set(cards.load(path)) == {7}

    def test_an_invalid_active_value_is_ignored(self, tmp_path):
        path = tmp_path / "cards.json"
        path.write_text(
            json.dumps({"version": 1, "banks": {}, "active": 99}),
            encoding="utf-8",
        )
        assert cards.load_active(str(path)) is None
        path.write_text(
            json.dumps({"version": 1, "banks": {}, "active": True}),
            encoding="utf-8",
        )
        assert cards.load_active(str(path)) is None

    def test_set_active_rejects_an_unknown_bank(self, tmp_path):
        with pytest.raises(LookupError):
            cards.set_active(40, str(tmp_path / "cards.json"))

    def test_names_are_selected_by_kind(self):
        bank = self._captured()
        assert bank.names(banks.Kind.PERFORMANCE) == {
            0: "First Performance",
            1: "Second",
        }
        assert bank.names(banks.Kind.PATCH) == {0: "First Patch"}
        with pytest.raises(LookupError):
            bank.names("waveseq")


class TestCardTitles:
    def _captured(self, number, name="", label=""):
        return cards.capture(
            number,
            name,
            {0: "First"},
            {0: "Patch"},
            label=label,
            captured_at="2026-11-07T12:00:00+00:00",
        )

    def test_a_named_user_bank_falls_back_to_the_printed_name(self):
        entry = cards.card(25)
        assert (
            cards.bank_title(self._captured(25), entry) == "Pro-Rec WS1 Super WaveDance"
        )

    def test_a_placeholder_user_bank_stays_unnamed(self):
        entry = cards.card(30)
        assert cards.bank_title(self._captured(30), entry) == "(unnamed)"

    def test_a_read_name_wins_over_the_printed_name(self):
        entry = cards.card(25)
        assert (
            cards.bank_title(self._captured(25, "Read From Card"), entry)
            == "Read From Card"
        )

    def test_a_label_wins_over_everything(self):
        entry = cards.card(25)
        assert (
            cards.bank_title(self._captured(25, "Read From Card", "My Label"), entry)
            == "My Label"
        )


class TestResolution:
    """Which bank is active, from a name and two slot names.

    The card's name is the only identity signal the synth exposes, and it
    is sixteen bytes, so both exact names and a truncated report are real
    inputs. Ambiguity is answered with ``None``: guessing a bank files a
    capture under the wrong one, which is worse than asking.
    """

    def _archive(self):
        return {
            7: cards.capture(
                7,
                'Korg WPC-13 "Performance 3"',
                {0: "Opening", 1: "Second"},
                {0: "Bell"},
                captured_at="2026-11-07T12:00:00+00:00",
            ),
            25: cards.capture(
                25,
                "User 01",
                {0: "Mine"},
                {0: "My Patch"},
                captured_at="2026-11-07T12:00:00+00:00",
            ),
        }

    def test_an_exact_table_name_resolves(self):
        assert cards.resolve("Korg WS Internal ROM", None, None, captured={}) == 0

    def test_an_exact_captured_name_resolves(self):
        assert cards.resolve("User 01", None, None, captured=self._archive()) == 25

    def test_a_truncated_name_field_still_resolves(self):
        assert cards.resolve('Korg WPC-13 "Perf', None, None, captured={}) == 7

    def test_a_name_collision_is_ambiguous(self):
        archive = self._archive()
        archive[8] = cards.capture(
            8,
            "User 01",
            {0: "Other"},
            {0: "Other Patch"},
            captured_at="2026-11-07T12:00:00+00:00",
        )
        assert cards.resolve("User 01", None, None, captured=archive) is None

    def test_a_fingerprint_breaks_a_name_collision(self):
        archive = self._archive()
        archive[8] = cards.capture(
            8,
            "User 01",
            {0: "Other"},
            {0: "Other Patch"},
            captured_at="2026-11-07T12:00:00+00:00",
        )
        assert cards.resolve("User 01", "Mine", "My Patch", captured=archive) == 25

    def test_an_unknown_name_falls_back_to_the_fingerprint(self):
        assert (
            cards.resolve("No Such Bank", "Opening", "Bell", captured=self._archive())
            == 7
        )

    def test_an_unknown_name_with_no_fingerprint_is_none(self):
        assert (
            cards.resolve("No Such Bank", None, None, captured=self._archive()) is None
        )

    def test_an_unknown_fingerprint_is_none(self):
        assert (
            cards.resolve(
                "No Such Bank", "Nothing", "Nothing", captured=self._archive()
            )
            is None
        )

    def test_empty_inputs_are_none(self):
        assert cards.resolve(None, None, None, captured=self._archive()) is None
        assert cards.resolve("", "", "", captured=self._archive()) is None

    def test_no_captures_and_no_table_name_is_none(self):
        assert cards.resolve("No Such Bank", "A", "B", captured={}) is None


class TestCardWords:
    """The two facts a person needs before trusting one bank."""

    def test_a_plain_captured_bank(self):
        assert cards.card_words(True, None) == "captured"

    def test_an_uncaptured_bank(self):
        assert cards.card_words(False, None) == "not captured"

    def test_a_pcm_dependency_is_always_spoken(self):
        assert cards.card_words(False, "WSC-01") == "not captured; needs WSC-01"
        assert cards.card_words(True, "WSC-03") == "captured; needs WSC-03"
