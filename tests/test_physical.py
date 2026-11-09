# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.

"""The archive of ordinary Program Cards, identified by content."""

import json
import os

import pytest

from kws import physical


def _card(card_id="c1", label="Orchestra card", digest="a" * 64):
    return physical.PhysicalCard(
        id=card_id,
        label=label,
        digest=digest,
        captured_at="2026-11-09T12:00:00+00:00",
        performances={0: "First Performance"},
        patches={0: "First Patch"},
        verified=True,
    )


class TestTheArchive:
    def test_round_trip_save_load(self, tmp_path):
        path = str(tmp_path / "physical-cards.json")
        card = _card()
        physical.save(card, path)
        assert physical.load(path) == {"c1": card}

    def test_the_schema_is_versioned_and_keyed_by_id(self, tmp_path):
        path = tmp_path / "physical-cards.json"
        physical.save(_card(), str(path))
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["version"] == 1
        assert set(raw["cards"]) == {"c1"}
        assert raw["cards"]["c1"]["label"] == "Orchestra card"
        assert raw["cards"]["c1"]["performances"] == {"0": "First Performance"}
        assert raw["cards"]["c1"]["patches"] == {"0": "First Patch"}

    def test_a_second_save_keeps_the_first_card(self, tmp_path):
        path = str(tmp_path / "physical-cards.json")
        physical.save(_card(), path)
        physical.save(_card("c2", "Live card", "b" * 64), path)
        loaded = physical.load(path)
        assert set(loaded) == {"c1", "c2"}
        assert loaded["c2"].label == "Live card"

    def test_a_missing_archive_is_not_an_error(self, tmp_path):
        assert physical.load(str(tmp_path / "absent.json")) == {}

    def test_a_damaged_archive_is_ignored_with_a_warning(self, tmp_path, capsys):
        path = tmp_path / "physical-cards.json"
        path.write_text("{not json", encoding="utf-8")
        assert physical.load(str(path)) == {}
        assert "ignoring" in capsys.readouterr().err

    def test_a_future_version_is_ignored(self, tmp_path):
        path = tmp_path / "physical-cards.json"
        path.write_text(json.dumps({"version": 99, "cards": {}}), encoding="utf-8")
        assert physical.load(str(path)) == {}

    def test_malformed_entries_are_dropped_not_guessed(self, tmp_path):
        path = tmp_path / "physical-cards.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "cards": {
                        "c1": {
                            "label": "Good",
                            "digest": "a" * 64,
                            "captured_at": "now",
                        },
                        "c2": "not a card",
                        "c3": {
                            "label": 3,
                            "digest": "b" * 64,
                            "captured_at": "now",
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        loaded = physical.load(str(path))
        assert set(loaded) == {"c1"}
        assert loaded["c1"].performances == {}

    def test_the_directory_is_created_and_no_temporary_file_is_left(self, tmp_path):
        path = tmp_path / "nested" / "deeper" / "physical-cards.json"
        physical.save(_card(), str(path))
        physical.save(_card("c2", "Second", "b" * 64), str(path))
        assert sorted(p.name for p in path.parent.iterdir()) == ["physical-cards.json"]

    def test_default_path_is_beside_the_favourites_database(
        self, tmp_path, monkeypatch
    ):
        from kws import favorites

        monkeypatch.setattr(favorites, "data_dir", lambda: str(tmp_path))
        path = physical.default_path()
        assert os.path.basename(path) == "physical-cards.json"
        assert os.path.dirname(path) == str(tmp_path)


class TestTheActiveCard:
    def test_the_active_card_is_persisted_and_survives_a_save(self, tmp_path):
        path = str(tmp_path / "physical-cards.json")
        physical.save(_card("c1"), path)
        physical.save(_card("c2", "Second", "b" * 64), path)
        physical.set_active("c2", path)
        assert physical.active(path) == "c2"
        # Capturing another card must not clear the active-card hint.
        physical.save(_card("c3", "Third", "c" * 64), path)
        assert physical.active(path) == "c2"

    def test_an_invalid_active_value_is_ignored(self, tmp_path):
        path = tmp_path / "physical-cards.json"
        path.write_text(
            json.dumps({"version": 1, "cards": {}, "active": "c99"}),
            encoding="utf-8",
        )
        assert physical.active(str(path)) is None
        path.write_text(
            json.dumps({"version": 1, "cards": {}, "active": 7}),
            encoding="utf-8",
        )
        assert physical.active(str(path)) is None

    def test_set_active_rejects_an_unknown_card(self, tmp_path):
        path = str(tmp_path / "physical-cards.json")
        physical.save(_card(), path)
        with pytest.raises(LookupError):
            physical.set_active("c9", path)
        assert physical.active(path) is None


class TestIdentity:
    def test_find_by_digest_returns_the_card(self):
        cards = {"c1": _card(), "c2": _card("c2", "Other", "b" * 64)}
        assert physical.find_by_digest("a" * 64, cards) == "c1"

    def test_find_by_digest_is_none_for_no_match(self):
        assert physical.find_by_digest("z" * 64, {"c1": _card()}) is None

    def test_find_by_digest_is_none_for_an_empty_digest(self):
        assert physical.find_by_digest("", {"c1": _card()}) is None

    def test_find_by_digest_is_none_when_two_cards_share_it(self):
        cards = {"c1": _card(), "c2": _card("c2", "Copy", "a" * 64)}
        assert physical.find_by_digest("a" * 64, cards) is None

    def test_next_id_starts_at_one(self):
        assert physical.next_id({}) == "c1"

    def test_next_id_takes_the_first_free_number(self):
        assert physical.next_id({"c1": _card()}) == "c2"
        assert physical.next_id({"c1": _card(), "c2": _card("c2")}) == "c3"
        assert physical.next_id({"c2": _card("c2")}) == "c1"

    def test_scope_id_is_the_per_card_key_space(self):
        assert physical.scope_id("c1") == "CARD/c1"
        assert physical.scope_id("c12") == "CARD/c12"


class TestLabels:
    def test_set_label_updates_only_the_label(self, tmp_path):
        path = str(tmp_path / "physical-cards.json")
        card = _card()
        physical.save(card, path)
        updated = physical.set_label("c1", "  Live card  ", path)
        assert updated.label == "Live card"
        assert updated.digest == card.digest
        assert updated.captured_at == card.captured_at
        assert updated.performances == card.performances
        assert updated.patches == card.patches
        assert updated.verified == card.verified
        assert physical.load(path)["c1"] == updated

    def test_set_label_refuses_an_unknown_card(self, tmp_path):
        path = str(tmp_path / "physical-cards.json")
        physical.save(_card(), path)
        with pytest.raises(LookupError):
            physical.set_label("c9", "Nope", path)
