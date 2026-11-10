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

"""Performance-first copying with atomic patch isolation.

Where :mod:`kws.bankbuild` is patch-first and lets performances share
patches, this module is performance-first: you choose performances, and each
one drags along its own private copies of the patches (and wave sequences)
it needs. Two performances that reference the same source patch never share
a target patch slot, so editing one can never alter the other. Patches are
still packed contiguously with no gaps, and wave sequences are still deduped
beneath the patches.

It writes bytes, never the instrument.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from kws import bankbuild, banks, deps, dumps, relations
from kws import records as rec

_DEFAULT_DESTINATION = banks.bank("CARD")
_DEFAULT_BUDGET = bankbuild.Budget()

__all__ = [
    "is_empty_performance",
    "AtomicPerfAllocation",
    "AtomicPlan",
    "AtomicBuilt",
    "plan_atomic_bank",
    "build_atomic_bank",
    "active_performances",
    "copy_into_bank",
]


def is_empty_performance(record: Sequence[int]) -> bool:
    """True when a performance slot is an unused/init placeholder.

    The factory ``Init`` performance is not all zeroes: it has a common
    block and one part switched on at bank 0 / patch 0.  Treat that shape
    as empty, so a bank of INIT slots is seen as available.  A non-init
    name, or an init-named slot that points anywhere else, is real work
    and is not overwritten.
    """
    parts = rec.performance_parts(record)
    if not any(p.used for p in parts):
        return True
    name = dumps.record_name(record).strip().lower()
    if name not in ("", "init", "init perf", "unnamed"):
        return False
    for part in parts:
        if part.used and (part.bank != 0 or part.patch != 0):
            return False
    return True


@dataclass
class AtomicPerfAllocation:
    request_index: int
    source_perf: relations.PerfId
    target_perf_slot: int
    name: str
    part_patch_slots: Dict[int, int] = field(default_factory=dict)
    allocated_patches: Tuple[Tuple[relations.PatchId, int], ...] = ()


@dataclass
class AtomicPlan:
    destination_bank: banks.Bank
    allocations: List[AtomicPerfAllocation] = field(default_factory=list)
    patch_count: int = 0
    sequence_map: Dict[deps.SequenceKey, int] = field(default_factory=dict)
    steps_used: int = 0
    rejected: List[str] = field(default_factory=list)

    @property
    def fits(self) -> bool:
        return not self.rejected


@dataclass
class AtomicBuilt:
    performances: bytes
    patches: bytes
    waveseq: Optional[bytes]
    plan: AtomicPlan


def plan_atomic_bank(  # noqa: C901
    sources: Mapping[str, deps.Source],
    chosen: Sequence[relations.PerfId],
    *,
    destination_bank: banks.Bank = _DEFAULT_DESTINATION,
    budget: bankbuild.Budget = _DEFAULT_BUDGET,
) -> AtomicPlan:

    plan = AtomicPlan(destination_bank=destination_bank)
    used: Set[Tuple[str, int, int]] = set()
    srcs = dict(sources)

    for request_index, perf_id in enumerate(chosen):
        scope = perf_id.scope_id
        num = perf_id.number

        if len(plan.allocations) >= budget.performances:
            plan.rejected.append(
                "no performance slot left "
                f"({budget.performances}/{budget.performances} used)"
            )
            continue

        source = srcs.get(scope)
        if source is None or num >= len(source.performances):
            plan.rejected.append(f"{scope}:{num:02d} - performance not loaded")
            continue

        record = source.performances[num]
        name = dumps.record_name(record)

        # Active parts in part order.
        parts = [p for p in rec.performance_parts(record) if p.used]

        # Resolve source patches, intra-performance dedup.
        local_map: Dict[relations.PatchId, int] = {}
        unique_patches: List[relations.PatchId] = []
        for part in parts:
            patch_scope = relations.resolve_part_scope(part.bank, scope)
            pid = relations.PatchId(patch_scope, part.patch)
            if pid not in local_map:
                local_map[pid] = len(unique_patches)
                unique_patches.append(pid)
        k = len(unique_patches)

        # Patch budget.
        if plan.patch_count + k > budget.patches:
            plan.rejected.append(
                f"{scope}:{num:02d} ({name}) needs {k} patch slot(s), "
                f"only {budget.patches - plan.patch_count} of "
                f"{budget.patches} left"
            )
            continue

        # Check every unique patch is loaded.
        missing_patch = False
        for pid in unique_patches:
            psrc = srcs.get(pid.scope_id)
            if psrc is None or pid.number >= len(psrc.patches):
                plan.rejected.append(
                    f"{scope}:{num:02d} ({name}) patch "
                    f"{pid.scope_id}:{pid.number:03d} is not loaded"
                )
                missing_patch = True
                break
        if missing_patch:
            continue

        # Collect seeds and run quoted closure.
        seeds: List[deps.SequenceKey] = []
        for pid in unique_patches:
            psrc = srcs[pid.scope_id]
            patch_record = psrc.patches[pid.number]
            seeds.extend(deps.patch_dependencies(patch_record, pid.scope_id))
        order, chains, unresolved = deps.quoted_closure(srcs, seeds)
        if unresolved:
            _src_id, wave_bank, seq = unresolved[0]
            plan.rejected.append(
                f"{scope}:{num:02d} ({name}) needs wave sequence {seq} "
                f"from bank {wave_bank}, whose wave sequence bank is not loaded"
            )
            continue

        new_seqs = [key for key in order if key not in plan.sequence_map]

        # fresh steps
        fresh = 0
        seen_fresh: Set[Tuple[str, int, int]] = set()
        for key in new_seqs:
            for step in chains[key].steps:
                tag = (key[0], key[1], step)
                if tag not in used and tag not in seen_fresh:
                    seen_fresh.add(tag)
                    fresh += 1

        if len(plan.sequence_map) + len(new_seqs) > budget.sequences:
            plan.rejected.append(
                f"{scope}:{num:02d} ({name}) needs {len(new_seqs)} more "
                f"wave sequence(s); only {budget.sequences} fit in a bank"
            )
            continue
        if len(used) + fresh > budget.steps:
            plan.rejected.append(
                f"{scope}:{num:02d} ({name}) needs {fresh} more step(s); "
                f"the bank's pool of {budget.steps} has "
                f"{budget.steps - len(used)} left"
            )
            continue

        # Commit.
        target_perf_slot = len(plan.allocations)
        start_patch = plan.patch_count
        pid_to_slot: Dict[relations.PatchId, int] = {}
        allocated: List[Tuple[relations.PatchId, int]] = []
        for i, pid in enumerate(unique_patches):
            slot = start_patch + i
            pid_to_slot[pid] = slot
            allocated.append((pid, slot))

        part_patch_slots: Dict[int, int] = {}
        for part in parts:
            patch_scope = relations.resolve_part_scope(part.bank, scope)
            pid = relations.PatchId(patch_scope, part.patch)
            part_patch_slots[part.index] = pid_to_slot[pid]

        for key in new_seqs:
            plan.sequence_map[key] = len(plan.sequence_map)

        for key in new_seqs:
            for step in chains[key].steps:
                used.add((key[0], key[1], step))

        alloc = AtomicPerfAllocation(
            request_index=request_index,
            source_perf=perf_id,
            target_perf_slot=target_perf_slot,
            name=name,
            part_patch_slots=part_patch_slots,
            allocated_patches=tuple(allocated),
        )
        plan.allocations.append(alloc)
        plan.patch_count += k

    plan.steps_used = len(used)
    return plan


def _clean_performance() -> bytes:
    """A performance record whose eight parts are all switched off."""
    one = bytearray(rec.PERFORMANCE_SIZE)
    for index in range(rec.PARTS_PER_PERFORMANCE):
        rec.set_performance_part(one, index, bank=0, patch=rec.UNUSED_PATCH)
    return bytes(one)


def _fit_template(blob: Optional[bytes], size: int, fill: bytes) -> bytes:
    """Exactly ``size`` bytes: pad a short template, truncate a long one.

    A template comes from a real dump, but a caller may hand over a partial
    bank (a cache written before every kind arrived). Slicing a record bank
    short would leave the output the wrong length, and every later slot
    offset would be wrong with it.
    """
    if blob is None:
        return fill
    fitted = bytes(blob[:size])
    return fitted + fill[len(fitted) :] if len(fitted) < size else fitted


def build_atomic_bank(  # noqa: C901
    sources: Mapping[str, deps.Source],
    made: AtomicPlan,
    *,
    template_performances: Optional[bytes] = None,
    template_patches: Optional[bytes] = None,
    template_waveseq: Optional[bytes] = None,
) -> AtomicBuilt:

    srcs = dict(sources)
    clean = _clean_performance()
    performance_size = banks.PERFORMANCES_PER_BANK * rec.PERFORMANCE_SIZE
    patch_size = banks.PATCHES_PER_BANK * rec.PATCH_SIZE

    template_patches = _fit_template(template_patches, patch_size, bytes(patch_size))
    template_performances = _fit_template(
        template_performances,
        performance_size,
        clean * banks.PERFORMANCES_PER_BANK,
    )

    if made.sequence_map:
        if template_waveseq is None:
            template_waveseq = bytes(rec.WAVESEQ_BANK_SIZE)
    else:
        # leave as supplied (possibly None)
        pass

    patches = bytearray(template_patches)
    performances = bytearray(template_performances)
    waveseq: Optional[bytearray]
    if made.sequence_map:
        waveseq = (
            bytearray(template_waveseq)
            if template_waveseq is not None
            else bytearray(rec.WAVESEQ_BANK_SIZE)
        )
    elif template_waveseq is not None:
        waveseq = bytearray(template_waveseq)
    else:
        waveseq = None

    # Wave sequences.
    if waveseq is not None and made.sequence_map:
        next_step = 1
        moved: Dict[Tuple[str, int, int], int] = {}
        for (src_id, wave_bank, seq), slot in sorted(
            made.sequence_map.items(), key=lambda kv: kv[1]
        ):
            holder = srcs[src_id]
            if holder.waveseq is None:
                raise ValueError(
                    f"{src_id} has no wave sequence bank, but the plan carries "
                    f"sequence {seq} from it. This should have been caught "
                    f"while planning."
                )
            chain = deps.walk_sequence(holder.waveseq, seq, bank_id=src_id)
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
        taken = set(made.sequence_map.values())
        for slot in range(rec.SEQUENCES_PER_BANK):
            if slot in taken:
                continue
            head = rec.SEQ_BASE + slot * rec.SEQ_SIZE
            waveseq[head : head + rec.SEQ_SIZE] = bytes(rec.SEQ_SIZE)
            name_at = rec.SEQNAME_BASE + slot * rec.SEQNAME_SIZE
            waveseq[name_at : name_at + rec.SEQNAME_SIZE] = bytes(rec.SEQNAME_SIZE)

    # Patches.
    for alloc in made.allocations:
        for patch_id, target_slot in alloc.allocated_patches:
            holder = srcs[patch_id.scope_id]
            record = bytearray(holder.patches[patch_id.number])
            for wave in rec.patch_waves(record):
                if not wave.is_sequence:
                    continue
                key = (patch_id.scope_id, wave.bank, wave.wave)
                if key not in made.sequence_map:
                    raise ValueError(
                        f"{patch_id.scope_id} patch {patch_id.number} uses "
                        f"wave sequence {wave.wave} from bank {wave.bank}, "
                        f"which the plan does not carry."
                    )
                rec.set_patch_wave(
                    record,
                    wave.index,
                    bank=made.destination_bank.number,
                    wave=made.sequence_map[key],
                )
            at = target_slot * rec.PATCH_SIZE
            patches[at : at + rec.PATCH_SIZE] = record

    # Performances.
    for alloc in made.allocations:
        holder = srcs[alloc.source_perf.scope_id]
        record = bytearray(holder.performances[alloc.source_perf.number])
        parts = rec.performance_parts(record)
        for part in parts:
            if not part.used:
                continue
            if part.index in alloc.part_patch_slots:
                rec.set_performance_part(
                    record,
                    part.index,
                    bank=made.destination_bank.number,
                    patch=alloc.part_patch_slots[part.index],
                )
        at = alloc.target_perf_slot * rec.PERFORMANCE_SIZE
        performances[at : at + rec.PERFORMANCE_SIZE] = record

    # Slots past the planned performances must not keep a template's active
    # performance. A target bank used as a template may hold records that the
    # plan deliberately dropped (clean, or a replaced slot); leaving them
    # behind would put a performance in the output that the caller never
    # asked for. Empty template records are kept, so an instrument's own
    # initialised slot survives byte for byte.
    for slot in range(len(made.allocations), banks.PERFORMANCES_PER_BANK):
        at = slot * rec.PERFORMANCE_SIZE
        existing = bytes(performances[at : at + rec.PERFORMANCE_SIZE])
        if len(existing) == rec.PERFORMANCE_SIZE and not is_empty_performance(existing):
            performances[at : at + rec.PERFORMANCE_SIZE] = clean

    return AtomicBuilt(
        performances=bytes(performances),
        patches=bytes(patches),
        waveseq=bytes(waveseq) if waveseq is not None else None,
        plan=made,
    )


def active_performances(source: deps.Source) -> List[int]:
    return [i for i, r in enumerate(source.performances) if not is_empty_performance(r)]


def copy_into_bank(
    sources: Mapping[str, deps.Source],
    target_scope: str,
    chosen: Sequence[relations.PerfId],
    *,
    mode: str = "append",
    replace_slot: Optional[int] = None,
    destination_bank: Optional[banks.Bank] = None,
    budget: bankbuild.Budget = _DEFAULT_BUDGET,
) -> AtomicBuilt:

    if mode not in ("clean", "append", "replace"):
        raise ValueError(f"unknown mode {mode!r}")
    if mode == "replace" and replace_slot is None:
        raise ValueError("replace mode requires replace_slot")

    if destination_bank is not None:
        dest = destination_bank
    elif target_scope.startswith("CARD/") or target_scope == "CARD":
        dest = banks.bank("CARD")
    else:
        dest = banks.bank(target_scope)

    srcs = dict(sources)
    target_src = srcs.get(target_scope)

    if mode == "clean":
        planned: List[relations.PerfId] = list(chosen)
    elif mode == "append":
        active = active_performances(target_src) if target_src is not None else []
        existing = [relations.PerfId(target_scope, s) for s in active]
        planned = existing + list(chosen)
    else:  # replace
        active = active_performances(target_src) if target_src is not None else []
        existing = [relations.PerfId(target_scope, s) for s in active]
        if replace_slot in active:
            idx = active.index(replace_slot)
            if not chosen:
                planned = existing[:idx] + existing[idx + 1 :]
            else:
                planned = existing[:idx] + list(chosen) + existing[idx + 1 :]
        else:
            planned = existing + list(chosen)

    plan = plan_atomic_bank(srcs, planned, destination_bank=dest, budget=budget)

    # Templates from target source if loaded with full record lists.
    template_performances = None
    template_patches = None
    template_waveseq = None
    if target_src is not None and target_src.performances and target_src.patches:
        template_performances = b"".join(target_src.performances)
        template_patches = b"".join(target_src.patches)
        template_waveseq = target_src.waveseq

    return build_atomic_bank(
        srcs,
        plan,
        template_performances=template_performances,
        template_patches=template_patches,
        template_waveseq=template_waveseq,
    )
