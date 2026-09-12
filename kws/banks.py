# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# The bank list is a fact transcribed from Korg's documentation, from the
# bank bytes in dumps the instrument produced, and from Edisyn's Wavestation
# support (Apache-2.0). No Edisyn code is used here.
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

"""What a Wavestation SR holds.

**Twelve banks**, numbered 0-11 in System Exclusive: RAM 1-3, ROM 4-11 and
a Card slot. Each bank holds **50 performances** and **35 patches**, which
are different things and not interchangeable:

* A **patch** is a sound -- four oscillators, each playing a wave or a wave
  sequence.
* A **performance** combines up to eight **parts**, each pointing at a patch,
  with its own MIDI channel, key range and level.

So a performance is the multi-timbral object here, and the thing a sequencer
usually selects.

**Only four banks are writable**: RAM 1, RAM 2, RAM 3 and the Card. The ROM
banks are exactly that. kwsed does not write to any of them, but the
distinction decides what a difference between the catalog and the unit
*means* -- in RAM it means somebody saved their own work, in ROM it means
the catalog is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

__all__ = [
    "Kind",
    "Bank",
    "Slot",
    "BANKS",
    "PERFORMANCES_PER_BANK",
    "PATCHES_PER_BANK",
    "PARTS_PER_PERFORMANCE",
    "bank",
    "bank_numbers",
    "slots",
    "slot",
    "writable_banks",
]

#: Each bank holds this many of each kind.
PERFORMANCES_PER_BANK = 50
PATCHES_PER_BANK = 35

#: A performance combines up to this many parts.
PARTS_PER_PERFORMANCE = 8


class Kind:
    PERFORMANCE = "performance"
    PATCH = "patch"


@dataclass(frozen=True)
class Bank:
    """One bank, as System Exclusive numbers it."""

    number: int          #: 0-11, the byte a dump carries.
    label: str           #: "RAM 1", "ROM 4", "Card".
    writable: bool

    @property
    def id(self) -> str:
        """Short key: ``RAM1``, ``ROM4``, ``CARD``."""
        return self.label.replace(" ", "").upper()


#: The twelve banks in System Exclusive order. Confirmed twice over: the
#: bank bytes in the owner's factory dumps run 0-11, and Edisyn's
#: Wavestation support lists the same twelve with the same four writable.
BANKS: Tuple[Bank, ...] = (
    Bank(0, "RAM 1", True),
    Bank(1, "RAM 2", True),
    Bank(2, "RAM 3", True),
    Bank(3, "ROM 4", False),
    Bank(4, "ROM 5", False),
    Bank(5, "ROM 6", False),
    Bank(6, "ROM 7", False),
    Bank(7, "ROM 8", False),
    Bank(8, "ROM 9", False),
    Bank(9, "ROM 10", False),
    Bank(10, "ROM 11", False),
    Bank(11, "Card", True),
)

_BY_NUMBER: Dict[int, Bank] = {entry.number: entry for entry in BANKS}
_BY_ID: Dict[str, Bank] = {entry.id: entry for entry in BANKS}


@dataclass(frozen=True)
class Slot:
    """One performance or patch in one bank."""

    bank: Bank
    kind: str
    number: int          #: 0-based within the bank; also the wire number.

    @property
    def key(self) -> str:
        # The whole kind, not an initial: "performance" and "patch" both
        # begin with P, so an abbreviation collides and silently merges a
        # bank's 50 performances with its 35 patches. A test caught this.
        return f"{self.bank.id}:{self.kind}:{self.number:03d}"

    @property
    def display(self) -> str:
        return f"{self.bank.label} {self.number:02d}"

    def __str__(self) -> str:
        return f"{self.bank.label} {self.kind} {self.number:02d}"


def bank(which) -> Bank:
    """A bank by its System Exclusive number or its short id."""
    if isinstance(which, int):
        try:
            return _BY_NUMBER[which]
        except KeyError:
            raise LookupError(
                f"no bank {which}; they are numbered "
                f"0-{len(BANKS) - 1}") from None
    try:
        return _BY_ID[str(which).replace(" ", "").upper()]
    except KeyError:
        raise LookupError(
            f"no bank {which!r}; have {', '.join(_BY_ID)}") from None


def bank_numbers() -> List[int]:
    return [entry.number for entry in BANKS]


def writable_banks() -> List[Bank]:
    return [entry for entry in BANKS if entry.writable]


def count_for(kind: str) -> int:
    if kind == Kind.PERFORMANCE:
        return PERFORMANCES_PER_BANK
    if kind == Kind.PATCH:
        return PATCHES_PER_BANK
    raise LookupError(
        f"no kind {kind!r}; have {Kind.PERFORMANCE}, {Kind.PATCH}")


def slots(which, kind: str) -> List[Slot]:
    entry = bank(which)
    return [Slot(bank=entry, kind=kind, number=index)
            for index in range(count_for(kind))]


def slot(which, kind: str, number: int) -> Slot:
    entry = bank(which)
    total = count_for(kind)
    if not 0 <= number < total:
        raise LookupError(
            f"{entry.label} holds {total} {kind}s, so {number} is outside "
            f"0-{total - 1}")
    return Slot(bank=entry, kind=kind, number=number)
