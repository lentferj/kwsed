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

"""The exact plan a whole-bank write is approved to perform (P1).

A write gate says *may this session write at all*. It does not say *may this
particular set of banks and kinds be replaced*. Those are different promises,
and the second one is what a person makes when they look at a plan and press
the key that starts it.

This module is the second promise as data: an immutable description of the
physical writes a request will attempt, a deterministic fingerprint over it,
and a one-shot approval carrying that fingerprint. Nothing here touches a
widget, a file or a device.

**Determinism is the whole point.** Two requests that would write the same
thing must fingerprint identically; any change that alters what is written
must change the fingerprint. The canonical encoding is versioned, sorted and
length-prefixed so a rename, a reordering or a concatenation ambiguity cannot
make two different plans look alike.

**One approval, one sequence.** A minted approval covers exactly one
finalized sequence: the ordered write list of one :class:`WholeBankIntent`
(an overflow bank plus its target, a split's groups in order, or the single
plain, companion or continuation write). A chain of companion writes
answered one at a time is not one approval -- each answer finalizes its own
plan and mints its own token for it.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from kws import banks, containment, deps, perfcopy

__all__ = [
    "INTENT_SCHEMA",
    "BankWriteIntent",
    "WholeBankIntent",
    "PlanApproval",
    "OVERFLOW_CARDS_ONLY",
    "OVERFLOW_PATCHES",
    "OVERFLOW_SELF_CONTAINED",
    "REMEDIATION_GREEN_FIELD",
    "REMEDIATION_INIT_AFFECTED",
    "REMEDIATION_LEAVE",
    "REMEDIATIONS",
    "source_digest",
    "sources_digest",
    "canonical_bytes",
    "fingerprint",
    "digest_map",
    "companion_write_patches_only",
    "new_approval",
    "approval_matches",
    "build_intent",
]

#: Bump when the canonical encoding changes. An approval minted under one
#: schema must never be read as approval for a plan under another.
INTENT_SCHEMA = 2

#: The three kinds a whole-bank write can send, in the order the bridge
#: sends them. Kinds are compared as a set of names but always emitted in
#: this order, so a caller cannot fingerprint two orders as different plans.
KIND_ORDER: Tuple[str, ...] = ("waveseq", "patches", "performances")

#: The three kinds a whole-bank write sends, in :data:`KIND_ORDER`.
ALL_KINDS: Tuple[str, ...] = KIND_ORDER

#: The overflow kinds a whole-bank choice can name. These are the same
#: vocabulary ``kwsed.import_bank``'s ``o`` menu dismisses with; they are
#: defined here so the intent builder and the screen cannot spell them
#: differently.
OVERFLOW_PATCHES = "patches"
OVERFLOW_SELF_CONTAINED = "self_contained"
OVERFLOW_CARDS_ONLY = "cards_only"

#: The remediation answers a whole-bank plan can carry. ``""`` is the
#: absent answer: the question has not been asked (or does not apply).
#: The names are ``kwsed.import_bank.Remediation``'s values, defined here
#: so the intent builder and the screen cannot spell them differently.
REMEDIATION_GREEN_FIELD = "green_field"
REMEDIATION_INIT_AFFECTED = "init_affected"
REMEDIATION_LEAVE = "leave"
REMEDIATIONS: Tuple[str, ...] = (
    "",
    REMEDIATION_GREEN_FIELD,
    REMEDIATION_INIT_AFFECTED,
    REMEDIATION_LEAVE,
)


@dataclass(frozen=True)
class BankWriteIntent:
    """One physical bank a request will write, and exactly what it gets.

    ``bank_id`` is a physical id (``RAM1``, ``CARD``), never an archive
    scope: the hardware has one card slot, and a plan that names
    ``CARD/07`` describes a different write from one that names ``CARD``.
    """

    bank_id: str
    kinds: Tuple[str, ...]
    source_scope: str = ""
    source_slots: Tuple[int, ...] = ()
    target_slots: Tuple[int, ...] = ()
    #: Digests of the patch and sequence assignment maps. The maps
    #: themselves are planner output; a digest is enough to tell two
    #: plans apart and keeps the intent small and comparable.
    patch_map_digest: str = ""
    sequence_map_digest: str = ""

    def __post_init__(self) -> None:
        unknown = [kind for kind in self.kinds if kind not in KIND_ORDER]
        if unknown:
            raise ValueError(f"unknown write kind {unknown[0]!r}")
        if len(set(self.kinds)) != len(self.kinds):
            raise ValueError("a bank's kinds are listed twice")
        if self.kinds != tuple(sorted(self.kinds, key=KIND_ORDER.index)):
            object.__setattr__(
                self, "kinds", tuple(sorted(self.kinds, key=KIND_ORDER.index))
            )


@dataclass(frozen=True)
class WholeBankIntent:
    """A finalized whole-bank request: source, choices, and ordered writes.

    Token granularity: one approval covers exactly the ordered
    ``writes`` list this intent carries, and nothing else. When a
    companion chain is answered one choice at a time, each answer is a
    finalized plan of its own and mints its own token; no single token
    covers writes the person has not yet been shown and answered for.

    ``source_digest`` digests the selected source's own records.
    ``sources_digest`` digests every source the planners plan from --
    including the sibling banks whose patch and wave-sequence records an
    overflow or split copies into its writes -- so a sibling record
    cannot change what is written while the fingerprint stands still.
    ``skipped_banks`` records the physical ids an earlier partial
    attempt already landed, so a retry's token is a different plan from
    the first attempt's even where the write list is the same: the
    first attempt's consumed token cannot be replayed for the remainder.
    """

    source_id: str
    source_digest: str
    mode: str
    writes: Tuple[BankWriteIntent, ...]
    overflow_kind: str = ""
    preflight_answer: str = ""
    session_id: str = ""
    pass_slots: Tuple[int, ...] = ()
    sources_digest: str = ""
    skipped_banks: Tuple[str, ...] = ()
    #: The remediation answer this plan carries: ``""`` (not asked),
    #: ``green_field``, ``init_affected`` or ``leave``. It is part of the
    #: plan: a token minted for one answer must not authorize another.
    remediation: str = ""
    #: The physical bank ids a green-field plan clears first (all three
    #: kinds, Init state), as a sorted set. Cleared banks are RAM banks;
    #: the Card is never among them.
    cleared_banks: Tuple[str, ...] = ()
    #: The Init-ed performance slots per bank, as sorted ``(bank_id,
    #: sorted slots)`` pairs for an ``init_affected`` plan. Only surviving
    #: (not wholesale-replaced) banks can appear, and only the slots the
    #: counts showed.
    init_banks: Tuple[Tuple[str, Tuple[int, ...]], ...] = ()

    def __post_init__(self) -> None:
        # A retry's skipped set is a set of banks, not a sequence: the
        # same attempt recorded in a different order is the same plan.
        object.__setattr__(
            self, "skipped_banks", tuple(sorted(set(self.skipped_banks)))
        )
        if self.remediation not in REMEDIATIONS:
            raise ValueError(f"unknown remediation {self.remediation!r}")
        if self.remediation == REMEDIATION_GREEN_FIELD and self.init_banks:
            raise ValueError("a green-field plan carries no Init-ed slots")
        if self.remediation == REMEDIATION_INIT_AFFECTED and self.cleared_banks:
            raise ValueError("an init-affected plan clears no bank")
        # The cleared set is RAM banks only, and the boundary is here, in
        # the plan itself, not only in the UI that fills it: the Card is
        # never cleared, and a ROM bank cannot be, so a plan that names
        # either is not a plan at all and must not fingerprint.
        for bank_id in self.cleared_banks:
            try:
                entry = banks.bank(bank_id)
            except LookupError as exc:
                raise ValueError(f"the cleared bank {bank_id!r} is not a bank") from exc
            if entry.id == "CARD":
                raise ValueError("the Card is never cleared")
            if not entry.writable:
                raise ValueError(
                    f"{entry.label} is not writable, so it cannot be cleared"
                )
        object.__setattr__(
            self, "cleared_banks", tuple(sorted(set(self.cleared_banks)))
        )
        normalised: Dict[str, Tuple[int, ...]] = {}
        for bank_id, slots in self.init_banks:
            chosen = tuple(sorted({int(slot) for slot in slots}))
            if not chosen:
                continue
            existing = normalised.get(str(bank_id))
            normalised[str(bank_id)] = (
                tuple(sorted(set(existing) | set(chosen))) if existing else chosen
            )
        object.__setattr__(
            self,
            "init_banks",
            tuple(sorted((bank, slots) for bank, slots in normalised.items())),
        )


@dataclass(frozen=True)
class PlanApproval:
    """One-shot permission to perform one fingerprint, once.

    ``nonce`` makes a replay detectable: the app consumes it before the
    first device read, and a second use is refused rather than repeating
    a write the person approved once.
    """

    fingerprint: str
    nonce: str


def _length_prefixed(chunks: Iterable[bytes]) -> bytes:
    """Join blobs so a boundary cannot be shifted between them."""
    out = bytearray()
    for chunk in chunks:
        out += len(chunk).to_bytes(8, "big")
        out += chunk
    return bytes(out)


def source_digest(
    performances: Iterable[bytes],
    patches: Iterable[bytes],
    waveseq: Optional[bytes],
) -> str:
    """A stable digest of the source records a plan was built from.

    Length-prefixed so ``("ab", "c")`` and ``("a", "bc")`` differ. The
    records are the plan's input: two files that produce the same plan
    must not fingerprint differently, and the same file edited must.
    """
    parts: list[bytes] = [
        b"performances",
        _length_prefixed(bytes(record) for record in performances),
        b"patches",
        _length_prefixed(bytes(record) for record in patches),
        b"waveseq",
        bytes(waveseq) if waveseq is not None else b"",
    ]
    return hashlib.sha256(_length_prefixed(parts)).hexdigest()


def sources_digest(sources: Mapping[str, deps.Source]) -> str:
    """A stable digest of every source record the planners plan from.

    An overflow or split copies sibling banks' patch and wave-sequence
    records into its writes, so the selected source's own digest does
    not cover everything the bytes depend on: a sibling record edited
    while the selected bank stayed put would change what is written
    without changing the old fingerprint. This digests every scope in
    the map -- each scope's records through :func:`source_digest`,
    scopes sorted, the scope name length-prefixed with its digest -- so
    the same records in a differently ordered dict digest the same, and
    a changed record anywhere, or a scope added or renamed, changes it.
    """
    parts: list[bytes] = []
    for scope in sorted(sources):
        source = sources[scope]
        per_source = source_digest(
            getattr(source, "performances", ()) or (),
            getattr(source, "patches", ()) or (),
            getattr(source, "waveseq", None),
        )
        parts.append(
            _length_prefixed((str(scope).encode("utf-8"), bytes.fromhex(per_source)))
        )
    return hashlib.sha256(_length_prefixed(parts)).hexdigest()


def _bank_dict(bank: BankWriteIntent) -> Dict[str, Any]:
    return {
        "bank_id": bank.bank_id,
        "kinds": list(bank.kinds),
        "source_scope": bank.source_scope,
        "source_slots": list(bank.source_slots),
        "target_slots": list(bank.target_slots),
        "patch_map_digest": bank.patch_map_digest,
        "sequence_map_digest": bank.sequence_map_digest,
    }


def _intent_dict(intent: WholeBankIntent) -> Dict[str, Any]:
    return {
        "schema": INTENT_SCHEMA,
        "source_id": intent.source_id,
        "source_digest": intent.source_digest,
        "sources_digest": intent.sources_digest,
        "mode": intent.mode,
        "overflow_kind": intent.overflow_kind,
        "preflight_answer": intent.preflight_answer,
        "session_id": intent.session_id,
        "pass_slots": list(intent.pass_slots),
        # A retry that already landed some banks is a different plan
        # from the first attempt, and from a retry that skipped others.
        "skipped_banks": list(intent.skipped_banks),
        # The remediation is part of the plan: the banks a green-field
        # plan clears and the slots an init-affected plan sets to Init
        # are what was approved, not menu state.
        "remediation": intent.remediation,
        "cleared_banks": list(intent.cleared_banks),
        "init_banks": [[bank, list(slots)] for bank, slots in intent.init_banks],
        # Write order is part of the plan: companions before the source
        # is a different request from the source before them.
        "writes": [_bank_dict(bank) for bank in intent.writes],
    }


def canonical_bytes(intent: WholeBankIntent) -> bytes:
    """The intent as deterministic UTF-8 JSON.

    Sorted keys and fixed separators, so the bytes depend only on the
    plan. Exposed for tests and for a readable refusal message; callers
    normally want :func:`fingerprint`.
    """
    text = json.dumps(
        _intent_dict(intent),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return text.encode("utf-8")


def fingerprint(intent: WholeBankIntent) -> str:
    """The hex SHA-256 of :func:`canonical_bytes`."""
    return hashlib.sha256(canonical_bytes(intent)).hexdigest()


def new_approval(
    intent: WholeBankIntent,
    *,
    nonce_factory: Optional[Callable[[], str]] = None,
) -> PlanApproval:
    """Mint a one-shot approval for ``intent``.

    ``nonce_factory`` exists so a test can pin the nonce; production
    callers leave it out and get a random one.
    """
    make = nonce_factory or (lambda: uuid.uuid4().hex)
    return PlanApproval(fingerprint=fingerprint(intent), nonce=make())


def approval_matches(approval: PlanApproval, intent: WholeBankIntent) -> bool:
    """Whether ``approval`` was minted for exactly this plan."""
    return bool(approval.fingerprint) and approval.fingerprint == fingerprint(intent)


def digest_map(items: Sequence[Tuple[str, int]]) -> str:
    """A stable digest of a planner's assignment map.

    ``items`` is ``(key, slot)`` pairs; the order is normalised so the
    same assignment always digests the same. Keys are strings because a
    patch identity and a sequence key both reduce to one.
    """
    ordered = sorted((str(key), int(slot)) for key, slot in items)
    payload = json.dumps(ordered, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The shared intent builder (P2)
#
# The screen and the application must not be able to derive different
# plans from the same choice. Both build the intent through
# :func:`build_intent`, which is the only place that turns a whole-bank
# choice into a :class:`WholeBankIntent`, and it fills every write from
# the same planners the write itself uses -- never from a re-derived
# guess. It is pure: it reads records already in memory and plans; it
# builds no bytes and talks to nothing.
# ---------------------------------------------------------------------------


def _mode_name(mode: Any) -> str:
    """The mode as the canonical string the fingerprint carries."""
    return str(getattr(mode, "value", mode))


def _active_slots(source: deps.Source) -> Tuple[int, ...]:
    """The source slots a whole-bank write carries: the non-empty ones.

    A record the parsers refuse is still carried when it holds bytes, the
    way the import screen counts it -- a slot that cannot be parsed is a
    reason to refuse a plan, not a reason to write fewer slots than the
    file shows.
    """
    out: List[int] = []
    for index, record in enumerate(source.performances or ()):
        try:
            empty = perfcopy.is_empty_performance(record)
        except (ValueError, TypeError):
            empty = not (record and any(record))
        if not empty:
            out.append(index)
    return tuple(out)


def companion_write_patches_only(
    loaded_performances: Optional[Sequence[bytes]],
) -> bool:
    """Whether a companion write must leave the bank's performances alone.

    A companion (``x``) write exists to fill the patch slots other
    performances' parts name; replacing that RAM bank's performances too
    was more than the choice asked for. When the *loaded copy* of the
    bank is empty of performances (all Init), a whole-bank write carries
    nothing away, so the bank travels whole instead. A bank whose
    contents are not loaded (``None``) counts as not empty: nothing is
    guessed about what the write would replace.

    This is the one rule the screen and the application both apply when
    they decide which of the two companion branches a choice will run --
    the branch is part of the plan, so both must decide it the same way
    from the same loaded copies. It is pure: the caller hands in the
    performances the session or cache holds for that bank.
    """
    if loaded_performances is None:
        return True
    performances = list(loaded_performances)
    if not performances:
        return True
    for record in performances:
        try:
            if not perfcopy.is_empty_performance(record):
                return True
        except (ValueError, TypeError):
            if record and any(record):
                return True
    return False


def _patch_items(plan: perfcopy.AtomicPlan) -> List[Tuple[str, int]]:
    """The plan's patch assignments as ``(key, slot)`` digest items."""
    items: List[Tuple[str, int]] = []
    for allocation in plan.allocations:
        for pid, slot in allocation.allocated_patches:
            items.append((f"{pid.scope_id}:{pid.number}", int(slot)))
    return items


def _patch_slot_items(
    slots: Mapping[Any, int],
) -> List[Tuple[str, int]]:
    """A planner's ``patch_slots`` map as digest items (overflow banks)."""
    return [(f"{pid.scope_id}:{pid.number}", int(slot)) for pid, slot in slots.items()]


def _sequence_items_from_keys(
    sequence_map: Mapping[deps.SequenceKey, int],
) -> List[Tuple[str, int]]:
    """A wave-sequence map as digest items."""
    return [
        (f"{key[0]}:{key[1]}:{key[2]}", int(slot)) for key, slot in sequence_map.items()
    ]


def _sequence_items(plan: perfcopy.AtomicPlan) -> List[Tuple[str, int]]:
    """The plan's wave-sequence assignments as digest items."""
    return _sequence_items_from_keys(plan.sequence_map)


def _refuse_unplanned(
    plan: Optional[perfcopy.AtomicPlan], fallback: str
) -> perfcopy.AtomicPlan:
    """``plan`` when the planners accepted it; a refusal with their words."""
    if plan is None:
        raise ValueError(fallback)
    if plan.rejected:
        raise ValueError("; ".join(plan.rejected))
    return plan


def _bank_write(
    bank: banks.Bank,
    source_scope: str,
    plan: perfcopy.AtomicPlan,
    kinds: Tuple[str, ...] = ALL_KINDS,
) -> BankWriteIntent:
    """One bank's write, filled from the planner's own :class:`AtomicPlan`.

    The slots and digests are the plan's, not a parallel derivation: the
    performance placements are the allocations the write will carry, and
    the digests are the patch and sequence maps those allocations name.
    """
    return BankWriteIntent(
        bank_id=str(bank.id),
        kinds=kinds,
        source_scope=source_scope,
        source_slots=tuple(a.source_perf.number for a in plan.allocations),
        target_slots=tuple(a.target_perf_slot for a in plan.allocations),
        patch_map_digest=digest_map(_patch_items(plan)),
        sequence_map_digest=digest_map(_sequence_items(plan)),
    )


def _group_write(group: perfcopy.SplitGroup, source_scope: str) -> BankWriteIntent:
    """One bank's write from a split group's plan."""
    return _bank_write(
        group.target_bank, source_scope, _refuse_unplanned(group.plan, "no plan")
    )


def _companion_write(target: banks.Bank, source: deps.Source) -> BankWriteIntent:
    """The ``x`` companion write: patches and wave sequences, no performances.

    A companion write sends the bank's own records with no planner in
    between -- the whole patch bank and the wave sequences travel as they
    are -- so there is no assignment map to digest, and no performance is
    placed. The kinds say what the source actually carries, because that
    is what the write sends.
    """
    kinds: List[str] = []
    if source.waveseq is not None:
        kinds.append("waveseq")
    if source.patches:
        kinds.append("patches")
    return BankWriteIntent(
        bank_id=str(target.id),
        kinds=tuple(kinds),
        patch_map_digest=digest_map(()),
        sequence_map_digest=digest_map(()),
    )


def _first_self_contained_pass(
    sources: Mapping[str, deps.Source],
    source: deps.Source,
    source_scope: str,
    target: banks.Bank,
    mode: perfcopy.ImportMode,
) -> perfcopy.AtomicPlan:
    """The first pass of a plain self-contained write, as the app plans it.

    The same audit and the same partition: the contained performances are
    split into bank-sized passes and this write carries the first one.
    """
    audit = containment.audit_containment(sources, source_scope)
    passes = containment.partition_self_contained(source, audit)
    if not passes:
        raise ValueError("no remaining performances to place")
    first = passes[0]
    placed = set(first.slots)
    pass_audit = replace(
        audit,
        performances=[entry for entry in audit.performances if entry.slot in placed],
    )
    plan = perfcopy.plan_self_contained(sources, pass_audit, target, mode)
    if not plan.fits:
        raise ValueError("; ".join(plan.rejected))
    return plan


def _plain_write(
    sources: Mapping[str, deps.Source],
    source: deps.Source,
    source_scope: str,
    target: banks.Bank,
    mode: perfcopy.ImportMode,
) -> BankWriteIntent:
    """The plain write: the one target bank, whatever the mode does to it.

    ``AS_IS`` stays byte-identical: every slot keeps its own number and
    there is no assignment map. The self-contained modes plan the first
    pass of the contained performances into the target.
    """
    if mode is perfcopy.ImportMode.AS_IS:
        slots = _active_slots(source)
        return BankWriteIntent(
            bank_id=str(target.id),
            kinds=ALL_KINDS,
            source_scope=source_scope,
            source_slots=slots,
            # As-is keeps each performance at its own slot.
            target_slots=slots,
            patch_map_digest=digest_map(()),
            sequence_map_digest=digest_map(()),
        )
    plan = _first_self_contained_pass(sources, source, source_scope, target, mode)
    return _bank_write(target, source_scope, plan)


def _pass_write(
    sources: Mapping[str, deps.Source],
    source_scope: str,
    slots: Sequence[int],
    target: banks.Bank,
    mode: perfcopy.ImportMode,
) -> BankWriteIntent:
    """One continuation pass: the first packed group of ``slots`` travels.

    The same packer the app's split continuation uses; the slots the
    group leaves out wait for the next pass, which is a new request with
    its own approval.
    """
    groups = perfcopy.pack_self_contained_into(
        sources, source_scope, list(slots), target, mode=mode
    )
    if not groups:
        raise ValueError("the continuation pass has no performances to place")
    plan = _refuse_unplanned(groups[0].plan, "the continuation pass cannot be planned")
    return _bank_write(target, source_scope, plan)


def _cards_only_write(
    sources: Mapping[str, deps.Source],
    source: deps.Source,
    source_scope: str,
    mode: perfcopy.ImportMode,
) -> BankWriteIntent:
    """The cards-only first pass: one self-contained group into the slot.

    Every non-empty performance is packed, in slot order, into groups
    that each fit one card; this write carries the first of them. A later
    group is a later pass with its own approval.
    """
    card = banks.bank("CARD")
    slots = perfcopy.active_performances(source)
    if not slots:
        raise ValueError("no active performances to place on a card")
    groups = perfcopy.pack_self_contained_into(
        sources, source_scope, slots, card, mode=mode
    )
    if not groups:
        raise ValueError("the cards-only pass has no group to write")
    plan = _refuse_unplanned(groups[0].plan, "the cards-only pass cannot be planned")
    return _bank_write(card, source_scope, plan)


def _split_writes(
    sources: Mapping[str, deps.Source],
    source_scope: str,
    target: banks.Bank,
    overflow: banks.Bank,
    mode: perfcopy.ImportMode,
) -> Tuple[BankWriteIntent, ...]:
    """The self-contained split's writes, in the order they are sent.

    A split that fits the two banks writes the partition's groups in the
    partition's order: the target first, then the overflow bank. One that
    needs more banks than the two offered writes the first pass the way
    the app splits it -- the first packed group of the contained
    performances into the target, the first of the rest into the overflow
    bank -- and the slots those leave out wait for another card, which is
    a later pass with its own approval.
    """
    partition = perfcopy.partition_self_contained_split(
        sources, source_scope, [target, overflow], mode=mode
    )
    if partition.fits:
        writes = [
            _group_write(group, source_scope)
            for group in partition.groups
            if group.plan is not None
        ]
        if not writes:
            raise ValueError("the split has no group to write")
        return tuple(writes)
    audit = containment.audit_containment(sources, source_scope)
    shares = (
        ([e.slot for e in audit.performances if e.contained], target),
        ([e.slot for e in audit.performances if not e.contained], overflow),
    )
    writes = []
    for slots, bank in shares:
        if not slots:
            continue
        packed = perfcopy.pack_self_contained_into(
            sources, source_scope, slots, bank, mode=mode
        )
        if not packed:
            raise ValueError(f"{bank.label} cannot hold its share of the split")
        plan = _refuse_unplanned(
            packed[0].plan, f"{bank.label} cannot hold its share of the split"
        )
        writes.append(_bank_write(bank, source_scope, plan))
    if not writes:
        raise ValueError("the split has no group to write")
    return tuple(writes)


def build_intent(
    *,
    source_id: str,
    source_scope: str,
    sources: Mapping[str, deps.Source],
    source: deps.Source,
    target: banks.Bank,
    mode: Any,
    overflow_kind: str = "",
    overflow_bank: str = "",
    preflight_answer: str = "",
    session_id: str = "",
    pass_slots: Sequence[int] = (),
    companion_only: bool = False,
    companion_all_init: bool = False,
    skipped_banks: Sequence[str] = (),
    remediation: str = "",
    cleared_banks: Sequence[str] = (),
    init_banks: Sequence[Tuple[str, Sequence[int]]] = (),
) -> WholeBankIntent:
    """The finalized intent behind one whole-bank choice, from the planners.

    This is the one place a whole-bank choice becomes a plan: the screen
    builds the approval from it, and (from P3) the application rebuilds
    it the same way before any device read, so the two cannot disagree
    about what a choice would write.

    The branch is the choice's own: ``companion_only`` writes the target's
    patches and wave sequences only -- unless ``companion_all_init`` says
    the unit's copy of that bank is empty of performances, where the
    whole-bank branch runs instead and the write carries all three kinds
    (nothing there can be destroyed, and the token must say which branch
    runs); non-empty ``pass_slots`` is a
    continuation pass whose first packed group travels; ``cards_only``
    writes the first self-contained group into the physical card slot; a
    ``patches`` overflow writes the overflow bank first, then the target;
    a ``self_contained`` overflow is the two-bank split; anything else is
    the plain write for the chosen mode. Every write's bank, kinds, slots
    and digests come from the planner that would produce the bytes -- an
    overflow kind with no overflow bank is no overflow at all and is
    planned plainly, because the approval describes effects, not menu
    state.

    A plan the planners refuse raises with their own reasons; nothing is
    approved for a write that cannot be planned.

    Token granularity: one approval is one ordered write list -- the
    sequence this builder returns, and nothing else. When a companion
    chain is answered one choice at a time, each answer finalizes one
    plan here and mints its own token for it; the next answer finalizes
    and mints the next.

    ``skipped_banks`` names the physical banks an earlier partial
    attempt already landed; the app's session state and the screen's
    pass state both carry it, so a retry's mint and the write
    boundary's rebuild derive the same set. The banks stay in
    ``writes`` -- the write list is the choice's plan -- but the retry's
    token differs from the first attempt's, so a consumed token cannot
    be replayed for the remainder, and the app can see which banks the
    retry is not writing again. Ordering does not matter here: the
    builder normalises them sorted.

    The intent's digests cover every record the bytes depend on:
    ``source_digest`` is the selected source's own records;
    ``sources_digest`` covers every scope in ``sources`` -- a sibling
    bank's patch or wave-sequence record an overflow or split copies
    into its writes is planned input too, and changing it changes the
    fingerprint even where the write digests stand still.
    """
    mode_name = _mode_name(mode)
    import_mode = (
        mode
        if isinstance(mode, perfcopy.ImportMode)
        else perfcopy.ImportMode(mode_name)
    )
    slots = tuple(int(slot) for slot in pass_slots)
    writes: Tuple[BankWriteIntent, ...]
    recorded_kind = ""
    if companion_only and not companion_all_init:
        writes = (_companion_write(target, source),)
    elif slots:
        writes = (_pass_write(sources, source_scope, slots, target, import_mode),)
    elif overflow_kind == OVERFLOW_CARDS_ONLY:
        writes = (_cards_only_write(sources, source, source_scope, import_mode),)
        recorded_kind = OVERFLOW_CARDS_ONLY
    elif overflow_bank and overflow_kind == OVERFLOW_PATCHES:
        overflow = banks.bank(overflow_bank)
        plan = perfcopy.plan_overflow_import(
            sources, source_scope, target, overflow, mode=import_mode
        )
        if not plan.fits:
            raise ValueError("; ".join(plan.rejected))
        writes = (
            BankWriteIntent(
                bank_id=str(overflow.id),
                kinds=("waveseq", "patches"),
                patch_map_digest=digest_map(_patch_slot_items(plan.patch_slots)),
                sequence_map_digest=digest_map(
                    _sequence_items_from_keys(plan.sequence_map)
                ),
            ),
            _bank_write(target, source_scope, plan.atomic),
        )
        recorded_kind = OVERFLOW_PATCHES
    elif overflow_bank and overflow_kind == OVERFLOW_SELF_CONTAINED:
        overflow = banks.bank(overflow_bank)
        writes = _split_writes(sources, source_scope, target, overflow, import_mode)
        recorded_kind = OVERFLOW_SELF_CONTAINED
    else:
        writes = (_plain_write(sources, source, source_scope, target, import_mode),)
    return WholeBankIntent(
        source_id=source_id,
        source_digest=source_digest(
            source.performances, source.patches, source.waveseq
        ),
        sources_digest=sources_digest(sources),
        mode=mode_name,
        writes=writes,
        overflow_kind=recorded_kind,
        preflight_answer=preflight_answer,
        session_id=session_id,
        pass_slots=slots,
        skipped_banks=tuple(str(bank) for bank in skipped_banks),
        remediation=str(remediation),
        cleared_banks=tuple(str(bank) for bank in cleared_banks),
        init_banks=tuple(
            (str(bank), tuple(int(slot) for slot in slots))
            for bank, slots in init_banks
        ),
    )
