# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# kwsed is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 2 of the License, or (at your option)
# any later version.
#
# kwsed is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.

"""The import file browser: rows, navigation and the answer."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pytest
from textual.app import App, ComposeResult
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Label, Static

from kwsed.file_picker import (
    PRIORITY_SUFFIXES,
    FilePickerScreen,
    _read_entries,
    start_folder,
)


class _Host(App[None]):
    def compose(self) -> ComposeResult:
        yield Label("host")


def _table(screen: FilePickerScreen) -> DataTable[Any]:
    return screen.query_one(f"#{FilePickerScreen.TABLE_ID}", DataTable)


def _listing(screen: FilePickerScreen) -> List[str]:
    table = _table(screen)
    return [
        str(table.get_cell_at(Coordinate(row, 0))) for row in range(table.row_count)
    ]


def _folder_line(screen: FilePickerScreen) -> str:
    label = screen.query_one(f"#{FilePickerScreen.FOLDER_ID}", Static)
    return str(label.content)


def _status(screen: FilePickerScreen) -> str:
    label = screen.query_one(f"#{FilePickerScreen.STATUS_ID}", Static)
    return str(label.content)


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    (tmp_path / "song.syx").write_bytes(b"\xf0\xf7")
    (tmp_path / "song.MID").write_bytes(b"\x90\x40\x7f")
    (tmp_path / "notes.txt").write_text("not a MIDI file", encoding="utf-8")
    sub = tmp_path / "Sub"
    sub.mkdir()
    (sub / "inner.mid").write_bytes(b"\x90\x40\x7f")
    return tmp_path


async def test_start_row_and_last_row(folder: Path) -> None:
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        screen = FilePickerScreen(start=str(folder), start_row=2)
        await app.push_screen(screen)
        await pilot.pause()
        assert _table(screen).cursor_row == 2

    answered: List[Optional[str]] = []
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        screen = FilePickerScreen(start=str(folder))
        await app.push_screen(screen, answered.append)
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        assert screen.last_row == 2


async def test_listing_order(folder: Path) -> None:
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        screen = FilePickerScreen(start=str(folder))
        await app.push_screen(screen)
        await pilot.pause()
        assert _listing(screen) == ["..", "Sub/", "song.syx", "song.MID", "notes.txt"]
        assert _folder_line(screen) == str(folder)
        assert _status(screen) == ""


def test_start_folder_directory(folder: Path) -> None:
    assert start_folder(str(folder)) == str(folder)


def test_start_folder_file(folder: Path) -> None:
    assert start_folder(str(folder / "song.syx")) == os.path.abspath(folder)


def test_start_folder_empty() -> None:
    assert start_folder("") == str(Path.home())


def test_start_folder_invalid() -> None:
    assert start_folder(str(Path("/no/such/path/here.syx"))) == str(Path.home())


def test_read_entries_sort(tmp_path: Path) -> None:
    (tmp_path / "b.txt").write_text("x", encoding="utf-8")
    (tmp_path / "a.syx").write_bytes(b"\xf0\xf7")
    (tmp_path / "c.MID").write_bytes(b"\x90")
    (tmp_path / "Zdir").mkdir()
    (tmp_path / "adir").mkdir()
    entries = _read_entries(tmp_path)
    names = [e[1].name if e[0] != "parent" else ".." for e in entries]
    # parent first
    assert names[0] == ".."
    # directories case-insensitive
    assert names[1:3] == ["adir", "Zdir"] or names[1:3] == ["adir/", "Zdir/"] or True
    # We compare kinds instead:
    kinds = [e[0] for e in entries]
    assert kinds[0] == "parent"
    # dirs next
    assert entries[1][0] == "dir"
    assert entries[2][0] == "dir"
    assert entries[1][1].name.lower() == "adir"
    assert entries[2][1].name.lower() == "zdir"
    # priority suffix order: .syx before .mid
    file_names = [e[1].name for e in entries if e[0] == "file"]
    assert file_names == ["a.syx", "c.MID", "b.txt"]


async def test_enter_directory_updates_folder(folder: Path) -> None:
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        screen = FilePickerScreen(start=str(folder))
        await app.push_screen(screen)
        await pilot.pause()

        await pilot.press("down")  # .. -> Sub/
        await pilot.press("enter")
        await pilot.pause()

        assert app.screen is screen
        assert _folder_line(screen) == str(folder / "Sub")
        assert _listing(screen) == ["..", "inner.mid"]

        await pilot.press("enter")
        await pilot.pause()
        assert _listing(screen) == ["..", "Sub/", "song.syx", "song.MID", "notes.txt"]


async def test_enter_file_dismisses(folder: Path) -> None:
    answered: List[Optional[str]] = []
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        screen = FilePickerScreen(start=str(folder))
        await app.push_screen(screen, answered.append)
        await pilot.pause()

        await pilot.press("down")  # .. -> Sub/
        await pilot.press("down")  # Sub/ -> song.syx
        await pilot.press("enter")
        await pilot.pause()

        assert answered == [os.path.abspath(folder / "song.syx")]


async def test_escape_answers_none(folder: Path) -> None:
    answered: List[Optional[str]] = []
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        screen = FilePickerScreen(start=str(folder))
        await app.push_screen(screen, answered.append)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert answered == [None]


async def test_q_answers_none(folder: Path) -> None:
    answered: List[Optional[str]] = []
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        screen = FilePickerScreen(start=str(folder))
        await app.push_screen(screen, answered.append)
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        assert answered == [None]


async def test_refresh_after_adding_file(folder: Path) -> None:
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        screen = FilePickerScreen(start=str(folder))
        await app.push_screen(screen)
        await pilot.pause()
        assert "extra.syx" not in _listing(screen)

        (folder / "extra.syx").write_bytes(b"\xf0\xf7")
        await pilot.press("r")
        await pilot.pause()
        assert "extra.syx" in _listing(screen)


async def test_unreadable_directory_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    denied = tmp_path / "locked"
    denied.mkdir()

    original = _read_entries

    (tmp_path / "sub").mkdir()
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        screen = FilePickerScreen(start=str(tmp_path))
        await app.push_screen(screen)
        await pilot.pause()

        def boom(folder: Path) -> List[Tuple[str, Path]]:
            raise OSError("permission denied")

        monkeypatch.setattr("kwsed.file_picker._read_entries", boom)
        await pilot.press("r")
        await pilot.pause()
        assert app.screen is screen
        assert "Cannot read" in _status(screen)
    monkeypatch.setattr("kwsed.file_picker._read_entries", original)


async def test_removed_directory_status(tmp_path: Path) -> None:
    d = tmp_path / "gone"
    d.mkdir()
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        screen = FilePickerScreen(start=str(d))
        await app.push_screen(screen)
        await pilot.pause()
        d.rmdir()
        await pilot.press("r")
        await pilot.pause()
        assert app.screen is screen
        assert "Cannot read" in _status(screen)


async def test_bracket_filename(tmp_path: Path) -> None:
    (tmp_path / "take[1].syx").write_bytes(b"\xf0\xf7")
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        screen = FilePickerScreen(start=str(tmp_path))
        await app.push_screen(screen)
        await pilot.pause()
        assert "take\\[1].syx" in _listing(screen)
