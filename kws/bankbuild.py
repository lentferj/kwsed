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

"""Build a favourites bank out of pieces of other banks.

**Patch first.** You choose patches; the builder works out what they drag
along, packs them into a bank, and then tells you which performances have
become complete -- every part of them pointing at a patch that made it in.
That order is the honest one, because the budgets bind on patches and not on
performances: a bank holds 50 performances but only 35 patches, so choosing
performances first means discovering late that they cannot all be satisfied.

**Four budgets, not one.** 50 performances, 35 patches, 32 wave sequences,
and 501 wave-sequence steps shared across the bank. The step pool is the one
that actually bites: banks in the wild run to 495 of 501.

**It builds on a template.** Every output starts as a real dump of a real
bank -- an empty one from the instrument is ideal -- and slots are replaced
in it. Nothing here invents the contents of a record. An initialised patch
is the instrument's idea of one, not this program's, and the difference is
the kind that loads cleanly and sounds wrong.

**It writes files, never the instrument.** The result is bytes you can save
as ``.syx``; getting them into the machine is the machine's own business,
through its front panel. That is not a limitation of this module, it is the
rule the whole project keeps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from kws import banks
from kws import deps
from kws import records as rec

__all__ = ["Budget", "Plan", "Built", "plan", "build"]


@dataclass(frozen=True)
class Budget:
    """What one bank holds."""

    performances: int = banks.PERFORMANCES_PER_BANK
    patches: int = banks.PATCHES_PER_BANK
    sequences: int = rec.SEQUENCES_PER_BANK
    steps: int = rec.STEPS_PER_BANK


@dataclass
class Plan:
    """What would be built, and what would not fit."""

    #: chosen patch -> its number in the new bank
    patch_map: Dict[deps.PatchId, int] = field(default_factory=dict)
    #: (bank id, wave bank, number) -> its number in the new bank
    sequence_map: Dict[Tuple[str, int, int], int] = field(default_factory=dict)
    #: (bank id, number) of performances every part of which is satisfied
    performances: List[Tuple[str, int]] = field(default_factory=list)
    steps_used: int = 0
    rejected: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def fits(self) -> bool:
        return not self.rejected


@dataclass
class Built:
    """The three banks, as decoded bytes ready to be framed."""

    patches: bytes
    performances: bytes
    waveseq: Optional[bytes]
    plan: Plan


def plan(sources: Dict[str, deps.Source],
         chosen: Sequence[deps.PatchId],
         *, budget: Budget = Budget(),
         include_performances: bool = True) -> Plan:
    """Work out what fits, in the order you asked for.

    Greedy on purpose. A solver could fit more patches into 35 slots, but it
    would choose *which* of your favourites to drop, and that is your
    decision rather than an optimisation. Taking them in your order and
    saying plainly where the budget ran out leaves it with you.
    """
    out = Plan()
    steps = 0

    for bank_id, number in chosen:
        source = sources.get(bank_id)
        if source is None or number >= len(source.patches):
            out.rejected.append(
                f"{bank_id}:{number:03d} - patch not loaded from any source")
            continue
        if (bank_id, number) in out.patch_map:
            continue
        if len(out.patch_map) >= budget.patches:
            out.rejected.append(
                f"{bank_id}:{number:03d} - no patch slot left "
                f"({budget.patches} used)")
            continue

        # what this patch drags along
        need: List[Tuple[str, int, int]] = []
        cost = 0
        blocked = None
        for src_id, wave_bank, seq in deps.patch_dependencies(
                source.patch(number), bank_id):
            key = (src_id, wave_bank, seq)
            if key in out.sequence_map:
                continue
            holder = sources.get(src_id)
            if holder is None or holder.waveseq is None:
                blocked = (f"{bank_id}:{number:03d} - needs wave sequence "
                           f"{seq}, and {src_id}'s wave sequence bank is "
                           f"not loaded")
                break
            chain = deps.walk_sequence(holder.waveseq, seq)
            need.append(key)
            cost += chain.cost
        if blocked:
            out.rejected.append(blocked)
            continue
        if len(out.sequence_map) + len(need) > budget.sequences:
            out.rejected.append(
                f"{bank_id}:{number:03d} - needs {len(need)} more wave "
                f"sequence(s); only {budget.sequences} fit in a bank")
            continue
        if steps + cost > budget.steps:
            out.rejected.append(
                f"{bank_id}:{number:03d} - needs {cost} more step(s); the "
                f"bank's pool of {budget.steps} has {budget.steps - steps} "
                f"left")
            continue

        out.patch_map[(bank_id, number)] = len(out.patch_map)
        for key in need:
            out.sequence_map[key] = len(out.sequence_map)
        steps += cost

    out.steps_used = steps

    if include_performances:
        for bank_id, source in sources.items():
            for index, record in enumerate(source.performances):
                parts = deps.performance_dependencies(record)
                if not parts:
                    continue
                if all((_bank_key(sources, b, bank_id), p) in out.patch_map
                       for b, p in parts):
                    if len(out.performances) >= budget.performances:
                        out.notes.append(
                            f"{bank_id}:{index:03d} is complete but the bank "
                            f"already holds {budget.performances} "
                            f"performances")
                        continue
                    out.performances.append((bank_id, index))
    return out


def _bank_key(sources: Dict[str, deps.Source], bank_number: int,
              same_bank: str) -> str:
    """Which source a part's bank number refers to.

    A part stores a bank *number*, and a library keyed by name has to turn
    that back into a source. When nothing matches, the part's own bank is
    assumed, which is the common case and is why a bank copied whole works
    while a patch copied alone does not.
    """
    for key, source in sources.items():
        try:
            if banks.bank(key).number == bank_number:
                return key
        except LookupError:
            continue
    return same_bank


def build(sources: Dict[str, deps.Source], made: Plan, *,
          template_patches: bytes,
          template_performances: bytes,
          template_waveseq: Optional[bytes] = None,
          destination_bank: int = 11) -> Built:
    """Assemble the banks, rewriting every reference as things move."""
    patches = bytearray(template_patches)
    performances = bytearray(template_performances)
    waveseq = bytearray(template_waveseq) if template_waveseq else None

    # --- wave sequences, and the steps they occupy --------------------------
    if waveseq is not None and made.sequence_map:
        next_step = 1          # step 0 is the pool's null link
        for (src_id, wave_bank, seq), slot in sorted(
                made.sequence_map.items(), key=lambda kv: kv[1]):
            holder = sources[src_id]
            chain = deps.walk_sequence(holder.waveseq, seq)
            moved = {}
            for old in chain.steps:
                moved[old] = next_step
                at_new = rec.STEP_BASE + next_step * rec.STEP_SIZE
                at_old = rec.STEP_BASE + old * rec.STEP_SIZE
                waveseq[at_new:at_new + rec.STEP_SIZE] = \
                    holder.waveseq[at_old:at_old + rec.STEP_SIZE]
                next_step += 1
            for old in chain.steps:
                info = rec.step(holder.waveseq, old)
                rec.set_step_links(
                    waveseq, moved[old],
                    forward=moved.get(info["forward"], 0),
                    back=moved.get(info["back"], 0),
                    loop=moved.get(info["loop"], 0))
            head = rec.SEQ_BASE + slot * rec.SEQ_SIZE
            old_head = rec.SEQ_BASE + seq * rec.SEQ_SIZE
            waveseq[head:head + rec.SEQ_SIZE] = \
                holder.waveseq[old_head:old_head + rec.SEQ_SIZE]
            first = rec.sequence_header(holder.waveseq, seq)["link"]
            waveseq[head] = (moved.get(first, 0) >> 8) & 0xFF
            waveseq[head + 1] = moved.get(first, 0) & 0xFF
            name_at = rec.SEQNAME_BASE + slot * rec.SEQNAME_SIZE
            old_name = rec.SEQNAME_BASE + seq * rec.SEQNAME_SIZE
            waveseq[name_at:name_at + rec.SEQNAME_SIZE] = \
                holder.waveseq[old_name:old_name + rec.SEQNAME_SIZE]

    # --- patches -------------------------------------------------------------
    for (bank_id, number), slot in sorted(made.patch_map.items(),
                                          key=lambda kv: kv[1]):
        record = bytearray(sources[bank_id].patch(number))
        for wave in rec.patch_waves(record):
            if not wave.is_sequence:
                continue
            key = (bank_id, wave.bank, wave.wave)
            if key in made.sequence_map:
                rec.set_patch_wave(record, wave.index,
                                   bank=destination_bank,
                                   wave=made.sequence_map[key])
        at = slot * rec.PATCH_SIZE
        patches[at:at + rec.PATCH_SIZE] = record

    # --- performances --------------------------------------------------------
    for slot, (bank_id, index) in enumerate(made.performances):
        record = bytearray(sources[bank_id].performances[index])
        for part in rec.performance_parts(record):
            if not part.used:
                continue
            key = (_bank_key(sources, part.bank, bank_id), part.patch)
            if key in made.patch_map:
                rec.set_performance_part(record, part.index,
                                         bank=destination_bank,
                                         patch=made.patch_map[key])
        at = slot * rec.PERFORMANCE_SIZE
        performances[at:at + rec.PERFORMANCE_SIZE] = record

    return Built(patches=bytes(patches), performances=bytes(performances),
                 waveseq=bytes(waveseq) if waveseq is not None else None,
                 plan=made)
