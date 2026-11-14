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

"""Whether a bank's performances are self-contained (plan D1).

A whole-bank write moves one bank and nothing else, so a performance whose
parts point at another writable bank arrives pointing there still -- usually
at a slot that holds something else, which is a silent slot, not an error.
Before the write, the person deserves to know which performances will sound
as authored and which will not.

The classification is the plan's D1: a performance is **contained** when
every used part points at its own bank or at ROM. ROM banks are always
present on the instrument, so self-plus-ROM sounds; the ROM references are
still counted separately so the number is never hidden. References to other
writable banks (RAM 1-3 and the Card) are what breaks containment, and the
audit reports which banks they name and how many parts name each.

Everything here reads records already in memory. Nothing writes, nothing
talks to the instrument.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Mapping, Optional, Set, Tuple

from kws import banks, deps
from kws import records as rec

__all__ = [
    "PartTarget",
    "PerformanceAudit",
    "ContainmentAudit",
    "audit_containment",
]


@dataclass(frozen=True)
class PartTarget:
    """Where one used part points: a bank and a patch slot in it.

    ``bank_number`` is the display number (0-11) :class:`records.PartRef`
    reports -- the same numbering :func:`records.set_performance_part` and
    :func:`kws.banks.bank` take, not the System Exclusive wire byte.
    ``is_rom`` says the bank is one of the eight ROM banks, using the same
    ``writable`` flag the write fence and the pre-audit staging checks use.
    """

    bank_number: int
    is_rom: bool
    patch: int


@dataclass
class PerformanceAudit:
    """One non-empty performance's part targets, classified."""

    slot: int
    #: The used parts, in part order.
    parts: Tuple[PartTarget, ...] = ()
    #: Other *writable* banks (never ROM, never the performance's own bank)
    #: that used parts point at.
    external_banks: FrozenSet[int] = frozenset()
    #: How many used parts point at ROM.
    rom_parts: int = 0

    @property
    def self_only(self) -> bool:
        """Every used part points at the performance's own bank."""
        return not self.external_banks and not self.rom_parts

    @property
    def contained(self) -> bool:
        """D1 containment: all used parts are self or ROM."""
        return not self.external_banks


@dataclass
class ContainmentAudit:
    """The whole source bank, performance by performance."""

    source_scope: str
    #: The source's real bank number, or ``None`` when the scope cannot be
    #: resolved to one. Parts are still classified through the scope.
    source_bank_number: Optional[int]
    #: One entry per non-empty performance, in slot order. Empty (init)
    #: slots are ignored: they are not performances and not failures.
    performances: List[PerformanceAudit] = field(default_factory=list)
    #: Non-empty performances with no reference to another writable bank.
    #: Includes the ROM-assisted ones -- D1 counts self-or-ROM as contained.
    contained: int = 0
    #: Non-empty performances that reference at least one other writable bank.
    other_bank: int = 0
    #: The subset of ``contained`` whose parts also point at ROM. Reported
    #: separately so the ROM references are never hidden behind the count.
    rom_assisted: int = 0
    #: Used parts per other writable bank, by bank number.
    parts_per_bank: Dict[int, int] = field(default_factory=dict)
    #: Distinct patch slots referenced per bank, by bank number. Keys cover
    #: the source's own bank (its parts' group) and every external bank;
    #: ROM parts are not grouped here, they are counted in ``rom_parts``.
    #: Two parts on the same patch slot count once; dedup by patch *content*
    #: is the planner's job, not the audit's.
    patch_records: Dict[int, Tuple[int, ...]] = field(default_factory=dict)


def _audit_record(
    slot: int,
    record: bytes,
    source_scope: str,
    sources: Mapping[str, deps.Source],
    source_bank_number: Optional[int],
    parts_per_bank: Dict[int, int],
    patch_slots: Dict[int, Set[int]],
) -> Optional[PerformanceAudit]:
    """Classify one performance record, tallying into the shared counters.

    Returns ``None`` for short or empty (init) records: those are not
    performances and not failures.
    """
    from kws import perfcopy

    if len(record) < rec.PERFORMANCE_SIZE:
        return None
    if perfcopy.is_empty_performance(record):
        return None
    targets: List[PartTarget] = []
    external: Set[int] = set()
    rom_parts = 0
    for part in rec.performance_parts(record):
        if not part.used:
            continue
        is_rom = not banks.bank(part.bank).writable
        if is_rom:
            rom_parts += 1
        elif perfcopy._part_is_local(part, source_scope, sources):
            if source_bank_number is not None:
                patch_slots.setdefault(source_bank_number, set()).add(part.patch)
        else:
            external.add(part.bank)
            parts_per_bank[part.bank] = parts_per_bank.get(part.bank, 0) + 1
            patch_slots.setdefault(part.bank, set()).add(part.patch)
        targets.append(
            PartTarget(bank_number=part.bank, is_rom=is_rom, patch=part.patch)
        )
    return PerformanceAudit(
        slot=slot,
        parts=tuple(targets),
        external_banks=frozenset(external),
        rom_parts=rom_parts,
    )


def audit_containment(
    sources: Mapping[str, deps.Source], source_scope: str
) -> ContainmentAudit:
    """Classify every used part of every performance in one source bank.

    ``sources`` is the loaded set the performance's parts are judged
    against -- a part is "self" when it points at the source's own bank,
    resolved either through the bank table or through a session alias's
    ``bank_number``. The result is pure data: no files, no MIDI, no
    instrument.
    """
    # Deferred imports: kws.import_audit pulls in kws.perfcopy, and the
    # whole-bank planner (plan §4/T2) gives kws.perfcopy an import of this
    # module, so a top-level chain would close a three-module cycle.
    from kws import import_audit

    source = sources.get(source_scope)
    if source is None:
        raise LookupError(f"source {source_scope!r} is not loaded")

    source_bank_number = import_audit.source_bank_number(source)

    audit = ContainmentAudit(
        source_scope=source_scope, source_bank_number=source_bank_number
    )
    parts_per_bank: Dict[int, int] = {}
    patch_slots: Dict[int, Set[int]] = {}

    for slot, record in enumerate(source.performances):
        pa = _audit_record(
            slot,
            record,
            source_scope,
            sources,
            source_bank_number,
            parts_per_bank,
            patch_slots,
        )
        if pa is None:
            continue
        audit.performances.append(pa)
        if pa.external_banks:
            audit.other_bank += 1
        else:
            audit.contained += 1
            if pa.rom_parts:
                audit.rom_assisted += 1

    audit.parts_per_bank = parts_per_bank
    audit.patch_records = {
        bank: tuple(sorted(slots)) for bank, slots in patch_slots.items()
    }
    return audit
