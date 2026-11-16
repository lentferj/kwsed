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
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Optional, Sequence, Tuple

__all__ = [
    "INTENT_SCHEMA",
    "BankWriteIntent",
    "WholeBankIntent",
    "PlanApproval",
    "source_digest",
    "canonical_bytes",
    "fingerprint",
    "digest_map",
    "new_approval",
    "approval_matches",
]

#: Bump when the canonical encoding changes. An approval minted under one
#: schema must never be read as approval for a plan under another.
INTENT_SCHEMA = 1

#: The three kinds a whole-bank write can send, in the order the bridge
#: sends them. Kinds are compared as a set of names but always emitted in
#: this order, so a caller cannot fingerprint two orders as different plans.
KIND_ORDER: Tuple[str, ...] = ("waveseq", "patches", "performances")


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
    """A finalized whole-bank request: source, choices, and ordered writes."""

    source_id: str
    source_digest: str
    mode: str
    writes: Tuple[BankWriteIntent, ...]
    overflow_kind: str = ""
    preflight_answer: str = ""
    session_id: str = ""
    pass_slots: Tuple[int, ...] = ()


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
        "mode": intent.mode,
        "overflow_kind": intent.overflow_kind,
        "preflight_answer": intent.preflight_answer,
        "session_id": intent.session_id,
        "pass_slots": list(intent.pass_slots),
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
