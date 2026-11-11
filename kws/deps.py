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

"""What depends on what, and what a wave sequence costs.

A performance names patches; a patch names wave sequences; a wave sequence
is a chain of steps drawn from a pool the whole bank shares, and a step can
name another wave sequence. So the closure is genuinely recursive, and a
sequence's cost in steps is only known by walking it.

Everything here reads. Nothing here writes to a record or to the
instrument -- assembling a bank is :mod:`kws.bankbuild`, and sending one
anywhere is nobody's job in this project.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from kws import records as rec

__all__ = [
    "PatchId",
    "SequenceId",
    "Source",
    "SequenceChain",
    "walk_sequence",
    "patch_dependencies",
    "performance_dependencies",
    "SequenceKey",
    "quoted_closure",
    "closure",
]

#: A patch, as (bank id, number).
PatchId = Tuple[str, int]

#: A wave sequence, as (bank id, number).
SequenceId = Tuple[str, int]


@dataclass
class Source:
    """One bank's worth of material, however it was loaded.

    Any of the three may be missing: a ``.syx`` holding only performances is
    perfectly normal, and the point of keeping them optional is that a plan
    can then say *which* piece it is short of instead of failing to load.
    """

    bank_id: str
    performances: List[bytes] = field(default_factory=list)
    patches: List[bytes] = field(default_factory=list)
    waveseq: Optional[bytes] = None
    #: The Wavestation bank *number* this source came from. It is usually
    #: derivable from ``bank_id`` (RAM1, CARD/07, ...), but an imported bank
    #: can be held under a unique session alias so several RAM1 files can be
    #: assembled into one destination bank. In that case the alias cannot be
    #: resolved by :mod:`kws.relations`; this field carries the real number.
    bank_number: Optional[int] = None

    def patch(self, number: int) -> bytes:
        try:
            return self.patches[number]
        except IndexError:
            raise LookupError(
                f"{self.bank_id} has {len(self.patches)} patches, so "
                f"{number} is not one of them"
            ) from None


@dataclass(frozen=True)
class SequenceChain:
    """One wave sequence, resolved into the steps it actually occupies."""

    bank_id: str
    number: int
    steps: Tuple[int, ...]
    #: Sequences quoted by this one's steps, which must travel with it.
    quotes: Tuple[SequenceId, ...]

    @property
    def cost(self) -> int:
        return len(self.steps)


def walk_sequence(
    bank: Sequence[int],
    number: int,
    *,
    bank_id: str = "",
    limit: int = rec.STEPS_PER_BANK,
) -> SequenceChain:
    """Follow one wave sequence through the shared step pool.

    The chain is followed by forward link, stopping on a link of 0, on a
    link past the end of the pool, on a step already seen, or at the size of
    the pool. All four stops matter: the data is a linked list written by an
    instrument, and a list that loops is normal here rather than corrupt --
    a wave sequence that repeats is the entire point of the machine.

    The out-of-range stop is not defensive programming. Three files in an
    ordinary library of 828 end a chain with a forward link of 0x0700
    instead of 0, in banks that are otherwise perfectly well formed, and
    without that stop this raises out of :func:`records.step` and takes the
    bank builder with it. Whatever wrote them, the instrument evidently
    stops there, so this does too.

    ``bank_id`` names the bank being walked, and is worth passing: the
    quotes this collects are useless without it, since a quote is only
    actionable as "sequence N *of this bank*", and the caller is the only
    one that knows which bank the bytes came from. It defaults to the empty
    string so that a caller which only wants the step list -- a test, or a
    cost estimate -- need not invent one.
    """
    header = rec.sequence_header(bank, number)
    steps: List[int] = []
    quotes: List[SequenceId] = []
    seen: Set[int] = set()
    at = header["link"]
    while at and at < rec.STEPS_PER_BANK and at not in seen and len(steps) < limit:
        seen.add(at)
        steps.append(at)
        info = rec.step(bank, at)
        if info["wave"] < rec.ROM_WAVE_BASE:
            quotes.append((bank_id, info["wave"]))
        at = info["forward"]
    return SequenceChain(
        bank_id=bank_id, number=number, steps=tuple(steps), quotes=tuple(quotes)
    )


def patch_dependencies(
    record: Sequence[int], bank_id: str
) -> List[Tuple[str, int, int]]:
    """The wave sequences one patch needs, in oscillator order.

    Each is ``(source bank, wave bank, sequence)``. A patch's oscillator
    carries its own wave *bank*, which need not be the bank the patch
    itself lives in. That is exactly the cross-bank reference that makes
    copying a patch between banks change how it sounds, so the bank is
    kept rather than assumed.
    """
    out: List[Tuple[str, int, int]] = []
    for wave in rec.patch_waves(record):
        if wave.is_sequence:
            out.append((bank_id, wave.bank, wave.wave))
    # collapse duplicates while keeping order
    seen = set()
    unique = []
    for item in out:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def performance_dependencies(record: Sequence[int]) -> List[Tuple[int, int]]:
    """``(bank, patch)`` for each *used* part of a performance."""
    return [(p.bank, p.patch) for p in rec.performance_parts(record) if p.used]


#: A wave sequence as the plan and the builder key it: the source bank it
#: was loaded from, the wave bank its oscillator named, and its number.
#: The middle field does not choose the bytes -- a Source has one wave
#: sequence bank -- it keeps two references that differ only in wave bank
#: from collapsing into one entry.
SequenceKey = Tuple[str, int, int]


def quoted_closure(
    sources: Dict[str, Source],
    seeds: Iterable[SequenceKey],
) -> Tuple[List[SequenceKey], Dict[SequenceKey, SequenceChain], List[SequenceKey]]:
    """Seed sequences plus every sequence they quote, transitively.

    A step whose wave number is below :data:`records.ROM_WAVE_BASE` plays
    another wave sequence in the *same bank* rather than a wave in ROM.
    That quoted sequence is as much a dependency of the patch as the one it
    was reached through: leave it behind and the step plays whatever
    occupies its number in the destination, which is usually nothing.

    Returns ``(order, chains, unresolved)``. ``order`` lists every sequence
    that must travel, seeds first and in the order given, each quoted
    sequence after the one that quotes it. ``chains`` gives the walk for
    each. ``unresolved`` holds seeds whose source has no wave sequence bank
    loaded -- a quote can never land there, since it names a sequence in
    the bank the quoting step was just read from.

    Quoting is transitive and may be circular: a sequence can quote one
    that quotes it back. The ``seen`` set is what makes that terminate, and
    it is the same reason :func:`walk_sequence` carries one for steps.
    """
    order: List[SequenceKey] = []
    chains: Dict[SequenceKey, SequenceChain] = {}
    unresolved: List[SequenceKey] = []
    seen: Set[SequenceKey] = set()
    queue: List[SequenceKey] = list(seeds)

    while queue:
        key = queue.pop(0)
        if key in seen:
            continue
        seen.add(key)
        src_id, wave_bank, number = key
        holder = sources.get(src_id)
        if holder is None or holder.waveseq is None:
            unresolved.append(key)
            continue
        if not 0 <= number < rec.SEQUENCES_PER_BANK:
            # A step's wave byte is one byte wide and any value in it is a
            # legal wave; only those below ROM_WAVE_BASE are sequences, so
            # this cannot trip on real data. It is here so that a truncated
            # or misaligned bank cannot walk the builder off the end.
            unresolved.append(key)
            continue
        chain = walk_sequence(holder.waveseq, number, bank_id=src_id)
        order.append(key)
        chains[key] = chain
        for _, quoted in chain.quotes:
            queue.append((src_id, wave_bank, quoted))

    return order, chains, unresolved


def closure(
    sources: Dict[str, Source], patches: Iterable[PatchId]
) -> Dict[str, object]:
    """Everything a set of patches drags along with it.

    Returns the patches themselves, the wave sequences they need, the total
    step cost, and whatever could not be resolved. Unresolved is reported
    rather than raised: a library assembled from files will routinely be
    missing the wave-sequence bank for some source, and the useful answer is
    "these four patches cannot come, and this is why".
    """
    want_patches: List[PatchId] = []
    want_seqs: List[Tuple[str, int, int]] = []
    missing: List[str] = []
    used: Set[Tuple[str, int, int]] = set()
    seen_seq: Set[Tuple[str, int, int]] = set()

    for bank_id, number in patches:
        source = sources.get(bank_id)
        if source is None or number >= len(source.patches):
            missing.append(f"{bank_id}:{number:03d} (patch not loaded)")
            continue
        want_patches.append((bank_id, number))
        order, chains, unresolved = quoted_closure(
            sources, patch_dependencies(source.patch(number), bank_id)
        )
        for src_id, wave_bank, seq_num in unresolved:
            missing.append(
                f"{bank_id}:{number:03d} needs wave sequence "
                f"{seq_num} from bank {wave_bank}, whose wave sequence "
                f"bank is not loaded"
            )
        for key in order:
            if key in seen_seq:
                continue
            seen_seq.add(key)
            want_seqs.append(key)
            for step in chains[key].steps:
                used.add((key[0], key[1], step))

    return {
        "patches": want_patches,
        "sequences": want_seqs,
        "steps": len(used),
        "missing": missing,
    }


def load_syx(paths: Iterable[str], bank_id: str) -> Source:
    """Gather one bank's material from ``.syx`` files.

    A bank arrives as up to three separate dumps -- performances, patches and
    wave sequences -- because that is how the instrument sends them. They are
    collected into one :class:`Source` here so that everything downstream can
    ask for a bank rather than for a file.
    """
    from kws import dumps
    from kws import messages as m

    source = Source(bank_id=bank_id)
    for path in paths:
        for packet in dumps.read_syx_file(path):
            if packet.function == m.Function.ALL_PERFORMANCE_DUMP:
                source.performances = list(dumps.parse_bulk(packet).records)
            elif packet.function == m.Function.ALL_PATCH_DUMP:
                source.patches = list(dumps.parse_bulk(packet).records)
            elif packet.function == WAVESEQ_DUMP:
                data = packet.data()
                if len(data) >= rec.WAVESEQ_BANK_SIZE:
                    source.waveseq = data[: rec.WAVESEQ_BANK_SIZE]
    return source


#: All-wave-sequence dump. Its data starts at the same offset as the bank
#: dumps', which is why it is read here rather than through DATA_OFFSET.
WAVESEQ_DUMP = 0x54
