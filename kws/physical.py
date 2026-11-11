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

"""The archive of ordinary Program Cards, identified by content.

An ordinary card carries no readable name: an inserted card is recognised by
the SHA-256 of its decoded performance and patch dumps. That digest is the
same identity signal the HCard banks use, but the archive is separate:
``physical-cards.json`` holds the cards a person has actually inserted,
while ``cards.json`` (see :mod:`kws.cards`) holds the forty HCard banks.

A card's ``id`` (``c1``, ``c2``, ...) is stable for the life of the archive
and is what the rest of the application uses to talk about it. The label is
only a display name: re-inserting a card with new content updates the digest
but keeps the id and the label, so a collection does not silently grow a new
row every time a card is edited.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, Optional

from kws import favorites
from kws.banks import PATCHES_PER_BANK, PERFORMANCES_PER_BANK

__all__ = [
    "FILE_NAME",
    "VERSION",
    "PhysicalCard",
    "active",
    "default_path",
    "find_by_digest",
    "load",
    "next_id",
    "save",
    "scope_id",
    "set_active",
    "set_label",
]

#: The archive's filename, beside the favourites database.
FILE_NAME = "physical-cards.json"

#: The schema version this module writes and the highest it reads.
VERSION = 1

#: One JSON slot map is a performance or patch bank like any other, so the
#: same upper bound applies to both kinds.
_SLOT_LIMIT = max(PERFORMANCES_PER_BANK, PATCHES_PER_BANK)


@dataclass(frozen=True)
class PhysicalCard:
    """One ordinary Program Card as it was last read off the unit."""

    id: str  #: Stable archive id, ``c1``, ``c2``, ...
    label: str  #: The display name, chosen locally.
    digest: str  #: Hex SHA-256 of the decoded performance and patch records.
    captured_at: str  #: ISO-8601, UTC.
    performances: Dict[int, str]  #: {slot: name}
    patches: Dict[int, str]  #: {slot: name}
    verified: bool = False  #: Confirmed against the stored digest, not adopted.


def default_path() -> str:
    """Where the archive lives, beside the favourites database."""
    return os.path.join(favorites.data_dir(), FILE_NAME)


def scope_id(card_id: str) -> str:
    """The per-card key space for favourites and live names.

    A physical card and an HCard bank both live in the synth's one ``CARD``
    slot, so the favourites and the live-name cache are filed under a
    per-card bank id rather than the shared ``CARD`` layer. Card ``c1``
    owns ``CARD/c1``; an HCard bank owns ``CARD/07`` (see
    :func:`kws.cards.scope_id`). The two spaces do not collide because a
    card id is never two decimal digits.
    """
    return f"CARD/{card_id}"


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
        if 0 <= number < _SLOT_LIMIT and isinstance(name, str):
            out[number] = name
    return out


def _card_from_json(key: str, raw: object) -> Optional[PhysicalCard]:
    """One archive entry, or ``None`` when it is not usable as written."""
    if not isinstance(raw, dict):
        return None
    card_id = str(key).strip()
    if not card_id:
        return None
    label = raw.get("label")
    digest = raw.get("digest")
    captured_at = raw.get("captured_at")
    if (
        not isinstance(label, str)
        or not isinstance(digest, str)
        or not isinstance(captured_at, str)
    ):
        return None
    verified = raw.get("verified", False)
    if not isinstance(verified, bool):
        verified = False
    return PhysicalCard(
        id=card_id,
        label=label,
        digest=digest,
        captured_at=captured_at,
        performances=_as_slot_names(raw.get("performances")),
        patches=_as_slot_names(raw.get("patches")),
        verified=verified,
    )


def load(path: Optional[str] = None) -> Dict[str, PhysicalCard]:
    """Every archived card, keyed by id. Empty if there are none.

    A missing file is normal: no card has been captured yet. A corrupt one
    is worth a warning rather than an exception -- the archive is a
    convenience, and a card's content can be read off the unit again.
    """
    where = path or default_path()
    try:
        with open(where, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print(
            f"kwsed: ignoring {where}: {exc}. It is a cache of cards read "
            "from the unit; delete it and capture them again.",
            file=sys.stderr,
        )
        return {}
    if not isinstance(raw, dict) or not isinstance(raw.get("cards"), dict):
        return {}
    version = raw.get("version", VERSION)
    if not isinstance(version, int) or version > VERSION:
        return {}
    out: Dict[str, PhysicalCard] = {}
    for key, value in raw["cards"].items():
        card = _card_from_json(str(key), value)
        if card is not None:
            out[card.id] = card
    return out


def _read_payload(where: str) -> Dict[str, Any]:
    """The archive file as a dict, or {} when it is absent or unusable."""
    try:
        with open(where, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def active(path: Optional[str] = None) -> Optional[str]:
    """The card last captured or identified as active, or ``None``.

    The active card is the one physically in the slot: the synth can only
    play what is inserted, so auditioning and the ``•`` marker follow this
    hint. An id that no longer names an archived card is not an answer.
    """
    where = path or default_path()
    value = _read_payload(where).get("active")
    if not isinstance(value, str) or not value:
        return None
    return value if value in load(where) else None


def set_active(card_id: str, path: Optional[str] = None) -> None:
    """Record which card is believed active, preserving the archived cards."""
    where = path or default_path()
    archive = load(where)
    if card_id not in archive:
        raise LookupError(f"Card {card_id!r} is not captured; capture it first with C")
    _write(archive, where, active=card_id)


def _to_json(card: PhysicalCard) -> Dict[str, object]:
    return {
        "label": card.label,
        "digest": card.digest,
        "captured_at": card.captured_at,
        "verified": card.verified,
        "performances": {str(slot): name for slot, name in card.performances.items()},
        "patches": {str(slot): name for slot, name in card.patches.items()},
    }


def save(card: PhysicalCard, path: Optional[str] = None) -> None:
    """Write one card into the archive, merging rather than replacing.

    Read-modify-write, exactly as :mod:`kws.cards` does: losing every other
    archived card because one was captured would be a poor trade. Best
    effort -- a capture is still on screen if the disk is full.
    """
    where = path or default_path()
    archive = load(where)
    archive[card.id] = card
    # Preserve the active-card hint; capturing a card must not clear it.
    _write(archive, where, active=active(where))


def find_by_digest(digest: str, cards: Dict[str, PhysicalCard]) -> Optional[str]:
    """The single card whose stored digest equals ``digest``.

    ``None`` for an empty digest, no match, or more than one match
    (ambiguous -- never guess). Two cards with identical content are
    indistinguishable from the wire, so the picker has to ask.
    """
    if not digest:
        return None
    matches = [card.id for card in cards.values() if card.digest == digest]
    if len(matches) != 1:
        return None
    return matches[0]


def next_id(cards: Dict[str, PhysicalCard]) -> str:
    """The first free ``c1``, ``c2``, ... id."""
    number = 1
    while f"c{number}" in cards:
        number += 1
    return f"c{number}"


def set_label(card_id: str, label: str, path: Optional[str] = None) -> PhysicalCard:
    """Set (or clear) the display name of one archived card.

    Read-modify-write on the archive. A card that has not been captured
    cannot be labelled: there is no record to attach the label to.
    """
    where = path or default_path()
    archive = load(where)
    if card_id not in archive:
        raise LookupError(f"Card {card_id!r} is not captured; capture it first with C")
    card = archive[card_id]
    updated = PhysicalCard(
        id=card.id,
        label=(label or "").strip(),
        digest=card.digest,
        captured_at=card.captured_at,
        performances=dict(card.performances),
        patches=dict(card.patches),
        verified=card.verified,
    )
    archive[card_id] = updated
    _write(archive, where, active=active(where))
    return updated


def _write(
    cards: Dict[str, PhysicalCard], where: str, *, active: Optional[str] = None
) -> None:
    payload: Dict[str, object] = {
        "version": VERSION,
        "cards": {key: _to_json(card) for key, card in sorted(cards.items())},
    }
    if active is not None:
        payload["active"] = active
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
