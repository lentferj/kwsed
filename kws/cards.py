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

import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Dict, List, Optional, Tuple

from kws import favorites
from kws.banks import PATCHES_PER_BANK, PERFORMANCES_PER_BANK, Kind, count_for

__all__ = [
    "Category",
    "Card",
    "CapturedBank",
    "USER_FIRST",
    "USER_LAST",
    "CARDS",
    "FILE_NAME",
    "PATCHES_PER_BANK",
    "PERFORMANCES_PER_BANK",
    "cards",
    "card",
    "is_user",
    "bank_numbers",
    "count_for",
    "capture",
    "default_path",
    "load",
    "resolve",
    "save",
]

#: The archive's filename, beside the favourites database and the live-name
#: cache.
FILE_NAME = "cards.json"

#: The schema version this module writes and the highest it reads.
VERSION = 1

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


# --- the per-bank archive ----------------------------------------------------


@dataclass(frozen=True)
class CapturedBank:
    """What one HCard bank held when it was read off the unit.

    The card-name field is separate from the slot names on purpose: the
    name identifies *which* bank is physically in front of the synth, and
    the slot names are what that bank contains. Keeping them in one record
    means a capture is one atomic statement rather than two files that can
    disagree.
    """

    number: int  #: 00-39.
    name: str  #: The name the card reported, or the table's if it said none.
    captured_at: str  #: ISO-8601, UTC.
    performances: Dict[int, str]  #: {slot: name}
    patches: Dict[int, str]  #: {slot: name}

    def names(self, kind: str) -> Dict[int, str]:
        """The captured names of one kind."""
        if kind == Kind.PERFORMANCE:
            return dict(self.performances)
        if kind == Kind.PATCH:
            return dict(self.patches)
        raise LookupError(
            f"no bank kind {kind!r}; have {Kind.PERFORMANCE}, {Kind.PATCH}"
        )


def capture(
    number: int,
    name: str,
    performances: Dict[int, str],
    patches: Dict[int, str],
    *,
    captured_at: Optional[str] = None,
) -> CapturedBank:
    """Bundle one bank's read into a record. Does no MIDI and no I/O."""
    return CapturedBank(
        number=card(number).number,
        name=name.strip(),
        captured_at=captured_at or datetime.now(UTC).isoformat(timespec="seconds"),
        performances={int(slot): str(value) for slot, value in performances.items()},
        patches={int(slot): str(value) for slot, value in patches.items()},
    )


def default_path() -> str:
    """Where the archive lives, beside the favourites database."""
    return os.path.join(favorites.data_dir(), FILE_NAME)


def _as_slot_names(raw: object) -> Dict[int, str]:
    """A JSON object of slot -> name, keeping only usable entries."""
    if not isinstance(raw, dict):
        return {}
    out: Dict[int, str] = {}
    for slot, name in raw.items():
        try:
            number = int(slot)
        except (TypeError, ValueError):
            continue
        if 0 <= number < max(PERFORMANCES_PER_BANK, PATCHES_PER_BANK) and isinstance(
            name, str
        ):
            out[number] = name
    return out


def _bank_from_json(key: str, raw: object) -> Optional[CapturedBank]:
    if not isinstance(raw, dict):
        return None
    try:
        number = int(key)
    except (TypeError, ValueError):
        return None
    if not 0 <= number < len(CARDS):
        return None
    name = raw.get("name")
    captured_at = raw.get("captured_at")
    if not isinstance(name, str) or not isinstance(captured_at, str):
        return None
    return CapturedBank(
        number=number,
        name=name,
        captured_at=captured_at,
        performances=_as_slot_names(raw.get("performances")),
        patches=_as_slot_names(raw.get("patches")),
    )


def load(path: Optional[str] = None) -> Dict[int, CapturedBank]:
    """Every captured bank, keyed by number. Empty if there are none.

    A missing file is normal: nothing has been captured yet. A corrupt one
    is worth a warning rather than an exception -- the archive is a
    convenience, and names can be read off the unit again.
    """
    where = path or default_path()
    try:
        with open(where, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print(
            f"kwsed: ignoring {where}: {exc}. It is a cache of card banks "
            "read from the unit; delete it and capture them again.",
            file=sys.stderr,
        )
        return {}
    if not isinstance(raw, dict) or not isinstance(raw.get("banks"), dict):
        return {}
    version = raw.get("version", VERSION)
    if not isinstance(version, int) or version > VERSION:
        return {}
    out: Dict[int, CapturedBank] = {}
    for key, value in raw["banks"].items():
        bank = _bank_from_json(str(key), value)
        if bank is not None:
            out[bank.number] = bank
    return out


def _to_json(bank: CapturedBank) -> Dict[str, object]:
    return {
        "name": bank.name,
        "captured_at": bank.captured_at,
        "performances": {str(slot): name for slot, name in bank.performances.items()},
        "patches": {str(slot): name for slot, name in bank.patches.items()},
    }


def save(bank: CapturedBank, path: Optional[str] = None) -> None:
    """Write one bank into the archive, merging rather than replacing.

    Read-modify-write, exactly as :mod:`vinsynlib.livenames` does: losing
    every other bank because one was captured would be a poor trade.
    Best effort -- a capture is still on screen if the disk is full.
    """
    where = path or default_path()
    banks = load(where)
    banks[bank.number] = bank
    _write(banks, where)


def _name_key(name: Optional[str]) -> str:
    return (name or "").strip().casefold()


def _name_matches(query: str, candidate: str) -> bool:
    """Whether a card's name field identifies this bank.

    Exact after case and padding, or a prefix long enough to be a real
    name: the name field is sixteen bytes, and several of the table's
    names (``WPC-13 "Performance 3"``) are longer than that. A two-letter
    prefix is not evidence, so shorter queries do not count.
    """
    query = _name_key(query)
    candidate = _name_key(candidate)
    if not query or not candidate:
        return False
    if query == candidate:
        return True
    return len(query) >= 8 and candidate.startswith(query)


def _fingerprint_matches(
    bank: CapturedBank, perf0: Optional[str], patch0: Optional[str]
) -> bool:
    """Whether a captured bank's first slot names match what was read.

    Only the checks the caller can supply are made. No names at all is no
    evidence rather than a match, or every unknown bank would fingerprint
    to every captured one.
    """
    checks: List[bool] = []
    if perf0:
        checks.append(_name_key(bank.performances.get(0)) == _name_key(perf0))
    if patch0:
        checks.append(_name_key(bank.patches.get(0)) == _name_key(patch0))
    return bool(checks) and all(checks)


def resolve(
    card_name: Optional[str],
    perf0: Optional[str],
    patch0: Optional[str],
    *,
    captured: Optional[Dict[int, CapturedBank]] = None,
) -> Optional[int]:
    """Which HCard bank is active, or ``None`` when that cannot be said.

    The order is docs/HCARD.md section 6:

    1. **Exact card-name match** against the table or a captured bank's
       stored name.
    2. **Fingerprint fallback**: the first performance and patch names
       against previously captured banks.
    3. Ambiguous or unknown -> ``None``. The caller offers "new / user
       bank" filing rather than guessing.

    ``captured`` defaults to the archive on disk. Passing it in is how the
    browser resolves against the archive it already holds, and how a test
    says what was captured without writing a file.
    """
    archive = load() if captured is None else captured

    named: set[int] = set()
    query = _name_key(card_name)
    if query:
        for entry in CARDS:
            if _name_matches(query, entry.name):
                named.add(entry.number)
        for number, bank in archive.items():
            if _name_matches(query, bank.name):
                named.add(number)

    fingerprinted = {
        number
        for number, bank in archive.items()
        if _fingerprint_matches(bank, perf0, patch0)
    }

    if len(named) == 1:
        return next(iter(named))
    if len(named) > 1:
        # The name alone is ambiguous; the contents may still separate two
        # banks that report the same name.
        narrowed = named & fingerprinted
        return next(iter(narrowed)) if len(narrowed) == 1 else None
    # No usable name: the fingerprint is the only evidence left.
    return next(iter(fingerprinted)) if len(fingerprinted) == 1 else None


def _write(banks: Dict[int, CapturedBank], where: str) -> None:
    payload = {
        "version": VERSION,
        "banks": {
            f"{number:02d}": _to_json(bank) for number, bank in sorted(banks.items())
        },
    }
    try:
        os.makedirs(os.path.dirname(os.path.abspath(where)), exist_ok=True)
        # Write-then-rename: a crash or a full disk mid-write leaves the
        # previous archive intact rather than a half-written one.
        temp = where + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
        os.replace(temp, where)
    except OSError:
        pass
