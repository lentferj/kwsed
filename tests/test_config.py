# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#

"""Import-folder UI state round-trip."""

import os
from pathlib import Path


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
