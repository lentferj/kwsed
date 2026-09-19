# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# The backup discipline -- read the destination in full before a byte goes
# out, and refuse to write if that read fails -- is ported from the sibling
# rxved project's xv/backup.py:
#   Copyright (C) 2026  rxved contributors           - GPL-2.0-or-later
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

"""Bank backups: the undo for the only destructive thing kwsed does.

Writing a bank replaces all fifty performances or all thirty-five patches
that were in it, and no power cycle brings them back. So the destination is
read first and written here before anything is sent.

**The backup is a real ``.syx``, not a container of our own.** The sibling
rxved stores its backups as JSON, on the grounds that a wall of bytes tells
a person nothing about which file to restore. That reasoning holds, but for
whole banks the more important property is different: a ``.syx`` can be sent
back into the instrument by any tool at all, including its own front panel,
on a day when kwsed is not installed or is the thing that went wrong. A
backup that can only be restored by the program that broke things is not
much of a backup.

So both: the dump itself, byte for byte as the instrument sent it, and a
JSON sidecar carrying the bank, the kind, the date and the names that were
in it -- which is what makes a directory of these readable by a person
deciding what to put back.

**Nothing here talks to MIDI.** It serialises; the bridge does the round
trips. That keeps backups testable without hardware.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from typing import Dict, List, Optional, Sequence

__all__ = [
    "FORMAT", "APP_NAME", "BackupError", "data_dir", "default_dir",
    "save", "load", "describe",
    "list_backups",
]

#: Bumped whenever the sidecar's shape changes.
FORMAT = 1


class BackupError(RuntimeError):
    """A backup could not be written, so a write must not proceed."""


#: The directory name used under whichever per-platform data root applies.
APP_NAME = "kwsed"


def data_dir() -> str:
    """Where this application's **data** lives, per platform.

    Data, not configuration: a backup is the user's only undo for the one
    destructive thing here, so it belongs somewhere that survives a `git
    clean` in a checkout. The layout follows the sibling projects'.
    """
    import sys as _sys
    if _sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return os.path.join(base, APP_NAME)
    elif _sys.platform == "darwin":
        home = os.path.expanduser("~")
        if home != "~":
            return os.path.join(home, "Library", "Application Support",
                                APP_NAME)
    base = os.environ.get("XDG_DATA_HOME")
    # XDG requires an absolute path and says a relative one must be ignored.
    if not base or not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, APP_NAME)


def default_dir(root: Optional[str] = None) -> str:
    """The backup directory, under the data directory unless told otherwise."""
    return os.path.join(root or data_dir(), "backups")


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")
    return cleaned or "bank"


def save(raw: bytes, directory: str, *, bank_label: str, kind: str,
         names: Optional[Sequence[str]] = None,
         channel: Optional[int] = None) -> str:
    """Write one bank's dump and its sidecar. Returns the ``.syx`` path.

    Raises :class:`BackupError` rather than returning on failure, because
    every caller's correct response is the same one: do not write.
    """
    if not raw:
        raise BackupError(
            "refusing to record an empty backup: nothing was read back from "
            "the destination, so there would be nothing to restore")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    stem = f"{stamp}-{_slug(bank_label)}-{_slug(kind)}"
    try:
        os.makedirs(directory, exist_ok=True)
        base = stem
        suffix = 0
        while (os.path.exists(os.path.join(directory, base + ".syx"))
               or os.path.exists(os.path.join(directory, base + ".json"))):
            suffix += 1
            base = f"{stem}-{suffix:02d}"
        syx = os.path.join(directory, base + ".syx")
        with open(syx, "wb") as handle:
            handle.write(raw)
        sidecar = {
            "format": FORMAT,
            "saved": datetime.datetime.now().isoformat(timespec="seconds"),
            "bank": bank_label,
            "kind": kind,
            "channel": channel,
            "bytes": len(raw),
            "names": list(names or []),
        }
        with open(os.path.join(directory, base + ".json"), "w",
                  encoding="utf-8") as handle:
            json.dump(sidecar, handle, indent=2)
            handle.write("\n")
    except OSError as exc:
        raise BackupError(
            f"could not write the backup into {directory}: {exc}. Nothing "
            f"has been sent to the instrument.") from exc
    return syx


def load(path: str) -> bytes:
    """The raw dump of a backup."""
    with open(path, "rb") as handle:
        return handle.read()


def describe(path: str) -> dict:
    """A backup's sidecar, or what can be worked out without one."""
    sidecar = os.path.splitext(path)[0] + ".json"
    if os.path.exists(sidecar):
        with open(sidecar, encoding="utf-8") as handle:
            found = json.load(handle)
        if found.get("format", 0) > FORMAT:
            raise BackupError(
                f"{sidecar} was written by a newer kwsed (format "
                f"{found['format']}, this build understands {FORMAT}). The "
                f"dump beside it is still a plain .syx and can be sent by "
                f"any tool.")
        return found
    return {"format": None, "bank": "?", "kind": "?",
            "bytes": os.path.getsize(path), "names": [],
            "saved": datetime.datetime.fromtimestamp(
                os.path.getmtime(path)).isoformat(timespec="seconds")}


def list_backups(directory: str) -> List[dict]:
    """Every backup in a directory, newest first."""
    if not os.path.isdir(directory):
        return []
    out = []
    for name in sorted(os.listdir(directory), reverse=True):
        if not name.endswith(".syx"):
            continue
        path = os.path.join(directory, name)
        entry = describe(path)
        entry["path"] = path
        out.append(entry)
    return out
