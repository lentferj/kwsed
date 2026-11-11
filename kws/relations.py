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

"""What depends on what, across banks, and how a patch is recognised.

A performance names patches, a patch names wave sequences, and a wave
sequence's steps can quote more sequences. That graph is walked here once,
in both directions: forward from a Multiset channel down to the wave
sequences a sound needs, and backward from a patch to every performance (and
Multiset channel) that would change if it were edited.

The structural digest is the reason this module exists. Two copies of the
same patch in different banks differ in their wave-sequence *slot numbers*
but not in what they play; hashing the walked step chains by content
(rather than by slot) makes those two copies compare equal. That is what
lets a copied bank be checked against its source, and it is why the digest
deliberately ignores the patch's name unless the caller asks for it.

Everything here reads. Nothing writes to a record, a file or the instrument.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, cast

from kws import banks, deps
from kws import dumps as dumps_mod
from kws import multi as multi_mod
from kws import records as rec

__all__ = [
    "PerfId",
    "PatchId",
    "resolve_part_scope",
    "PartMapping",
    "PerformanceRelation",
    "MultiChannelRelation",
    "patch_structural_digest",
    "RelationGraph",
]


@dataclass(frozen=True)
class PerfId:
    scope_id: str
    number: int


@dataclass(frozen=True)
class PatchId:
    scope_id: str
    number: int


def resolve_part_scope(part_bank_number: int, perf_scope_id: str) -> str:
    """The bank a part's interior bank number refers to.

    A part stores the display index (``Bank.number``), not the wire byte.
    An index of 11 is the Card *slot*; when the performance itself came from
    an archived card (``CARD/07``, ``CARD/c1``) that slot is that archive,
    because the card is the only thing that was in front of the synth.
    Every other index names a resident bank and is resolved by the table.
    """
    if part_bank_number == banks.bank("CARD").number and perf_scope_id.startswith(
        "CARD/"
    ):
        return perf_scope_id
    return banks.bank(part_bank_number).id


@dataclass(frozen=True)
class PartMapping:
    part_index: int
    source_patch: PatchId
    target_patch_slot: Optional[int] = None


@dataclass(frozen=True)
class PerformanceRelation:
    perf: PerfId
    name: str
    parts: Tuple[PartMapping, ...]
    unique_patches: Tuple[PatchId, ...]
    sequences: Tuple[deps.SequenceKey, ...]
    step_cost: int
    missing: Tuple[str, ...]


@dataclass(frozen=True)
class MultiChannelRelation:
    multiset: int
    channel: int
    perf: PerfId
    level: int
    perf_relation: Optional[PerformanceRelation]


def _feed(hasher: Any, *items: object) -> None:
    """Hash one token unambiguously: a type byte and a length, then bytes.

    Without the framing, ``("ab", "c")`` and ``("a", "bc")`` hash the same
    sequence of bytes, and a structural digest that can collide across a
    boundary is not a fingerprint.
    """
    for item in items:
        if isinstance(item, bool):
            hasher.update(b"\x01")
            hasher.update(struct.pack(">q", int(item)))
        elif isinstance(item, int):
            hasher.update(b"\x01")
            hasher.update(struct.pack(">q", item))
        elif isinstance(item, str):
            data = item.encode("utf-8")
            hasher.update(b"\x02")
            hasher.update(struct.pack(">I", len(data)))
            hasher.update(data)
        elif isinstance(item, (bytes, bytearray)):
            data = bytes(item)
            hasher.update(b"\x03")
            hasher.update(struct.pack(">I", len(data)))
            hasher.update(data)
        else:
            raise TypeError(f"unsupported feed item {type(item)!r}")


def patch_structural_digest(
    patch_record: Sequence[int],
    source_scope: str,
    sources: Mapping[str, deps.Source],
    *,
    include_name: bool = False,
) -> str:
    """A canonical fingerprint of what a patch *plays*.

    The 74 settings bytes and each oscillator are hashed as they stand, with
    the oscillator's wave reference blanked out; the reference is then fed
    separately in a normalised form. A PCM ROM wave is named by its wave
    number alone, because the bank it sits in does not change the sound. A
    wave sequence is replaced by the content of the chain it walks, with
    step links reduced to positions *within that chain* and a quoted
    sequence identified by its place in the closure rather than by its slot.

    The result is unchanged by copying a patch into another bank and
    relocating its wave sequences, which is exactly the comparison a bank
    copy needs. ``include_name`` adds the 16-byte name when a caller wants to
    treat differently-named patches as different.
    """
    hasher = hashlib.sha256()
    if include_name:
        _feed(hasher, bytes(patch_record[0:16]))
    settings = bytearray(patch_record[16:90])
    settings[rec.PATCH_BANK_EXP - 16] = 0
    _feed(hasher, bytes(settings))

    waves = rec.patch_waves(patch_record)
    srcs = dict(sources)
    for osc_index in range(rec.OSCILLATORS_PER_PATCH):
        base = rec.OSC_BASE + osc_index * rec.OSC_SIZE
        slc = bytearray(patch_record[base : base + rec.OSC_SIZE])
        slc[2] = 0
        slc[3] = 0
        slc[4] = 0
        slc[83] = 0
        _feed(hasher, bytes(slc))

        wave = waves[osc_index]
        if not wave.used:
            _feed(hasher, "UNUSED")
            continue
        if not wave.is_sequence:
            _feed(hasher, "ROM", wave.wave)
            continue
        seq_scope = resolve_part_scope(wave.bank, source_scope)
        key: deps.SequenceKey = (seq_scope, wave.bank, wave.wave)
        order, chains, _unresolved = deps.quoted_closure(srcs, [key])
        if key not in chains:
            _feed(hasher, "SEQ-MISSING", seq_scope, wave.wave)
            continue
        index_of = {k: i for i, k in enumerate(order)}
        _feed(hasher, "SEQ-COUNT", len(order))
        for seq_key in order:
            chain = chains[seq_key]
            holder = srcs.get(seq_key[0])
            if holder is not None and holder.waveseq is not None:
                header = rec.sequence_header(holder.waveseq, seq_key[2])
                _feed(
                    hasher,
                    "LOOP",
                    header["loop_start"],
                    header["loop_end"],
                    header["loop_count"],
                )
            else:
                _feed(hasher, "LOOP", 0, 0, 0)
            _feed(hasher, "STEPS", len(chain.steps))
            step_positions = {step_num: pos for pos, step_num in enumerate(chain.steps)}
            for step_num in chain.steps:
                info = rec.step(
                    holder.waveseq
                    if (holder is not None and holder.waveseq is not None)
                    else bytearray(rec.WAVESEQ_BANK_SIZE),
                    step_num,
                )
                dur = info["duration"]
                level = info["level"]
                wave_num = info["wave"]
                if wave_num >= rec.ROM_WAVE_BASE:
                    _feed(hasher, dur, level, "ROM", wave_num)
                else:
                    quoted_key = (seq_key[0], seq_key[1], wave_num)
                    rel = index_of.get(quoted_key, -1)
                    _feed(hasher, dur, level, "SEQ", rel)
                fwd = info["forward"]
                back = info["back"]
                loop = info["loop"]
                fwd_rel = step_positions.get(fwd, -1) if fwd != 0 else -1
                back_rel = step_positions.get(back, -1) if back != 0 else -1
                loop_rel = step_positions.get(loop, -1) if loop != 0 else -1
                _feed(hasher, fwd_rel, back_rel, loop_rel)

    return hasher.hexdigest()


def _is_empty_performance(record: Sequence[int]) -> bool:
    parts = rec.performance_parts(record)
    if not any(p.used for p in parts):
        return True
    name = dumps_mod.record_name(record).strip().lower()
    if name not in (
        "",
        "init",
        "init perf",
        "init perf.",
        "init performance",
        "unnamed",
    ):
        return False
    return all(not p.used or (p.bank == 0 and p.patch == 0) for p in parts)


def _is_empty_patch(record: Sequence[int]) -> bool:
    if not any(record):
        return True
    name = dumps_mod.record_name(record).strip().lower()
    return name in ("", "init", "init patch", "init patch.", "unnamed")


class RelationGraph:
    def __init__(
        self,
        sources: Mapping[str, deps.Source],
        multisets: Sequence[multi_mod.Multiset] = (),
        active_card_scope: str = "CARD",
    ) -> None:
        self.sources: Dict[str, deps.Source] = dict(sources)
        self.multisets: List[multi_mod.Multiset] = list(multisets)
        self.active_card_scope = active_card_scope
        self._perf_cache: Dict[PerfId, PerformanceRelation] = {}
        self._multi_cache: Dict[int, List[MultiChannelRelation]] = {}

    def _scope_loaded(self, scope_id: str) -> bool:
        return scope_id in self.sources

    def _perf_loaded(self, perf: PerfId) -> bool:
        src = self.sources.get(perf.scope_id)
        return src is not None and perf.number < len(src.performances)

    def performance_relation(self, perf: PerfId) -> PerformanceRelation:
        if perf in self._perf_cache:
            return self._perf_cache[perf]
        src = self.sources.get(perf.scope_id)
        if src is None or perf.number >= len(src.performances):
            raise LookupError(f"performance {perf} is not loaded")
        record = src.performances[perf.number]
        name = dumps_mod.record_name(record)
        parts_list: List[PartMapping] = []
        seen_patches: List[PatchId] = []
        seen_set: set[PatchId] = set()
        for part in rec.performance_parts(record):
            if not part.used:
                continue
            patch_scope = resolve_part_scope(part.bank, perf.scope_id)
            pid = PatchId(patch_scope, part.patch)
            parts_list.append(PartMapping(part_index=part.index, source_patch=pid))
            if pid not in seen_set:
                seen_set.add(pid)
                seen_patches.append(pid)
        # sequences via closure over unique patches
        patch_list = [(p.scope_id, p.number) for p in seen_patches]
        closure_result: Dict[str, object] = deps.closure(self.sources, patch_list)
        seqs: Tuple[deps.SequenceKey, ...] = tuple(
            cast(List[deps.SequenceKey], closure_result["sequences"])
        )
        step_cost = cast(int, closure_result["steps"])
        missing: Tuple[str, ...] = tuple(cast(List[str], closure_result["missing"]))
        relation = PerformanceRelation(
            perf=perf,
            name=name,
            parts=tuple(parts_list),
            unique_patches=tuple(seen_patches),
            sequences=seqs,
            step_cost=step_cost,
            missing=missing,
        )
        self._perf_cache[perf] = relation
        return relation

    def multi_relations(self, multiset_number: int) -> List[MultiChannelRelation]:
        if multiset_number in self._multi_cache:
            return self._multi_cache[multiset_number]
        # find multiset
        target = None
        for ms in self.multisets:
            if ms.number == multiset_number:
                target = ms
                break
        if target is None:
            self._multi_cache[multiset_number] = []
            return []
        out: List[MultiChannelRelation] = []
        for part in target.parts:
            if not part.used:
                continue
            if part.bank == banks.bank("CARD").sysex:
                scope = self.active_card_scope
            else:
                scope = banks.by_sysex(part.bank).id
            perf = PerfId(scope, part.performance)
            perf_rel = None
            if self._perf_loaded(perf):
                try:
                    perf_rel = self.performance_relation(perf)
                except LookupError:
                    perf_rel = None
            out.append(
                MultiChannelRelation(
                    multiset=multiset_number,
                    channel=part.channel,
                    perf=perf,
                    level=part.level,
                    perf_relation=perf_rel,
                )
            )
        # channel order: target.parts already channel order; ensure sorted by channel
        out.sort(key=lambda r: r.channel)
        self._multi_cache[multiset_number] = out
        return out

    def performances_using_patch(self, patch: PatchId) -> List[PerfId]:
        result: List[PerfId] = []
        for scope_id, src in self.sources.items():
            for number in range(len(src.performances)):
                if _is_empty_performance(src.performances[number]):
                    continue
                perf = PerfId(scope_id, number)
                try:
                    rel = self.performance_relation(perf)
                except LookupError:
                    continue
                if patch in rel.unique_patches:
                    result.append(perf)
        result.sort(key=lambda p: (p.scope_id, p.number))
        return result

    def shared_patches_in_bank(self, scope_id: str) -> Dict[int, List[int]]:
        # map patch number -> set of perf numbers
        patch_to_perfs: Dict[int, set[int]] = {}
        for scope, src in self.sources.items():
            if scope != scope_id:
                continue
            for number in range(len(src.performances)):
                if _is_empty_performance(src.performances[number]):
                    continue
                perf = PerfId(scope, number)
                try:
                    rel = self.performance_relation(perf)
                except LookupError:
                    continue
                for pid in rel.unique_patches:
                    if pid.scope_id != scope_id:
                        continue
                    patch_to_perfs.setdefault(pid.number, set()).add(number)
        out: Dict[int, List[int]] = {}
        for patch_num in sorted(patch_to_perfs):
            perfs = sorted(patch_to_perfs[patch_num])
            if len(perfs) >= 2:
                out[patch_num] = perfs
        return out

    def unreferenced_patches_in_bank(self, scope_id: str) -> List[int]:
        """Patch slots in ``scope_id`` that no active part anywhere names."""
        referenced: set[int] = set()
        for scope, src in self.sources.items():
            for number in range(len(src.performances)):
                if _is_empty_performance(src.performances[number]):
                    continue
                perf = PerfId(scope, number)
                try:
                    rel = self.performance_relation(perf)
                except LookupError:
                    continue
                for pm in rel.parts:
                    if pm.source_patch.scope_id == scope_id:
                        referenced.add(pm.source_patch.number)
        return [p for p in range(35) if p not in referenced]

    def multisets_using_performance(self, perf: PerfId) -> List[Tuple[int, int]]:
        result: List[Tuple[int, int]] = []
        for ms in self.multisets:
            for part in ms.parts:
                if not part.used:
                    continue
                if part.bank == banks.bank("CARD").sysex:
                    scope = self.active_card_scope
                else:
                    scope = banks.by_sysex(part.bank).id
                if scope == perf.scope_id and part.performance == perf.number:
                    result.append((ms.number, part.channel))
        result.sort()
        return result

    def duplicate_patches_in_bank(self, scope_id: str) -> Dict[str, List[int]]:
        src = self.sources.get(scope_id)
        if src is None:
            return {}
        groups: Dict[str, List[int]] = {}
        for number in range(len(src.patches)):
            rec_bytes = src.patches[number]
            if _is_empty_patch(rec_bytes):
                continue
            digest = patch_structural_digest(
                rec_bytes, scope_id, self.sources, include_name=False
            )
            groups.setdefault(digest, []).append(number)
        out: Dict[str, List[int]] = {}
        for digest, nums in groups.items():
            sorted_nums = sorted(nums)
            if len(sorted_nums) >= 2:
                out[digest] = sorted_nums
        return out
