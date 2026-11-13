# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#

"""Import-folder UI state round-trip."""

import os
from pathlib import Path

import pytest


def test_import_folder_round_trip(tmp_path):
    from kws import config

    path = str(tmp_path / "config.toml")
    assert config.load_import_folder(path) == ""

    config.save_import_folder("/some/folder", path)
    assert config.load_import_folder(path) == "/some/folder"

    ui_path = config.ui_state_path(path)
    assert ui_path is not None
    assert os.path.exists(ui_path)
    assert config.load_import_folder(path) == "/some/folder"


def test_import_folder_non_string_falls_back(tmp_path):
    from kws import config

    path = tmp_path / "config.toml"
    ui_path = config.ui_state_path(str(path))
    assert ui_path is not None
    Path(ui_path).write_text("[import]\nfolder = 42\n", encoding="utf-8")
    assert config.load_import_folder(str(path)) == ""


def test_import_folder_paths_independent(tmp_path):
    from kws import config

    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    path_a = str(dir_a / "config.toml")
    path_b = str(dir_b / "config.toml")

    config.save_import_folder("/folder/a", path_a)
    config.save_import_folder("/folder/b", path_b)

    assert config.load_import_folder(path_a) == "/folder/a"
    assert config.load_import_folder(path_b) == "/folder/b"


def test_parse_preaudit_perf_round_trip(tmp_path):
    from kws import config

    result = config.parse_preaudit_perf("RAM3:49")
    assert result is not None
    bank, slot = result
    assert bank.id == "RAM3"
    assert slot == 49

    result = config.parse_preaudit_perf("Card:07")
    assert result is not None
    bank, slot = result
    assert bank.id == "CARD"
    assert slot == 7


def test_parse_preaudit_perf_unset(tmp_path):
    from kws import config

    assert config.parse_preaudit_perf("") is None
    assert config.parse_preaudit_perf(None) is None


@pytest.mark.parametrize(
    "value",
    ["ROM4:10", "RAM3:50", "RAM3", "RAM3:1x", "BANANA:3"],
)
def test_parse_preaudit_perf_value_error(value):
    from kws import config

    with pytest.raises(ValueError):
        config.parse_preaudit_perf(value)


def test_parse_preaudit_patch_range_round_trip(tmp_path):
    from kws import config

    result = config.parse_preaudit_patch_range("RAM3:27-34")
    assert result is not None
    bank, slots = result
    assert bank.id == "RAM3"
    assert slots == tuple(range(27, 35))
    assert len(slots) == 8

    result = config.parse_preaudit_patch_range("RAM2:5")
    assert result is not None
    bank, slots = result
    assert bank.id == "RAM2"
    assert slots == (5,)

    result = config.parse_preaudit_patch_range("RAM3:0-34")
    assert result is not None
    bank, slots = result
    assert bank.id == "RAM3"
    assert slots == tuple(range(0, 35))


@pytest.mark.parametrize(
    "value",
    ["RAM3:34-27", "ROM5:1-2", "RAM3:35"],
)
def test_parse_preaudit_patch_range_value_error(value):
    from kws import config

    with pytest.raises(ValueError):
        config.parse_preaudit_patch_range(value)


def test_parse_preaudit_patch_range_unset(tmp_path):
    from kws import config

    assert config.parse_preaudit_patch_range("") is None
    assert config.parse_preaudit_patch_range(None) is None


def test_parse_preaudit_backup_defaults_on():
    from kws import config

    assert config.parse_preaudit_backup(None) is True
    assert config.parse_preaudit_backup("") is True
    assert config.parse_preaudit_backup(False) is False
    assert config.parse_preaudit_backup("off") is False
    with pytest.raises(ValueError):
        config.parse_preaudit_backup("maybe")


def test_load_preaudit_backup_round_trip(tmp_path):
    from kws import config

    path = str(tmp_path / "config.toml")
    assert config.load_preaudit_backup(path) is True

    (tmp_path / "config.toml").write_text("preaudit_backup = false\n", encoding="utf-8")
    assert config.load_preaudit_backup(path) is False


def test_load_preaudit_target_round_trip(tmp_path):
    from kws import config

    path = str(tmp_path / "config.toml")
    assert config.load_preaudit_target(path) is None
    assert config.load_preaudit_patch_area(path) is None

    (tmp_path / "config.toml").write_text(
        'preaudit_target_perf = "RAM3:49"\npreaudit_target_patches = "RAM3:27-34"\n',
        encoding="utf-8",
    )
    res = config.load_preaudit_target(path)
    assert res is not None
    assert res[0].id == "RAM3"
    assert res[1] == 49
    res2 = config.load_preaudit_patch_area(path)
    assert res2 is not None
    assert res2[0].id == "RAM3"
    assert res2[1] == tuple(range(27, 35))


def test_load_preaudit_target_malformed(tmp_path):
    from kws import config

    path = tmp_path / "config.toml"
    path.write_text('preaudit_target_perf = "ROM4:1"\n', encoding="utf-8")
    assert config.load_preaudit_target(str(path)) is None


@pytest.mark.parametrize("value", ["true", "false"])
def test_load_single_object_timeout_bool_falls_back(tmp_path, value):
    from kws import config

    path = tmp_path / "config.toml"
    path.write_text(f"single_object_timeout = {value}\n", encoding="utf-8")
    assert config.load_single_object_timeout(str(path)) == (
        config.DEFAULT_SINGLE_OBJECT_TIMEOUT
    )


@pytest.mark.parametrize("value", ["0", "0.0", "-1", "-5.5"])
def test_load_single_object_timeout_non_positive_falls_back(tmp_path, value):
    from kws import config

    path = tmp_path / "config.toml"
    path.write_text(f"single_object_timeout = {value}\n", encoding="utf-8")
    assert config.load_single_object_timeout(str(path)) == (
        config.DEFAULT_SINGLE_OBJECT_TIMEOUT
    )


@pytest.mark.parametrize("value", ["3600", "1e9"])
def test_load_single_object_timeout_absurd_falls_back(tmp_path, value):
    from kws import config

    path = tmp_path / "config.toml"
    path.write_text(f"single_object_timeout = {value}\n", encoding="utf-8")
    assert config.load_single_object_timeout(str(path)) == (
        config.DEFAULT_SINGLE_OBJECT_TIMEOUT
    )


def test_load_single_object_timeout_valid_unchanged(tmp_path):
    from kws import config

    path = tmp_path / "config.toml"
    path.write_text("single_object_timeout = 5.0\n", encoding="utf-8")
    assert config.load_single_object_timeout(str(path)) == 5.0
