# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# kwsed is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version.

"""Names read off the unit, kept between runs.

Thin binding over :mod:`vinsynlib.livenames`; the format, the atomic
write and the corrupt-file tolerance are the family's. What this module
adds is the per-application default path, beside the favourites database.
"""

from __future__ import annotations

import json
import os
from typing import Any

from kws import favorites
from vinsynlib import livenames as _livenames
from vinsynlib.catalog import wire_to_display
from vinsynlib.livenames import FILE_NAME, key

__all__ = [
    "FILE_NAME",
    "apply_to",
    "default_path",
    "key",
    "load",
    "migrate_legacy",
    "names_from_snapshot",
    "save",
]


def default_path() -> str:
    """Where the hardware-read names live, beside the favourites database."""
    return os.path.join(favorites.data_dir(), FILE_NAME)


def load(path: str | None = None) -> dict[str, str]:
    """Previously-read names, keyed ``"BANK:NNN"``. Empty if there are none."""
    return _livenames.load(path or default_path())


def save(names: dict[str, str], path: str | None = None) -> None:
    """Write the live layer out. Best effort -- a cache is not required."""
    _livenames.save(path or default_path(), names)


def apply_to(catalog: Any, path: str | None = None) -> int:
    """Lay the stored names over ``catalog``. Returns how many applied."""
    return _livenames.apply_to(catalog, path or default_path())


def names_from_snapshot(snapshot: dict[str, dict[int, str]]) -> dict[str, str]:
    """Convert a live snapshot to the flat key: name format.

    The input is ``{bank_id: {program_change: name}}``. Kind is not
    carried by that shape, so the default (empty) kind is used, matching
    :func:`vinsynlib.livenames.key`'s default.
    """
    flat: dict[str, str] = {}
    for bank_id, names in snapshot.items():
        for program_change, name in names.items():
            flat[key(bank_id, program_change)] = name
    return flat


def _legacy_path(legacy: str | None) -> str:
    return legacy or os.path.join(os.path.dirname(__file__), "data", "live.json")


def _rows_to_names(bank_id: str, rows: object) -> dict[str, str]:
    if not isinstance(rows, list):
        return {}
    names: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            # Legacy rows carried a wire (program-change) number under
            # "pc", the same convention vinsynlib.catalog.load uses for
            # "pc" rows; convert to the display number the live layer keys
            # on, per the family's wire_to_display convention.
            number = wire_to_display(int(row["pc"]))
            name = row["name"]
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(name, str):
            names[key(bank_id, number)] = name
    return names


def _read_legacy(path: str) -> dict[str, str]:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    banks = payload.get("banks", {})
    if not isinstance(banks, dict):
        return {}
    names: dict[str, str] = {}
    for bank_id, rows in banks.items():
        if isinstance(bank_id, str):
            names.update(_rows_to_names(bank_id, rows))
    return names


def migrate_legacy(path: str | None = None, legacy: str | None = None) -> str | None:
    target = path or default_path()
    if os.path.exists(target):
        return None
    names = _read_legacy(_legacy_path(legacy))
    if not names:
        return None
    save(names, target)
    return target if os.path.exists(target) else None
