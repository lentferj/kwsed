# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# The bank table is transcribed as a fact from the Hypersynth HCard-101
# manual's Wavestation-SR mode, cross-checked against the card names the
# instrument itself reports. No vendor code is used here.
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

"""The forty banks behind one HCard slot.

The Wavestation SR has **one** card slot. An HCard-101 multiplexes forty
banks behind it, selected physically on the card: 00 is the Wavestation's
own ROM, 01-04 the factory preloads, 05-24 the Korg ROM cards and 25-39
the user banks. The synth can address none of them remotely -- which bank
is in front of it is a property of the card's front panel, not of a wire
byte.

So this module is the first of the three layers in ``docs/HCARD.md``:

1. **A static table** of what the forty banks are (this module).
2. A per-bank archive of what was read off the unit (``cards.json``).
3. Identity resolution from the live card name and its first names.

The physical ``CARD`` slot stays ``CARD`` for writes, favourites and
backups -- decision 8-B1. The forty banks are a browse overlay over it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from kws.banks import PATCHES_PER_BANK, PERFORMANCES_PER_BANK, count_for

__all__ = [
    "Category",
    "Card",
    "USER_FIRST",
    "USER_LAST",
    "CARDS",
    "PATCHES_PER_BANK",
    "PERFORMANCES_PER_BANK",
    "cards",
    "card",
    "is_user",
    "bank_numbers",
    "count_for",
]

#: The first user bank. 00-24 are factory, preload and ROM-card banks.
USER_FIRST = 25

#: The last user bank.
USER_LAST = 39


class Category:
    """What kind of bank this is.

    The words are the design document's, and they are different claims:
    ``factory`` is the Wavestation's own ROM, ``preload`` is the factory
    preload set, ``rom-card`` is a Korg card and ``user`` is somewhere a
    person writes.
    """

    FACTORY = "factory"
    PRELOAD = "preload"
    ROM_CARD = "rom-card"
    USER = "user"


@dataclass(frozen=True)
class Card:
    """One of the forty banks, as the HCard manual names it."""

    number: int  #: 00-39 in Wavestation-SR mode.
    name: str  #: The name the card reports for this bank.
    category: str  #: One of :class:`Category`.
    #: The PCM card a bank needs, if any. 16-18 need WSC-01/02/03; a patch
    #: that names a missing PCM wave plays the wrong sound, silently.
    needs_pcm: Optional[str] = None

    @property
    def is_user(self) -> bool:
        return self.category == Category.USER

    @property
    def is_rom(self) -> bool:
        """True for a bank the HCard holds read-only."""
        return self.category != Category.USER


def _wpc(number: int) -> str:
    """A WPC card's reported name: ``WPC-13 "Performance 3"``."""
    return f'WPC-{number} "Performance {number - 10}"'


#: The forty banks in HCard order. The manual gives 00-04 and 25-39 as
#: ranges; 05-24 are the Korg cards, and the WPC series comes first, then
#: the MCR03 wave-sequence cards, then the WSC PCM-card sets. Banks 16-18
#: are the three that require a WSC PCM card to sound right.
CARDS: Tuple[Card, ...] = (
    Card(0, "WS Internal ROM", Category.FACTORY),
    Card(1, "Factory Preload 1", Category.PRELOAD),
    Card(2, "Factory Preload 2", Category.PRELOAD),
    Card(3, "Factory Preload 3", Category.PRELOAD),
    Card(4, "Factory Preload 4", Category.PRELOAD),
    Card(5, _wpc(11), Category.ROM_CARD),
    Card(6, _wpc(12), Category.ROM_CARD),
    Card(7, _wpc(13), Category.ROM_CARD),
    Card(8, _wpc(14), Category.ROM_CARD),
    Card(9, _wpc(15), Category.ROM_CARD),
    Card(10, _wpc(16), Category.ROM_CARD),
    Card(11, _wpc(17), Category.ROM_CARD),
    Card(12, _wpc(18), Category.ROM_CARD),
    Card(13, _wpc(19), Category.ROM_CARD),
    Card(14, _wpc(20), Category.ROM_CARD),
    Card(15, _wpc(21), Category.ROM_CARD),
    Card(16, _wpc(22), Category.ROM_CARD, needs_pcm="WSC-01"),
    Card(17, _wpc(23), Category.ROM_CARD, needs_pcm="WSC-02"),
    Card(18, _wpc(24), Category.ROM_CARD, needs_pcm="WSC-03"),
    Card(19, "MCR03-WS1", Category.ROM_CARD),
    Card(20, "MCR03-WS2", Category.ROM_CARD),
    Card(21, "MCR03-WS3", Category.ROM_CARD),
    Card(22, "WSC-1S", Category.ROM_CARD),
    Card(23, "WSC-2S", Category.ROM_CARD),
    Card(24, "WSC-3S", Category.ROM_CARD),
    Card(25, "User 01", Category.USER),
    Card(26, "User 02", Category.USER),
    Card(27, "User 03", Category.USER),
    Card(28, "User 04", Category.USER),
    Card(29, "User 05", Category.USER),
    Card(30, "User 06", Category.USER),
    Card(31, "User 07", Category.USER),
    Card(32, "User 08", Category.USER),
    Card(33, "User 09", Category.USER),
    Card(34, "User 10", Category.USER),
    Card(35, "User 11", Category.USER),
    Card(36, "User 12", Category.USER),
    Card(37, "User 13", Category.USER),
    Card(38, "User 14", Category.USER),
    Card(39, "User 15", Category.USER),
)

_BY_NUMBER: Dict[int, Card] = {entry.number: entry for entry in CARDS}
_BY_NAME: Dict[str, Card] = {entry.name.casefold(): entry for entry in CARDS}


def cards() -> Tuple[Card, ...]:
    """All forty banks, in HCard order."""
    return CARDS


def card(number: int) -> Card:
    """One bank by its 00-39 number."""
    # ``isinstance(True, int)`` is True, and ``card(True)`` would return
    # bank 01 -- the same shape of mistake :func:`kws.banks.bank` guards.
    if isinstance(number, bool):
        raise LookupError(f"a card bank is not a boolean, got {number!r}")
    try:
        return _BY_NUMBER[int(number)]
    except (KeyError, TypeError, ValueError):
        raise LookupError(
            f"no card bank {number!r}; the HCard has banks 00-39"
        ) from None


def card_by_name(name: str) -> Optional[Card]:
    """A bank by its default name, or ``None``.

    Names are compared case-insensitively with surrounding space removed.
    The card's own name field is space-padded and may be upper- or
    lower-case, so a byte-for-byte comparison against the manual would miss
    the bank it is plainly naming.
    """
    return _BY_NAME.get(name.strip().casefold())


def is_user(number: int) -> bool:
    """True for the fifteen user banks, 25-39."""
    return USER_FIRST <= int(number) <= USER_LAST


def bank_numbers() -> List[int]:
    """00-39, in order."""
    return [entry.number for entry in CARDS]
