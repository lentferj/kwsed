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
from typing import Dict, List, Tuple

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
    """One bank: what the instrument calls it, and the byte that names it.

    **These are two different numbers and conflating them was a real bug.**
    The banks in the order a person sees them -- RAM 1-3, ROM 4-11, Card --
    are not numbered 0-11 on the wire. The Card is System Exclusive bank
    **3**, RAM 3 is **4**, and ROM 11 is **2**.

    kwsed used the display position as the wire byte for its whole life, so
    every "Card" it read was really ROM 10. That is why the card appeared
    never to change when the card in the slot was swapped, power cycled and
    reformatted: ROM does not change. Confirmed on hardware -- a freshly
    formatted card reads back 35 of 35 unnamed patches at byte 3, while byte
    11 holds a ROM bank's sounds.

    **Bank Select addressing** (Player's Guide §4.8 and Reference Appendix 3
    §1.3): the SR's twelve banks are six MIDI banks addressed by CC32 (LSB)
    0-5; CC0 (MSB) is always 0; each MIDI bank holds two internal banks of
    50, so Program Change 0-49 selects the first and 50-99 the second. The
    SR's "Bank Select" MIDI-receive parameter must be ON for this to work;
    kwsed cannot read it.
    """

    number: int  #: 0-11, the position a person sees it in.
    sysex: int  #: the byte a request and a dump actually carry.
    label: str  #: "RAM 1", "ROM 4", "Card".
    writable: bool
    midi_bank: int  #: CC32 (Bank Select LSB) value, 0-5.
    program_base: int  #: 0 or 50, added to the slot number for Program Change.

    # Bank Select addressing per Player's Guide §4.8 and Reference Appendix 3
    # §1.3. The SR's "Bank Select" MIDI-receive parameter must be ON for
    # this to work; kwsed cannot read it.

    @property
    def id(self) -> str:
        """Short key: ``RAM1``, ``ROM4``, ``CARD``."""
        return self.label.replace(" ", "").upper()


#: The twelve banks in System Exclusive order. Confirmed twice over: the
#: bank bytes in the owner's factory dumps run 0-11, and Edisyn's
#: Wavestation support lists the same twelve with the same four writable.
#: Bank Select addressing per Player's Guide §4.8 and Reference Appendix 3
#: §1.3: the SR's twelve banks are six MIDI banks addressed by CC32 (LSB)
#: 0-5; CC0 (MSB) is always 0; each MIDI bank holds two internal banks of
#: 50, so Program Change 0-49 selects the first and 50-99 the second.
#: The SR's "Bank Select" MIDI-receive parameter must be ON for this to work;
#: kwsed cannot read it.
BANKS: Tuple[Bank, ...] = (
    #    pos  wire  label      writable  midi_bank  program_base
    Bank(0, 0, "RAM 1", True, 0, 0),
    Bank(1, 1, "RAM 2", True, 0, 50),
    Bank(2, 4, "RAM 3", True, 2, 0),
    Bank(3, 5, "ROM 4", False, 2, 50),
    Bank(4, 6, "ROM 5", False, 3, 0),
    Bank(5, 7, "ROM 6", False, 3, 50),
    Bank(6, 8, "ROM 7", False, 4, 0),
    Bank(7, 9, "ROM 8", False, 4, 50),
    Bank(8, 10, "ROM 9", False, 5, 0),
    Bank(9, 11, "ROM 10", False, 5, 50),
    Bank(10, 2, "ROM 11", False, 1, 0),
    Bank(11, 3, "Card", True, 1, 50),
)

#: Wire byte -> bank, for reading a dump's own bank byte back into a bank.
_BY_SYSEX: Dict[int, Bank] = {entry.sysex: entry for entry in BANKS}


def by_sysex(byte: int) -> Bank:
    """The bank a dump's bank byte refers to."""
    try:
        return _BY_SYSEX[byte]
    except KeyError:
        raise LookupError(
            f"no bank carries the System Exclusive byte {byte}; the twelve "
            f"use {sorted(_BY_SYSEX)}"
        ) from None


_BY_NUMBER: Dict[int, Bank] = {entry.number: entry for entry in BANKS}
_BY_ID: Dict[str, Bank] = {entry.id: entry for entry in BANKS}


@dataclass(frozen=True)
class Slot:
    """One performance or patch in one bank."""

    bank: Bank
    kind: str
    number: int  #: 0-based within the bank; also the wire number.

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


def bank(which: int | str) -> Bank:
    """A bank by its System Exclusive number or its short id."""
    # `isinstance(True, int)` is True, and `bank(True)` would return RAM 2 --
    # the same shape of mistake config.load_channel guards against at the
    # TOML boundary. A bool is not a bank id here.
    if isinstance(which, bool):
        raise LookupError(f"a bank is not a boolean, got {which!r}")
    if isinstance(which, int):
        try:
            return _BY_NUMBER[which]
        except KeyError:
            raise LookupError(
                f"no bank {which}; they are numbered 0-{len(BANKS) - 1}"
            ) from None
    try:
        return _BY_ID[str(which).replace(" ", "").upper()]
    except KeyError:
        raise LookupError(f"no bank {which!r}; have {', '.join(_BY_ID)}") from None


def bank_numbers() -> List[int]:
    return [entry.number for entry in BANKS]


def writable_banks() -> List[Bank]:
    return [entry for entry in BANKS if entry.writable]


def count_for(kind: str) -> int:
    if kind == Kind.PERFORMANCE:
        return PERFORMANCES_PER_BANK
    if kind == Kind.PATCH:
        return PATCHES_PER_BANK
    raise LookupError(f"no kind {kind!r}; have {Kind.PERFORMANCE}, {Kind.PATCH}")


def slots(which: int | str, kind: str) -> List[Slot]:
    entry = bank(which)
    return [
        Slot(bank=entry, kind=kind, number=index) for index in range(count_for(kind))
    ]


def slot(which: int | str, kind: str, number: int) -> Slot:
    entry = bank(which)
    total = count_for(kind)
    if not 0 <= number < total:
        raise LookupError(
            f"{entry.label} holds {total} {kind}s, so {number} is outside 0-{total - 1}"
        )
    return Slot(bank=entry, kind=kind, number=number)
