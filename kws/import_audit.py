# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# kwsed is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version.

"""Audit a ``deps.Source`` performance bank for missing local dependencies.

This is a warning layer: it reports which patch and wave-sequence slots a
bank's performances reference locally but do not carry, without refusing to
plan or build.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from kws import banks, deps, perfcopy
from kws import records as rec

__all__ = ["DependencyAudit", "source_bank_number", "audit_source"]


@dataclass(frozen=True)
class DependencyAudit:
    bank_number: Optional[int]
    performance_count: int
    missing_patches: Tuple[int, ...]
    missing_waveseqs: Tuple[int, ...]
    referenced_patches: Tuple[int, ...]
    referenced_waveseqs: Tuple[int, ...]

    @property
    def complete(self) -> bool:
        return not self.missing_patches and not self.missing_waveseqs

    def summary(self) -> str:
        parts: list[str] = []
        if self.missing_patches:
            slots = ", ".join(f"{s:02d}" for s in self.missing_patches)
            parts.append(f"missing {len(self.missing_patches)} patch slot(s): {slots}")
        if self.missing_waveseqs:
            slots = ", ".join(f"{s:02d}" for s in self.missing_waveseqs)
            parts.append(
                f"missing {len(self.missing_waveseqs)} wave sequence(s): {slots}"
            )
        if not parts:
            return "no missing local dependencies"
        return "; ".join(parts)


def source_bank_number(source: deps.Source) -> Optional[int]:
    """The real bank number for ``source``, or ``None`` when unknown."""
    if source.bank_number is not None:
        return int(source.bank_number)
    scope_id = source.scope_id
    if scope_id == "CARD" or (
        isinstance(scope_id, str) and scope_id.startswith("CARD/")
    ):
        try:
            return banks.bank("CARD").number
        except LookupError:
            return None
    try:
        return banks.bank(scope_id).number
    except LookupError:
        return None


def _patch_is_missing(patches: Sequence[bytes], slot: int) -> bool:
    if slot < 0 or slot >= len(patches):
        return True
    try:
        patch_record = patches[slot]
    except (IndexError, TypeError):
        return True
    try:
        return perfcopy.is_empty_patch(patch_record)
    except (ValueError, IndexError, TypeError):
        return True


def _seq_is_missing(waveseq: Optional[bytes], seq_slot: int) -> bool:
    if waveseq is None:
        return True
    return seq_slot < 0 or seq_slot >= rec.SEQUENCES_PER_BANK


def _part_contribution(
    part: rec.PartRef,
    bn: Optional[int],
    patches: Sequence[bytes],
    waveseq: Optional[bytes],
) -> Optional[Tuple[set[int], set[int], set[int], set[int]]]:
    if not part.used:
        return None
    if part.bank != bn:
        return None
    slot = int(part.patch)

    referenced_patches: set[int] = {slot}
    missing_patches: set[int] = set()
    referenced_waveseqs: set[int] = set()
    missing_waveseqs: set[int] = set()

    if _patch_is_missing(patches, slot):
        missing_patches.add(slot)
        return (
            referenced_patches,
            missing_patches,
            referenced_waveseqs,
            missing_waveseqs,
        )

    patch_record = patches[slot]
    try:
        waves = rec.patch_waves(patch_record)
    except (ValueError, IndexError, TypeError):
        return (
            referenced_patches,
            missing_patches,
            referenced_waveseqs,
            missing_waveseqs,
        )

    for wave in waves:
        if not wave.is_sequence:
            continue
        if wave.bank != bn:
            continue
        seq_slot = int(wave.wave)
        referenced_waveseqs.add(seq_slot)
        if _seq_is_missing(waveseq, seq_slot):
            missing_waveseqs.add(seq_slot)

    return (
        referenced_patches,
        missing_patches,
        referenced_waveseqs,
        missing_waveseqs,
    )


def _performance_contributions(
    record: bytes, bn: Optional[int], patches: Sequence[bytes], waveseq: Optional[bytes]
) -> Optional[Tuple[set[int], set[int], set[int], set[int]]]:
    try:
        parts = rec.performance_parts(record)
    except (ValueError, IndexError, TypeError):
        return None
    try:
        if perfcopy.is_empty_performance(record):
            return None
    except (ValueError, IndexError, TypeError):
        pass

    referenced_patches: set[int] = set()
    missing_patches: set[int] = set()
    referenced_waveseqs: set[int] = set()
    missing_waveseqs: set[int] = set()

    for part in parts:
        result = _part_contribution(part, bn, patches, waveseq)
        if result is None:
            continue
        rp, mp, rs, ms = result
        referenced_patches |= rp
        missing_patches |= mp
        referenced_waveseqs |= rs
        missing_waveseqs |= ms

    return (
        referenced_patches,
        missing_patches,
        referenced_waveseqs,
        missing_waveseqs,
    )


def audit_source(source: deps.Source) -> DependencyAudit:
    bn = source_bank_number(source)
    performance_count = len(source.performances)

    referenced_patches: set[int] = set()
    referenced_waveseqs: set[int] = set()
    missing_patches: set[int] = set()
    missing_waveseqs: set[int] = set()

    patches = source.patches
    waveseq = source.waveseq

    for record in source.performances:
        result = _performance_contributions(record, bn, patches, waveseq)
        if result is None:
            continue
        rp, mp, rs, ms = result
        referenced_patches |= rp
        missing_patches |= mp
        referenced_waveseqs |= rs
        missing_waveseqs |= ms

    return DependencyAudit(
        bank_number=bn,
        performance_count=performance_count,
        missing_patches=tuple(sorted(missing_patches)),
        missing_waveseqs=tuple(sorted(missing_waveseqs)),
        referenced_patches=tuple(sorted(referenced_patches)),
        referenced_waveseqs=tuple(sorted(referenced_waveseqs)),
    )
