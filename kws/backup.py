# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# kwsed is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version.

"""Bank backups: a thin binding over :mod:`vinsynlib.backup`.

The on-disk envelope (JSON with base64 blocks, format byte, never-overwrite
filename, lenient listing) is the family's and lives in the library. What is
added here is the application's name, so the default directory is kwsed's
per-platform data directory.
"""

from __future__ import annotations

import os
from typing import Any

from kws import favorites
from vinsynlib import backup as _backup
from vinsynlib.backup import FORMAT, Backup

__all__ = [
    "APP_NAME",
    "Backup",
    "BackupError",
    "FORMAT",
    "data_dir",
    "default_dir",
    "list_backups",
    "load",
    "save",
    "describe",
]

APP_NAME = "kwsed"


class BackupError(RuntimeError):
    """A backup could not be written, so a write must not proceed."""


def data_dir() -> str:
    return favorites.data_dir()


def default_dir(root: str | None = None) -> str:
    return os.path.join(root or data_dir(), "backups")


def save(*args: Any, **kwargs: Any) -> str:
    """Binding over :func:`vinsynlib.backup.save`.

    Accepts the library's calling convention (``target_dir`` first, or
    ``directory=``) and the legacy kwsed one (raw bytes first).
    """
    # Legacy kwsed calling convention: save(raw, directory, ...).
    if (args and isinstance(args[0], (bytes, bytearray))) or "bank_label" in kwargs:
        raw = args[0] if args else kwargs.pop("raw", b"")
        directory = args[1] if len(args) > 1 else kwargs.pop("directory", None)
        kind = kwargs.pop("kind", "bank")
        bank_label = kwargs.pop("bank_label", kwargs.pop("slot", "bank"))
        channel = kwargs.pop("channel", None)
        kwargs.pop("names", None)
        target_dir = (
            directory if directory is not None else kwargs.pop("target_dir", None)
        )
        if target_dir is None:
            target_dir = default_dir()
        try:
            return _backup.save(
                target_dir,
                kind=kind,
                slot=str(bank_label),
                name=str(bank_label),
                device_id="" if channel is None else str(channel),
                source="kwsed",
                blocks={"dump": bytes(raw)},
                **{k: v for k, v in kwargs.items() if k not in ("target_dir",)},
            )
        except OSError as exc:
            raise BackupError(
                f"could not write the backup into {target_dir}: {exc}"
            ) from exc

    directory = kwargs.pop("directory", None)
    if directory is not None and not args:
        args = (directory,)
    try:
        return _backup.save(*args, **kwargs)
    except OSError as exc:
        target = args[0] if args else directory
        raise BackupError(f"could not write the backup into {target}: {exc}") from exc


def load(*args: Any, **kwargs: Any) -> Backup:
    try:
        return _backup.load(*args, **kwargs)
    except OSError as exc:
        raise BackupError(str(exc)) from exc


def list_backups(*args: Any, **kwargs: Any) -> list[Backup]:
    return _backup.list_backups(*args, **kwargs)


def describe(path: str) -> dict[str, Any]:
    record = _backup.load(path)
    return {
        "format": FORMAT,
        "bank": record.slot or record.name or "?",
        "kind": record.kind,
        "bytes": sum(len(value) for value in record.blocks.values()),
        "saved": record.saved,
        "path": path,
    }
