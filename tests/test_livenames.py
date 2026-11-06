# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors

"""The cache of names read off a unit."""

import json
import os

from kws import catalog as cat
from kws import favorites, livenames


class TestLiveRoundTrip:
    def test_round_trip_save_load(self, tmp_path):
        path = str(tmp_path / "live-names.json")
        names = {livenames.key("RAM1", 0): "First", livenames.key("RAM1", 99): "Last"}
        livenames.save(names, path)
        loaded = livenames.load(path)
        assert loaded == names

    def test_a_missing_cache_is_not_an_error(self, tmp_path):
        assert livenames.load(str(tmp_path / "absent.json")) == {}

    def test_the_directory_is_created(self, tmp_path):
        path = str(tmp_path / "nested" / "deeper" / "live-names.json")
        livenames.save({livenames.key("RAM1", 1): "Something"}, path)
        assert livenames.load(path) == {livenames.key("RAM1", 1): "Something"}

    def test_no_temporary_file_is_left_behind(self, tmp_path):
        path = tmp_path / "live-names.json"
        livenames.save({livenames.key("RAM1", 0): "One"}, str(path))
        livenames.save({livenames.key("RAM1", 0): "Two"}, str(path))
        assert [p.name for p in tmp_path.iterdir()] == ["live-names.json"]

    def test_a_damaged_cache_is_ignored(self, tmp_path, capsys):
        path = tmp_path / "live-names.json"
        path.write_text("{not json", encoding="utf-8")
        assert livenames.load(str(path)) == {}
        assert "ignoring" in capsys.readouterr().err


class TestSnapshot:
    def test_names_from_snapshot_flattens(self):
        flat = livenames.names_from_snapshot({"A": {0: "One"}, "G": {1: "Two"}})
        assert flat == {"A:000": "One", "G:001": "Two"}


class TestDefaultPath:
    def test_default_path_ends_with_live_names_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(favorites, "data_dir", lambda: str(tmp_path))
        path = livenames.default_path()
        assert path.endswith("live-names.json")
        assert os.path.dirname(path) == str(tmp_path)


class TestApplyTo:
    def test_apply_to_lays_names_over_catalog(self, tmp_path):
        path = str(tmp_path / "live-names.json")
        names = {livenames.key("RAM1", 0): "LiveName"}
        livenames.save(names, path)
        catalog = cat.empty()
        applied = livenames.apply_to(catalog, path)
        assert applied == 1
        assert catalog.live_name("RAM1", 0) == "LiveName"

    def test_apply_to_skips_malformed_key(self, tmp_path):
        path = str(tmp_path / "live-names.json")
        names = {livenames.key("RAM1", 0): "Good", "BADKEY": "Bad"}
        # save bypasses validation? vinsynlib save merges; direct file write
        import json

        with open(path, "w", encoding="utf-8") as handle:
            json.dump(names, handle)
        catalog = cat.empty()
        applied = livenames.apply_to(catalog, path)
        assert applied == 1
        assert catalog.live_name("RAM1", 0) == "Good"


class TestMigrateLegacy:
    def test_migrate_legacy_converts_old_nested_format(self, tmp_path):
        legacy = tmp_path / "live.json"
        legacy.write_text(
            json.dumps(
                {
                    "banks": {
                        "A": [{"pc": 0, "name": "First"}, {"pc": 5, "name": "Fifth"}],
                        "C": [{"pc": 1, "name": "Combo"}],
                    }
                }
            ),
            encoding="utf-8",
        )
        target = tmp_path / "live-names.json"
        result = livenames.migrate_legacy(path=str(target), legacy=str(legacy))
        assert result == str(target)
        assert os.path.exists(target)
        loaded = livenames.load(str(target))
        assert loaded[livenames.key("A", 0)] == "First"
        assert loaded[livenames.key("A", 5)] == "Fifth"
        assert loaded[livenames.key("C", 1)] == "Combo"
        assert legacy.exists()

    def test_migrate_legacy_returns_none_if_target_exists(self, tmp_path):
        legacy = tmp_path / "live.json"
        legacy.write_text(
            json.dumps({"banks": {"A": [{"pc": 0, "name": "X"}]}}), encoding="utf-8"
        )
        target = tmp_path / "live-names.json"
        target.write_text("{}", encoding="utf-8")
        result = livenames.migrate_legacy(path=str(target), legacy=str(legacy))
        assert result is None
