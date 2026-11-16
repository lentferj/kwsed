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

"""The plan-approval fingerprint (P1). Every name here is invented."""

from __future__ import annotations

import json

import pytest

from kwsed import import_intent as intent_mod
from kwsed.import_intent import (
    INTENT_SCHEMA,
    BankWriteIntent,
    PlanApproval,
    WholeBankIntent,
    approval_matches,
    canonical_bytes,
    digest_map,
    fingerprint,
    new_approval,
    source_digest,
)


def _intent(**overrides: object) -> WholeBankIntent:
    base: dict[str, object] = {
        "source_id": "file:two-banks",
        "source_digest": "d" * 64,
        "mode": "as_is",
        "writes": (
            BankWriteIntent(
                bank_id="RAM3",
                kinds=("patches", "waveseq"),
                source_scope="RAM2",
                source_slots=(1,),
                target_slots=(1,),
                patch_map_digest="p" * 64,
                sequence_map_digest="s" * 64,
            ),
            BankWriteIntent(
                bank_id="CARD",
                kinds=("performances", "patches", "waveseq"),
                source_scope="RAM1",
                source_slots=(0, 2),
                target_slots=(0, 2),
            ),
        ),
    }
    base.update(overrides)
    return WholeBankIntent(**base)  # type: ignore[arg-type]


class TestCanonicalForm:
    def test_equal_plans_have_equal_bytes_and_fingerprint(self):
        first, second = _intent(), _intent()
        assert canonical_bytes(first) == canonical_bytes(second)
        assert fingerprint(first) == fingerprint(second)

    def test_the_encoding_is_versioned_and_readable(self):
        payload = json.loads(canonical_bytes(_intent()))
        assert payload["schema"] == INTENT_SCHEMA
        assert payload["source_id"] == "file:two-banks"
        assert [bank["bank_id"] for bank in payload["writes"]] == ["RAM3", "CARD"]

    def test_kinds_are_normalised_to_bridge_order(self):
        bank = BankWriteIntent(
            bank_id="CARD", kinds=("performances", "waveseq", "patches")
        )
        assert bank.kinds == ("waveseq", "patches", "performances")

    def test_duplicate_or_unknown_kinds_are_refused(self):
        with pytest.raises(ValueError, match="listed twice"):
            BankWriteIntent(bank_id="CARD", kinds=("patches", "patches"))
        with pytest.raises(ValueError, match="unknown write kind"):
            BankWriteIntent(bank_id="CARD", kinds=("multisets",))


class TestEveryMaterialChangeChangesTheFingerprint:
    def _changed(self, **overrides: object) -> bool:
        return fingerprint(_intent(**overrides)) != fingerprint(_intent())

    def test_source_identity_and_content(self):
        assert self._changed(source_id="file:other")
        assert self._changed(source_digest="e" * 64)

    def test_mode_and_choices(self):
        assert self._changed(mode="self_contained_packed")
        assert self._changed(overflow_kind="self_contained")
        assert self._changed(preflight_answer="both")
        assert self._changed(session_id="session-2")
        assert self._changed(pass_slots=(0, 2))

    def test_write_set_and_order(self):
        original = _intent()
        swapped = WholeBankIntent(
            source_id=original.source_id,
            source_digest=original.source_digest,
            mode=original.mode,
            writes=tuple(reversed(original.writes)),
        )
        assert fingerprint(swapped) != fingerprint(original)

    def test_a_bank_changing_kind_bank_or_slots(self):
        original = _intent()
        first = original.writes[0]
        variants = (
            BankWriteIntent(bank_id="RAM2", kinds=first.kinds),
            BankWriteIntent(bank_id=first.bank_id, kinds=("patches",)),
            BankWriteIntent(
                bank_id=first.bank_id, kinds=first.kinds, source_slots=(1, 3)
            ),
            BankWriteIntent(
                bank_id=first.bank_id, kinds=first.kinds, target_slots=(4,)
            ),
            BankWriteIntent(
                bank_id=first.bank_id, kinds=first.kinds, patch_map_digest="x" * 64
            ),
            BankWriteIntent(
                bank_id=first.bank_id,
                kinds=first.kinds,
                sequence_map_digest="y" * 64,
            ),
        )
        for variant in variants:
            changed = WholeBankIntent(
                source_id=original.source_id,
                source_digest=original.source_digest,
                mode=original.mode,
                writes=(variant, original.writes[1]),
            )
            assert fingerprint(changed) != fingerprint(original)


class TestSourceDigest:
    def test_same_records_same_digest(self):
        assert source_digest([b"a", b"b"], [b"p"], b"w") == source_digest(
            [b"a", b"b"], [b"p"], b"w"
        )

    def test_a_boundary_cannot_be_shifted(self):
        assert source_digest([b"ab", b"c"], [], None) != source_digest(
            [b"a", b"bc"], [], None
        )

    def test_a_changed_record_changes_it(self):
        assert source_digest([b"a"], [], None) != source_digest([b"b"], [], None)
        assert source_digest([], [b"p"], None) != source_digest([], [], None)
        assert source_digest([], [], None) != source_digest([], [], b"w")


class TestApproval:
    def test_a_minted_approval_matches_its_plan_only(self):
        intent = _intent()
        approval = new_approval(intent, nonce_factory=lambda: "fixed")
        assert approval_matches(approval, intent)
        assert not approval_matches(approval, _intent(mode="self_contained_1to1"))

    def test_an_empty_fingerprint_never_matches(self):
        assert not approval_matches(PlanApproval(fingerprint="", nonce="n"), _intent())

    def test_nonces_differ_by_default(self):
        first = new_approval(_intent()).nonce
        second = new_approval(_intent()).nonce
        assert first != second


class TestAssignmentMapDigest:
    def test_order_does_not_matter(self):
        assert digest_map([("a", 1), ("b", 2)]) == digest_map([("b", 2), ("a", 1)])

    def test_a_changed_slot_changes_it(self):
        assert digest_map([("a", 1)]) != digest_map([("a", 2)])

    def test_the_schema_constant_is_part_of_the_bytes(self):
        payload = json.loads(canonical_bytes(_intent()))
        payload["schema"] = INTENT_SCHEMA + 1
        assert intent_mod.INTENT_SCHEMA == INTENT_SCHEMA
        assert payload["schema"] != INTENT_SCHEMA
