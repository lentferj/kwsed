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

"""Self-contained whole-bank writes (plan §4/T2). Every name here is invented."""

import pytest

from kws import banks, containment, deps, dumps, perfcopy
from kws import records as r
from tests.test_bankbuild import make_patch, make_performance, make_waveseq

RAM1 = banks.bank("RAM1")
CARD = banks.bank("CARD")
MODE_1TO1 = perfcopy.ImportMode.SELF_CONTAINED_1TO1
MODE_PACKED = perfcopy.ImportMode.SELF_CONTAINED_PACKED


def _source(*, scope_id="RAM1", performances=(), patches=(), waveseq=None):
    return deps.Source(
        scope_id=scope_id,
        performances=list(performances),
        patches=list(patches),
        waveseq=waveseq,
    )


def _plan(sources, scope="RAM1", mode=MODE_PACKED, destination=CARD, **kwargs):
    audit = containment.audit_containment(sources, scope)
    return perfcopy.plan_self_contained(sources, audit, destination, mode, **kwargs)


def _performance(built, slot):
    at = slot * r.PERFORMANCE_SIZE
    return built.performances[at : at + r.PERFORMANCE_SIZE]


def _patch(built, slot):
    at = slot * r.PATCH_SIZE
    return built.patches[at : at + r.PATCH_SIZE]


def _as_written(record, part_targets):
    """``record`` with the used parts repointed, byte for byte.

    The builder leaves every other byte of a performance alone, so the
    expected record is the source's with the part fields replaced. Comparing
    whole records catches a stray bank byte that comparing decoded parts
    would miss.
    """
    out = bytearray(record)
    for index, (bank, patch) in part_targets.items():
        r.set_performance_part(out, index, bank=bank, patch=patch)
    return bytes(out)


def _used_parts(record):
    return [part for part in r.performance_parts(record) if part.used]


class TestSharedPatch:
    """Two performances on one source patch: 1TO1 isolates, PACKED shares."""

    def _sources(self):
        patch = make_patch("Cinder Pair")
        performances = [
            make_performance("Cinder One", ((0, 0),)),
            make_performance("Cinder Two", ((0, 0),)),
        ]
        return {
            "RAM1": _source(performances=performances, patches=[patch]),
        }

    def test_one_to_one_keeps_a_private_copy_per_performance(self):
        sources = self._sources()
        plan = _plan(sources, mode=MODE_1TO1)
        built = perfcopy.build_self_contained(sources, plan)

        assert plan.fits, plan.rejected
        assert plan.patch_count == 2
        assert [alloc.target_perf_slot for alloc in plan.allocations] == [0, 1]

        first = sources["RAM1"].performances[0]
        second = sources["RAM1"].performances[1]
        assert _performance(built, 0) == _as_written(first, {0: (CARD.number, 0)})
        assert _performance(built, 1) == _as_written(second, {0: (CARD.number, 1)})
        assert _patch(built, 0) == sources["RAM1"].patches[0]
        assert _patch(built, 1) == sources["RAM1"].patches[0]

    def test_packed_uses_fewer_patch_slots_than_one_to_one(self):
        sources = self._sources()
        one_to_one = _plan(sources, mode=MODE_1TO1)
        packed = _plan(sources, mode=MODE_PACKED)

        assert packed.patch_count < one_to_one.patch_count

        built = perfcopy.build_self_contained(sources, packed)
        assert packed.fits, packed.rejected
        assert packed.patch_count == 1
        assert [alloc.target_perf_slot for alloc in packed.allocations] == [0, 1]

        # Both performances now name the one shared patch slot, byte for
        # byte -- the second one's part moved from slot 1 to slot 0.
        first = sources["RAM1"].performances[0]
        second = sources["RAM1"].performances[1]
        assert _performance(built, 0) == _as_written(first, {0: (CARD.number, 0)})
        assert _performance(built, 1) == _as_written(second, {0: (CARD.number, 0)})
        assert _patch(built, 0) == sources["RAM1"].patches[0]
        for slot in (0, 1):
            parts = _used_parts(_performance(built, slot))
            assert [part.patch for part in parts] == [0]


class TestWaveSequence:
    def test_reference_relocates_with_its_steps(self):
        waves, starts = make_waveseq({2: [40, 41, 42]})
        patch = make_patch("Kite Weave", ((0, 2), (0, 40), (0, 40), (0, 40)))
        performance = make_performance("Kite Path", ((0, 0),))
        sources = {
            "RAM1": _source(performances=[performance], patches=[patch], waveseq=waves)
        }

        plan = _plan(sources, mode=MODE_PACKED)
        built = perfcopy.build_self_contained(sources, plan)

        assert plan.fits, plan.rejected
        assert plan.sequence_map == {("RAM1", 0, 2): 0}

        oscillator = r.patch_waves(_patch(built, 0))[0]
        assert oscillator.is_sequence
        assert (oscillator.bank, oscillator.wave) == (CARD.number, 0)

        assert built.waveseq is not None
        chain = deps.walk_sequence(built.waveseq, 0, scope_id=CARD.id)
        assert len(chain.steps) == 3
        assert [r.step(built.waveseq, step)["wave"] for step in chain.steps] == [
            40,
            41,
            42,
        ]
        # The chain is where the copied header says it is, at the first
        # step the planner hands out.
        assert r.sequence_header(built.waveseq, 0)["link"] == starts[2]
        # And the source's own sequence slot was not left carrying a copy:
        # the import names slot 0 now.
        assert r.sequence_header(built.waveseq, 2)["link"] == 0


class TestSkippedPerformance:
    def _sources(self):
        return {
            "RAM1": _source(
                performances=[
                    make_performance("Pale Loom", ((0, 0),)),
                    make_performance("Cross Tide", ((1, 1),)),  # RAM 2: external
                    make_performance("Pale Reed", ((0, 1),)),
                    bytes(r.PERFORMANCE_SIZE),
                ],
                patches=[make_patch("Pale Patch"), make_patch("Pale Reed Patch")],
            )
        }

    def test_only_contained_performances_are_chosen(self):
        sources = self._sources()
        audit = containment.audit_containment(sources, "RAM1")
        assert audit.contained == 2
        assert perfcopy.contained_slots(audit) == [0, 2]

    def test_one_to_one_cleans_the_skipped_slot(self):
        sources = self._sources()
        plan = _plan(sources, mode=MODE_1TO1)
        # The source bank as the write's template: the skipped performance
        # is sitting in slot 1 and must not survive the build.
        template = b"".join(sources["RAM1"].performances)
        built = perfcopy.build_self_contained(
            sources, plan, template_performances=template
        )

        assert plan.fits, plan.rejected
        assert [alloc.target_perf_slot for alloc in plan.allocations] == [0, 2]

        first = sources["RAM1"].performances[0]
        third = sources["RAM1"].performances[2]
        assert _performance(built, 0) == _as_written(first, {0: (CARD.number, 0)})
        assert _performance(built, 2) == _as_written(third, {0: (CARD.number, 1)})

        skipped = _performance(built, 1)
        assert perfcopy.is_empty_performance(skipped)
        assert _used_parts(skipped) == []
        assert dumps.record_name(skipped).strip() == ""
        assert perfcopy.is_empty_performance(_performance(built, 3))

    def test_packed_shrinks_the_layout(self):
        sources = self._sources()
        plan = _plan(sources, mode=MODE_PACKED)
        template = b"".join(sources["RAM1"].performances)
        built = perfcopy.build_self_contained(
            sources, plan, template_performances=template
        )

        assert plan.fits, plan.rejected
        assert [alloc.target_perf_slot for alloc in plan.allocations] == [0, 1]

        first = sources["RAM1"].performances[0]
        third = sources["RAM1"].performances[2]
        assert _performance(built, 0) == _as_written(first, {0: (CARD.number, 0)})
        assert _performance(built, 1) == _as_written(third, {0: (CARD.number, 1)})
        # Slot 2 held a chosen performance in the template; packed moved it
        # down, so the slot must come out clean rather than duplicated.
        assert perfcopy.is_empty_performance(_performance(built, 2))
        assert _patch(built, 0) == sources["RAM1"].patches[0]
        assert _patch(built, 1) == sources["RAM1"].patches[1]

        names = [
            dumps.record_name(_performance(built, slot)).strip()
            for slot in range(banks.PERFORMANCES_PER_BANK)
        ]
        assert names[:3] == ["Pale Loom", "Pale Reed", ""]
        assert "Cross Tide" not in names


class TestPerfSlots:
    def _sources(self):
        return {
            "RAM1": _source(
                performances=[make_performance("Wren Field", ((0, 0),))],
                patches=[make_patch("Wren Patch")],
            )
        }

    def test_override_places_a_performance_elsewhere(self):
        sources = self._sources()
        plan = _plan(sources, mode=MODE_1TO1, perf_slots={0: 40})
        built = perfcopy.build_self_contained(sources, plan)

        assert plan.fits, plan.rejected
        assert [alloc.target_perf_slot for alloc in plan.allocations] == [40]
        assert _performance(built, 40) == _as_written(
            sources["RAM1"].performances[0], {0: (CARD.number, 0)}
        )
        assert perfcopy.is_empty_performance(_performance(built, 0))

    def test_override_missing_a_chosen_slot_says_so(self):
        sources = self._sources()
        audit = containment.audit_containment(sources, "RAM1")
        with pytest.raises(ValueError):
            perfcopy.plan_self_contained(
                sources, audit, CARD, MODE_1TO1, perf_slots={7: 0}
            )


def test_as_is_is_not_a_plan():
    sources = {
        "RAM1": _source(
            performances=[make_performance("Wren Field", ((0, 0),))],
            patches=[make_patch("Wren Patch")],
        )
    }
    audit = containment.audit_containment(sources, "RAM1")
    with pytest.raises(ValueError):
        perfcopy.plan_self_contained(sources, audit, CARD, perfcopy.ImportMode.AS_IS)


class TestOverflowImport:
    """External patches go to a second bank; the parts are repointed there."""

    def _sources(self):
        # RAM 1: two performances, each naming one own patch and one RAM 2
        # patch. RAM 2's patch 0 names a wave sequence that lives in RAM 1 --
        # the cross-bank reference the holder resolution exists for.
        seq = make_waveseq({0: [40, 41]})[0]
        own = [make_patch("Local One"), make_patch("Local Two")]
        foreign = make_patch("Foreign One")
        buf = bytearray(foreign)
        r.set_patch_wave(buf, 0, bank=0, wave=0)
        foreign = bytes(buf)
        performances = [
            make_performance("Overflow One", ((0, 0), (1, 0))),
            make_performance("Overflow Two", ((0, 1), (1, 0))),
        ]
        sources = {
            "RAM1": deps.Source(
                scope_id="RAM1",
                performances=performances,
                patches=own,
                waveseq=seq,
            ),
            "RAM2": deps.Source(
                scope_id="RAM2", performances=[], patches=[foreign], waveseq=None
            ),
        }
        return sources, foreign

    def test_every_performance_travels_and_parts_are_repointed(self):
        sources, foreign = self._sources()
        plan = perfcopy.plan_overflow_import(
            sources, "RAM1", CARD, banks.bank("RAM3"), mode=MODE_1TO1
        )
        assert plan.fits, plan.rejected
        built = perfcopy.build_overflow_import(sources, plan)
        assert len(plan.atomic.allocations) == 2
        for slot in (0, 1):
            parts = _used_parts(_performance(built.destination, slot))
            assert parts[0].bank == CARD.number  # own patch -> the card
            assert parts[1].bank == banks.bank("RAM3").number
            assert parts[1].patch == 0  # one shared overflow slot
        assert built.repointed_parts == 2
        assert built.patch_count == 1
        at = 0
        assert built.overflow_patches[at : at + r.PATCH_SIZE] == _as_wave_bank(
            foreign, banks.bank("RAM3").number
        )
        # The card's own patches travelled too.
        assert _patch(built.destination, 0) == make_patch("Local One")

    def test_overflow_patch_reads_the_bank_its_sequence_names(self):
        """A RAM 2 patch naming a RAM 1 sequence gets RAM 1's bytes."""
        sources, _foreign = self._sources()
        plan = perfcopy.plan_overflow_import(
            sources, "RAM1", CARD, banks.bank("RAM3"), mode=MODE_1TO1
        )
        built = perfcopy.build_overflow_import(sources, plan)
        assert built.overflow_waveseq is not None
        header = r.sequence_header(built.overflow_waveseq, 0)
        assert header["link"] != 0
        step = r.step(built.overflow_waveseq, header["link"])
        assert step["wave"] == 40  # the RAM 1 sequence's first step

    def test_an_overflow_bank_of_the_target_is_refused(self):
        sources, _foreign = self._sources()
        with pytest.raises(ValueError, match="overflow bank is the destination"):
            perfcopy.plan_overflow_import(sources, "RAM1", CARD, CARD)


def _as_wave_bank(record, bank):
    """``record`` with every wave-sequence reference repointed to ``bank``."""
    out = bytearray(record)
    for wave in r.patch_waves(out):
        if wave.is_sequence:
            r.set_patch_wave(out, wave.index, bank=bank, wave=wave.wave)
    return bytes(out)
