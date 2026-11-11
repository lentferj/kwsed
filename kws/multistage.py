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

"""Staging performances from many cards into the banks a Multiset can reach.

A Multiset has sixteen channels but the synth has **one** physical Card slot,
and every card bank answers on the same wire byte (3). Two channels cannot
hear ``CARD/07`` and ``CARD/13`` at once, no matter what the Multiset says.
The same is true of a performance that only exists on an archived card: it is
not in the machine at all until it is copied somewhere resident.

So a Multiset that mixes cards is assembled rather than pointed at: every
performance that needs a card is copied into a writable resident bank
(``RAM 1``-``RAM 3`` or one user card), with its patches atomized by
:mod:`kws.perfcopy`, and its channel is rewritten to that bank's wire byte and
new performance slot. Resident ROM/RAM performances whose dependencies are
also resident stay exactly where they are. When one bank's 35-patch budget
runs out the planner spills into the next bank in the pool, which is how a
full sixteen-channel setup can still be 100% atomic.

Nothing here writes to the instrument. The result is the bytes for each
touched bank plus the remapped Multiset, ready for the fenced writer.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from kws import banks, deps, multi, perfcopy, relations
from kws import records as rec

__all__ = [
    "DesiredChannel",
    "ChannelStageResult",
    "MultiStagePlan",
    "needs_staging",
    "plan_multi_staging",
]


def _is_card_scope(scope_id: Optional[str]) -> bool:
    return scope_id == "CARD" or (scope_id is not None and scope_id.startswith("CARD/"))


@dataclass(frozen=True)
class DesiredChannel:
    channel: int
    perf: Optional[relations.PerfId]
    level: int = 127
    flag: int = 1
    force_stage: bool = False


@dataclass(frozen=True)
class ChannelStageResult:
    channel: int
    source_perf: Optional[relations.PerfId]
    staged: bool
    target_bank: Optional[banks.Bank]
    target_perf_slot: int
    target_patch_slots: Tuple[int, ...]


@dataclass
class MultiStagePlan:
    updated_multiset: multi.Multiset
    built_banks: Dict[str, perfcopy.AtomicBuilt]
    channels: Tuple[ChannelStageResult, ...]
    rejected: List[str] = field(default_factory=list)

    @property
    def fits(self) -> bool:
        return not self.rejected


def needs_staging(
    perf: relations.PerfId,
    sources: Mapping[str, deps.Source],
    *,
    resident_card_scope: Optional[str] = None,
    stage_all_cards: bool = True,
    force_atomic: bool = False,
) -> bool:
    """Whether this performance has to be copied before a Multiset can use it.

    A card performance is staged by default because the card slot can only
    hold one bank. ``resident_card_scope`` names the archived card that is
    physically in the slot right now; with ``stage_all_cards=False`` its
    performances may stay pointed at. A resident performance is staged when
    one of its parts reaches a card that is not resident, and
    ``force_atomic`` stages anything so its patches can be edited privately.
    """
    if force_atomic:
        return True

    source = sources.get(perf.scope_id)
    if source is None or perf.number >= len(source.performances):
        return _is_card_scope(perf.scope_id)

    scope = perf.scope_id
    if _is_card_scope(scope) and (stage_all_cards or scope != resident_card_scope):
        return True

    record = source.performances[perf.number]
    for part in rec.performance_parts(record):
        if not part.used:
            continue
        patch_scope = relations.resolve_part_scope(part.bank, scope)
        if _is_card_scope(patch_scope) and patch_scope != resident_card_scope:
            return True
    return False


def plan_multi_staging(  # noqa: C901
    base_multiset: multi.Multiset,
    desired: Sequence[DesiredChannel],
    sources: Mapping[str, deps.Source],
    *,
    target_banks: Sequence[str] = ("RAM3",),
    mode: str = "append",
    resident_card_scope: Optional[str] = None,
    stage_all_cards: bool = True,
    force_atomic: bool = False,
) -> MultiStagePlan:
    """Copy what needs a home into ``target_banks`` and remap the Multiset.

    Each channel is resolved in order. Resident performances are left alone;
    the rest are tried against each target bank in turn, and the first one
    whose four budgets still fit accepts the performance. Every staged
    performance is copied by :mod:`kws.perfcopy`, so its patches are private
    and packed contiguously, and the returned Multiset points at the bank and
    slot it actually occupies. A channel that no bank can hold is reported in
    ``rejected`` and its part is left unchanged rather than half-written.
    """
    if mode not in ("append", "clean"):
        raise ValueError(f"unknown mode {mode!r}")

    for name in target_banks:
        bank = banks.bank(name)
        if not bank.writable:
            raise ValueError(f"target bank {name!r} is not writable")

    srcs = dict(sources)

    base_lists: Dict[str, List[relations.PerfId]] = {}
    for name in target_banks:
        if mode == "append":
            source = srcs.get(name)
            active = perfcopy.active_performances(source) if source is not None else []
            base_lists[name] = [relations.PerfId(name, slot) for slot in active]
        else:
            base_lists[name] = []

    staged_so_far: Dict[str, List[relations.PerfId]] = {
        name: [] for name in target_banks
    }

    ordered_desired = sorted(desired, key=lambda d: d.channel)

    results: List[ChannelStageResult] = []
    result_by_channel: Dict[int, ChannelStageResult] = {}
    rejected: List[str] = []

    for d in ordered_desired:
        channel = d.channel
        perf = d.perf

        if perf is None:
            bp = base_multiset.parts[channel - 1]
            target_bank = None
            try:
                target_bank = banks.by_sysex(bp.bank)
            except LookupError:
                target_bank = None
            res = ChannelStageResult(
                channel=channel,
                source_perf=None,
                staged=False,
                target_bank=target_bank,
                target_perf_slot=50,
                target_patch_slots=(),
            )
            results.append(res)
            result_by_channel[channel] = res
            continue

        need = needs_staging(
            perf,
            srcs,
            resident_card_scope=resident_card_scope,
            stage_all_cards=stage_all_cards,
            force_atomic=force_atomic or d.force_stage,
        ) or (perf.scope_id in target_banks)

        if not need:
            if perf.scope_id == resident_card_scope and _is_card_scope(
                resident_card_scope
            ):
                target_bank = banks.bank("CARD")
            else:
                try:
                    target_bank = banks.bank(perf.scope_id)
                except LookupError:
                    rejected.append(
                        f"channel {channel}: could not keep "
                        f"{perf.scope_id}:{perf.number:02d}: no such bank"
                    )
                    res = ChannelStageResult(
                        channel=channel,
                        source_perf=perf,
                        staged=False,
                        target_bank=None,
                        target_perf_slot=perf.number,
                        target_patch_slots=(),
                    )
                    results.append(res)
                    result_by_channel[channel] = res
                    continue
            res = ChannelStageResult(
                channel=channel,
                source_perf=perf,
                staged=False,
                target_bank=target_bank,
                target_perf_slot=perf.number,
                target_patch_slots=(),
            )
            results.append(res)
            result_by_channel[channel] = res
            continue

        accepted_bank: Optional[str] = None
        last_plan: Optional[perfcopy.AtomicPlan] = None
        for bank_name in target_banks:
            candidate = base_lists[bank_name] + staged_so_far[bank_name] + [perf]
            plan = perfcopy.plan_atomic_bank(
                srcs, candidate, destination_bank=banks.bank(bank_name)
            )
            last_plan = plan
            if plan.fits:
                accepted_bank = bank_name
                break

        if accepted_bank is None:
            if last_plan is not None and last_plan.rejected:
                reason = (
                    f"channel {channel}: could not stage "
                    f"{perf.scope_id}:{perf.number:02d}: "
                    + "; ".join(last_plan.rejected)
                )
            else:
                reason = (
                    f"channel {channel}: could not stage "
                    f"{perf.scope_id}:{perf.number:02d}: no target bank accepted"
                )
            rejected.append(reason)
            res = ChannelStageResult(
                channel=channel,
                source_perf=perf,
                staged=False,
                target_bank=None,
                target_perf_slot=perf.number,
                target_patch_slots=(),
            )
            results.append(res)
            result_by_channel[channel] = res
            continue

        plan = perfcopy.plan_atomic_bank(
            srcs,
            base_lists[accepted_bank] + staged_so_far[accepted_bank] + [perf],
            destination_bank=banks.bank(accepted_bank),
        )
        alloc = plan.allocations[-1]
        target_perf_slot = alloc.target_perf_slot
        patch_slots = tuple(slot for _patch, slot in alloc.allocated_patches)
        staged_so_far[accepted_bank].append(perf)

        res = ChannelStageResult(
            channel=channel,
            source_perf=perf,
            staged=True,
            target_bank=banks.bank(accepted_bank),
            target_perf_slot=target_perf_slot,
            target_patch_slots=patch_slots,
        )
        results.append(res)
        result_by_channel[channel] = res

    built_banks: Dict[str, perfcopy.AtomicBuilt] = {}
    for bank_name in target_banks:
        if not staged_so_far[bank_name]:
            continue
        built = perfcopy.copy_into_bank(
            srcs,
            bank_name,
            staged_so_far[bank_name],
            mode=mode,
            destination_bank=banks.bank(bank_name),
        )
        built_banks[banks.bank(bank_name).id] = built
        if built.plan.rejected:
            for rtxt in built.plan.rejected:
                rejected.append(rtxt)

    new_parts = list(base_multiset.parts)
    for d in ordered_desired:
        channel = d.channel
        bp = base_multiset.parts[channel - 1]
        res = result_by_channel[channel]

        if d.perf is None:
            new_parts[channel - 1] = dataclasses.replace(
                bp, performance=50, level=d.level, flag=d.flag
            )
            continue

        if res.staged:
            if res.target_bank is None:
                continue
            new_parts[channel - 1] = dataclasses.replace(
                bp,
                bank=res.target_bank.sysex,
                performance=res.target_perf_slot,
                level=d.level,
                flag=d.flag,
            )
            continue

        if res.target_bank is not None and not res.staged:
            new_parts[channel - 1] = dataclasses.replace(
                bp,
                bank=res.target_bank.sysex,
                performance=res.target_perf_slot,
                level=d.level,
                flag=d.flag,
            )
            continue

    updated = dataclasses.replace(base_multiset, parts=tuple(new_parts))

    return MultiStagePlan(
        updated_multiset=updated,
        built_banks=built_banks,
        channels=tuple(sorted(results, key=lambda r: r.channel)),
        rejected=rejected,
    )
