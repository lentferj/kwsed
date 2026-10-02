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

"""Preset names, and where each one came from.

Two sources, kept apart all the way to the screen:

* **Printed** names, from the instrument-definition files shipped for the
  X5 family. Generated locally into ``p2k/data/catalog.json``, which is
  gitignored -- those names are Korg's and are not redistributed here.
* **Live** names, read from a unit over System Exclusive.

A single mapping cannot keep them distinguishable, and the distinction is
the useful part: a user bank whose live name differs from the printed one is
a bank somebody has saved their own work into, and that is the single most
informative thing this program can put on a screen.

An Wavestation will not name one program on request; it dumps all hundred at once,
which is a single silent message. So a catalog is a convenience for working
without the unit rather than a substitute for asking it, and there is no
"this will play the instrument" warning anywhere in kwsed -- reading names
does not.
"""

from __future__ import annotations

import json
import sys
import os
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

__all__ = ["Entry", "Catalog", "load", "empty", "DEFAULT_PATH"]

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "catalog.json"
)


@dataclass(frozen=True)
class Entry:
    """One named slot.

    Keyed on bank **and kind** as well as number: a Wavestation bank holds
    50 performances *and* 35 patches, and performance 7 is not patch 7. A
    catalog keyed on the number alone would quietly merge them.
    """

    bank_id: str
    kind: str
    number: int
    name: str

    @property
    def key(self) -> str:
        return f"{self.bank_id}:{self.kind}:{self.number:03d}"


class Catalog:
    """Printed names, plus whatever has been read from a unit."""

    def __init__(
        self, entries: Iterable[Entry] = (), *, source: str = "", generated: str = ""
    ) -> None:
        self._entries: Dict[str, Entry] = {}
        for entry in entries:
            self._entries[entry.key] = entry
        self._live: Dict[str, str] = {}
        self.source = source
        self.generated = generated

    # --- printed ------------------------------------------------------------

    def entry(self, bank_id: str, kind: str, number: int) -> Optional[Entry]:
        return self._entries.get(f"{bank_id}:{kind}:{number:03d}")

    def name(self, bank_id: str, kind: str, number: int) -> Optional[str]:
        found = self.entry(bank_id, kind, number)
        return found.name if found is not None else None

    def has(self, bank_id: str) -> bool:
        prefix = f"{bank_id}:"
        return any(key.startswith(prefix) for key in self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)

    # --- live ---------------------------------------------------------------

    def set_live_name(self, bank_id: str, kind: str, number: int, name: str) -> None:
        self._live[f"{bank_id}:{kind}:{number:03d}"] = name

    def live_name(self, bank_id: str, kind: str, number: int) -> Optional[str]:
        return self._live.get(f"{bank_id}:{kind}:{number:03d}")

    def is_live(self, bank_id: str, kind: str, number: int) -> bool:
        return f"{bank_id}:{kind}:{number:03d}" in self._live

    def differs(self, bank_id: str, kind: str, number: int) -> bool:
        """Whether the unit and the printed list disagree about this slot.

        **What a disagreement means depends on the bank.** In a user bank it
        means somebody has saved their own work over the factory preset,
        which is the most useful thing this program can point out. In a ROM
        bank it cannot mean that -- ROM does not change -- so it means the
        printed list is wrong, and the unit is right.

        That is not hypothetical. Reading all 640 presets of one ROM off a
        unit on 2026-09-12 and comparing them with Korg's own patch list for
        that ROM left 11 names differing, every one by internal whitespace
        alone -- the instrument holds two spaces where the document prints
        one. Callers should say which kind of disagreement they are
        showing; see :meth:`disagreement` and kwsed/app.py.
        """
        live = self.live_name(bank_id, kind, number)
        if live is None:
            return False
        printed = self.name(bank_id, kind, number)
        return printed is not None and printed != live

    def disagreement(
        self, bank_id: str, kind: str, number: int, writable: bool
    ) -> Optional[str]:
        """Why the unit and the catalog differ here, in words.

        ``None`` when they agree or the slot has not been read.
        """
        if not self.differs(bank_id, kind, number):
            return None
        if writable:
            return "saved over the factory preset"
        return "the printed list is wrong; the unit is right"

    def display_name(self, bank_id: str, kind: str, number: int) -> str:
        """What to show: the unit's name if known, else the printed one."""
        live = self.live_name(bank_id, kind, number)
        if live is not None:
            return live
        return self.name(bank_id, kind, number) or ""

    # --- search -------------------------------------------------------------

    def search(self, needle: str) -> List[Tuple[str, str, int, str]]:
        """Case-insensitive substring search over both sources."""
        wanted = needle.strip().lower()
        if not wanted:
            return []
        seen: Dict[str, Tuple[str, int, str]] = {}
        for key, entry in self._entries.items():
            if wanted in entry.name.lower():
                seen[key] = (entry.bank_id, entry.kind, entry.number, entry.name)
        for key, name in self._live.items():
            if wanted in name.lower():
                # Bank ids are "RAM1" .. "ROM11" and "CARD" -- no colon.
                # An id with one would unpack wrong here; the catalog's
                # bank_id comes from Bank.id, which strips spaces and uppercases.
                bank_id, kind, number = key.split(":")
                seen[key] = (bank_id, kind, int(number), name)
        return [seen[key] for key in sorted(seen)]


def empty() -> Catalog:
    return Catalog()


def load(path: Optional[str] = None) -> Catalog:
    """Load the generated catalog, or an empty one.

    A missing catalog is normal and not an error: kwsed works without it,
    showing numbers only, and a connected unit can supply every name itself.
    """
    target = path or DEFAULT_PATH
    if not os.path.exists(target):
        return empty()
    try:
        with open(target, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return empty()
    try:
        banks_payload = payload.get("banks", {})
    except AttributeError:
        return empty()
    entries = []
    skipped = 0
    for bank_id, rows in banks_payload.items():
        for row in rows:
            # Per row rather than around the loop: one unreadable row used
            # to discard every good one with it, which turns a typo in a
            # generated file into "no names at all".
            try:
                entries.append(
                    Entry(
                        bank_id=bank_id,
                        kind=row["kind"],
                        number=int(row["n"]),
                        name=row["name"],
                    )
                )
            except (KeyError, TypeError, ValueError):
                skipped += 1
    if skipped:
        print(
            f"warning: {target} has {skipped} unreadable row(s), skipped; "
            f"regenerate it with tools/extract_catalog.py",
            file=sys.stderr,
        )
    return Catalog(
        entries,
        source=payload.get("source", ""),
        generated=payload.get("generated", ""),
    )
