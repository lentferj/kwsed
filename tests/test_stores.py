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

"""The user's own stores: favourites, backups, the catalog. Names invented.

These are the parts of kwsed that are neither the instrument's data nor
the manufacturer's, so they are also the parts where a mistake is
unrecoverable: a lost favourite cannot be read back off a device. The
tests below are mostly about a round trip rather than a single call.
"""

import json
import os

import pytest

from kws import backup, banks, dumps
from kws import catalog as cat
from kws import messages as m
from kws.favorites import Favorites


@pytest.fixture()
def store(tmp_path):
    with Favorites(str(tmp_path / "f.db")) as opened:
        yield opened


class TestFavouritesAnnotations:
    """Rating, tags and note each change one field and keep the rest.

    Without that, setting a rating on a favourite blanks its tags and
    note, because `add` takes every field and defaults the ones it is not
    given. The `_merge` helper exists for exactly this.
    """

    def test_a_rating_leaves_tags_and_note_alone(self, store):
        store.add("RAM1", 1, name="Invented", tags="pad, lead", note="keep me")
        store.set_rating("RAM1", 1, 5)
        got = store.get("RAM1", 1)
        assert got is not None
        assert got.rating == 5
        assert got.tag_list == ["pad", "lead"]
        assert got.note == "keep me"

    def test_tags_are_trimmed_and_deduplicated(self, store):
        store.add("RAM1", 2, name="Invented")
        store.set_tags("RAM1", 2, "  pad , lead ,PAD,  ")
        got = store.get("RAM1", 2)
        assert got is not None
        assert got.tag_list == ["pad", "lead"]

    def test_a_note_leaves_the_tags_alone(self, store):
        store.add("RAM1", 3, name="Invented", tags="pad")
        store.set_note("RAM1", 3, "changed my mind")
        got = store.get("RAM1", 3)
        assert got is not None
        assert got.note == "changed my mind"
        assert got.tag_list == ["pad"]

    def test_a_rating_outside_zero_to_five_is_refused(self, store):
        store.add("RAM1", 4)
        for bad in (-1, 6):
            with pytest.raises(ValueError):
                store.set_rating("RAM1", 4, bad)

    def test_setting_on_something_not_favourited_creates_it(self, store):
        store.set_note("RAM1", 9, "made a note")
        got = store.get("RAM1", 9)
        assert got is not None and got.note == "made a note"


class TestFavouritesTags:
    def test_tags_counts_every_favourite_carrying_one(self, store):
        store.add("RAM1", 1, tags="pad, lead")
        store.add("RAM1", 2, tags="pad")
        store.add("RAM2", 3, tags="bass")
        assert store.tags() == {"pad": 2, "lead": 1, "bass": 1}

    def test_tags_counts_dormant_rows_too(self, store):
        """A tag does not stop existing because the slot was unfavourited."""
        store.add("RAM1", 1, tags="pad")
        store.remove("RAM1", 1)
        assert store.tags() == {"pad": 1}

    def test_with_tag_matches_a_whole_tag_not_a_substring(self, store):
        store.add("RAM1", 1, tags="padded")
        store.add("RAM1", 2, tags="lead")
        store.add("RAM1", 3, tags="misleading")
        # Neither "pad" inside "padded" nor "lead" inside "misleading"
        # counts: a tag is a member of a set, not a span of text.
        assert [f.number for f in store.with_tag("pad")] == []
        assert [f.number for f in store.with_tag("lead")] == [2]

    def test_with_tag_ignores_case(self, store):
        store.add("RAM1", 1, tags="Pad")
        assert [f.number for f in store.with_tag("PAD")] == [1]

    def test_an_unfavourited_row_is_not_found_by_tag(self, store):
        store.add("RAM1", 1, tags="pad")
        store.remove("RAM1", 1)
        assert store.with_tag("pad") == []


class TestFavouritesLookup:
    def test_contains_accepts_a_string_key_or_a_tuple(self, store):
        store.add("RAM1", 5, name="Invented")
        assert "RAM1:005" in store
        assert ("RAM1", 5) in store
        assert "RAM1:006" not in store

    def test_keys_lists_every_live_slot(self, store):
        store.add("RAM1", 1)
        store.add("RAM2", 2)
        assert store.keys() == {"RAM1:001", "RAM2:002"}

    def test_keys_for_bank_is_numbers_not_strings(self, store):
        store.add("RAM1", 1)
        store.add("RAM1", 7)
        assert store.keys_for_bank("RAM1") == {1, 7}

    def test_all_can_be_ordered_by_each_field(self, store):
        store.add("RAM2", 2, name="Zebra")
        store.add("RAM1", 1, name="Alpha", rating=3)
        by_name = [f.name for f in store.all(order="name")]
        assert by_name == ["Alpha", "Zebra"]
        by_rating = [f.rating for f in store.all(order="rating")]
        assert by_rating == [3, 0]

    def test_an_unknown_order_is_refused_rather_than_interpolated(self, store):
        """`order` reaches here from the command line, and ORDER BY is the
        one clause a placeholder cannot carry."""
        with pytest.raises(ValueError, match="unknown order"):
            store.all(order="name; DROP TABLE favorites")


class TestFavouritesForgetAndDormant:
    def test_forget_removes_the_row_outright(self, store):
        store.add("RAM1", 1, name="Invented", note="bye")
        assert store.forget("RAM1", 1) is True
        assert store.dormant() == []
        assert store.forget("RAM1", 1) is False, "a second forget is a no-op"

    def test_dormant_lists_what_is_saved_but_not_favourited(self, store):
        store.add("RAM1", 1, name="Invented", rating=2)
        store.remove("RAM1", 1)
        dormant = store.dormant()
        assert len(dormant) == 1 and dormant[0].rating == 2


class TestRefreshNames:
    def test_it_relabels_from_a_source_and_counts_the_changes(self, store):
        store.add("RAM1", 1, name="Old")
        store.add("RAM1", 2, name="Same")
        lookup = lambda bank, number: {1: "New", 2: "Same"}[number]  # noqa: E731
        assert store.refresh_names(lookup) == 1
        first = store.get("RAM1", 1)
        assert first is not None and first.name == "New"

    def test_a_source_with_nothing_to_say_changes_nothing(self, store):
        store.add("RAM1", 1, name="Kept")
        assert store.refresh_names(lambda b, n: None) == 0
        got = store.get("RAM1", 1)
        assert got is not None and got.name == "Kept"


class TestBackupRoundTrip:
    """A backup is the only undo for the one destructive thing kwsed does."""

    def _dump(self, bank=3, name=b"Invented"):
        payload = bytearray(banks.PATCHES_PER_BANK * 426)
        payload[0 : len(name)] = name
        return bytes(m.frame_bulk(m.Function.ALL_PATCH_DUMP, bank, bytes(payload)))

    def test_saving_then_loading_returns_the_same_bytes(self, tmp_path):
        raw = self._dump()
        path = backup.save(
            raw,
            str(tmp_path),
            bank_label="Card",
            kind="patch",
            names=["Invented"],
            channel=4,
        )
        assert backup.load(path) == raw

    def test_the_sidecar_carries_what_a_person_needs_to_choose(self, tmp_path):
        path = backup.save(
            self._dump(),
            str(tmp_path),
            bank_label="Card",
            kind="patch",
            names=["Invented"],
            channel=4,
        )
        described = backup.describe(path)
        assert described["bank"] == "Card"
        assert described["kind"] == "patch"
        assert described["names"] == ["Invented"]
        assert described["bytes"] > 0
        assert described["saved"]

    def test_describe_copes_with_a_sidecar_that_is_missing(self, tmp_path):
        path = tmp_path / "orphan.syx"
        path.write_bytes(self._dump())
        described = backup.describe(str(path))
        assert described["format"] is None
        assert described["bank"] == "?"
        assert described["names"] == []

    def test_an_empty_backup_is_refused(self, tmp_path):
        """Nothing was read back, so there would be nothing to restore."""
        with pytest.raises(backup.BackupError, match="empty backup"):
            backup.save(b"", str(tmp_path), bank_label="Card", kind="patch")

    def test_a_directory_that_cannot_be_created_is_refused(self, tmp_path):
        blocker = tmp_path / "a-file"
        blocker.write_text("in the way")
        with pytest.raises(backup.BackupError, match="Nothing has been sent"):
            backup.save(
                self._dump(), str(blocker / "sub"), bank_label="Card", kind="patch"
            )

    def test_listing_is_newest_first_and_skips_the_sidecars(self, tmp_path):
        for index in range(3):
            backup.save(
                self._dump(name=b"Name%02d" % index),
                str(tmp_path),
                bank_label="Card",
                kind="patch",
            )
        listed = backup.list_backups(str(tmp_path))
        assert len(listed) == 3
        assert all(entry["path"].endswith(".syx") for entry in listed)
        stamps = [entry["saved"] for entry in listed]
        assert stamps == sorted(stamps, reverse=True)

    def test_listing_a_directory_that_does_not_exist_is_empty(self, tmp_path):
        assert backup.list_backups(str(tmp_path / "nowhere")) == []

    def test_a_sidecar_from_a_newer_build_is_refused(self, tmp_path):
        path = backup.save(self._dump(), str(tmp_path), bank_label="Card", kind="patch")
        sidecar = os.path.splitext(path)[0] + ".json"
        with open(sidecar, encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["format"] = backup.FORMAT + 1
        with open(sidecar, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        with pytest.raises(backup.BackupError, match="newer kwsed"):
            backup.describe(path)

    def test_two_saves_in_the_same_second_do_not_collide(self, tmp_path):
        """The stamp carries microseconds, and the loop is the backstop."""
        first = backup.save(
            self._dump(), str(tmp_path), bank_label="Card", kind="patch"
        )
        second = backup.save(
            self._dump(), str(tmp_path), bank_label="Card", kind="patch"
        )
        assert first != second
        assert len(backup.list_backups(str(tmp_path))) == 2


class TestBackupDirectories:
    def test_default_dir_sits_under_the_data_directory(self):
        assert backup.default_dir("/somewhere").startswith("/somewhere")

    def test_data_dir_is_absolute_and_names_the_app(self):
        # A relative XDG_DATA_HOME must be ignored, not obeyed.
        assert os.path.isabs(backup.data_dir())
        assert backup.data_dir().endswith("kwsed")


class TestCatalogRoundTrip:
    def _catalog(self):
        return cat.Catalog(
            [
                cat.Entry("CARD", banks.Kind.PATCH, 3, "Invented Chime"),
                cat.Entry("CARD", banks.Kind.PERFORMANCE, 3, "Invented Layer"),
            ]
        )

    def test_the_two_kinds_do_not_collide(self):
        """Performance 3 is not patch 3 -- the collision this key exists for."""
        catalog = self._catalog()
        assert catalog.name("CARD", "patch", 3) == "Invented Chime"
        assert catalog.name("CARD", "performance", 3) == "Invented Layer"

    def test_has_reports_a_bank_by_any_of_its_kinds(self):
        assert self._catalog().has("CARD")
        assert not self._catalog().has("ROM4")

    def test_a_live_name_wins_over_the_printed_one(self):
        catalog = self._catalog()
        catalog.set_live_name("CARD", "patch", 3, "Renamed")
        assert catalog.display_name("CARD", "patch", 3) == "Renamed"

    def test_a_slot_with_neither_shows_nothing(self):
        assert self._catalog().display_name("ROM4", "patch", 0) == ""

    def test_it_reports_whether_the_unit_disagrees(self):
        catalog = self._catalog()
        assert not catalog.differs("CARD", "patch", 3)
        catalog.set_live_name("CARD", "patch", 3, "Renamed")
        assert catalog.differs("CARD", "patch", 3)

    def test_the_disagreement_says_which_way_is_wrong(self):
        catalog = self._catalog()
        catalog.set_live_name("CARD", "patch", 3, "Renamed")
        # A writable bank means somebody saved their own work over it.
        assert (
            catalog.disagreement("CARD", "patch", 3, True)
            == "saved over the factory preset"
        )
        # ROM cannot change, so there the printed list is the wrong one.
        assert (
            catalog.disagreement("CARD", "patch", 3, False)
            == "the printed list is wrong; the unit is right"
        )

    def test_agreement_is_not_a_disagreement(self):
        catalog = self._catalog()
        catalog.set_live_name("CARD", "patch", 3, "Invented Chime")
        assert catalog.disagreement("CARD", "patch", 3, True) is None

    def test_search_covers_both_sources_and_sorts(self):
        catalog = self._catalog()
        catalog.set_live_name("ROM4", "patch", 0, "Invented Chime Too")
        hits = catalog.search("invented chime")
        assert [h[0] for h in hits] == ["CARD", "ROM4"]

    def test_an_empty_search_returns_nothing(self):
        assert self._catalog().search("   ") == []

    def test_len_and_bool_follow_the_entries(self):
        assert len(self._catalog()) == 2
        assert bool(self._catalog())
        assert not bool(cat.empty())


class TestCatalogLoading:
    def test_a_missing_file_is_an_empty_catalog_not_an_error(self, tmp_path):
        assert len(cat.load(str(tmp_path / "nope.json"))) == 0

    def test_unparsable_json_is_an_empty_catalog(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        assert len(cat.load(str(path))) == 0

    def test_a_payload_that_is_not_an_object_is_an_empty_catalog(self, tmp_path):
        path = tmp_path / "list.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        assert len(cat.load(str(path))) == 0

    def test_the_source_and_date_survive_a_round_trip(self, tmp_path):
        path = tmp_path / "cat.json"
        path.write_text(
            json.dumps(
                {
                    "source": "CARD=mine.syx",
                    "generated": "2026-01-01",
                    "banks": {"CARD": [{"kind": "patch", "n": 0, "name": "Invented"}]},
                }
            ),
            encoding="utf-8",
        )
        loaded = cat.load(str(path))
        assert loaded.source == "CARD=mine.syx"
        assert loaded.generated == "2026-01-01"

    def test_an_unreadable_row_is_skipped_not_fatal(self, tmp_path):
        path = tmp_path / "cat.json"
        path.write_text(
            json.dumps(
                {
                    "banks": {
                        "CARD": [
                            {"kind": "patch", "n": "not a number", "name": "Bad"},
                            {"kind": "patch", "n": 1, "name": "Invented"},
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        assert len(cat.load(str(path))) == 1


class TestDumpsEdgeCases:
    """The refusals, which are the point of parse_bulk."""

    def _packet(self, function, bank, payload):
        return m.parse(m.frame(function, [bank] + list(m.to_nibbles(payload))))

    def test_a_performance_dump_yields_exactly_181_bytes(self):
        packet = self._packet(
            m.Function.PERFORMANCE_DUMP,
            0,
            bytes(2 * 181),
        )
        # offset 7 for a single-object dump: bank *and* number
        assert dumps.parse_performance(packet) == bytes(181)

    def test_a_wrong_function_is_refused(self):
        packet = m.parse(m.frame(m.Function.WRITE_COMPLETED, [0, 0]))
        assert packet is not None
        with pytest.raises(m.ProtocolError, match="not a performance dump"):
            dumps.parse_performance(packet)

    def test_a_short_performance_is_refused(self):
        packet = self._packet(m.Function.PERFORMANCE_DUMP, 0, bytes(100))
        with pytest.raises(m.ProtocolError, match="fewer than"):
            dumps.parse_performance(packet)

    def test_a_dump_with_no_bank_byte_is_refused(self):
        packet = m.parse(m.frame(m.Function.ALL_PATCH_DUMP, []))
        assert packet is not None
        with pytest.raises(m.ProtocolError):
            dumps.parse_bulk(packet)

    def test_names_omits_records_with_no_name(self):
        """'Called nothing' and 'not read' are different claims."""

        def record(label):
            data = bytearray(426)
            if label:
                data[0 : len(label)] = label.encode()
            return bytes(data)

        labelled = {0: "First", 2: "Third", 34: "Last"}
        raw = b"".join(record(labelled.get(i, "")) for i in range(35))
        packet = self._packet(m.Function.ALL_PATCH_DUMP, 3, raw)
        dump = dumps.parse_bulk(packet)
        names = dump.names()
        assert names == {0: "First", 2: "Third", 34: "Last"}
        assert dump.count == 35, "all 35 records are there, named or not"

    def test_reading_a_file_skips_foreign_messages(self, tmp_path):
        path = tmp_path / "mixed.syx"
        good = m.frame_bulk(m.Function.ALL_PATCH_DUMP, 3, bytes(35 * 426))
        foreign = bytes([0xF0, 0x41, 0x10, 0x00, 0x10, 0x12, 0xF7])
        path.write_bytes(foreign + bytes(good) + foreign)
        packets = dumps.read_syx_file(str(path))
        assert [p.function for p in packets] == [m.Function.ALL_PATCH_DUMP]

    def test_reading_a_file_with_no_sysex_returns_nothing(self, tmp_path):
        path = tmp_path / "empty.syx"
        path.write_bytes(b"not a dump at all")
        assert dumps.read_syx_file(str(path)) == []

    def test_a_truncated_trailing_message_is_dropped(self, tmp_path):
        path = tmp_path / "cut.syx"
        good = bytes(m.frame_bulk(m.Function.ALL_PATCH_DUMP, 3, bytes(35 * 426)))
        path.write_bytes(good + b"\xf0\x42\x34\x28\x4c")  # no F7
        assert len(dumps.read_syx_file(str(path))) == 1

    def test_a_missing_file_raises(self, tmp_path):
        with pytest.raises(OSError):
            dumps.read_syx_file(str(tmp_path / "nope.syx"))

    def test_record_name_handles_both_padding_conventions(self):
        assert dumps.record_name(b"Ski Jam        \x00") == "Ski Jam"
        assert dumps.record_name(b"Wave Dance      ") == "Wave Dance"
