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


def disagreement_words(writable: bool) -> str:
    """How to read a difference between the catalog and the unit.

    **What a disagreement means depends on the bank.** In a writable bank
    (RAM 1-3 and the Card) it means somebody has saved their own work over
    the factory preset, which is the most useful thing this program can
    point out. In a ROM bank it cannot mean that -- ROM does not change --
    so it means the printed list is wrong, and the unit is right.

    Callers should say which kind of disagreement they are showing: this
    is the wording of both, with one home, used by the browser's detail
    line, the status after a read and the command line. The strings are
    the family's; x5ded words its two cases identically.
    """
    if writable:
        return "saved over the factory preset"
    return "the printed list is wrong; the unit is right"


def empty() -> "Catalog":
    return Catalog()


def _copy_from_base(base: _Catalog) -> "Catalog":
    # vinsynlib does not yet expose a public copy/constructor that carries
    # the live layer, so the private state is read here and only here. A
    # future library bump that renames these attributes fails in this one
    # place. Upstream improvement: a public Catalog copy/constructor.
    c = Catalog(base._entries.values(), source=base.source, generated=base.generated)
    c._live = dict(base._live)
    return c


def load(path: str | None = None) -> "Catalog":
    try:
        base = _load(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return Catalog()
    return _copy_from_base(base)


__all__ = [
    "Catalog",
    "Entry",
    "UNNAMED",
    "disagreement_words",
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
        """Why the unit and the catalog differ here, in words.

        ``None`` when they agree or the slot has not been read; see
        :meth:`vinsynlib.catalog.Catalog.differs`. Which way is wrong is
        not a property of the difference: :func:`disagreement_words`
        carries the two meanings, and the writable flag the caller passes
        picks between them.
        """
        if not self.differs(bank_id, number, kind):
            return None
        return disagreement_words(writable)
