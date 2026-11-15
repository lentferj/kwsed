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
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from kws import bankbuild, banks, containment, deps, dumps, relations
from kws import records as rec

_DEFAULT_DESTINATION = banks.bank("CARD")
_DEFAULT_BUDGET = bankbuild.Budget()

__all__ = [
    "is_empty_performance",
    "is_empty_patch",
    "AtomicPerfAllocation",
    "AtomicPlan",
    "AtomicBuilt",
    "plan_atomic_bank",
    "build_atomic_bank",
    "ImportMode",
    "contained_slots",
    "Pass",
    "remaining_performances",
    "partition_self_contained",
    "plan_self_contained",
    "OverflowPlan",
    "OverflowBuilt",
    "plan_overflow_import",
    "build_overflow_import",
    "SplitPlan",
    "SplitBuilt",
    "SplitGroup",
    "SplitPartition",
    "partition_self_contained_split",
    "pack_self_contained_into",
    "build_split_partition",
    "plan_self_contained_split",
    "build_split_import",
    "build_self_contained",
    "active_performances",
    "copy_into_bank",
    "SlotImportPlan",
    "RelocatedBank",
    "SlotImportBuilt",
    "plan_slot_import",
    "build_slot_import",
    "planned_performance_record",
    "copy_slots_into_bank",
    "relocate_bank",
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
    if name not in (
        "",
        "init",
        "init perf",
        "init perf.",
        "init performance",
        "unnamed",
    ):
        return False
    for part in parts:
        if part.used and (part.bank != 0 or part.patch != 0):
            return False
    return True


def is_empty_patch(record: Sequence[int]) -> bool:
    """True when a patch slot is unused or a factory Init patch.

    A patch slot that is all zero bytes is unused. A patch whose trimmed
    lower-case name is one of the init placeholders (``""``, ``"init"``,
    ``"init patch"``, ``"init patch."``, ``"unnamed"``) is also treated as
    free, even if its common/oscillator bytes are nonzero: the factory
    ``Init`` patch carries a nonzero default and must still be seen as
    available by the import planner. A user patch named ``Init`` is
    therefore treated as free by design.
    """
    if not any(record):
        return True
    name = dumps.record_name(record).strip().lower()
    return name in ("", "init", "init patch", "init patch.", "unnamed")


@dataclass
class AtomicPerfAllocation:
    request_index: int
    source_perf: relations.PerfId
    target_perf_slot: int
    name: str
    part_patch_slots: Dict[int, int] = field(default_factory=dict)
    allocated_patches: Tuple[Tuple[relations.PatchId, int], ...] = ()
    # Slots from ``allocated_patches`` whose planned content is already in
    # the target bank (byte-identical reuse). The rest need a real write,
    # which is what audition staging works out from this split.
    reused_patch_slots: Tuple[int, ...] = ()


@dataclass
class AtomicPlan:
    destination_bank: banks.Bank
    allocations: List[AtomicPerfAllocation] = field(default_factory=list)
    patch_count: int = 0
    sequence_map: Dict[deps.SequenceKey, int] = field(default_factory=dict)
    #: Patches that were left in another bank on purpose (``carry_external``
    #: off), in first-use order, so the caller can plan their overflow.
    external_patches: List[relations.PatchId] = field(default_factory=list)
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


def _check_step_budget_atomic(
    plan: AtomicPlan,
    budget: bankbuild.Budget,
    used: Set[Tuple[str, int, int]],
    fresh: int,
    scope: str,
    num: int,
    name: str,
) -> bool:
    """Check step budget for atomic bank planning. Return True if exceeded."""
    if len(used) + fresh > budget.steps:
        plan.rejected.append(
            f"{scope}:{num:02d} ({name}) needs {fresh} more step(s); "
            f"the bank's pool of {budget.steps} has "
            f"{budget.steps - len(used)} left"
        )
        return True
    return False


def _check_performance_budget(
    plan: AtomicPlan, budget: bankbuild.Budget, scope: str, num: int, name: str
) -> bool:
    """Check if there's a free performance slot. Return True if budget exceeded."""
    if len(plan.allocations) >= budget.performances:
        plan.rejected.append(
            "no performance slot left "
            f"({budget.performances}/{budget.performances} used)"
        )
        return True
    return False


def _load_and_validate_source(
    srcs: Mapping[str, deps.Source], scope: str, num: int
) -> Optional[Sequence[int]]:
    """Load source performance record. Return None if not loaded."""
    source = srcs.get(scope)
    if source is None or num >= len(source.performances):
        return None
    return source.performances[num]


def _collect_unique_patches(
    record: Sequence[int],
    scope: str,
    *,
    local_bank_number: Optional[int] = None,
) -> Tuple[List[Any], List[relations.PatchId], List[relations.PatchId]]:
    """Active parts plus the patches worth carrying, deduped in order.

    Returns ``(parts, carry, external)``. With ``local_bank_number`` set,
    ``carry`` holds only the patches that live in the performance's own
    bank; ``external`` holds the rest, in first-use order, so a caller can
    heap them into another bank and repoint them afterwards. Without it,
    every patch is carried and ``external`` is empty.

    A part whose bank is not writable is a ROM reference and is neither
    carried nor external: the ROM banks are always on the instrument, so
    the patch stays where it is and costs no destination slot. Treating
    one as external would copy a ROM patch record into the overflow bank
    when a ROM source happens to be loaded, and reject with "not loaded"
    when it is not -- though nothing needs copying either way.
    """
    parts = [p for p in rec.performance_parts(record) if p.used]
    local_map: Dict[relations.PatchId, int] = {}
    carry: List[relations.PatchId] = []
    external: List[relations.PatchId] = []
    seen_external: Set[relations.PatchId] = set()
    for part in parts:
        if not banks.bank(part.bank).writable:
            continue
        patch_scope = relations.resolve_part_scope(part.bank, scope)
        pid = relations.PatchId(patch_scope, part.patch)
        if local_bank_number is not None and part.bank != local_bank_number:
            if pid not in seen_external:
                seen_external.add(pid)
                external.append(pid)
            continue
        if pid not in local_map:
            local_map[pid] = len(carry)
            carry.append(pid)
    return parts, carry, external


def _check_patch_budget(
    plan: AtomicPlan, budget: bankbuild.Budget, k: int, scope: str, num: int, name: str
) -> bool:
    """Check if there are enough patch slots. Return True if budget exceeded."""
    if plan.patch_count + k > budget.patches:
        plan.rejected.append(
            f"{scope}:{num:02d} ({name}) needs {k} patch slot(s), "
            f"only {budget.patches - plan.patch_count} of "
            f"{budget.patches} left"
        )
        return True
    return False


def _validate_patches_loaded(
    srcs: Mapping[str, deps.Source],
    unique_patches: List[relations.PatchId],
    scope: str,
    num: int,
    name: str,
    plan: AtomicPlan,
) -> bool:
    """Check all unique patches are loaded. Return True if any missing."""
    for pid in unique_patches:
        psrc = srcs.get(pid.scope_id)
        if psrc is None or pid.number >= len(psrc.patches):
            plan.rejected.append(
                f"{scope}:{num:02d} ({name}) patch "
                f"{pid.scope_id}:{pid.number:03d} is not loaded"
            )
            return True
    return False


def _collect_sequence_seeds(
    srcs: Mapping[str, deps.Source], unique_patches: List[relations.PatchId]
) -> List[deps.SequenceKey]:
    """Collect wave sequence seeds from all unique patches."""
    seeds: List[deps.SequenceKey] = []
    for pid in unique_patches:
        psrc = srcs[pid.scope_id]
        patch_record = psrc.patches[pid.number]
        seeds.extend(deps.patch_dependencies(patch_record, pid.scope_id))
    return seeds


def _check_sequence_budget(
    plan: AtomicPlan,
    budget: bankbuild.Budget,
    new_seqs: List[deps.SequenceKey],
    scope: str,
    num: int,
    name: str,
) -> bool:
    """Check sequence budget. Return True if exceeded."""
    if len(plan.sequence_map) + len(new_seqs) > budget.sequences:
        plan.rejected.append(
            f"{scope}:{num:02d} ({name}) needs {len(new_seqs)} more "
            f"wave sequence(s); only {budget.sequences} fit in a bank"
        )
        return True
    return False


def _assign_patch_slots(
    plan: AtomicPlan,
    unique_patches: List[relations.PatchId],
    shared: Optional[Dict[relations.PatchId, int]],
) -> Tuple[Dict[relations.PatchId, int], List[Tuple[relations.PatchId, int]]]:
    """Give each patch a slot, reusing one when ``shared`` holds it.

    ``shared`` is the plan-wide map of source patch to target slot that the
    packed mode fills as it goes. ``None`` means each performance gets its
    own copies, which is the isolation this module exists for. Either way
    the slots come out in first-use order with no gaps.
    """
    pid_to_slot: Dict[relations.PatchId, int] = {}
    allocated: List[Tuple[relations.PatchId, int]] = []
    for pid in unique_patches:
        slot = shared.get(pid) if shared is not None else None
        if slot is None:
            slot = plan.patch_count
            plan.patch_count += 1
            if shared is not None:
                shared[pid] = slot
        pid_to_slot[pid] = slot
        allocated.append((pid, slot))
    return pid_to_slot, allocated


def _commit_allocation(
    plan: AtomicPlan,
    used: Set[Tuple[str, int, int]],
    request_index: int,
    perf_id: relations.PerfId,
    name: str,
    parts: List[Any],
    unique_patches: List[relations.PatchId],
    new_seqs: List[deps.SequenceKey],
    chains: Mapping[deps.SequenceKey, Any],
    shared_patches: Optional[Dict[relations.PatchId, int]] = None,
) -> None:
    """Commit the allocation to the plan."""
    target_perf_slot = len(plan.allocations)
    pid_to_slot, allocated = _assign_patch_slots(plan, unique_patches, shared_patches)

    part_patch_slots: Dict[int, int] = {}
    for part in parts:
        patch_scope = relations.resolve_part_scope(part.bank, perf_id.scope_id)
        pid = relations.PatchId(patch_scope, part.patch)
        if pid in pid_to_slot:
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


def _calculate_fresh_steps_atomic(
    new_seqs: List[deps.SequenceKey],
    chains: Mapping[deps.SequenceKey, Any],
    used: Set[Tuple[str, int, int]],
) -> int:
    """Count fresh steps needed for atomic planning."""
    fresh = 0
    seen_fresh: Set[Tuple[str, int, int]] = set()
    for key in new_seqs:
        for step in chains[key].steps:
            tag = (key[0], key[1], step)
            if tag not in used and tag not in seen_fresh:
                seen_fresh.add(tag)
                fresh += 1
    return fresh


def _count_fresh_patches(
    unique_patches: List[relations.PatchId],
    shared: Optional[Mapping[relations.PatchId, int]],
) -> int:
    """How many of ``unique_patches`` still need a slot of their own."""
    if shared is None:
        return len(unique_patches)
    return sum(1 for pid in unique_patches if pid not in shared)


def _plan_one_atomic(
    plan: AtomicPlan,
    budget: bankbuild.Budget,
    used: Set[Tuple[str, int, int]],
    srcs: Mapping[str, deps.Source],
    request_index: int,
    perf_id: relations.PerfId,
    shared_patches: Optional[Dict[relations.PatchId, int]] = None,
    carry_external: bool = True,
) -> None:
    """Plan a single performance for an atomic bank."""
    scope = perf_id.scope_id
    num = perf_id.number

    if _check_performance_budget(plan, budget, scope, num, ""):
        return

    record = _load_and_validate_source(srcs, scope, num)
    if record is None:
        plan.rejected.append(f"{scope}:{num:02d} - performance not loaded")
        return

    name = dumps.record_name(record)
    local_bank = None if carry_external else _source_bank_number(scope, srcs)
    parts, unique_patches, external = _collect_unique_patches(
        record, scope, local_bank_number=local_bank
    )
    for pid in external:
        if pid not in plan.external_patches:
            plan.external_patches.append(pid)
    k = _count_fresh_patches(unique_patches, shared_patches)

    if _check_patch_budget(plan, budget, k, scope, num, name):
        return

    if _validate_patches_loaded_slot(srcs, unique_patches, scope, num, name, plan):
        return

    seeds = _collect_sequence_seeds(srcs, unique_patches)
    order, chains, unresolved = deps.quoted_closure(dict(srcs), seeds)
    if unresolved:
        _src_id, wave_bank, seq = unresolved[0]
        plan.rejected.append(
            f"{scope}:{num:02d} ({name}) needs wave sequence {seq} "
            f"from bank {wave_bank}, whose wave sequence bank is not loaded"
        )
        return

    new_seqs = [key for key in order if key not in plan.sequence_map]
    fresh = _calculate_fresh_steps_atomic(new_seqs, chains, used)

    if _check_sequence_budget(plan, budget, new_seqs, scope, num, name):
        return
    if _check_step_budget_atomic(plan, budget, used, fresh, scope, num, name):
        return

    _commit_allocation(
        plan,
        used,
        request_index,
        perf_id,
        name,
        parts,
        unique_patches,
        new_seqs,
        chains,
        shared_patches,
    )


def plan_atomic_bank(
    sources: Mapping[str, deps.Source],
    chosen: Sequence[relations.PerfId],
    *,
    destination_bank: banks.Bank = _DEFAULT_DESTINATION,
    budget: bankbuild.Budget = _DEFAULT_BUDGET,
    share_patches: bool = False,
    carry_external: bool = True,
) -> AtomicPlan:
    """Plan ``chosen`` performances into one bank, in the order given.

    With ``share_patches`` false (the default) each performance keeps its
    own copies of the patches it uses, which is this module's isolation
    rule. True packs them instead: one target slot per source patch, in
    first-use order, for callers that want a defragmented bank (plan D3).
    """

    plan = AtomicPlan(destination_bank=destination_bank)
    used: Set[Tuple[str, int, int]] = set()
    srcs = dict(sources)
    shared: Optional[Dict[relations.PatchId, int]] = {} if share_patches else None

    for request_index, perf_id in enumerate(chosen):
        _plan_one_atomic(
            plan, budget, used, srcs, request_index, perf_id, shared, carry_external
        )

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


def _prepare_templates(
    made: AtomicPlan,
    template_performances: Optional[bytes],
    template_patches: Optional[bytes],
    template_waveseq: Optional[bytes],
) -> Tuple[bytearray, bytearray, Optional[bytearray]]:
    """Prepare template bytearrays for performances, patches, and waveseq."""
    clean = _clean_performance()
    performance_size = banks.PERFORMANCES_PER_BANK * rec.PERFORMANCE_SIZE
    patch_size = banks.PATCHES_PER_BANK * rec.PATCH_SIZE

    template_patches_fitted = _fit_template(
        template_patches, patch_size, bytes(patch_size)
    )
    template_performances_fitted = _fit_template(
        template_performances,
        performance_size,
        clean * banks.PERFORMANCES_PER_BANK,
    )

    if made.sequence_map and template_waveseq is None:
        template_waveseq = bytes(rec.WAVESEQ_BANK_SIZE)
    # else: leave as supplied (possibly None)

    patches = bytearray(template_patches_fitted)
    performances = bytearray(template_performances_fitted)
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

    return performances, patches, waveseq


def _copy_wave_sequence_steps(
    holder: deps.Source,
    seq: int,
    src_id: str,
    wave_bank: int,
    waveseq: bytearray,
    next_step: int,
    moved: Dict[Tuple[str, int, int], int],
) -> int:
    """Copy steps for one wave sequence. Return the new next_step."""
    if holder.waveseq is None:
        raise ValueError(f"{src_id} has no wave sequence bank")
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
    return next_step


def _link_wave_sequence_steps(
    holder: deps.Source,
    seq: int,
    src_id: str,
    wave_bank: int,
    waveseq: bytearray,
    made: AtomicPlan,
    moved: Dict[Tuple[str, int, int], int],
) -> None:
    """Relink steps and wave sequence quotes."""
    if holder.waveseq is None:
        raise ValueError(f"{src_id} has no wave sequence bank")
    chain = deps.walk_sequence(holder.waveseq, seq, scope_id=src_id)
    pool = (src_id, wave_bank)
    for old in chain.steps:
        info = rec.step(holder.waveseq, old)
        rec.set_step_links(
            waveseq,
            moved[pool + (old,)],
            forward=_step_link(info["forward"], moved.get(pool + (info["forward"],))),
            back=_step_link(info["back"], moved.get(pool + (info["back"],))),
            loop=_step_link(info["loop"], moved.get(pool + (info["loop"],))),
        )
        if info["wave"] < rec.ROM_WAVE_BASE:
            quoted = (src_id, wave_bank, info["wave"])
            if quoted not in made.sequence_map:
                raise ValueError(
                    f"{src_id} sequence {seq} quotes sequence "
                    f"{info['wave']}, which the plan does not carry. "
                    f"This should have been caught while planning."
                )
            rec.set_step_wave(waveseq, moved[pool + (old,)], made.sequence_map[quoted])


def _build_wave_sequences(
    srcs: Mapping[str, deps.Source],
    made: AtomicPlan,
    waveseq: bytearray,
) -> None:
    """Build wave sequences into the waveseq bytearray."""
    next_step = 1
    moved: Dict[Tuple[str, int, int], int] = {}
    sorted_map = sorted(made.sequence_map.items(), key=lambda kv: kv[1])

    # First pass: copy steps
    for (src_id, wave_bank, seq), _slot in sorted_map:
        holder = deps.sequence_holder(dict(srcs), src_id, wave_bank)
        if holder is None or holder.waveseq is None:
            raise ValueError(f"{src_id} has no wave sequence bank")
        next_step = _copy_wave_sequence_steps(
            holder, seq, src_id, wave_bank, waveseq, next_step, moved
        )

    # Second pass: links and headers
    for (src_id, wave_bank, seq), slot in sorted_map:
        holder = deps.sequence_holder(dict(srcs), src_id, wave_bank)
        if holder is None or holder.waveseq is None:
            raise ValueError(f"{src_id} has no wave sequence bank")
        _link_wave_sequence_steps(holder, seq, src_id, wave_bank, waveseq, made, moved)

        head = rec.SEQ_BASE + slot * rec.SEQ_SIZE
        old_head = rec.SEQ_BASE + seq * rec.SEQ_SIZE
        if holder.waveseq is not None:
            waveseq[head : head + rec.SEQ_SIZE] = holder.waveseq[
                old_head : old_head + rec.SEQ_SIZE
            ]
            first = rec.sequence_header(holder.waveseq, seq)["link"]
            pool = (src_id, wave_bank)
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


def _build_patches(
    srcs: Mapping[str, deps.Source],
    made: AtomicPlan,
    patches: bytearray,
) -> None:
    """Build patches into the patches bytearray."""
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


def _build_performances(
    srcs: Mapping[str, deps.Source],
    made: AtomicPlan,
    performances: bytearray,
) -> None:
    """Build performances into the performances bytearray."""
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


def _cleanup_unused_performances(
    made: AtomicPlan,
    performances: bytearray,
) -> None:
    """Clear template performances in slots the plan does not fill.

    A planned slot is left alone even when its number is not below the
    allocation count: 1:1 self-contained writes keep performances at their
    own source slots, so the filled slots are not a prefix.
    """
    clean = _clean_performance()
    planned = {alloc.target_perf_slot for alloc in made.allocations}
    for slot in range(banks.PERFORMANCES_PER_BANK):
        if slot in planned:
            continue
        at = slot * rec.PERFORMANCE_SIZE
        existing = bytes(performances[at : at + rec.PERFORMANCE_SIZE])
        if len(existing) == rec.PERFORMANCE_SIZE and not is_empty_performance(existing):
            performances[at : at + rec.PERFORMANCE_SIZE] = clean


def build_atomic_bank(
    sources: Mapping[str, deps.Source],
    made: AtomicPlan,
    *,
    template_performances: Optional[bytes] = None,
    template_patches: Optional[bytes] = None,
    template_waveseq: Optional[bytes] = None,
) -> AtomicBuilt:

    srcs = dict(sources)
    performances, patches, waveseq = _prepare_templates(
        made, template_performances, template_patches, template_waveseq
    )

    # Wave sequences.
    if waveseq is not None and made.sequence_map:
        _build_wave_sequences(srcs, made, waveseq)

    # Patches.
    _build_patches(srcs, made, patches)

    # Performances.
    _build_performances(srcs, made, performances)

    # Slots past the planned performances must not keep a template's active
    # performance. A target bank used as a template may hold records that the
    # plan deliberately dropped (clean, or a replaced slot); leaving them
    # behind would put a performance in the output that the caller never
    # asked for. Empty template records are kept, so an instrument's own
    # initialised slot survives byte for byte.
    _cleanup_unused_performances(made, performances)

    return AtomicBuilt(
        performances=bytes(performances),
        patches=bytes(patches),
        waveseq=bytes(waveseq) if waveseq is not None else None,
        plan=made,
    )


class ImportMode(Enum):
    """How a whole-bank write treats the source bank (plan §4).

    ``AS_IS`` is the existing :func:`relocate_bank` path and is listed so a
    caller can carry one mode through the UI; the self-contained planner
    refuses it, because it is not a plan.
    """

    AS_IS = "as_is"
    SELF_CONTAINED_1TO1 = "self_contained_1to1"
    SELF_CONTAINED_PACKED = "self_contained_packed"


def contained_slots(audit: containment.ContainmentAudit) -> List[int]:
    """The source slots a self-contained write keeps, in slot order.

    Empty (init) slots never appear in the audit, so they can never be
    chosen here; neither can a performance that names another writable
    bank. ROM references are contained by plan D1.
    """
    return [entry.slot for entry in audit.performances if entry.contained]


# Re-exported from containment for callers that reach this module first.
Pass = containment.Pass
remaining_performances = containment.remaining_performances
partition_self_contained = containment.partition_self_contained


def _target_perf_slots(
    slots: Sequence[int],
    mode: ImportMode,
    perf_slots: Optional[Mapping[int, int]],
) -> Dict[int, int]:
    """Source slot -> target slot for the chosen performances."""
    if perf_slots is not None:
        missing = [slot for slot in slots if slot not in perf_slots]
        if missing:
            raise ValueError(f"no target slot planned for source slot {missing[0]}")
        return {slot: perf_slots[slot] for slot in slots}
    if mode is ImportMode.SELF_CONTAINED_1TO1:
        return {slot: slot for slot in slots}
    return {slot: index for index, slot in enumerate(slots)}


def plan_self_contained(
    sources: Mapping[str, deps.Source],
    audit: containment.ContainmentAudit,
    destination_bank: banks.Bank,
    mode: ImportMode,
    *,
    perf_slots: Optional[Mapping[int, int]] = None,
    budget: bankbuild.Budget = _DEFAULT_BUDGET,
) -> AtomicPlan:
    """Plan the contained performances of ``audit`` into one bank.

    This is a slot policy only. Which performances are contained is the
    audit's decision, and the patch compaction, wave-sequence relocation
    and slot budgets are :func:`plan_atomic_bank`'s.

    * ``SELF_CONTAINED_1TO1`` keeps each performance at its source slot,
      and every other slot is cleaned by the builder.
    * ``SELF_CONTAINED_PACKED`` re-slots them 0..n-1 in source order and
      shares one patch slot between performances that use the same source
      patch (plan D3).

    ``perf_slots`` overrides the 1:1 placement for callers that have
    already decided where things go. ``AS_IS`` raises: it is
    :func:`relocate_bank`'s path, not a planned write.
    """
    if mode is ImportMode.AS_IS:
        raise ValueError(
            "AS_IS is relocate_bank's path; plan_self_contained writes a "
            "self-contained bank only"
        )
    if mode is not ImportMode.SELF_CONTAINED_1TO1 and (
        mode is not ImportMode.SELF_CONTAINED_PACKED
    ):
        raise ValueError(f"unknown import mode {mode!r}")

    slots = contained_slots(audit)
    targets = _target_perf_slots(slots, mode, perf_slots)
    chosen = [relations.PerfId(audit.source_scope, slot) for slot in slots]
    plan = plan_atomic_bank(
        sources,
        chosen,
        destination_bank=destination_bank,
        budget=budget,
        share_patches=mode is ImportMode.SELF_CONTAINED_PACKED,
    )
    for alloc in plan.allocations:
        alloc.target_perf_slot = targets[alloc.source_perf.number]
    return plan


def build_self_contained(
    sources: Mapping[str, deps.Source],
    made: AtomicPlan,
    *,
    template_performances: Optional[bytes] = None,
    template_patches: Optional[bytes] = None,
    template_waveseq: Optional[bytes] = None,
) -> AtomicBuilt:
    """Build the bytes for a :func:`plan_self_contained` plan.

    Thin on purpose: the placement is already in the plan, and patch
    compaction, wave-sequence relocation and the cleaning of every unfilled
    slot are :func:`build_atomic_bank`'s.
    """
    return build_atomic_bank(
        sources,
        made,
        template_performances=template_performances,
        template_patches=template_patches,
        template_waveseq=template_waveseq,
    )


@dataclass
class OverflowPlan:
    """One import split across a destination bank and an overflow bank.

    The destination holds the performances and the patches they reference in
    their own bank; the overflow bank holds the patches they reference in
    *other* writable banks, so the destination's parts can be repointed at
    it. Two banks, two writes, one self-contained result: every patch a
    performance names now lives in a bank the instrument has.
    """

    destination_bank: banks.Bank
    overflow_bank: banks.Bank
    atomic: AtomicPlan
    #: Patch → overflow slot, first-use order, deduplicated by content.
    patch_slots: Dict[relations.PatchId, int] = field(default_factory=dict)
    sequence_map: Dict[deps.SequenceKey, int] = field(default_factory=dict)
    rejected: List[str] = field(default_factory=list)

    @property
    def fits(self) -> bool:
        return not self.rejected


@dataclass
class OverflowBuilt:
    """The bytes of both banks, and what the repoint touched."""

    destination: AtomicBuilt
    overflow_patches: bytes
    overflow_waveseq: Optional[bytes]
    repointed_parts: int
    patch_count: int


def _external_patch_slots(
    srcs: Mapping[str, deps.Source],
    patches: Sequence[relations.PatchId],
) -> Tuple[Dict[relations.PatchId, int], List[bytes]]:
    """Assign overflow slots, deduplicating byte-identical patch records."""
    slots: Dict[relations.PatchId, int] = {}
    records: List[bytes] = []
    by_content: Dict[bytes, int] = {}
    for pid in patches:
        holder = srcs.get(pid.scope_id)
        if holder is None or pid.number >= len(holder.patches):
            continue
        record = bytes(holder.patches[pid.number])
        if record in by_content:
            slots[pid] = by_content[record]
            continue
        slot = len(records)
        by_content[record] = slot
        slots[pid] = slot
        records.append(record)
    return slots, records


def plan_overflow_import(
    sources: Mapping[str, deps.Source],
    source_scope: str,
    destination_bank: banks.Bank,
    overflow_bank: banks.Bank,
    *,
    mode: ImportMode = ImportMode.SELF_CONTAINED_PACKED,
    budget: bankbuild.Budget = _DEFAULT_BUDGET,
) -> OverflowPlan:
    """Plan a self-contained import whose external patches go to another bank.

    Every non-empty performance travels, not only the already-contained
    ones: the destination receives the performances and the patches they
    name in their own bank, and the patches they name in other writable
    banks are deduplicated by content and slotted into ``overflow_bank``.
    The caller writes both banks and repoints the parts, which is what the
    instrument needs to sound as authored -- no performance is dropped for
    the fault of pointing at a second bank.

    ``mode`` chooses placement: 1:1 keeps each performance's own slot;
    packed re-slots them 0..n-1 and shares one slot per source patch.
    """
    if destination_bank.number == overflow_bank.number:
        raise ValueError("the overflow bank is the destination bank")
    if mode is ImportMode.AS_IS:
        # A whole-bank import keeps each performance at its own slot; with
        # an overflow bank the placement question is the same, so as-is
        # means 1:1 here rather than "refuse".
        mode = ImportMode.SELF_CONTAINED_1TO1
    source = sources.get(source_scope)
    if source is None:
        raise LookupError(f"source {source_scope!r} is not loaded")
    slots = [
        slot
        for slot, record in enumerate(source.performances)
        if len(record) >= rec.PERFORMANCE_SIZE and not is_empty_performance(record)
    ]
    chosen = [relations.PerfId(source_scope, slot) for slot in slots]
    atomic = plan_atomic_bank(
        sources,
        chosen,
        destination_bank=destination_bank,
        budget=budget,
        share_patches=True,
        carry_external=False,
    )
    targets = _target_perf_slots(slots, mode, None)
    for alloc in atomic.allocations:
        alloc.target_perf_slot = targets[alloc.source_perf.number]
    plan = OverflowPlan(
        destination_bank=destination_bank,
        overflow_bank=overflow_bank,
        atomic=atomic,
    )
    plan.rejected.extend(atomic.rejected)
    srcs = dict(sources)
    plan.patch_slots, records = _external_patch_slots(srcs, atomic.external_patches)
    if len(records) > budget.patches:
        plan.rejected.append(
            f"{len(records)} overflow patches do not fit {overflow_bank.label}'s "
            f"{budget.patches} slots"
        )
    seeds: List[deps.SequenceKey] = []
    for pid in atomic.external_patches:
        holder = srcs.get(pid.scope_id)
        if holder is None or pid.number >= len(holder.patches):
            continue
        seeds.extend(deps.patch_dependencies(holder.patches[pid.number], pid.scope_id))
    order, _chains, unresolved = deps.quoted_closure(srcs, seeds)
    if unresolved:
        _src, wave_bank, seq = unresolved[0]
        plan.rejected.append(
            f"overflow patch needs wave sequence {seq} from bank {wave_bank}, "
            f"whose bank is not loaded"
        )
    if len(order) > budget.sequences:
        plan.rejected.append(
            f"{len(order)} overflow wave sequences do not fit "
            f"{overflow_bank.label}'s {budget.sequences} slots"
        )
    plan.sequence_map = {key: index for index, key in enumerate(order)}
    return plan


def _repoint_built_parts(
    built: AtomicBuilt,
    srcs: Mapping[str, deps.Source],
    overflow_bank: banks.Bank,
    patch_slots: Mapping[relations.PatchId, int],
) -> Tuple[bytes, int]:
    """Point the destination's external parts at their overflow slots."""
    blob = bytearray(built.performances)
    moved = 0
    for alloc in built.plan.allocations:
        at = alloc.target_perf_slot * rec.PERFORMANCE_SIZE
        # The builder already rewrote the self-references; take its record,
        # never the source one, or the self-remap would be undone here.
        record = bytearray(blob[at : at + rec.PERFORMANCE_SIZE])
        changed = False
        for part in rec.performance_parts(record):
            if not part.used:
                continue
            pid = relations.PatchId(
                relations.resolve_part_scope(part.bank, alloc.source_perf.scope_id),
                part.patch,
            )
            slot = patch_slots.get(pid)
            if slot is None:
                continue
            rec.set_performance_part(
                record, part.index, bank=overflow_bank.number, patch=slot
            )
            changed = True
            moved += 1
        if changed:
            blob[at : at + rec.PERFORMANCE_SIZE] = record
    return bytes(blob), moved


def build_overflow_import(
    sources: Mapping[str, deps.Source],
    plan: OverflowPlan,
) -> OverflowBuilt:
    """Build both banks. The destination's external parts are repointed."""
    if not plan.fits:
        raise ValueError("; ".join(plan.rejected))
    srcs = dict(sources)
    built = build_atomic_bank(srcs, plan.atomic)
    performances, moved = _repoint_built_parts(
        built, srcs, plan.overflow_bank, plan.patch_slots
    )
    patches = bytearray(banks.PATCHES_PER_BANK * rec.PATCH_SIZE)
    for pid, slot in sorted(plan.patch_slots.items(), key=lambda kv: kv[1]):
        holder = srcs[pid.scope_id]
        record = bytearray(holder.patches[pid.number])
        for wave in rec.patch_waves(record):
            if not wave.is_sequence:
                continue
            key = (pid.scope_id, wave.bank, wave.wave)
            sequence = plan.sequence_map.get(key)
            if sequence is None:
                raise ValueError(
                    f"{pid.scope_id} patch {pid.number} uses wave sequence "
                    f"{wave.wave} from bank {wave.bank}, which the plan does "
                    f"not carry"
                )
            rec.set_patch_wave(
                record, wave.index, bank=plan.overflow_bank.number, wave=sequence
            )
        at = slot * rec.PATCH_SIZE
        patches[at : at + rec.PATCH_SIZE] = record
    waveseq: Optional[bytes] = None
    if plan.sequence_map:
        holder_bank = bytearray(rec.WAVESEQ_BANK_SIZE)
        carrier = AtomicPlan(destination_bank=plan.overflow_bank)
        carrier.sequence_map = dict(plan.sequence_map)
        _build_wave_sequences(srcs, carrier, holder_bank)
        waveseq = bytes(holder_bank)
    destination = AtomicBuilt(
        performances=performances,
        patches=built.patches,
        waveseq=built.waveseq,
        plan=built.plan,
    )
    return OverflowBuilt(
        destination=destination,
        overflow_patches=bytes(patches),
        overflow_waveseq=waveseq,
        repointed_parts=moved,
        patch_count=len(plan.patch_slots),
    )


@dataclass
class SplitPlan:
    """One source bank split into two self-contained banks.

    The destination receives the performances that are already contained
    (self or ROM only); the overflow bank receives every performance that
    names another writable bank. Each group is planned against its own
    bank with every referenced patch carried and rewritten, so the rule
    holds in each output bank: every used part of every performance
    names that bank or ROM. Content used by performances that land in
    both banks is duplicated, because a patch must not be shared across
    banks.
    """

    destination_bank: banks.Bank
    overflow_bank: banks.Bank
    source_scope: str
    destination: AtomicPlan
    overflow: AtomicPlan
    rejected: List[str] = field(default_factory=list)

    @property
    def fits(self) -> bool:
        return not self.rejected


@dataclass
class SplitBuilt:
    """The bytes of both banks of a split, and how many patches each holds."""

    destination: AtomicBuilt
    overflow: AtomicBuilt
    destination_patches: int
    overflow_patches: int


@dataclass
class SplitGroup:
    """One bank's worth of a partitioned self-contained split.

    ``target_bank`` is the bank this group is written to. ``performances``
    holds the source performance slots (in source order) that live in it.
    ``plan`` is filled in by :func:`partition_self_contained_split`.
    """

    target_bank: banks.Bank
    performances: Tuple[int, ...] = ()
    plan: Optional[AtomicPlan] = None


@dataclass
class SplitPartition:
    """A self-contained split spread across as many banks as it needs."""

    groups: List[SplitGroup] = field(default_factory=list)
    rejected: List[str] = field(default_factory=list)

    @property
    def fits(self) -> bool:
        return not self.rejected


#: A planning budget with no ceiling. The split plans each group against
#: it so the totals are the group's real needs, not what fit before the
#: planner gave up; whether those needs fit the bank is the separate,
#: bank-naming check in :func:`_split_group_rejections`.
_UNBOUNDED = bankbuild.Budget(
    performances=10**6, patches=10**6, sequences=10**6, steps=10**6
)


def _split_group_rejections(
    bank: banks.Bank,
    group: Sequence[relations.PerfId],
    made: AtomicPlan,
    budget: bankbuild.Budget,
) -> List[str]:
    """One sentence per budget dimension a whole group exceeds.

    The message names the bank and the group size, so the caller can see
    which side of the split is over and by how much, rather than one
    performance's shortfall at the moment the planner ran out.
    """
    size = len(group)
    out: List[str] = []
    if size > budget.performances:
        out.append(
            f"{bank.label}: {size} performances do not fit "
            f"{budget.performances} performance slots"
        )
    if made.patch_count > budget.patches:
        out.append(
            f"{bank.label}: {size} performances with {made.patch_count} "
            f"patches do not fit {budget.patches} patch slots"
        )
    if len(made.sequence_map) > budget.sequences:
        out.append(
            f"{bank.label}: {size} performances with "
            f"{len(made.sequence_map)} wave sequences do not fit "
            f"{budget.sequences} wave sequence slots"
        )
    if made.steps_used > budget.steps:
        out.append(
            f"{bank.label}: {size} performances with {made.steps_used} "
            f"steps do not fit {budget.steps} steps"
        )
    return out


def _plan_split_group(
    sources: Mapping[str, deps.Source],
    source_scope: str,
    slots: Sequence[int],
    bank: banks.Bank,
    mode: ImportMode,
    budget: bankbuild.Budget,
) -> Tuple[AtomicPlan, List[str]]:
    """Plan one group of the split into ``bank``; report its rejections.

    Every referenced patch is carried (ROM parts stay in ROM), and
    ``share_patches`` packs the group so one source patch takes one
    target slot. ``slots`` are source slots; ``mode`` decides whether
    each performance keeps its own or is re-slotted 0..n-1.
    """
    chosen = [relations.PerfId(source_scope, slot) for slot in slots]
    made = plan_atomic_bank(
        sources,
        chosen,
        destination_bank=bank,
        budget=_UNBOUNDED,
        share_patches=True,
        carry_external=True,
    )
    targets = _target_perf_slots(slots, mode, None)
    for alloc in made.allocations:
        alloc.target_perf_slot = targets[alloc.source_perf.number]
    rejections = _split_group_rejections(bank, chosen, made, budget)
    rejections.extend(made.rejected)
    return made, rejections


def plan_self_contained_split(
    sources: Mapping[str, deps.Source],
    source_scope: str,
    destination_bank: banks.Bank,
    overflow_bank: banks.Bank,
    *,
    mode: ImportMode = ImportMode.SELF_CONTAINED_PACKED,
    budget: bankbuild.Budget = _DEFAULT_BUDGET,
) -> SplitPlan:
    """Plan a self-contained split of one source bank across two banks.

    A performance the containment audit calls contained -- every used
    part points at its own bank or at ROM -- goes to
    ``destination_bank``; every other performance goes to
    ``overflow_bank``. Each group is planned against its own bank, which
    copies all referenced patches, rewrites all parts to that bank,
    relocates wave sequences and rewrites patch sequence refs.

    ``mode`` chooses placement per group: 1:1 keeps each performance's
    source slot; packed re-slots each group 0..n-1. ``AS_IS`` means 1:1
    here, the same convention :func:`plan_overflow_import` uses.
    """
    if destination_bank.number == overflow_bank.number:
        raise ValueError("the overflow bank is the destination bank")
    if mode is ImportMode.AS_IS:
        mode = ImportMode.SELF_CONTAINED_1TO1
    audit = containment.audit_containment(sources, source_scope)
    destination_slots = [entry.slot for entry in audit.performances if entry.contained]
    overflow_slots = [entry.slot for entry in audit.performances if not entry.contained]
    destination, destination_rejections = _plan_split_group(
        sources, source_scope, destination_slots, destination_bank, mode, budget
    )
    overflow, overflow_rejections = _plan_split_group(
        sources, source_scope, overflow_slots, overflow_bank, mode, budget
    )
    return SplitPlan(
        destination_bank=destination_bank,
        overflow_bank=overflow_bank,
        source_scope=source_scope,
        destination=destination,
        overflow=overflow,
        rejected=destination_rejections + overflow_rejections,
    )


def build_split_import(
    sources: Mapping[str, deps.Source],
    plan: SplitPlan,
) -> SplitBuilt:
    """Build both banks of a split. Refuses a plan that does not fit."""
    if not plan.fits:
        raise ValueError("; ".join(plan.rejected))
    srcs = dict(sources)
    destination = build_atomic_bank(srcs, plan.destination)
    overflow = build_atomic_bank(srcs, plan.overflow)
    return SplitBuilt(
        destination=destination,
        overflow=overflow,
        destination_patches=plan.destination.patch_count,
        overflow_patches=plan.overflow.patch_count,
    )


def _pack_performance_groups(
    sources: Mapping[str, deps.Source],
    source_scope: str,
    slots: Sequence[int],
    bank: banks.Bank,
    mode: ImportMode,
    budget: bankbuild.Budget,
) -> List[List[int]]:
    """Greedy pack ``slots`` into the fewest groups that fit ``budget``.

    A candidate group is planned with :func:`plan_atomic_bank` on an
    unbounded budget; it is kept only when its real totals fit ``budget``.
    A performance that cannot join the current group starts a new one; one
    that cannot fit even alone still gets its own group, so nothing is
    dropped. Groups and their slots stay in source order.
    """
    groups: List[List[int]] = []
    current: List[int] = []
    for slot in slots:
        candidate = current + [slot]
        _made, rejections = _plan_split_group(
            sources, source_scope, candidate, bank, mode, budget
        )
        if not rejections:
            current = candidate
            continue
        if current:
            groups.append(current)
            current = [slot]
        else:
            groups.append([slot])
            current = []
    if current:
        groups.append(current)
    return groups


def partition_self_contained_split(
    sources: Mapping[str, deps.Source],
    source_scope: str,
    banks_in_order: Sequence[banks.Bank],
    *,
    mode: ImportMode = ImportMode.SELF_CONTAINED_PACKED,
    budget: bankbuild.Budget = _DEFAULT_BUDGET,
) -> SplitPartition:
    """Partition a self-contained split across as many banks as it needs.

    ``banks_in_order`` is ordered: the first bank receives the contained
    performances, the rest are overflow banks in the user's chosen order.
    Contained performances go to the first bank only; if they do not fit,
    the partition is rejected, naming how many banks the content needs.
    Non-contained performances are greedily packed, in source order, into
    the remaining banks, each group carrying every patch it names so that
    each output bank is self-contained. A performance that alone exceeds a
    bank's budget still gets its own group and a rejection naming it.
    """
    if mode is ImportMode.AS_IS:
        mode = ImportMode.SELF_CONTAINED_1TO1

    bank_list = list(banks_in_order)
    audit = containment.audit_containment(sources, source_scope)
    contained_slots = [entry.slot for entry in audit.performances if entry.contained]
    non_contained_slots = [
        entry.slot for entry in audit.performances if not entry.contained
    ]

    partition = SplitPartition()
    placeholder_bank = bank_list[0] if bank_list else banks.bank("CARD")

    # Contained performances: first bank only.
    if bank_list and contained_slots:
        first_bank = bank_list[0]
        contained_made, contained_rejections = _plan_split_group(
            sources, source_scope, contained_slots, first_bank, mode, budget
        )
        contained_group = SplitGroup(
            target_bank=first_bank,
            performances=tuple(contained_slots),
            plan=contained_made,
        )
        partition.groups.append(contained_group)
        if contained_rejections:
            needed = len(
                _pack_performance_groups(
                    sources, source_scope, contained_slots, first_bank, mode, budget
                )
            )
            needed = max(needed, 1)
            partition.rejected.append(f"contained performances need {needed} banks")
            partition.rejected.extend(contained_rejections)

    # Non-contained performances: greedy pack into the remaining banks.
    available = bank_list[1:]
    packed = _pack_performance_groups(
        sources, source_scope, non_contained_slots, placeholder_bank, mode, budget
    )
    for index, slots in enumerate(packed):
        if index >= len(available):
            # No bank left for this group: never assign two groups to one
            # bank, which would make the second overwrite the first.
            break
        target_bank = available[index]
        made, rejections = _plan_split_group(
            sources, source_scope, slots, target_bank, mode, budget
        )
        partition.groups.append(
            SplitGroup(
                target_bank=target_bank,
                performances=tuple(slots),
                plan=made,
            )
        )
        partition.rejected.extend(rejections)

    if len(packed) > len(available):
        needed_total = 1 + len(packed)
        partition.rejected.append(f"needs {needed_total} banks")

    return partition


def build_split_partition(
    sources: Mapping[str, deps.Source],
    partition: SplitPartition,
) -> List[AtomicBuilt]:
    """Build every group of a partition. Refuses a plan that does not fit."""
    if not partition.fits:
        raise ValueError("; ".join(partition.rejected))
    srcs = dict(sources)
    built: List[AtomicBuilt] = []
    for group in partition.groups:
        if group.plan is None:
            raise ValueError("partition group has no plan")
        built.append(build_atomic_bank(srcs, group.plan))
    return built


def pack_self_contained_into(
    sources: Mapping[str, deps.Source],
    source_scope: str,
    slots: Sequence[int],
    bank: banks.Bank,
    *,
    mode: ImportMode = ImportMode.SELF_CONTAINED_PACKED,
    budget: bankbuild.Budget = _DEFAULT_BUDGET,
) -> List[SplitGroup]:
    """Pack source slots, in order, into groups that each fit one bank.

    The greedy packer fills one bank at a time: a slot joins the current
    group while the group's real totals fit ``budget``, and one that cannot
    join starts the next. Every group is planned against ``bank`` with
    every patch it names carried (ROM parts stay in ROM), so each group is
    a self-contained bank on its own and the caller may write one group
    per card, one pass at a time. ``AS_IS`` means 1:1, the same convention
    :func:`partition_self_contained_split` uses. Groups and their slots
    stay in source order.
    """
    if mode is ImportMode.AS_IS:
        mode = ImportMode.SELF_CONTAINED_1TO1
    groups: List[SplitGroup] = []
    for packed in _pack_performance_groups(
        sources, source_scope, slots, bank, mode, budget
    ):
        made, _rejections = _plan_split_group(
            sources, source_scope, packed, bank, mode, budget
        )
        groups.append(
            SplitGroup(target_bank=bank, performances=tuple(packed), plan=made)
        )
    return groups


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


@dataclass
class SlotImportPlan:
    destination_bank: banks.Bank
    assignments: List[Tuple[int, relations.PerfId]] = field(default_factory=list)
    allocations: List[AtomicPerfAllocation] = field(default_factory=list)
    patch_count: int = 0
    sequence_map: Dict[deps.SequenceKey, int] = field(default_factory=dict)
    sequence_count: int = 0
    steps_used: int = 0
    rejected: List[str] = field(default_factory=list)

    @property
    def fits(self) -> bool:
        return not self.rejected


@dataclass
class RelocatedBank:
    performances: bytes
    patches: bytes
    waveseq: Optional[bytes]


@dataclass
class SlotImportBuilt:
    performances: bytes
    patches: bytes
    waveseq: Optional[bytes]
    plan: SlotImportPlan


#: Step links at or above this are sentinels, not pool indices. The
#: manual names 0xFFFF: "pointer to loop start (0xFFFF except last step)".
_STEP_SENTINEL = 0xFFF0


def _step_link(value: int, mapped: Any) -> int:
    """Where a moved step's link points in the destination pool.

    A link not in the copied chain is a sentinel and must stay one. The
    naive ``moved.get(..., 0)`` turned "no loop link" into 0 -- pool step
    0, which is another sequence's step or an empty one -- and a sequence
    that loops into an empty step is a quiet or truncated sequence.
    """
    if mapped is not None:
        return int(mapped)
    if value >= _STEP_SENTINEL:
        return int(value)
    return 0


def _source_bank_number(
    scope_id: str, sources: Optional[Mapping[str, deps.Source]] = None
) -> Optional[int]:
    if sources is not None:
        source = sources.get(scope_id)
        number = getattr(source, "bank_number", None) if source is not None else None
        if number is not None:
            return int(number)
    if scope_id.startswith("CARD/"):
        return banks.bank("CARD").number
    try:
        return banks.bank(scope_id).number
    except LookupError:
        return None


def _bank_number_for_source(source: deps.Source) -> Optional[int]:
    if source.bank_number is not None:
        return int(source.bank_number)
    return _source_bank_number(source.scope_id)


def _part_is_local(
    part: Any, scope_id: str, sources: Mapping[str, deps.Source]
) -> bool:
    """Whether a part points at the performance's own source bank.

    Canonical scopes resolve through the bank table. A session alias (for
    assembling two RAM1 files into one target) carries its real bank number
    on the ``Source`` instead.
    """
    number = _source_bank_number(scope_id, sources)
    if number is not None:
        return bool(part.bank == number)
    return bool(relations.resolve_part_scope(part.bank, scope_id) == scope_id)


def _find_target_scope(
    sources: Mapping[str, deps.Source], destination_bank: banks.Bank
) -> Optional[str]:
    for scope, src in sources.items():
        if not (
            src.performances and len(src.performances) == banks.PERFORMANCES_PER_BANK
        ):
            continue
        if not (src.patches and len(src.patches) == banks.PATCHES_PER_BANK):
            continue
        if scope == destination_bank.id:
            return scope
        if destination_bank.id == "CARD" and scope.startswith("CARD/"):
            return scope
    return None


def _collect_pinned_patches(
    target_src: deps.Source,
    target_scope: str,
    assigned_slots: Set[int],
) -> Set[int]:
    """Collect patch slots that are pinned by unassigned performances."""
    pinned_patches: Set[int] = set()
    for slot in range(banks.PERFORMANCES_PER_BANK):
        if slot in assigned_slots:
            continue
        rec_bytes = target_src.performances[slot]
        if is_empty_performance(rec_bytes):
            continue
        for part in rec.performance_parts(rec_bytes):
            if not part.used:
                continue
            if relations.resolve_part_scope(part.bank, target_scope) != target_scope:
                continue
            pinned_patches.add(part.patch)
    return pinned_patches


def _extract_pinned_patch_bytes(
    target_src: deps.Source, pinned_patches: Set[int]
) -> Dict[int, bytes]:
    """Extract byte content of pinned patches."""
    pinned_patch_bytes: Dict[int, bytes] = {}
    for ps in pinned_patches:
        if 0 <= ps < len(target_src.patches):
            pinned_patch_bytes[ps] = target_src.patches[ps]
    return pinned_patch_bytes


def _collect_pinned_sequence_seeds(
    target_src: deps.Source,
    target_scope: str,
    destination_bank: banks.Bank,
    pinned_patches: Set[int],
) -> List[deps.SequenceKey]:
    """Collect wave sequence seeds from pinned patches."""
    seeds: List[deps.SequenceKey] = []
    for ps in pinned_patches:
        if ps >= len(target_src.patches):
            continue
        patch_rec = target_src.patches[ps]
        for wave in rec.patch_waves(patch_rec):
            if wave.is_sequence and wave.bank == destination_bank.number:
                seeds.append((target_scope, wave.bank, wave.wave))
    # dedup
    seen: Set[deps.SequenceKey] = set()
    uniq: List[deps.SequenceKey] = []
    for s in seeds:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def _resolve_pinned_sequences(
    srcs: Mapping[str, deps.Source], seeds: List[deps.SequenceKey]
) -> Tuple[List[deps.SequenceKey], Mapping[deps.SequenceKey, Any], List[Any]]:
    """Run quoted closure on pinned sequence seeds."""
    return deps.quoted_closure(dict(srcs), seeds)


def _extract_pinned_sequence_info(
    order: List[deps.SequenceKey], chains: Mapping[deps.SequenceKey, Any]
) -> Tuple[Set[int], Set[int]]:
    """Extract pinned sequence slots and step indices from closure result."""
    pinned_seqs: Set[int] = {key[2] for key in order}
    pinned_steps: Set[int] = set()
    for key in order:
        for step in chains[key].steps:
            pinned_steps.add(step)
    return pinned_seqs, pinned_steps


def _compute_pinned(
    sources: Mapping[str, deps.Source],
    target_scope: str,
    destination_bank: banks.Bank,
    target_src: deps.Source,
    assigned_slots: Set[int],
) -> Tuple[Set[int], Dict[int, bytes], Set[int], Set[int], List[Any]]:
    srcs = dict(sources)
    pinned_patches = _collect_pinned_patches(target_src, target_scope, assigned_slots)
    pinned_patch_bytes = _extract_pinned_patch_bytes(target_src, pinned_patches)
    seeds = _collect_pinned_sequence_seeds(
        target_src, target_scope, destination_bank, pinned_patches
    )
    order, chains, unresolved = _resolve_pinned_sequences(dict(srcs), seeds)
    if unresolved:
        return (
            pinned_patches,
            pinned_patch_bytes,
            set(),
            set(),
            [],
        )
    pinned_seqs, pinned_steps = _extract_pinned_sequence_info(order, chains)
    return pinned_patches, pinned_patch_bytes, pinned_seqs, pinned_steps, unresolved


def _validate_assignments(plan: SlotImportPlan) -> bool:
    """Validate assignment slots for duplicates and range. Return True if any errors."""
    seen_slots: Set[int] = set()
    for slot, _ in plan.assignments:
        if slot in seen_slots:
            plan.rejected.append(f"duplicate assignment target slot {slot}")
        seen_slots.add(slot)
        if not (0 <= slot < banks.PERFORMANCES_PER_BANK):
            plan.rejected.append(f"target slot {slot} out of range")
    return bool(plan.rejected)


def _load_and_validate_destination(
    srcs: Mapping[str, deps.Source], target_scope: str, plan: SlotImportPlan
) -> Optional[deps.Source]:
    """Load and validate destination source. Return None if not loaded."""
    target_src = srcs.get(target_scope)
    if (
        target_src is None
        or len(target_src.performances) != banks.PERFORMANCES_PER_BANK
        or len(target_src.patches) != banks.PATCHES_PER_BANK
    ):
        plan.rejected.append("destination bank is not loaded")
        return None
    return target_src


def _compute_free_slots(
    pinned_patches: Set[int], pinned_seqs: Set[int]
) -> Tuple[List[int], List[int]]:
    """Compute free patch and sequence slots excluding pinned ones."""
    free_patch_slots = [
        s for s in range(banks.PATCHES_PER_BANK) if s not in pinned_patches
    ]
    free_seq_slots = [s for s in range(rec.SEQUENCES_PER_BANK) if s not in pinned_seqs]
    return free_patch_slots, free_seq_slots


def _initialize_patch_reuse_map(
    share_existing: bool, pinned_patch_bytes: Dict[int, bytes]
) -> Dict[bytes, int]:
    """Initialize the patch bytes to slot mapping for reuse detection."""
    if share_existing:
        return {b: s for s, b in pinned_patch_bytes.items()}
    return {}


def _collect_local_unique_patches(
    parts: List[Any], scope: str, srcs: Mapping[str, deps.Source]
) -> List[relations.PatchId]:
    """Collect unique patches from local parts only."""
    local_map: Dict[relations.PatchId, int] = {}
    unique_patches: List[relations.PatchId] = []
    for part in parts:
        if not _part_is_local(part, scope, srcs):
            continue
        pid = relations.PatchId(scope, part.patch)
        if pid not in local_map:
            local_map[pid] = len(unique_patches)
            unique_patches.append(pid)
    return unique_patches


def _validate_patches_loaded_slot(
    srcs: Mapping[str, deps.Source],
    unique_patches: List[relations.PatchId],
    scope: str,
    num: int,
    name: str,
    plan: AtomicPlan | SlotImportPlan,
) -> bool:
    """Check all unique patches are loaded. Return True if any missing."""
    for pid in unique_patches:
        psrc = srcs.get(pid.scope_id)
        if psrc is None or pid.number >= len(psrc.patches):
            plan.rejected.append(
                f"{scope}:{num:02d} ({name}) patch "
                f"{pid.scope_id}:{pid.number:03d} is not loaded"
            )
            return True
    return False


def _collect_sequence_seeds_for_patches(
    srcs: Mapping[str, deps.Source],
    unique_patches: List[relations.PatchId],
    source_bank_number: Optional[int],
) -> List[deps.SequenceKey]:
    """Collect wave sequence seeds from patches, filtered by source bank."""
    seeds: List[deps.SequenceKey] = []
    for pid in unique_patches:
        psrc = srcs[pid.scope_id]
        patch_rec = psrc.patches[pid.number]
        for dep in deps.patch_dependencies(patch_rec, pid.scope_id):
            if dep[1] == source_bank_number:
                seeds.append(dep)
    # dedup
    seen_seed = set()
    uniq_seed: List[deps.SequenceKey] = []
    for s in seeds:
        if s not in seen_seed:
            seen_seed.add(s)
            uniq_seed.append(s)
    return uniq_seed


def _check_sequence_slot_budget(
    new_seqs: List[deps.SequenceKey],
    free_seq_slots: List[int],
    used_new_seq_slots: Set[int],
    scope: str,
    num: int,
    name: str,
    plan: SlotImportPlan,
) -> bool:
    """Check sequence slot budget. Return True if exceeded."""
    free_seq_left = len(free_seq_slots) - len(used_new_seq_slots)
    if len(new_seqs) > free_seq_left:
        plan.rejected.append(
            f"{scope}:{num:02d} ({name}) needs {len(new_seqs)} "
            f"wave sequence(s), only {free_seq_left} free"
        )
        return True
    return False


def _count_fresh_steps(
    new_seqs: List[deps.SequenceKey],
    chains: Mapping[deps.SequenceKey, Any],
    pinned_steps: Set[int],
) -> int:
    """Count fresh steps needed for new sequences."""
    fresh = 0
    seen_fresh: Set[Tuple[deps.SequenceKey, int]] = set()
    for key in new_seqs:
        for step in chains[key].steps:
            if step in pinned_steps:
                continue
            tag = (key, step)
            if tag in seen_fresh:
                continue
            seen_fresh.add(tag)
            fresh += 1
    return fresh


def _check_step_budget(
    fresh: int,
    budget: bankbuild.Budget,
    pinned_steps: Set[int],
    new_steps_count: int,
    scope: str,
    num: int,
    name: str,
    plan: SlotImportPlan,
) -> bool:
    """Check step budget. Return True if exceeded."""
    free_steps_left = (budget.steps - len(pinned_steps)) - new_steps_count
    if fresh > free_steps_left:
        plan.rejected.append(
            f"{scope}:{num:02d} ({name}) needs {fresh} "
            f"more step(s); only {free_steps_left} free"
        )
        return True
    return False


def _build_tentative_seq_slot_map(
    plan: SlotImportPlan,
    new_seqs: List[deps.SequenceKey],
    free_seq_slots: List[int],
    used_new_seq_slots: Set[int],
) -> Dict[deps.SequenceKey, int]:
    """Build tentative sequence slot map for patch wave remapping."""
    seq_slot_map = dict(plan.sequence_map)
    avail_seq_slots = [s for s in free_seq_slots if s not in used_new_seq_slots]
    for i, key in enumerate(new_seqs):
        seq_slot_map[key] = avail_seq_slots[i]
    return seq_slot_map


def _compute_patch_dest_bytes(
    srcs: Mapping[str, deps.Source],
    unique_patches: List[relations.PatchId],
    source_bank_number: Optional[int],
    dest_bank_number: int,
    seq_slot_map: Dict[deps.SequenceKey, int],
) -> List[Tuple[relations.PatchId, bytes]]:
    """Compute destination patch bytes with wave sequence remapping."""
    result: List[Tuple[relations.PatchId, bytes]] = []
    for pid in unique_patches:
        psrc = srcs[pid.scope_id]
        src_bytes = psrc.patches[pid.number]
        record_b = bytearray(src_bytes)
        for wave in rec.patch_waves(record_b):
            if not wave.is_sequence:
                continue
            if wave.bank != source_bank_number:
                continue
            key = (pid.scope_id, wave.bank, wave.wave)
            if key in seq_slot_map:
                rec.set_patch_wave(
                    record_b, wave.index, bank=dest_bank_number, wave=seq_slot_map[key]
                )
        dest_bytes = bytes(record_b)
        result.append((pid, dest_bytes))
    return result


def _determine_patch_slots(
    patch_dest_bytes: List[Tuple[relations.PatchId, bytes]],
    patch_bytes_to_slot: Dict[bytes, int],
) -> Tuple[
    Dict[relations.PatchId, int], List[int], int, List[Tuple[relations.PatchId, bytes]]
]:
    """Determine patch slots (reused or new) and return mapping,
    reused slots, count of new, and new patch list."""
    patch_slot_for_pid: Dict[relations.PatchId, int] = {}
    needed_new_patches = 0
    new_patch_bytes_list: List[Tuple[relations.PatchId, bytes]] = []
    reused: List[int] = []

    for pid, dest_bytes in patch_dest_bytes:
        if dest_bytes in patch_bytes_to_slot:
            patch_slot_for_pid[pid] = patch_bytes_to_slot[dest_bytes]
            reused.append(patch_bytes_to_slot[dest_bytes])
        else:
            needed_new_patches += 1
            new_patch_bytes_list.append((pid, dest_bytes))

    return patch_slot_for_pid, reused, needed_new_patches, new_patch_bytes_list


def _check_patch_slot_budget(
    needed_new_patches: int,
    free_patch_slots: List[int],
    used_new_patch_slots: Set[int],
    scope: str,
    num: int,
    name: str,
    plan: SlotImportPlan,
) -> bool:
    """Check patch slot budget. Return True if exceeded."""
    free_patch_left = len(free_patch_slots) - len(used_new_patch_slots)
    if needed_new_patches > free_patch_left:
        plan.rejected.append(
            f"{scope}:{num:02d} ({name}) needs {needed_new_patches} "
            f"patch slot(s), only {free_patch_left} free"
        )
        return True
    return False


def _check_performance_budget_slot_import(
    plan: SlotImportPlan, budget: bankbuild.Budget, scope: str, num: int, name: str
) -> bool:
    """Check performance budget. Return True if exceeded."""
    if len(plan.allocations) + 1 > budget.performances:
        plan.rejected.append(f"{scope}:{num:02d} ({name}) no performance slot left")
        return True
    return False


def _commit_sequences(
    plan: SlotImportPlan,
    new_seqs: List[deps.SequenceKey],
    avail_seq_slots: List[int],
    used_new_seq_slots: Set[int],
    new_steps_count: int,
    fresh: int,
) -> int:
    """Commit sequence slots to plan and return updated new_steps_count."""
    for i, key in enumerate(new_seqs):
        plan.sequence_map[key] = avail_seq_slots[i]
        used_new_seq_slots.add(avail_seq_slots[i])
    return new_steps_count + fresh


def _commit_patches(
    patch_dest_bytes: List[Tuple[relations.PatchId, bytes]],
    patch_slot_for_pid: Dict[relations.PatchId, int],
    patch_bytes_to_slot: Dict[bytes, int],
    free_patch_slots: List[int],
    used_new_patch_slots: Set[int],
    new_patch_slots_used: Set[int],
) -> List[Tuple[relations.PatchId, int]]:
    """Commit patch slots and return allocated list."""
    allocated: List[Tuple[relations.PatchId, int]] = []
    avail_patch_slots = [s for s in free_patch_slots if s not in used_new_patch_slots]
    pi = 0
    for pid, dest_bytes in patch_dest_bytes:
        if dest_bytes in patch_bytes_to_slot:
            slot_p = patch_bytes_to_slot[dest_bytes]
        else:
            slot_p = avail_patch_slots[pi]
            pi += 1
            patch_bytes_to_slot[dest_bytes] = slot_p
            used_new_patch_slots.add(slot_p)
            new_patch_slots_used.add(slot_p)
        patch_slot_for_pid[pid] = slot_p
        allocated.append((pid, slot_p))
    return allocated


def _build_part_patch_slots(
    parts: List[Any],
    scope: str,
    srcs: Mapping[str, deps.Source],
    patch_slot_for_pid: Dict[relations.PatchId, int],
) -> Dict[int, int]:
    """Build part index to patch slot mapping."""
    part_patch_slots: Dict[int, int] = {}
    for part in parts:
        if not part.used:
            continue
        if not _part_is_local(part, scope, srcs):
            continue
        pid = relations.PatchId(scope, part.patch)
        part_patch_slots[part.index] = patch_slot_for_pid[pid]
    return part_patch_slots


def _compute_totals(
    plan: SlotImportPlan,
    pinned_patches: Set[int],
    pinned_seqs: Set[int],
    pinned_steps: Set[int],
    new_steps_count: int,
) -> None:
    """Compute final totals for the plan."""
    all_new_patch_slots = set()
    for alloc in plan.allocations:
        for item in alloc.allocated_patches:
            all_new_patch_slots.add(item[1])
    plan.patch_count = len(pinned_patches | all_new_patch_slots)
    plan.sequence_count = len(pinned_seqs | set(plan.sequence_map.values()))
    plan.steps_used = len(pinned_steps) + new_steps_count


def _plan_one_slot_import(
    plan: SlotImportPlan,
    budget: bankbuild.Budget,
    srcs: Mapping[str, deps.Source],
    slot: int,
    perf_id: relations.PerfId,
    dest: banks.Bank,
    pinned_steps: Set[int],
    pinned_seqs: Set[int],
    free_patch_slots: List[int],
    free_seq_slots: List[int],
    state: Dict[str, Any],
) -> None:
    """Plan one performance for slot import."""
    scope = perf_id.scope_id
    num = perf_id.number
    source = srcs.get(scope)
    if source is None or num >= len(source.performances):
        plan.rejected.append(f"{scope}:{num:02d} - performance not loaded")
        return

    record = source.performances[num]
    name = dumps.record_name(record)
    parts = [p for p in rec.performance_parts(record) if p.used]
    unique_patches = _collect_local_unique_patches(parts, scope, srcs)

    if _validate_patches_loaded_slot(srcs, unique_patches, scope, num, name, plan):
        return

    source_bank_number = _source_bank_number(scope, srcs)
    uniq_seed = _collect_sequence_seeds_for_patches(
        srcs, unique_patches, source_bank_number
    )
    order, chains, unresolved = deps.quoted_closure(dict(srcs), uniq_seed)
    if unresolved:
        u = unresolved[0]
        plan.rejected.append(
            f"{scope}:{num:02d} ({name}) needs wave sequence {u[2]} "
            f"from bank {u[1]}, not loaded"
        )
        return

    new_seqs = [key for key in order if key not in plan.sequence_map]
    if _check_sequence_slot_budget(
        new_seqs, free_seq_slots, state["used_new_seq_slots"], scope, num, name, plan
    ):
        return

    fresh = _count_fresh_steps(new_seqs, chains, pinned_steps)
    if _check_step_budget(
        fresh,
        budget,
        pinned_steps,
        state["new_steps_count"],
        scope,
        num,
        name,
        plan,
    ):
        return

    seq_slot_map = _build_tentative_seq_slot_map(
        plan, new_seqs, free_seq_slots, state["used_new_seq_slots"]
    )
    patch_dest_bytes = _compute_patch_dest_bytes(
        srcs, unique_patches, source_bank_number, dest.number, seq_slot_map
    )

    patch_slot_for_pid, reused, needed_new_patches, _new_patch_bytes_list = (
        _determine_patch_slots(
            patch_dest_bytes,
            state["patch_bytes_to_slot"],
        )
    )

    if _check_patch_slot_budget(
        needed_new_patches,
        free_patch_slots,
        state["used_new_patch_slots"],
        scope,
        num,
        name,
        plan,
    ):
        return

    if _check_performance_budget_slot_import(plan, budget, scope, num, name):
        return

    avail_seq_slots = [
        s for s in free_seq_slots if s not in state["used_new_seq_slots"]
    ]
    state["new_steps_count"] = _commit_sequences(
        plan,
        new_seqs,
        avail_seq_slots,
        state["used_new_seq_slots"],
        state["new_steps_count"],
        fresh,
    )

    allocated = _commit_patches(
        patch_dest_bytes,
        patch_slot_for_pid,
        state["patch_bytes_to_slot"],
        free_patch_slots,
        state["used_new_patch_slots"],
        state["new_patch_slots_used"],
    )

    part_patch_slots = _build_part_patch_slots(parts, scope, srcs, patch_slot_for_pid)

    alloc = AtomicPerfAllocation(
        request_index=len(plan.allocations),
        source_perf=perf_id,
        target_perf_slot=slot,
        name=name,
        part_patch_slots=part_patch_slots,
        allocated_patches=tuple(allocated),
        reused_patch_slots=tuple(reused),
    )
    plan.allocations.append(alloc)


def _check_pinned_sequences(
    plan: SlotImportPlan,
    target_src: deps.Source,
    target_scope: str,
    dest: banks.Bank,
    pinned_patches: Set[int],
    srcs: Mapping[str, deps.Source],
) -> bool:
    """Check if all pinned wave sequences are loaded. Return True if errors found."""
    seeds_check = []
    for ps in pinned_patches:
        if ps < len(target_src.patches):
            for wave in rec.patch_waves(target_src.patches[ps]):
                if wave.is_sequence and wave.bank == dest.number:
                    seeds_check.append((target_scope, wave.bank, wave.wave))
    _order, _chains, unresolved_list = deps.quoted_closure(
        dict(srcs), list(set(seeds_check))
    )
    if unresolved_list:
        u = unresolved_list[0]
        plan.rejected.append(
            f"{target_scope} pinned patch uses wave sequence {u[2]} from bank "
            f"{u[1]}, whose wave sequence bank is not loaded"
        )
        return True
    return False


def plan_slot_import(
    sources: Mapping[str, deps.Source],
    target_scope: str,
    assignments: Mapping[int, relations.PerfId],
    *,
    destination_bank: Optional[banks.Bank] = None,
    budget: bankbuild.Budget = _DEFAULT_BUDGET,
    share_existing: bool = True,
) -> SlotImportPlan:
    """Plan a slot import.

    ``share_existing`` controls whether byte-identical patches already
    present in the target bank are reused. When True (default), the seed
    map is populated from pinned patches, so an import that carries a
    patch identical to a pinned one reuses that slot. When False, the seed
    map starts empty and every patch the import carries is copied into a
    fresh free slot, making the imported performances independent of the
    target bank's existing patches. Dedup within a single plan (two
    assigned performances needing the same source patch) still applies in
    both modes: ``patch_bytes_to_slot`` grows as planning proceeds.
    """
    if destination_bank is not None:
        dest = destination_bank
    elif target_scope.startswith("CARD/") or target_scope == "CARD":
        dest = banks.bank("CARD")
    else:
        dest = banks.bank(target_scope)

    srcs = dict(sources)
    plan = SlotImportPlan(
        destination_bank=dest, assignments=sorted(assignments.items())
    )

    if _validate_assignments(plan):
        return plan

    target_src = _load_and_validate_destination(srcs, target_scope, plan)
    if target_src is None:
        return plan

    assigned_slots: Set[int] = {slot for slot, _ in plan.assignments}
    pinned_patches, pinned_patch_bytes, pinned_seqs, pinned_steps, _unresolved = (
        _compute_pinned(srcs, target_scope, dest, target_src, assigned_slots)
    )

    if _check_pinned_sequences(
        plan, target_src, target_scope, dest, pinned_patches, srcs
    ):
        return plan

    free_patch_slots, free_seq_slots = _compute_free_slots(pinned_patches, pinned_seqs)

    state: Dict[str, Any] = {
        "used_new_seq_slots": set(),
        "used_new_patch_slots": set(),
        "new_steps_count": 0,
        "patch_bytes_to_slot": _initialize_patch_reuse_map(
            share_existing, pinned_patch_bytes
        ),
        "new_patch_slots_used": set(),
    }

    for slot, perf_id in plan.assignments:
        _plan_one_slot_import(
            plan,
            budget,
            srcs,
            slot,
            perf_id,
            dest,
            pinned_steps,
            pinned_seqs,
            free_patch_slots,
            free_seq_slots,
            state,
        )

    new_steps_count = state["new_steps_count"]
    _compute_totals(
        plan,
        pinned_patches,
        pinned_seqs,
        pinned_steps,
        new_steps_count,
    )
    return plan


def planned_performance_record(
    sources: Mapping[str, deps.Source],
    allocation: AtomicPerfAllocation,
    destination_bank: banks.Bank,
    *,
    slot_override: Optional[Mapping[int, int]] = None,
    bank_override: Optional[int] = None,
) -> bytes:
    """The performance record as the import would write it.

    ``slot_override`` remaps part index -> patch slot, for auditioning
    through a staging area whose slots differ from the final plan's.
    ``bank_override`` says which bank those remapped slots actually live
    in then: the staging bank is not the destination bank, and pointing a
    part at the destination bank with a staging slot number names a
    different patch -- usually an empty one, which is a silent audition.
    Parts left unremapped keep pointing at ``destination_bank``.
    """
    srcs = dict(sources)
    holder = srcs[allocation.source_perf.scope_id]
    record = bytearray(holder.performances[allocation.source_perf.number])
    parts = rec.performance_parts(record)
    for part in parts:
        if not part.used:
            continue
        if part.index in allocation.part_patch_slots:
            planned_slot = allocation.part_patch_slots[part.index]
            bank = destination_bank.number
            if slot_override and part.index in slot_override:
                planned_slot = slot_override[part.index]
                if bank_override is not None:
                    bank = bank_override
            rec.set_performance_part(
                record,
                part.index,
                bank=bank,
                patch=planned_slot,
            )
    return bytes(record)


def _prepare_slot_import_templates(
    target_src: deps.Source, made: SlotImportPlan
) -> Tuple[bytearray, bytearray, Optional[bytearray]]:
    """Prepare template bytearrays for slot import build."""
    template_performances = b"".join(target_src.performances)
    template_patches = b"".join(target_src.patches)
    template_waveseq = target_src.waveseq

    performances = bytearray(template_performances)
    patches = bytearray(template_patches)

    waveseq: Optional[bytearray]
    if made.sequence_map and template_waveseq is None:
        template_waveseq = bytes(rec.WAVESEQ_BANK_SIZE)
        waveseq = bytearray(template_waveseq)
    elif template_waveseq is not None:
        waveseq = bytearray(template_waveseq)
    else:
        waveseq = None

    return performances, patches, waveseq


def _build_slot_import_wave_sequences(
    srcs: Mapping[str, deps.Source],
    made: SlotImportPlan,
    waveseq: bytearray,
    pinned_steps: Set[int],
    pinned_seqs: Set[int],
) -> None:
    """Build new wave sequences into the waveseq bytearray."""
    free_steps_sorted = [
        s for s in range(1, rec.STEPS_PER_BANK) if s not in pinned_steps
    ]
    used_step_numbers: Set[int] = set()
    step_cursor = [0]  # mutable for helper access
    moved: Dict[Tuple[deps.SequenceKey, int], int] = {}
    sorted_map = sorted(made.sequence_map.items(), key=lambda kv: kv[1])

    # First pass: copy steps
    for key, _slot in sorted_map:
        holder = deps.sequence_holder(dict(srcs), key[0], key[1])
        if holder is None or holder.waveseq is None:
            raise ValueError(f"{key[0]} has no wave sequence bank")
        _copy_slot_import_steps(
            holder,
            key,
            waveseq,
            free_steps_sorted,
            used_step_numbers,
            step_cursor,
            moved,
        )

    # Second pass: links and headers
    for key, slot in sorted_map:
        holder = deps.sequence_holder(dict(srcs), key[0], key[1])
        if holder is None or holder.waveseq is None:
            raise ValueError(f"{key[0]} has no wave sequence bank")
        _link_slot_import_steps(holder, key, waveseq, made, moved)

        head = rec.SEQ_BASE + slot * rec.SEQ_SIZE
        old_head = rec.SEQ_BASE + key[2] * rec.SEQ_SIZE
        if holder.waveseq is not None:
            waveseq[head : head + rec.SEQ_SIZE] = holder.waveseq[
                old_head : old_head + rec.SEQ_SIZE
            ]
            first = rec.sequence_header(holder.waveseq, key[2])["link"]
            waveseq[head] = (moved.get((key, first), 0) >> 8) & 0xFF
            waveseq[head + 1] = moved.get((key, first), 0) & 0xFF
            name_at = rec.SEQNAME_BASE + slot * rec.SEQNAME_SIZE
            old_name = rec.SEQNAME_BASE + key[2] * rec.SEQNAME_SIZE
            waveseq[name_at : name_at + rec.SEQNAME_SIZE] = holder.waveseq[
                old_name : old_name + rec.SEQNAME_SIZE
            ]

    taken_seq_slots = pinned_seqs | set(made.sequence_map.values())
    for slot in range(rec.SEQUENCES_PER_BANK):
        if slot in taken_seq_slots:
            continue
        head = rec.SEQ_BASE + slot * rec.SEQ_SIZE
        waveseq[head : head + rec.SEQ_SIZE] = bytes(rec.SEQ_SIZE)
        name_at = rec.SEQNAME_BASE + slot * rec.SEQNAME_SIZE
        waveseq[name_at : name_at + rec.SEQNAME_SIZE] = bytes(rec.SEQNAME_SIZE)


def _copy_slot_import_steps(
    holder: deps.Source,
    key: deps.SequenceKey,
    waveseq: bytearray,
    free_steps_sorted: List[int],
    used_step_numbers: Set[int],
    step_cursor: List[int],
    moved: Dict[Tuple[deps.SequenceKey, int], int],
) -> None:
    """Copy steps for one wave sequence in slot import."""
    if holder.waveseq is None:
        raise ValueError(f"{key[0]} has no wave sequence bank")
    chain = deps.walk_sequence(holder.waveseq, key[2], scope_id=key[0])
    for old in chain.steps:
        tag = (key, old)
        if tag in moved:
            continue
        while (
            step_cursor[0] < len(free_steps_sorted)
            and free_steps_sorted[step_cursor[0]] in used_step_numbers
        ):
            step_cursor[0] += 1
        if step_cursor[0] >= len(free_steps_sorted):
            raise ValueError("no free step")
        new_step = free_steps_sorted[step_cursor[0]]
        used_step_numbers.add(new_step)
        moved[tag] = new_step
        at_new = rec.STEP_BASE + new_step * rec.STEP_SIZE
        at_old = rec.STEP_BASE + old * rec.STEP_SIZE
        waveseq[at_new : at_new + rec.STEP_SIZE] = holder.waveseq[
            at_old : at_old + rec.STEP_SIZE
        ]
        step_cursor[0] += 1


def _link_slot_import_steps(
    holder: deps.Source,
    key: deps.SequenceKey,
    waveseq: bytearray,
    made: SlotImportPlan,
    moved: Dict[Tuple[deps.SequenceKey, int], int],
) -> None:
    """Relink steps and wave sequence quotes in slot import."""
    if holder.waveseq is None:
        raise ValueError(f"{key[0]} has no wave sequence bank")
    chain = deps.walk_sequence(holder.waveseq, key[2], scope_id=key[0])
    for old in chain.steps:
        info = rec.step(holder.waveseq, old)
        rec.set_step_links(
            waveseq,
            moved[(key, old)],
            forward=_step_link(info["forward"], moved.get((key, info["forward"]))),
            back=_step_link(info["back"], moved.get((key, info["back"]))),
            loop=_step_link(info["loop"], moved.get((key, info["loop"]))),
        )
        if info["wave"] < rec.ROM_WAVE_BASE:
            quoted = (key[0], key[1], info["wave"])
            if quoted not in made.sequence_map:
                raise ValueError(
                    f"{key[0]} sequence {key[2]} quotes "
                    f"sequence {info['wave']}, not carried"
                )
            rec.set_step_wave(waveseq, moved[(key, old)], made.sequence_map[quoted])


def _build_slot_import_patches(
    srcs: Mapping[str, deps.Source],
    made: SlotImportPlan,
    patches: bytearray,
) -> None:
    """Build patches into the patches bytearray."""
    for alloc in made.allocations:
        for patch_id, target_slot in alloc.allocated_patches:
            holder = srcs[patch_id.scope_id]
            record = bytearray(holder.patches[patch_id.number])
            source_bank_number = _source_bank_number(patch_id.scope_id, srcs)
            for wave in rec.patch_waves(record):
                if not wave.is_sequence:
                    continue
                if source_bank_number is None or wave.bank != source_bank_number:
                    continue
                key = (patch_id.scope_id, wave.bank, wave.wave)
                if key not in made.sequence_map:
                    raise ValueError(
                        f"{patch_id.scope_id} patch {patch_id.number} "
                        f"uses wave sequence {wave.wave}, not carried"
                    )
                rec.set_patch_wave(
                    record,
                    wave.index,
                    bank=made.destination_bank.number,
                    wave=made.sequence_map[key],
                )
            at = target_slot * rec.PATCH_SIZE
            patches[at : at + rec.PATCH_SIZE] = record


def _build_slot_import_performances(
    srcs: Mapping[str, deps.Source],
    made: SlotImportPlan,
    performances: bytearray,
) -> None:
    """Build performances into the performances bytearray."""
    for alloc in made.allocations:
        perf_record = planned_performance_record(srcs, alloc, made.destination_bank)
        at = alloc.target_perf_slot * rec.PERFORMANCE_SIZE
        performances[at : at + rec.PERFORMANCE_SIZE] = bytearray(perf_record)


def build_slot_import(
    sources: Mapping[str, deps.Source],
    made: SlotImportPlan,
) -> SlotImportBuilt:
    srcs = dict(sources)
    scope = _find_target_scope(srcs, made.destination_bank)
    if scope is None:
        raise ValueError("destination bank is not loaded")
    target_src = srcs[scope]
    if (
        len(target_src.performances) != banks.PERFORMANCES_PER_BANK
        or len(target_src.patches) != banks.PATCHES_PER_BANK
    ):
        raise ValueError("destination bank is not loaded")

    if not made.fits:
        raise ValueError("plan does not fit")

    assigned_slots = {slot for slot, _ in made.assignments}
    (_pinned_patches, _pinned_patch_bytes, pinned_seqs, pinned_steps, _) = (
        _compute_pinned(srcs, scope, made.destination_bank, target_src, assigned_slots)
    )

    performances, patches, waveseq = _prepare_slot_import_templates(target_src, made)

    # Wave sequences (new only)
    if waveseq is not None and made.sequence_map:
        _build_slot_import_wave_sequences(
            srcs, made, waveseq, pinned_steps, pinned_seqs
        )

    # Patches
    _build_slot_import_patches(srcs, made, patches)

    # Performances
    _build_slot_import_performances(srcs, made, performances)

    return SlotImportBuilt(
        performances=bytes(performances),
        patches=bytes(patches),
        waveseq=bytes(waveseq) if waveseq is not None else None,
        plan=made,
    )


def copy_slots_into_bank(
    sources: Mapping[str, deps.Source],
    target_scope: str,
    assignments: Mapping[int, relations.PerfId],
    *,
    destination_bank: Optional[banks.Bank] = None,
    budget: bankbuild.Budget = _DEFAULT_BUDGET,
    share_existing: bool = True,
) -> SlotImportBuilt:
    plan = plan_slot_import(
        sources,
        target_scope,
        assignments,
        destination_bank=destination_bank,
        budget=budget,
        share_existing=share_existing,
    )
    return build_slot_import(sources, plan)


def relocate_bank(source: deps.Source, destination_bank: banks.Bank) -> RelocatedBank:
    sbn = _bank_number_for_source(source)
    perf_bytes = bytearray(b"".join(source.performances))
    if sbn is not None:
        for i in range(len(source.performances)):
            at = i * rec.PERFORMANCE_SIZE
            rec_bytes = bytearray(perf_bytes[at : at + rec.PERFORMANCE_SIZE])
            for part in rec.performance_parts(rec_bytes):
                if not part.used:
                    continue
                if part.bank == sbn:
                    rec.set_performance_part(
                        rec_bytes,
                        part.index,
                        bank=destination_bank.number,
                        patch=part.patch,
                    )
            perf_bytes[at : at + rec.PERFORMANCE_SIZE] = rec_bytes
    patch_bytes = bytearray(b"".join(source.patches))
    if sbn is not None:
        for i in range(len(source.patches)):
            at = i * rec.PATCH_SIZE
            rec_bytes = bytearray(patch_bytes[at : at + rec.PATCH_SIZE])
            for wave in rec.patch_waves(rec_bytes):
                if wave.is_sequence and wave.bank == sbn:
                    rec.set_patch_wave(
                        rec_bytes,
                        wave.index,
                        bank=destination_bank.number,
                        wave=wave.wave,
                    )
            patch_bytes[at : at + rec.PATCH_SIZE] = rec_bytes
    return RelocatedBank(
        performances=bytes(perf_bytes),
        patches=bytes(patch_bytes),
        waveseq=source.waveseq,
    )
