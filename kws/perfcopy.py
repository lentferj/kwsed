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
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from kws import bankbuild, banks, deps, dumps, relations
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
    record: Sequence[int], scope: str
) -> Tuple[List[Any], List[relations.PatchId]]:
    """Extract active parts and collect unique patches with intra-performance dedup."""
    parts = [p for p in rec.performance_parts(record) if p.used]
    local_map: Dict[relations.PatchId, int] = {}
    unique_patches: List[relations.PatchId] = []
    for part in parts:
        patch_scope = relations.resolve_part_scope(part.bank, scope)
        pid = relations.PatchId(patch_scope, part.patch)
        if pid not in local_map:
            local_map[pid] = len(unique_patches)
            unique_patches.append(pid)
    return parts, unique_patches


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
) -> None:
    """Commit the allocation to the plan."""
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
        patch_scope = relations.resolve_part_scope(part.bank, perf_id.scope_id)
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
    plan.patch_count += len(unique_patches)


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

        if _check_performance_budget(plan, budget, scope, num, ""):
            continue

        record = _load_and_validate_source(srcs, scope, num)
        if record is None:
            plan.rejected.append(f"{scope}:{num:02d} - performance not loaded")
            continue

        name = dumps.record_name(record)
        parts, unique_patches = _collect_unique_patches(record, scope)
        k = len(unique_patches)

        if _check_patch_budget(plan, budget, k, scope, num, name):
            continue

        if _validate_patches_loaded_slot(srcs, unique_patches, scope, num, name, plan):
            continue

        seeds = _collect_sequence_seeds(srcs, unique_patches)
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

        if _check_sequence_budget(plan, budget, new_seqs, scope, num, name):
            continue
        if _check_step_budget_atomic(plan, budget, used, fresh, scope, num, name):
            continue

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


def _build_wave_sequences(  # noqa: C901
    srcs: Mapping[str, deps.Source],
    made: AtomicPlan,
    waveseq: bytearray,
) -> None:
    """Build wave sequences into the waveseq bytearray."""
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
                forward=_step_link(
                    info["forward"], moved.get(pool + (info["forward"],))
                ),
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
    """Zero out performance slots past the planned allocations
    if they contain non-empty performances."""
    clean = _clean_performance()
    for slot in range(len(made.allocations), banks.PERFORMANCES_PER_BANK):
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
    free_patch_slots: List[int],
    used_new_patch_slots: Set[int],
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


def plan_slot_import(  # noqa: C901
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

    # duplicate / range check
    if _validate_assignments(plan):
        return plan

    target_src = _load_and_validate_destination(srcs, target_scope, plan)
    if target_src is None:
        return plan

    assigned_slots: Set[int] = {slot for slot, _ in plan.assignments}

    # pinned
    pinned_patches, pinned_patch_bytes, pinned_seqs, pinned_steps, unresolved = (
        _compute_pinned(srcs, target_scope, dest, target_src, assigned_slots)
    )
    # detect unresolved
    seeds_check: List[deps.SequenceKey] = []
    for ps in pinned_patches:
        if ps >= len(target_src.patches):
            continue
        for wave in rec.patch_waves(target_src.patches[ps]):
            if wave.is_sequence and wave.bank == dest.number:
                seeds_check.append((target_scope, wave.bank, wave.wave))
    seen_s = set()
    uniq_s = []
    for s in seeds_check:
        if s not in seen_s:
            seen_s.add(s)
            uniq_s.append(s)
    _order, _chains, unresolved_list = deps.quoted_closure(srcs, uniq_s)
    if unresolved_list:
        u = unresolved_list[0]
        plan.rejected.append(
            f"{target_scope} pinned patch uses wave sequence {u[2]} from bank "
            f"{u[1]}, whose wave sequence bank is not loaded"
        )
        return plan

    free_patch_slots, free_seq_slots = _compute_free_slots(pinned_patches, pinned_seqs)

    used_new_seq_slots: Set[int] = set()
    used_new_patch_slots: Set[int] = set()
    new_steps_count = 0
    patch_bytes_to_slot = _initialize_patch_reuse_map(
        share_existing, pinned_patch_bytes
    )
    new_patch_slots_used: Set[int] = set()

    for slot, perf_id in plan.assignments:
        scope = perf_id.scope_id
        num = perf_id.number
        source = srcs.get(scope)
        if source is None or num >= len(source.performances):
            plan.rejected.append(f"{scope}:{num:02d} - performance not loaded")
            continue
        record = source.performances[num]
        name = dumps.record_name(record)
        parts = [p for p in rec.performance_parts(record) if p.used]

        # localized unique patches
        unique_patches = _collect_local_unique_patches(parts, scope, srcs)

        if _validate_patches_loaded_slot(srcs, unique_patches, scope, num, name, plan):
            continue

        source_bank_number = _source_bank_number(scope, srcs)

        # source seq seeds filtered
        uniq_seed = _collect_sequence_seeds_for_patches(
            srcs, unique_patches, source_bank_number
        )
        order, chains, unresolved = deps.quoted_closure(srcs, uniq_seed)
        if unresolved:
            u = unresolved[0]
            plan.rejected.append(
                f"{scope}:{num:02d} ({name}) needs wave sequence "
                f"{u[2]} from bank {u[1]}, not loaded"
            )
            continue

        new_seqs = [key for key in order if key not in plan.sequence_map]

        # sequence slots availability
        if _check_sequence_slot_budget(
            new_seqs, free_seq_slots, used_new_seq_slots, scope, num, name, plan
        ):
            continue

        # fresh steps
        fresh = _count_fresh_steps(new_seqs, chains, pinned_steps)
        if _check_step_budget(
            fresh, budget, pinned_steps, new_steps_count, scope, num, name, plan
        ):
            continue

        # tentative seq slot map
        seq_slot_map = _build_tentative_seq_slot_map(
            plan, new_seqs, free_seq_slots, used_new_seq_slots
        )

        # patch bytes with remapped waves
        patch_dest_bytes = _compute_patch_dest_bytes(
            srcs, unique_patches, source_bank_number, dest.number, seq_slot_map
        )

        # determine patch slots (reused vs new)
        patch_slot_for_pid, reused, needed_new_patches, _new_patch_bytes_list = (
            _determine_patch_slots(
                patch_dest_bytes,
                patch_bytes_to_slot,
                free_patch_slots,
                used_new_patch_slots,
            )
        )

        if _check_patch_slot_budget(
            needed_new_patches,
            free_patch_slots,
            used_new_patch_slots,
            scope,
            num,
            name,
            plan,
        ):
            continue

        # performance budget
        if _check_performance_budget_slot_import(plan, budget, scope, num, name):
            continue

        # commit sequences
        avail_seq_slots = [s for s in free_seq_slots if s not in used_new_seq_slots]
        new_steps_count = _commit_sequences(
            plan, new_seqs, avail_seq_slots, used_new_seq_slots, new_steps_count, fresh
        )

        # commit patches
        allocated = _commit_patches(
            patch_dest_bytes,
            patch_slot_for_pid,
            patch_bytes_to_slot,
            free_patch_slots,
            used_new_patch_slots,
            new_patch_slots_used,
        )

        # part patch slots
        part_patch_slots = _build_part_patch_slots(
            parts, scope, srcs, patch_slot_for_pid
        )

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

    # totals
    _compute_totals(plan, pinned_patches, pinned_seqs, pinned_steps, new_steps_count)
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


def _build_slot_import_wave_sequences(  # noqa: C901
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
    step_cursor = 0
    moved: Dict[Tuple[deps.SequenceKey, int], int] = {}
    for key, slot in sorted(made.sequence_map.items(), key=lambda kv: kv[1]):
        holder = srcs[key[0]]
        if holder.waveseq is None:
            raise ValueError(f"{key[0]} has no wave sequence bank")
        chain = deps.walk_sequence(holder.waveseq, key[2], scope_id=key[0])
        for old in chain.steps:
            tag = (key, old)
            if tag in moved:
                continue
            while (
                step_cursor < len(free_steps_sorted)
                and free_steps_sorted[step_cursor] in used_step_numbers
            ):
                step_cursor += 1
            if step_cursor >= len(free_steps_sorted):
                raise ValueError("no free step")
            new_step = free_steps_sorted[step_cursor]
            used_step_numbers.add(new_step)
            moved[tag] = new_step
            at_new = rec.STEP_BASE + new_step * rec.STEP_SIZE
            at_old = rec.STEP_BASE + old * rec.STEP_SIZE
            waveseq[at_new : at_new + rec.STEP_SIZE] = holder.waveseq[
                at_old : at_old + rec.STEP_SIZE
            ]
            step_cursor += 1
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
        head = rec.SEQ_BASE + slot * rec.SEQ_SIZE
        old_head = rec.SEQ_BASE + key[2] * rec.SEQ_SIZE
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
