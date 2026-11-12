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

"""Unified source entries for file and unit banks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Mapping, Optional

from kws import banks, deps


@dataclass
class SourceEntry:
    """One source bank, file or unit, held under a session scope."""

    label: str
    scope: str
    source: deps.Source
    origin: str = ""
    read_only: bool = False

    @property
    def source_path(self) -> str:
        return self.origin

    @property
    def source_origin(self) -> str:
        return self.origin


def source_bank_number(scope: str) -> Optional[int]:
    """The real bank number for ``scope``.

    Card scopes (including ``CARD/07``) resolve to the Card bank (11);
    other scopes resolve through the bank table; unknown scopes give None.
    """
    if scope == "CARD" or scope.startswith("CARD/"):
        return banks.bank("CARD").number
    try:
        return banks.bank(scope).number
    except LookupError:
        return None


def unique_source_scope(base: str, existing: Mapping[str, Any]) -> str:
    """Return ``base`` if free, otherwise ``base#2``, ``base#3``, ..."""
    if base not in existing:
        return base
    index = 2
    while f"{base}#{index}" in existing:
        index += 1
    return f"{base}#{index}"


def make_source_entry(
    *,
    label: str,
    scope: str,
    source: deps.Source,
    origin: str = "",
    existing: Optional[Mapping[str, Any]] = None,
    read_only: bool = False,
    bank_number: Optional[int] = None,
) -> SourceEntry:
    """Build a :class:`SourceEntry` with a unique session scope.

    The input ``source`` is never mutated. The returned entry's source is a
    copy carrying ``bank_id == scope (or alias)`` and the real
    ``bank_number``.
    """
    alias = unique_source_scope(scope, existing if existing is not None else {})
    if bank_number is not None:
        number: Optional[int] = bank_number
    elif source.bank_number is not None:
        number = source.bank_number
    else:
        number = source_bank_number(scope)
    aliased = deps.Source(
        scope_id=alias,
        performances=list(source.performances),
        patches=list(source.patches),
        waveseq=source.waveseq,
        bank_number=number,
    )
    entry_label = label
    if alias != scope:
        entry_label = f"{label} (alias)"
    return SourceEntry(
        label=entry_label,
        scope=alias,
        source=aliased,
        origin=origin,
        read_only=read_only,
    )


@dataclass(frozen=True)
class UnitBankChoice:
    label: str
    scope: str
    read_only: bool
    origin: str


def unit_bank_choices() -> List[UnitBankChoice]:
    """The twelve unit banks, in bank-table order."""
    out: List[UnitBankChoice] = []
    for bank in banks.BANKS:
        out.append(
            UnitBankChoice(
                label=bank.label,
                scope=bank.id,
                read_only=not bank.writable,
                origin=bank.label,
            )
        )
    return out


def source_bank_number_for(source: deps.Source) -> Optional[int]:
    """The real bank number for a Source, falling back to its scope."""
    if source.bank_number is not None:
        return int(source.bank_number)
    return source_bank_number(source.scope_id)


def same_source_bank(left: deps.Source, right: deps.Source) -> bool:
    left_number = source_bank_number_for(left)
    right_number = source_bank_number_for(right)
    return left_number is not None and left_number == right_number


def merge_source_components(
    existing: deps.Source, incoming: deps.Source
) -> Optional[deps.Source]:
    """Combine complementary performance/patch/waveseq files.

    A SoundArtMedia-style package often has ``*_Performances.syx``,
    ``*_Patches.syx`` and ``*_Waveseq.syx`` instead of one complete bank.
    This merges them when each side supplies a non-overlapping component.
    Two performance banks, two patch banks or two waveseq banks are *not*
    merged; those are real alternatives and get session aliases.
    """
    if existing.performances and incoming.performances:
        return None
    if existing.patches and incoming.patches:
        return None
    if existing.waveseq and incoming.waveseq:
        return None
    if not (incoming.performances or incoming.patches or incoming.waveseq):
        return None
    perfs = list(existing.performances or incoming.performances)
    patches = list(existing.patches or incoming.patches)
    waveseq = existing.waveseq or incoming.waveseq
    if (
        perfs == existing.performances
        and patches == existing.patches
        and waveseq == existing.waveseq
    ):
        return None
    return deps.Source(
        scope_id=existing.scope_id,
        performances=perfs,
        patches=patches,
        waveseq=waveseq,
        bank_number=existing.bank_number or incoming.bank_number,
    )


def origin_for_unit(scope: str) -> str:
    """Canonical bank label for a unit scope."""
    if scope == "CARD":
        return "Card"
    if scope.startswith("CARD/"):
        suffix = scope.split("/", 1)[1]
        return f"Card {suffix}"
    try:
        return banks.bank(scope).label
    except LookupError:
        return scope


def compact_origin(origin: str) -> str:
    """Compact a file path, leaving short unit labels untouched."""
    if not origin:
        return origin
    normalized = origin.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    if len(parts) < 3:
        return origin
    # Guard against treating a spaced unit label as a path: a genuine unit
    # label contains no path separator, so it never reaches here.
    return ".../" + "/".join(parts[-3:])
