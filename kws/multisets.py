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

"""The live multi catalog: what each Multiset pointed at when it was written.

A Multiset part stores only a bank byte and a performance number. For the
Card that names the *slot*, not which card or HCard bank is in it, and a
RAM slot is only a position whose contents can be overwritten. The device
cannot remember either, so kwsed does: when a Multiset is written it records
a snapshot -- the card scope, the performance names and, for the card, the
archive digest -- and on the next load it compares the snapshot with what
is actually there.

The snapshots live in ``multisets.json`` (the multi catalog), separate from
the static name catalog: one is user state, the other is derived from files.
The same record travels in a Multi Mode Setup backup's ``annotations``
block, so a restore after a factory reset still says which card to insert.

The comparison is evidence-based and quiet about what it does not know: an
uncached RAM slot or an unknown card bank is not a mismatch.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Callable, Dict, List, Mapping, Optional

from kws import banks, favorites

__all__ = [
    "VERSION",
    "ChannelBinding",
    "MultiSnapshot",
    "Mismatch",
    "build_snapshot",
    "check",
    "default_path",
    "load",
    "record_digest",
    "save",
    "to_dict",
]

#: The archive format. Added fields are tolerated; an incompatible change
#: bumps this and the loader skips what it does not understand.
VERSION = 1

FILE_NAME = "multisets.json"


def default_path() -> str:
    """Where the multi catalog lives, beside the favourites database."""
    return os.path.join(favorites.data_dir(), FILE_NAME)


def record_digest(record: bytes) -> str:
    """The digest of one 86-byte Multiset record."""
    return sha256(bytes(record)).hexdigest()


@dataclass(frozen=True)
class ChannelBinding:
    """What one channel pointed at when the Multiset was recorded."""

    channel: int  #: 0-15
    bank: int  #: the part's bank byte (0-11)
    performance: int
    name: str = ""
    #: The card scope this channel meant, e.g. ``CARD/39`` or ``CARD/c1``.
    #: ``None`` when the bank was not known at record time.
    card_scope: Optional[str] = None
    #: The card archive's content digest at record time (strongest identity).
    card_digest: str = ""


@dataclass(frozen=True)
class MultiSnapshot:
    """One Multiset as it was when kwsed last wrote it."""

    number: int
    name: str
    recorded_at: str
    digest: str
    channels: Dict[int, ChannelBinding] = field(default_factory=dict)


@dataclass(frozen=True)
class Mismatch:
    """One channel whose recorded binding no longer matches the device."""

    multi: int
    channel: int  #: 0-15
    kind: str  #: "card" or "ram"
    text: str


def _bank_label(bank: int) -> str:
    try:
        return banks.by_sysex(bank).label
    except LookupError:
        return f"bank {bank}"


def build_snapshot(
    multiset: Any,
    record: bytes,
    *,
    card_scope_for: Callable[[int], Optional[str]],
    name_for: Callable[[int, int, Optional[str]], str],
    card_digest_for: Callable[[Optional[str]], str],
    recorded_at: Optional[str] = None,
) -> MultiSnapshot:
    """Record one Multiset's current bindings.

    ``record`` is the Multiset's 86-byte record (used for the digest).
    ``card_scope_for(channel)`` is the scope the writer meant for a card
    channel, ``name_for(bank, performance, scope)`` resolves a name, and
    ``card_digest_for(scope)`` the archive digest of that card.
    """
    card_byte = banks.bank("CARD").sysex
    channels: Dict[int, ChannelBinding] = {}
    for index, part in enumerate(multiset.parts):
        if not getattr(part, "used", False):
            continue
        scope = card_scope_for(index) if part.bank == card_byte else None
        channels[index] = ChannelBinding(
            channel=index,
            bank=part.bank,
            performance=part.performance,
            name=name_for(part.bank, part.performance, scope),
            card_scope=scope,
            card_digest=card_digest_for(scope) if scope else "",
        )
    return MultiSnapshot(
        number=multiset.number,
        name=multiset.name,
        recorded_at=recorded_at or datetime.now(UTC).isoformat(timespec="seconds"),
        digest=record_digest(record),
        channels=channels,
    )


def check(
    snapshot: MultiSnapshot,
    *,
    active_card_scope: Optional[str],
    active_card_digest: str,
    name_at: Callable[[int, int], Optional[str]],
) -> List[Mismatch]:
    """Compare a snapshot with the device as it is now.

    ``active_card_scope`` is the scope of the card currently in the slot
    (``CARD/39``, ``CARD/c1`` or ``None``), ``active_card_digest`` its
    archive digest, and ``name_at(bank, performance)`` the current name at
    a RAM slot -- ``None`` when that bank has not been read, which is not
    evidence of a change.
    """
    card_byte = banks.bank("CARD").sysex
    out: List[Mismatch] = []
    for channel, binding in snapshot.channels.items():
        if binding.bank == card_byte:
            if not binding.card_scope:
                out.append(
                    Mismatch(
                        multi=snapshot.number,
                        channel=channel,
                        kind="card",
                        text=(
                            "points to the Card slot but no bank was recorded; "
                            f"the active card is {active_card_scope or 'unknown'}"
                            " — switch to the right bank and record it with r"
                        ),
                    )
                )
                continue
            if active_card_scope == binding.card_scope:
                continue
            if binding.card_digest and binding.card_digest == active_card_digest:
                # The same card content under another bank: the dependency
                # data is there, so this is not a break.
                continue
            out.append(
                Mismatch(
                    multi=snapshot.number,
                    channel=channel,
                    kind="card",
                    text=(
                        f"expects {binding.card_scope} "
                        f"({binding.name or 'unnamed'}); the active card is "
                        f"{active_card_scope or 'unknown'}"
                    ),
                )
            )
            continue
        try:
            entry = banks.by_sysex(binding.bank)
        except LookupError:
            continue
        if not entry.writable or entry.id == "CARD":
            # ROM is factory state; the Card is handled above.
            continue
        current = name_at(binding.bank, binding.performance)
        if current is None:
            continue  # not cached: no evidence either way
        if current != binding.name:
            out.append(
                Mismatch(
                    multi=snapshot.number,
                    channel=channel,
                    kind="ram",
                    text=(
                        f"expects {_bank_label(binding.bank)} "
                        f"{binding.performance:02d} {binding.name!r}; it now "
                        f"holds {current!r}"
                    ),
                )
            )
    return out


def _binding_to_json(binding: ChannelBinding) -> Dict[str, Any]:
    return {
        "bank": binding.bank,
        "performance": binding.performance,
        "name": binding.name,
        "card_scope": binding.card_scope or "",
        "card_digest": binding.card_digest,
    }


def _binding_from_json(raw: Any) -> Optional[ChannelBinding]:
    if not isinstance(raw, dict):
        return None
    try:
        bank = int(raw["bank"])
        performance = int(raw["performance"])
    except (KeyError, TypeError, ValueError):
        return None
    scope = raw.get("card_scope")
    return ChannelBinding(
        channel=-1,  # filled by the caller
        bank=bank,
        performance=performance,
        name=str(raw.get("name") or ""),
        card_scope=str(scope) if scope else None,
        card_digest=str(raw.get("card_digest") or ""),
    )


def to_dict(snapshot: MultiSnapshot) -> Dict[str, Any]:
    """A snapshot as JSON-ready data, for a backup's annotations block."""
    return _snapshot_to_json(snapshot)


def _snapshot_to_json(snapshot: MultiSnapshot) -> Dict[str, Any]:
    return {
        "name": snapshot.name,
        "recorded_at": snapshot.recorded_at,
        "digest": snapshot.digest,
        "channels": {
            f"{channel:02d}": _binding_to_json(binding)
            for channel, binding in sorted(snapshot.channels.items())
        },
    }


def _snapshot_from_json(number: int, raw: Any) -> Optional[MultiSnapshot]:
    if not isinstance(raw, dict):
        return None
    channels: Dict[int, ChannelBinding] = {}
    raw_channels = raw.get("channels")
    if isinstance(raw_channels, dict):
        for key, value in raw_channels.items():
            try:
                channel = int(key)
            except (TypeError, ValueError):
                continue
            binding = _binding_from_json(value)
            if binding is None:
                continue
            channels[channel] = ChannelBinding(
                channel=channel,
                bank=binding.bank,
                performance=binding.performance,
                name=binding.name,
                card_scope=binding.card_scope,
                card_digest=binding.card_digest,
            )
    return MultiSnapshot(
        number=number,
        name=str(raw.get("name") or ""),
        recorded_at=str(raw.get("recorded_at") or ""),
        digest=str(raw.get("digest") or ""),
        channels=channels,
    )


def save(snapshots: Mapping[int, MultiSnapshot], path: Optional[str] = None) -> None:
    """Write the multi catalog atomically; a failure leaves the old one."""
    payload = {
        "version": VERSION,
        "multisets": {
            f"{number:02d}": _snapshot_to_json(snapshot)
            for number, snapshot in sorted(snapshots.items())
        },
    }
    where = path or default_path()
    try:
        os.makedirs(os.path.dirname(os.path.abspath(where)), exist_ok=True)
        temp = where + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
        os.replace(temp, where)
    except OSError:
        pass


def load(path: Optional[str] = None) -> Dict[int, MultiSnapshot]:
    """Read the multi catalog, leniently: a bad entry is skipped, not fatal."""
    where = path or default_path()
    try:
        with open(where, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    raw_sets = raw.get("multisets")
    if not isinstance(raw_sets, dict):
        return {}
    out: Dict[int, MultiSnapshot] = {}
    for key, value in raw_sets.items():
        try:
            number = int(key)
        except (TypeError, ValueError):
            continue
        snapshot = _snapshot_from_json(number, value)
        if snapshot is not None:
            out[number] = snapshot
    return out
