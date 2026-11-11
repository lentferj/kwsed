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

"""A modal file browser for the import path.

**The screen is a question, not a read.**  It lists a directory and
answers the app with one absolute path, or ``None`` when the user backs
out.  Nothing here opens the file, talks to a unit or imports
:mod:`kwsed.app`: the app decides what to do with the answer, and this
module stays usable in a test with no application around it.

**One directory at a time.**  ``⏎`` on ``..`` or on a directory rebuilds
the table in place and keeps the screen open, so walking to the file is
arrow keys and ``⏎`` rather than a stack of screens.  ``⏎`` on a file is
the answer.  ``escape``/``q`` cancel, ``r`` re-reads the directory --
useful when the file was copied in after the screen opened.

**Every file is listed.**  A browser that hid everything but ``.syx``
would be lying about what is in the folder, and the user might be
looking for a ``.mid`` this build does not import yet.  The SysEx,
MIDI, SoundQuest and SoundDiver extensions sort first instead, so the
common case is at the top.

**A directory that cannot be read is a message, not a crash.**  The
listing is one I/O call and it can fail with permission denied, a stale
mount or a removed drive.  The status line says which directory and
why, and the table keeps showing whatever it showed before.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Tuple

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import DataTable, Label, Static

__all__ = ["PRIORITY_SUFFIXES", "FilePickerScreen", "start_folder"]

#: Extensions the importers understand, sorted ahead of every other file.
#: They are still listed with a name like any other; the suffix only
#: decides the order.
PRIORITY_SUFFIXES = (
    ".syx",
    ".sysex",
    ".mid",
    ".midi",
    ".sqs",
    ".lib",
    ".img",
    ".zip",
    ".bnk",
)

#: The row that walks up one level.  It is a row, not a binding, so it is
#: reached and opened the same way as a directory.
_PARENT = ".."

#: One row: what it is (``parent``, ``dir`` or ``file``) and where it is.
_Entry = Tuple[str, Path]


def start_folder(path: str) -> str:
    """The directory a picker should open for ``path``.

    A directory is opened itself, a file is opened at its parent, and
    anything empty or unusable falls back to the home directory.  The
    answer is absolute, because it is also what the app persists as the
    remembered import folder -- a relative answer would depend on where
    the program happened to start.
    """
    home = str(Path.home())
    if not path or not path.strip():
        return home
    try:
        candidate = Path(path).expanduser()
        if candidate.is_dir():
            return os.path.abspath(candidate)
        if candidate.is_file():
            return os.path.abspath(candidate.parent)
    except OSError:
        return home
    return home


def _literal(text: str) -> str:
    """``text`` as markup that renders as exactly itself.

    A file called ``take[1].syx`` must keep its bracket in a table cell
    instead of being read as a tag and losing it.
    """
    return text.replace("[", "\\[")


def _sort_key(entry: _Entry) -> Tuple[int, int, str]:
    """Directories first, then priority suffixes in order, then the rest.

    Every group is ordered case-insensitively, so ``Drums`` and ``drums``
    are neighbours rather than two ends of the list.
    """
    kind, path = entry
    if kind == "dir":
        return (0, 0, path.name.lower())
    suffix = path.suffix.lower()
    if suffix in PRIORITY_SUFFIXES:
        return (1, PRIORITY_SUFFIXES.index(suffix), path.name.lower())
    return (2, 0, path.name.lower())


def _read_entries(folder: Path) -> List[_Entry]:
    """The rows for ``folder``, parent first, or an :class:`OSError`.

    ``os.scandir`` stats each entry as it goes, which is one call rather
    than one per name.  An entry whose type cannot be read is treated as
    a file: opening it answers with its path, and the import path will
    say whether it is usable.
    """
    entries: List[_Entry] = [("parent", folder.parent)]
    with os.scandir(folder) as scan:
        for item in scan:
            try:
                kind = "dir" if item.is_dir() else "file"
            except OSError:
                kind = "file"
            entries.append((kind, Path(item.path)))
    # `..` stays first; the rest are sorted among themselves.
    return [entries[0], *sorted(entries[1:], key=_sort_key)]


class FilePickerScreen(ModalScreen[Optional[str]]):
    """Choose an import file, one directory at a time.

    ``start`` is a directory, a file or nothing; :func:`start_folder`
    decides what is opened.  The answer is the absolute path of the file
    ``⏎`` landed on, or ``None`` when the user cancels.  The screen
    never closes on navigation, so the answer is always one deliberate
    key away.
    """

    DEFAULT_CSS = """
    FilePickerScreen { align: center middle; }
    FilePickerScreen > Vertical {
        width: 90; height: 80%; max-height: 90%;
        border: thick $accent; background: $surface; padding: 1 2;
    }
    FilePickerScreen .title { height: auto; text-style: bold; }
    FilePickerScreen .folder { height: auto; color: $text-muted; }
    FilePickerScreen DataTable { height: 1fr; }
    FilePickerScreen .status { height: auto; color: $warning; }
    FilePickerScreen .hint { height: auto; color: $text-muted; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("q", "cancel", "Cancel"),
        Binding("r", "refresh", "Refresh"),
        Binding("enter", "open", "Open", priority=True),
    ]

    TABLE_ID = "picker-table"
    FOLDER_ID = "picker-folder"
    STATUS_ID = "picker-status"

    def __init__(
        self,
        *,
        start: str = "",
        title: str = "Open an import file",
        start_row: int = 0,
    ) -> None:
        super().__init__()
        self._title = title
        self._folder = Path(start_folder(start))
        # A direct unreadable start folder falls back to home.
        try:
            _read_entries(self._folder)
        except OSError:
            self._folder = Path(start_folder(""))
        self._start_row = max(0, start_row)
        self._last_row = 0
        self._entries: List[_Entry] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._title, classes="title")
            yield Static(
                str(self._folder), id=self.FOLDER_ID, classes="folder", markup=False
            )
            yield DataTable(
                id=self.TABLE_ID,
                cursor_type="row",
                zebra_stripes=True,
            )
            yield Static("", id=self.STATUS_ID, classes="status", markup=False)
            yield Static(
                "⏎ open · r refresh · esc / q cancel",
                classes="hint",
                markup=False,
            )

    def on_mount(self) -> None:
        table = self.query_one(f"#{self.TABLE_ID}", DataTable)
        table.add_column("name", key="name")
        self._load(self._folder, self._start_row)
        table.focus()

    def _show_folder(self) -> None:
        self.query_one(f"#{self.FOLDER_ID}", Static).update(str(self._folder))

    def _set_status(self, message: str) -> None:
        self.query_one(f"#{self.STATUS_ID}", Static).update(message)

    def _rebuild(self, cursor: int = 0) -> None:
        table = self.query_one(f"#{self.TABLE_ID}", DataTable)
        table.clear(columns=False)
        for index, (kind, path) in enumerate(self._entries):
            if kind == "parent":
                label = _PARENT
            elif kind == "dir":
                label = f"{path.name}/"
            else:
                label = path.name
            table.add_row(_literal(label), key=str(index))
        if self._entries:
            table.move_cursor(row=min(max(cursor, 0), len(self._entries) - 1))

    def _load(self, folder: Path, cursor: int = 0) -> bool:
        try:
            entries = _read_entries(folder)
        except OSError as exc:
            self._set_status(f"Cannot read {folder}: {exc}")
            return False
        self._folder = folder
        self._entries = entries
        self._show_folder()
        self._rebuild(cursor)
        self._set_status("")
        return True

    def action_open(self) -> None:
        if not self._entries:
            return
        table = self.query_one(f"#{self.TABLE_ID}", DataTable)
        row = table.cursor_row
        if not 0 <= row < len(self._entries):
            return
        kind, path = self._entries[row]
        if kind == "file":
            self._last_row = row
            self.dismiss(os.path.abspath(path))
            return
        self._load(path)

    def on_key(self, event: Key) -> None:
        key = getattr(event, "key", "")
        if len(key) != 1 or not key.isalpha() or key in ("q", "r"):
            return
        table = self.query_one(f"#{self.TABLE_ID}", DataTable)
        if not self._entries:
            return
        wanted = key.lower()
        current = table.cursor_row
        start = current + 1 if 0 <= current < len(self._entries) else 0
        order = list(range(start, len(self._entries))) + list(range(0, start))
        for row in order:
            kind, path = self._entries[row]
            if kind == "parent":
                continue
            if path.name.lower().startswith(wanted):
                table.move_cursor(row=row)
                event.stop()
                event.prevent_default()
                return

    def action_refresh(self) -> None:
        table = self.query_one(f"#{self.TABLE_ID}", DataTable)
        row = table.cursor_row if self._entries else 0
        self._load(self._folder, row)

    @property
    def folder(self) -> Path:
        return self._folder

    @property
    def last_row(self) -> int:
        return self._last_row

    def action_cancel(self) -> None:
        self.dismiss(None)
