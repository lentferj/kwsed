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
from typing import Any, Dict, Tuple, cast

import pytest

from kws import banks as kws_banks
from kws import deps
from kws import records as r
from kws.perfcopy import ImportMode
from kwsed import import_intent as intent_mod
from kwsed.import_intent import (
    ALL_KINDS,
    INTENT_SCHEMA,
    BankWriteIntent,
    PlanApproval,
    WholeBankIntent,
    approval_matches,
    build_intent,
    canonical_bytes,
    companion_write_patches_only,
    digest_map,
    fingerprint,
    new_approval,
    source_digest,
)
from tests.test_bankbuild import make_patch, make_performance


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


def _file(
    *, with_ram2: bool = True, waveseq: bool = False
) -> Tuple[Dict[str, deps.Source], deps.Source]:
    """A one-bank source: two contained performances, one naming RAM 2.

    Slot 0 and slot 1 use their own bank's patches; slot 2 names RAM 2's
    patch 0, which is loaded only when ``with_ram2`` asks for it. Every
    name here is invented.
    """
    performances = [bytes(r.PERFORMANCE_SIZE)] * 8
    performances[0] = make_performance("Harbour Belle", ((0, 0),))
    performances[1] = make_performance("Inlet Drift", ((0, 1),))
    performances[2] = make_performance("Crossing Deck", ((1, 0),))
    source = deps.Source(
        scope_id="RAM1",
        performances=performances,
        patches=[make_patch(f"Ashgrove Tone {index:02d}") for index in range(4)],
        waveseq=bytes(r.WAVESEQ_BANK_SIZE) if waveseq else None,
    )
    sources: Dict[str, deps.Source] = {"RAM1": source}
    if with_ram2:
        sources["RAM2"] = deps.Source(
            scope_id="RAM2",
            performances=[],
            patches=[make_patch("Quay Slate")],
            waveseq=None,
        )
    return sources, source


class TestBuildIntent:
    """The shared builder: one choice, planned by the real planners."""

    def _args(self, **overrides: Any) -> Dict[str, Any]:
        with_ram2 = overrides.pop("with_ram2", True)
        sources, source = _file(with_ram2=with_ram2)
        base: Dict[str, Any] = {
            "source_id": "file:one",
            "source_scope": "RAM1",
            "sources": sources,
            "source": source,
            "target": kws_banks.bank("RAM3"),
            "mode": ImportMode.AS_IS,
        }
        base.update(overrides)
        return base

    def test_plain_as_is_names_the_target_with_all_three_kinds(self):
        args = self._args()
        intent = build_intent(**args)
        write = intent.writes[0]
        assert len(intent.writes) == 1
        assert write.bank_id == "RAM3"
        assert write.kinds == ("waveseq", "patches", "performances")
        assert write.source_slots == (0, 1, 2)
        # As-is keeps each performance at its own slot.
        assert write.target_slots == (0, 1, 2)
        assert write.patch_map_digest == digest_map(())
        assert write.sequence_map_digest == digest_map(())
        assert intent.mode == "as_is"
        assert intent.overflow_kind == ""
        assert intent.source_id == "file:one"
        assert intent.source_digest == source_digest(
            args["source"].performances, args["source"].patches, None
        )

    def test_patches_overflow_writes_the_overflow_bank_first(self):
        intent = build_intent(
            **self._args(
                mode=ImportMode.SELF_CONTAINED_PACKED,
                overflow_kind="patches",
                overflow_bank="RAM2",
            )
        )
        assert [write.bank_id for write in intent.writes] == ["RAM2", "RAM3"]
        overflow, target = intent.writes
        assert overflow.kinds == ("waveseq", "patches")
        assert overflow.patch_map_digest == digest_map([("RAM2:0", 0)])
        assert overflow.sequence_map_digest == digest_map(())
        assert target.kinds == ALL_KINDS
        assert target.source_slots == (0, 1, 2)
        # Packed re-slots the performances 0..n-1.
        assert target.target_slots == (0, 1, 2)
        assert target.patch_map_digest == digest_map([("RAM1:0", 0), ("RAM1:1", 1)])
        assert intent.overflow_kind == "patches"

    def test_self_contained_split_writes_target_then_overflow(self):
        intent = build_intent(
            **self._args(overflow_kind="self_contained", overflow_bank="RAM2")
        )
        assert [write.bank_id for write in intent.writes] == ["RAM3", "RAM2"]
        target, overflow = intent.writes
        assert target.kinds == ALL_KINDS and overflow.kinds == ALL_KINDS
        # The contained performances go to the target, the others to the
        # overflow bank; as-is means each keeps its own slot.
        assert target.source_slots == (0, 1)
        assert target.target_slots == (0, 1)
        assert overflow.source_slots == (2,)
        assert overflow.target_slots == (2,)
        assert intent.overflow_kind == "self_contained"

    def test_cards_only_writes_the_first_group_into_the_card_slot(self):
        # The selector's RAM bank must not leak into a cards-only write.
        intent = build_intent(
            **self._args(overflow_kind="cards_only", target=kws_banks.bank("RAM1"))
        )
        write = intent.writes[0]
        assert len(intent.writes) == 1
        assert write.bank_id == "CARD"
        assert write.kinds == ALL_KINDS
        assert write.source_slots == (0, 1, 2)
        assert write.target_slots == (0, 1, 2)
        assert intent.overflow_kind == "cards_only"

    def test_companion_writes_patches_and_waveseq_only(self):
        sources, source = _file(waveseq=True)
        intent = build_intent(
            source_id="file:one",
            source_scope="RAM1",
            sources=sources,
            source=source,
            target=kws_banks.bank("RAM2"),
            mode=ImportMode.AS_IS,
            companion_only=True,
        )
        write = intent.writes[0]
        assert len(intent.writes) == 1
        assert write.bank_id == "RAM2"
        assert write.kinds == ("waveseq", "patches")
        assert write.source_slots == () and write.target_slots == ()
        assert write.patch_map_digest == digest_map(())

    def test_a_companion_without_a_wave_bank_sends_patches_only(self):
        sources, source = _file()
        intent = build_intent(
            source_id="file:one",
            source_scope="RAM1",
            sources=sources,
            source=source,
            target=kws_banks.bank("RAM2"),
            mode=ImportMode.AS_IS,
            companion_only=True,
        )
        assert intent.writes[0].kinds == ("patches",)

    def test_the_all_init_companion_branch_records_all_three_kinds(self):
        """The whole-bank branch of an ``x`` choice names every kind (P4).

        When the loaded copy of the companion bank is empty of
        performances, the bank travels whole: the token must say so, not
        promise patches and wave sequences only.
        """
        sources, source = _file(waveseq=True)
        intent = build_intent(
            source_id="file:one",
            source_scope="RAM1",
            sources=sources,
            source=source,
            target=kws_banks.bank("RAM2"),
            mode=ImportMode.AS_IS,
            companion_only=True,
            companion_all_init=True,
        )
        write = intent.writes[0]
        assert len(intent.writes) == 1
        assert write.bank_id == "RAM2"
        assert write.kinds == ALL_KINDS
        assert intent.pass_slots == ()

    def test_the_two_companion_branches_never_share_a_fingerprint(self):
        """Patches-only and all-Init ``x`` writes are different plans."""
        sources, source = _file(waveseq=True)
        common: Dict[str, Any] = {
            "source_id": "file:one",
            "source_scope": "RAM1",
            "sources": sources,
            "source": source,
            "target": kws_banks.bank("RAM2"),
            "mode": ImportMode.AS_IS,
            "companion_only": True,
        }
        patches_only = build_intent(**common)
        whole_bank = build_intent(**common, companion_all_init=True)
        assert patches_only.writes[0].kinds != whole_bank.writes[0].kinds
        assert fingerprint(patches_only) != fingerprint(whole_bank)

    def test_a_continuation_pass_plans_the_first_packed_group(self):
        intent = build_intent(
            **self._args(
                mode=ImportMode.SELF_CONTAINED_PACKED,
                pass_slots=(1, 2),
                session_id="file:one|RAM1|pass",
            )
        )
        write = intent.writes[0]
        assert len(intent.writes) == 1
        assert write.bank_id == "RAM3"
        assert write.kinds == ALL_KINDS
        assert write.source_slots == (1, 2)
        assert write.target_slots == (0, 1)
        assert intent.pass_slots == (1, 2)
        assert intent.session_id == "file:one|RAM1|pass"

    def test_identical_choices_fingerprint_identically(self):
        flows: Tuple[Dict[str, Any], ...] = (
            {},
            {
                "mode": ImportMode.SELF_CONTAINED_PACKED,
                "overflow_kind": "patches",
                "overflow_bank": "RAM2",
            },
            {"overflow_kind": "self_contained", "overflow_bank": "RAM2"},
            {"overflow_kind": "cards_only"},
            {
                "mode": ImportMode.SELF_CONTAINED_PACKED,
                "pass_slots": (1, 2),
            },
        )
        for flow in flows:
            first = build_intent(**self._args(**flow))
            second = build_intent(**self._args(**flow))
            assert fingerprint(first) == fingerprint(second)

    def test_a_material_change_changes_the_fingerprint(self):
        base = build_intent(
            **self._args(
                target=kws_banks.bank("RAM1"),
                overflow_kind="patches",
                overflow_bank="RAM2",
            )
        )
        variants = (
            self._args(
                target=kws_banks.bank("RAM1"),
                overflow_kind="patches",
                overflow_bank="RAM3",
            ),
            self._args(
                target=kws_banks.bank("RAM1"),
                overflow_kind="self_contained",
                overflow_bank="RAM2",
            ),
            self._args(
                target=kws_banks.bank("CARD"),
                overflow_kind="patches",
                overflow_bank="RAM2",
            ),
            self._args(
                target=kws_banks.bank("RAM1"),
                overflow_kind="patches",
                overflow_bank="RAM2",
                preflight_answer="both",
            ),
        )
        for variant in variants:
            assert fingerprint(build_intent(**variant)) != fingerprint(base)

    def test_an_overflow_kind_without_a_bank_is_no_overflow(self):
        # The choice's default kind with no bank chosen is a plain write;
        # the approval describes the effect, not the menu state.
        plain = build_intent(**self._args())
        defaulted = build_intent(**self._args(overflow_kind="patches"))
        assert fingerprint(plain) == fingerprint(defaulted)
        assert defaulted.overflow_kind == ""

    def test_an_unplannable_split_is_refused_with_the_planners_words(self):
        with pytest.raises(ValueError, match="not loaded"):
            build_intent(
                **self._args(
                    with_ram2=False,
                    overflow_kind="self_contained",
                    overflow_bank="RAM2",
                )
            )

    def test_the_intent_and_approval_survive_a_round_trip(self):
        intent = build_intent(
            **self._args(overflow_kind="cards_only", session_id="one")
        )
        approval = new_approval(intent, nonce_factory=lambda: "fixed")
        assert approval_matches(approval, intent)
        again = build_intent(**self._args(overflow_kind="cards_only", session_id="one"))
        assert approval_matches(approval, again)
        other = build_intent(**self._args(session_id="one"))
        assert not approval_matches(approval, other)


def _simple_namespace_source() -> Any:
    """A source shaped like the screen's own fixtures (SimpleNamespace)."""
    from types import SimpleNamespace

    return SimpleNamespace(
        performances=[bytes([1]) * r.PERFORMANCE_SIZE, bytes(r.PERFORMANCE_SIZE)],
        patches=[bytes([2]) * r.PATCH_SIZE],
        waveseq=None,
    )


class TestBuildIntentOnScreenShapedSources:
    """The screen's loose sources build the same intents deps.Source does."""

    def test_a_plain_as_is_intent_from_a_namespace_source(self):
        source = cast(Any, _simple_namespace_source())
        sources = {"RAM1": source}
        first = build_intent(
            source_id="foo.syx|RAM1",
            source_scope="RAM1",
            sources=cast(Dict[str, deps.Source], sources),
            source=source,
            target=kws_banks.bank("RAM1"),
            mode=ImportMode.AS_IS,
        )
        second = build_intent(
            source_id="foo.syx|RAM1",
            source_scope="RAM1",
            sources=cast(Dict[str, deps.Source], sources),
            source=source,
            target=kws_banks.bank("RAM1"),
            mode=ImportMode.AS_IS,
        )
        assert first.writes[0].source_slots == (0,)
        assert fingerprint(first) == fingerprint(second)


class TestCompanionBranchRule:
    """The one rule that decides an ``x`` choice's branch (P4).

    The screen and the application must decide the branch the same way,
    from the same loaded copies, or the token names a plan the write does
    not follow. These records are invented, like every other fixture.
    """

    def test_a_bank_that_is_not_loaded_counts_as_not_empty(self):
        assert companion_write_patches_only(None) is True

    def test_a_loaded_bank_of_init_slots_travels_whole(self):
        from tests.test_bankbuild import make_performance

        performances = [bytes(r.PERFORMANCE_SIZE)] * 50
        performances[0] = bytes(r.PERFORMANCE_SIZE)
        assert companion_write_patches_only(performances) is False

    def test_a_loaded_bank_with_real_work_keeps_its_performances(self):
        performances = [bytes(r.PERFORMANCE_SIZE)] * 50
        performances[4] = make_performance("Anchor Line", ((0, 0),))
        assert companion_write_patches_only(performances) is True

    def test_a_copy_with_no_performances_at_all_is_treated_as_loaded(self):
        assert companion_write_patches_only([]) is True

    def test_an_unparseable_record_is_carried_when_it_holds_bytes(self):
        assert companion_write_patches_only([bytes([9]) * 8]) is True
