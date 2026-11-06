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

import os
from typing import Any

from kws import favorites
from vinsynlib import livenames as _livenames
from vinsynlib.livenames import FILE_NAME, key

__all__ = [
    "FILE_NAME",
    "apply_to",
    "default_path",
    "key",
    "load",
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
