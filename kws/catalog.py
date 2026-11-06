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
# kwsed is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.

"""Thin binding to vinsynlib.catalog for kwsed."""

from __future__ import annotations

import json
import os

from vinsynlib.catalog import (
    UNNAMED,
    Catalog as _Catalog,
    Entry,
    load as _load,
    make_key,
    split_key,
)


def empty() -> "Catalog":
    return Catalog()


def load(path: str | None = None) -> "Catalog":
    try:
        base = _load(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return Catalog()
    c = Catalog(base._entries.values(), source=base.source, generated=base.generated)
    c._live = dict(base._live)
    return c


__all__ = [
    "Catalog",
    "Entry",
    "UNNAMED",
    "empty",
    "load",
    "make_key",
    "split_key",
    "DEFAULT_PATH",
]

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "catalog.json"
)


class Catalog(_Catalog):
    def disagreement(
        self, bank_id: str, number: int, writable: bool, kind: str = ""
    ) -> str | None:
        if not self.differs(bank_id, number, kind):
            return None
        if writable:
            return "saved over the factory preset"
        return "the printed list is wrong; the unit is right"
