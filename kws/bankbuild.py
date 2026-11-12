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


def plan(
    sources: Dict[str, deps.Source],
    chosen: Sequence[deps.PatchId],
    *,
    budget: Budget = Budget(),
    include_performances: bool = True,
) -> Plan:
    """Work out what fits, in the order you asked for.

    Greedy on purpose. A solver could fit more patches into 35 slots, but it
    would choose *which* of your favourites to drop, and that is your
    decision rather than an optimisation. Taking them in your order and
    saying plainly where the budget ran out leaves it with you.
    """
    out = Plan()
    #: Every step already spoken for, as (source scope, wave bank, step):
    #: pools are per wave bank, and chains within one can share steps, so
    #: summing chain lengths double-counts the overlap and rejects plans
    #: that fit. It has to match the key the builder relocates by, or the
    #: budget counts steps the build does not actually share.
    used: Set[Tuple[str, int, int]] = set()

    for scope_id, number in chosen:
        source = sources.get(scope_id)
        if source is None or number >= len(source.patches):
            out.rejected.append(
                f"{scope_id}:{number:03d} - patch not loaded from any source"
            )
            continue
        if (scope_id, number) in out.patch_map:
            continue
        if len(out.patch_map) >= budget.patches:
            out.rejected.append(
                f"{scope_id}:{number:03d} - no patch slot left ({budget.patches} used)"
            )
            continue

        # What this patch drags along -- including the sequences that its
        # sequences quote, which cost slots and steps of their own and are
        # counted here for the same reason they are copied later: a quoted
        # sequence left behind does not degrade the sound, it removes it.
        order, chains, unresolved = deps.quoted_closure(
            sources, deps.patch_dependencies(source.patch(number), scope_id)
        )
        if unresolved:
            src_id, _, seq = unresolved[0]
            out.rejected.append(
                f"{scope_id}:{number:03d} - needs wave sequence "
                f"{seq}, and {src_id}'s wave sequence bank is not loaded"
            )
            continue
        need: List[Tuple[str, int, int]] = [
            key for key in order if key not in out.sequence_map
        ]
        chains = {key: chains[key] for key in need}
        fresh = 0
        seen_fresh = set()
        for key in need:
            for step in chains[key].steps:
                tag = (key[0], key[1], step)
                if tag not in used and tag not in seen_fresh:
                    seen_fresh.add(tag)
                    fresh += 1
        if len(out.sequence_map) + len(need) > budget.sequences:
            out.rejected.append(
                f"{scope_id}:{number:03d} - needs {len(need)} more wave "
                f"sequence(s); only {budget.sequences} fit in a bank"
            )
            continue
        if len(used) + fresh > budget.steps:
            out.rejected.append(
                f"{scope_id}:{number:03d} - needs {fresh} more step(s); the "
                f"bank's pool of {budget.steps} has "
                f"{budget.steps - len(used)} left"
            )
            continue

        out.patch_map[(scope_id, number)] = len(out.patch_map)
        for key in need:
            out.sequence_map[key] = len(out.sequence_map)
        for key, chain in chains.items():
            for step in chain.steps:
                used.add((key[0], key[1], step))

    out.steps_used = len(used)

    if include_performances:
        for scope_id, source in sources.items():
            for index, record in enumerate(source.performances):
                parts = deps.performance_dependencies(record)
                if not parts:
                    continue
                if all(
                    (_bank_key(sources, b, scope_id), p) in out.patch_map
                    for b, p in parts
                ):
                    if len(out.performances) >= budget.performances:
                        out.notes.append(
                            f"{scope_id}:{index:03d} is complete but the bank "
                            f"already holds {budget.performances} "
                            f"performances"
                        )
                        continue
                    out.performances.append((scope_id, index))
    return out


def _bank_key(sources: Dict[str, deps.Source], bank_number: int, same_bank: str) -> str:
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


def _prepare_build_templates(
    template_patches: bytes,
    template_performances: bytes,
    template_waveseq: Optional[bytes],
) -> Tuple[bytearray, bytearray, Optional[bytearray]]:
    """Prepare mutable bytearray templates from the immutable input bytes."""
    patches = bytearray(template_patches)
    performances = bytearray(template_performances)
    waveseq = bytearray(template_waveseq) if template_waveseq else None
    return patches, performances, waveseq


def _build_bank_wave_sequences(
    waveseq: Optional[bytearray],
    sources: Dict[str, deps.Source],
    made: Plan,
) -> None:
    """Build wave sequences into the template, relocating steps and remapping links."""
    if waveseq is None or not made.sequence_map:
        return

    next_step = 1  # step 0 is the pool's null link
    # One map for the whole build, keyed by the *pool* a step came from
    # and its number in it, so a step two sequences share is copied once
    # and both chains point at the same slot. Copying it twice is what
    # plan() stopped counting twice: with a per-sequence map the plan
    # accepted a set the build then overflowed, and past step 500 the
    # writes land on the sequence-name table.
    #
    # The pool is (source scope, wave bank), not the source scope alone.
    # Two banks number their own pools independently -- step 7 of one is
    # not step 7 of the other -- and so do two wave banks reached
    # through the same source. Sharing across those was wrong in a way
    # that only shows once steps quote: one physical step cannot hold
    # two different quote targets, and the second sequence to be written
    # silently took the first one's.
    moved: Dict[Tuple[str, int, int], int] = {}
    for (src_id, wave_bank, seq), slot in sorted(
        made.sequence_map.items(), key=lambda kv: kv[1]
    ):
        holder = sources[src_id]
        # plan() rejects any patch whose wave sequence bank is missing, so
        # a Plan reaching build() cannot name a sequence it has no bytes
        # for. Raising rather than asserting: an assert disappears under
        # -O, and this is the same "should have been caught while
        # planning" condition the pool-full check below already raises.
        if holder.waveseq is None:
            raise ValueError(
                f"{src_id} has no wave sequence bank, but the plan carries "
                f"sequence {seq} from it. This should have been caught "
                f"while planning."
            )
        chain = deps.walk_sequence(holder.waveseq, seq, scope_id=src_id)
        pool = (src_id, wave_bank)
        for old in chain.steps:
            if pool + (old,) in moved:
                continue
            if next_step >= rec.STEPS_PER_BANK:
                raise ValueError(
                    f"the destination's pool of {rec.STEPS_PER_BANK} "
                    f"steps is full; {src_id} sequence {seq} does not "
                    f"fit. This should have been caught while planning."
                )
            moved[pool + (old,)] = next_step
            at_new = rec.STEP_BASE + next_step * rec.STEP_SIZE
            at_old = rec.STEP_BASE + old * rec.STEP_SIZE
            waveseq[at_new : at_new + rec.STEP_SIZE] = holder.waveseq[
                at_old : at_old + rec.STEP_SIZE
            ]
            next_step += 1
        for old in chain.steps:
            info = rec.step(holder.waveseq, old)
            rec.set_step_links(
                waveseq,
                moved[pool + (old,)],
                forward=moved.get(pool + (info["forward"],), 0),
                back=moved.get(pool + (info["back"],), 0),
                loop=moved.get(pool + (info["loop"],), 0),
            )
            # A step's wave number below ROM_WAVE_BASE is not a wave at
            # all: it plays another wave sequence of this same bank, and
            # so has to be renumbered exactly as a patch's oscillator
            # reference is. Copying it unchanged is what made a built
            # bank point at whatever the destination happens to hold at
            # that number -- usually a slot the build zeroed, so the
            # step played nothing and the file looked perfect.
            if info["wave"] < rec.ROM_WAVE_BASE:
                quoted = (src_id, wave_bank, info["wave"])
                if quoted not in made.sequence_map:
                    raise ValueError(
                        f"{src_id} sequence {seq} quotes sequence "
                        f"{info['wave']}, which the plan does not carry. "
                        f"This should have been caught while planning."
                    )
                rec.set_step_wave(
                    waveseq, moved[pool + (old,)], made.sequence_map[quoted]
                )
        head = rec.SEQ_BASE + slot * rec.SEQ_SIZE
        old_head = rec.SEQ_BASE + seq * rec.SEQ_SIZE
        waveseq[head : head + rec.SEQ_SIZE] = holder.waveseq[
            old_head : old_head + rec.SEQ_SIZE
        ]
        first = rec.sequence_header(holder.waveseq, seq)["link"]
        waveseq[head] = (moved.get(pool + (first,), 0) >> 8) & 0xFF
        waveseq[head + 1] = moved.get(pool + (first,), 0) & 0xFF
        name_at = rec.SEQNAME_BASE + slot * rec.SEQNAME_SIZE
        old_name = rec.SEQNAME_BASE + seq * rec.SEQNAME_SIZE
        waveseq[name_at : name_at + rec.SEQNAME_SIZE] = holder.waveseq[
            old_name : old_name + rec.SEQNAME_SIZE
        ]
    # Slots the plan did not fill would otherwise keep the template's
    # headers pointing at step indices this relocation overwrote: real
    # headers, real names, wrong steps. Park them on the null link.
    taken = set(made.sequence_map.values())
    for slot in range(rec.SEQUENCES_PER_BANK):
        if slot in taken:
            continue
        head = rec.SEQ_BASE + slot * rec.SEQ_SIZE
        waveseq[head : head + rec.SEQ_SIZE] = bytes(rec.SEQ_SIZE)
        name_at = rec.SEQNAME_BASE + slot * rec.SEQNAME_SIZE
        waveseq[name_at : name_at + rec.SEQNAME_SIZE] = bytes(rec.SEQNAME_SIZE)


def _build_bank_patches(
    patches: bytearray,
    sources: Dict[str, deps.Source],
    made: Plan,
    destination_bank: int,
) -> None:
    """Build patches into the template, remapping wave sequence references."""
    for (scope_id, number), slot in sorted(
        made.patch_map.items(), key=lambda kv: kv[1]
    ):
        record = bytearray(sources[scope_id].patch(number))
        for wave in rec.patch_waves(record):
            if not wave.is_sequence:
                continue
            key = (scope_id, wave.bank, wave.wave)
            if key in made.sequence_map:
                rec.set_patch_wave(
                    record,
                    wave.index,
                    bank=destination_bank,
                    wave=made.sequence_map[key],
                )
        at = slot * rec.PATCH_SIZE
        patches[at : at + rec.PATCH_SIZE] = record


def _build_bank_performances(
    performances: bytearray,
    sources: Dict[str, deps.Source],
    made: Plan,
    destination_bank: int,
) -> None:
    """Build performances into the template, remapping patch references."""
    for slot, (scope_id, index) in enumerate(made.performances):
        record = bytearray(sources[scope_id].performances[index])
        for part in rec.performance_parts(record):
            if not part.used:
                continue
            patch_key = (_bank_key(sources, part.bank, scope_id), part.patch)
            if patch_key in made.patch_map:
                rec.set_performance_part(
                    record,
                    part.index,
                    bank=destination_bank,
                    patch=made.patch_map[patch_key],
                )
        at = slot * rec.PERFORMANCE_SIZE
        performances[at : at + rec.PERFORMANCE_SIZE] = record


def build(
    sources: Dict[str, deps.Source],
    made: Plan,
    *,
    template_patches: bytes,
    template_performances: bytes,
    template_waveseq: Optional[bytes] = None,
    destination_bank: int = 11,
) -> Built:
    """Assemble the banks, rewriting every reference as things move."""
    patches, performances, waveseq = _prepare_build_templates(
        template_patches, template_performances, template_waveseq
    )

    _build_bank_wave_sequences(waveseq, sources, made)
    _build_bank_patches(patches, sources, made, destination_bank)
    _build_bank_performances(performances, sources, made, destination_bank)

    return Built(
        patches=bytes(patches),
        performances=bytes(performances),
        waveseq=bytes(waveseq) if waveseq is not None else None,
        plan=made,
    )
