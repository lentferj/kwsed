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

"""Card-mode settings round-trip and validation."""


def test_defaults_on_missing_file(tmp_path):
    from kws import config

    path = str(tmp_path / "config.toml")
    assert config.load_card_mode(path) == "cards"
    assert config.load_show_card_defaults(path) is False


def test_round_trip_modes(tmp_path):
    from kws import config

    path = str(tmp_path / "config.toml")
    for mode in config.CARD_MODES:
        config.save_card_mode(mode, path)
        assert config.load_card_mode(path) == mode


def test_round_trip_bools(tmp_path):
    from kws import config

    path = str(tmp_path / "config.toml")
    for value in (False, True):
        config.save_show_card_defaults(value, path)
        assert config.load_show_card_defaults(path) is value


def test_invalid_mode_falls_back(tmp_path):
    from kws import config

    path = tmp_path / "config.toml"
    for raw in ('"hcardx"', "42", "true"):
        path.write_text(f"card_mode = {raw}\n", encoding="utf-8")
        assert config.load_card_mode(str(path)) == "cards"


def test_non_bool_show_defaults_falls_back(tmp_path):
    from kws import config

    path = tmp_path / "config.toml"
    for raw in ("1", '"yes"', "42"):
        path.write_text(f"show_card_defaults = {raw}\n", encoding="utf-8")
        assert config.load_show_card_defaults(str(path)) is False


def test_save_invalid_mode_raises(tmp_path):
    import pytest

    from kws import config

    path = str(tmp_path / "config.toml")
    with pytest.raises(ValueError):
        config.save_card_mode("bogus", path)


def test_saving_keeps_port_and_channel(tmp_path):
    from kws import config

    path = str(tmp_path / "config.toml")
    config.save_channel(7, path)
    config.save_last_port("some port", path)

    config.save_card_mode("hcard", path)
    config.save_show_card_defaults(True, path)

    assert config.load_channel(path) == 7
    assert config.load_last_port(path) == "some port"
    assert config.load_card_mode(path) == "hcard"
    assert config.load_show_card_defaults(path) is True


def test_browser_ui_flags_round_trip(tmp_path):
    from kws import config

    path = str(tmp_path / "config.toml")
    assert config.load_show_patches(path) is True
    assert config.load_deps_pane(path) is True

    config.save_show_patches(False, path)
    config.save_deps_pane(False, path)
    assert config.load_show_patches(path) is False
    assert config.load_deps_pane(path) is False

    config.save_show_patches(True, path)
    assert config.load_show_patches(path) is True


def test_non_bool_browser_flags_fall_back(tmp_path):
    from kws import config

    path = tmp_path / "config.toml"
    path.write_text(
        '"browser.show_patches" = 1\n"browser.deps_pane" = "yes"\n',
        encoding="utf-8",
    )
    assert config.load_show_patches(str(path)) is True
    assert config.load_deps_pane(str(path)) is True
