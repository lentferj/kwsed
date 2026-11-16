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

"""Find performances whose patches a whole-bank rewrite will replace.

A whole-bank import replaces every patch slot in each bank it writes. A
performance that survives the write and points into one of those banks still
names the same slot numbers, but those slots now hold different sounds. This
module reads the surviving banks and reports which performances that affects.

The question is deliberately narrow: a used part is affected when its bank
number is one of the written banks. That includes a part naming its own bank
-- rewriting the bank's patches changes it too. ROM banks are normally absent
from ``written_banks``, so ROM parts usually drop out; this module does not
test the writable flag, it trusts the caller's set. Everything here is pure
data: no files, no MIDI, no instrument.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Set, Tuple

from kws import banks, deps
from kws import records as rec

__all__ = [
    "AffectedPerformance",
    "AffectedBank",
    "affected_performances",
    "affected_summary",
]


@dataclass(frozen=True)
class AffectedPerformance:
    """One surviving performance slot with affected parts.

    ``parts`` counts the used parts that name a written bank; ``banks`` holds
    the distinct bank numbers those parts name, sorted.
    """

    slot: int
    parts: int
    banks: Tuple[int, ...]


@dataclass(frozen=True)
class AffectedBank:
    """The affected performances in one surviving bank, in slot order."""

    bank_id: str
    performances: Tuple[AffectedPerformance, ...]
    part_total: int
    banks: Tuple[int, ...]

    @property
    def count(self) -> int:
        return len(self.performances)


def _bank_id(scope_id: str, sources: Mapping[str, deps.Source]) -> str:
    """The canonical bank id for a source scope.

    Reuse the import's own encoding rules (including a session alias's
    ``bank_number``) rather than repeating scope parsing here. A scope the
    import cannot resolve keeps its own id, so the analysis still reports it.
    """
    from kws import perfcopy

    number = perfcopy._source_bank_number(scope_id, sources)
    if number is None:
        return scope_id
    try:
        return banks.bank(number).id
    except LookupError:
        return scope_id


def affected_performances(
    sources: Mapping[str, deps.Source], written_banks: Iterable[int]
) -> Dict[str, AffectedBank]:
    """Report surviving performances that name a rewritten bank.

    Only sources present in ``sources`` are examined, and empty (init) slots
    are ignored. A bank appears in the result only when at least one of its
    performances has an affected part. The result is keyed and ordered by
    bank id; within each bank slots, and the named bank numbers, are sorted.
    """
    from kws import perfcopy

    written = set(written_banks)
    grouped: Dict[str, AffectedBank] = {}

    for scope_id, source in sources.items():
        found: list[AffectedPerformance] = []
        for slot, record in enumerate(source.performances):
            if len(record) < rec.PERFORMANCE_SIZE:
                continue
            if perfcopy.is_empty_performance(record):
                continue
            named: Set[int] = set()
            parts = 0
            for part in rec.performance_parts(record):
                if part.used and part.bank in written:
                    parts += 1
                    named.add(part.bank)
            if parts:
                found.append(AffectedPerformance(slot, parts, tuple(sorted(named))))
        if not found:
            continue

        identifier = _bank_id(scope_id, sources)
        part_total = sum(entry.parts for entry in found)
        all_banks = tuple(sorted({number for entry in found for number in entry.banks}))
        existing = grouped.get(identifier)
        if existing is None:
            grouped[identifier] = AffectedBank(
                bank_id=identifier,
                performances=tuple(found),
                part_total=part_total,
                banks=all_banks,
            )
        else:
            performances = tuple(
                sorted(existing.performances + tuple(found), key=lambda p: p.slot)
            )
            grouped[identifier] = AffectedBank(
                bank_id=identifier,
                performances=performances,
                part_total=existing.part_total + part_total,
                banks=tuple(sorted(set(existing.banks) | set(all_banks))),
            )

    return dict(sorted(grouped.items()))


def _label(identifier: str | int) -> str:
    try:
        return banks.bank(identifier).label
    except LookupError:
        return str(identifier)


def affected_summary(banks: Mapping[str, AffectedBank]) -> str:
    """One line describing the affected banks, or "" when there are none."""
    if not banks:
        return ""
    lines: list[str] = []
    for entry in banks.values():
        named = ", ".join(_label(number) for number in entry.banks)
        lines.append(
            f"{_label(entry.bank_id)}: {entry.count} performances, "
            f"{entry.part_total} parts, naming {named}"
        )
    return "; ".join(lines)
