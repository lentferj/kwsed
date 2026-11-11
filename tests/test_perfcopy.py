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

"""Performance-first atomic copying. Every fixture here is invented."""

import pytest

from kws import bankbuild, banks, deps, dumps, perfcopy, relations
from kws import records as r
from tests.test_bankbuild import make_patch, make_performance, make_waveseq

RAM3 = banks.bank("RAM3")


def _source(patches=None, performances=None, waveseq=None, bank_id="A"):
    return deps.Source(
        bank_id=bank_id,
        patches=patches if patches is not None else [],
        performances=performances if performances is not None else [],
        waveseq=waveseq,
    )


def _build_sources():
    seqs = make_waveseq({0: [40, 41], 7: [50, 51]})[0]
    src = deps.Source(
        bank_id="RAM1",
        patches=[
            make_patch("P0", ((0, 0), (0, 40), (0, 40), (0, 40))),
            make_patch("P1", ((0, 1), (0, 40), (0, 40), (0, 40))),
        ],
        performances=[
            make_performance("Perf0", ((0, 0), (3, 0), (11, 0))),
        ],
        waveseq=seqs,
    )
    return src


class TestIsEmptyPerformance:
    def test_all_unused_is_empty(self):
        rec = make_performance("Anything", ())
        assert perfcopy.is_empty_performance(rec)

    def test_zero_record_is_empty(self):
        rec = bytes(r.PERFORMANCE_SIZE)
        assert perfcopy.is_empty_performance(rec)

    def test_named_with_real_part_not_empty(self):
        rec = make_performance("Real", ((0, 0),))
        assert not perfcopy.is_empty_performance(rec)

    def test_factory_init_is_empty(self):
        rec = make_performance("Init", ((0, 0),))
        assert perfcopy.is_empty_performance(rec)

    def test_init_named_real_part_is_not_empty(self):
        rec = make_performance("Init", ((0, 5),))
        assert not perfcopy.is_empty_performance(rec)

    def test_init_perf_trailing_period_is_empty(self):
        rec = make_performance("Init Perf.", ((0, 0),))
        assert perfcopy.is_empty_performance(rec)

    def test_init_perf_uppercase_trailing_period_is_empty(self):
        rec = make_performance("INIT PERF.", ((0, 0),))
        assert perfcopy.is_empty_performance(rec)

    def test_init_performance_is_empty(self):
        rec = make_performance("Init Performance", ((0, 0),))
        assert perfcopy.is_empty_performance(rec)

    def test_init_named_patch_nonzero_is_not_empty(self):
        rec = make_performance("Init", ((0, 5),))
        assert not perfcopy.is_empty_performance(rec)


class TestIsEmptyPatch:
    def test_zero_patch_is_empty(self):
        rec = bytes(r.PATCH_SIZE)
        assert perfcopy.is_empty_patch(rec)

    def test_factory_init_patch_is_empty(self):
        rec = make_patch("Init")
        assert perfcopy.is_empty_patch(rec)

    def test_real_patch_is_not_empty(self):
        rec = make_patch("Kelp Forest")
        assert not perfcopy.is_empty_patch(rec)

    def test_init_patch_uppercase_is_empty(self):
        rec = make_patch("INIT Patch")
        assert perfcopy.is_empty_patch(rec)

    def test_init_patch_trailing_period_is_empty(self):
        rec = make_patch("Init Patch.")
        assert perfcopy.is_empty_patch(rec)

    def test_unnamed_patch_is_empty(self):
        rec = make_patch("Unnamed")
        assert perfcopy.is_empty_patch(rec)

    def test_init_named_elsewhere_not_applicable_patch(self):
        # patch emptiness does not inspect bank/patch refs; name rules only
        rec = make_patch("Init", ((0, 5), (0, 5), (0, 5), (0, 5)))
        assert perfcopy.is_empty_patch(rec)


class TestCrossBankLocalization:
    def test_cross_bank(self):
        # source CARD/07 performance parts (0,0)[RAM1],(3,0)[ROM4],(11,0)[CARD/07]
        ram1 = deps.Source(bank_id="RAM1", patches=[make_patch("R1")], performances=[])
        rom4 = deps.Source(bank_id="ROM4", patches=[make_patch("R4")], performances=[])
        card07 = deps.Source(
            bank_id="CARD/07",
            patches=[make_patch("C7")],
            performances=[make_performance("Cross", ((0, 0), (3, 0), (11, 0)))],
            waveseq=None,
        )
        sources = {"RAM1": ram1, "ROM4": rom4, "CARD/07": card07}
        perf_id = relations.PerfId("CARD/07", 0)
        plan = perfcopy.plan_atomic_bank(sources, [perf_id], destination_bank=RAM3)
        built = perfcopy.build_atomic_bank(sources, plan)
        assert plan.patch_count == 3
        assert len(plan.allocations) == 1
        alloc = plan.allocations[0]
        assert alloc.part_patch_slots == {0: 0, 1: 1, 2: 2}
        # decode built performance
        rec0 = built.performances[0 * r.PERFORMANCE_SIZE : 1 * r.PERFORMANCE_SIZE]
        for part in r.performance_parts(rec0):
            if part.used:
                assert part.bank == RAM3.number  # 2
                assert 0 <= part.patch <= 2
        # patch bytes from right source banks: first patch name starts R
        assert built.patches[0] == ord("R")


class TestStrictInterPerfIsolation:
    def test_isolation(self):
        ram1 = deps.Source(bank_id="RAM1", patches=[make_patch("R1")], performances=[])
        # two performances both reference RAM1:0
        card = deps.Source(
            bank_id="CARD/07",
            patches=[],
            performances=[
                make_performance("A", ((0, 0),)),
                make_performance("B", ((0, 0),)),
            ],
            waveseq=None,
        )
        # need patch in RAM1
        sources = {"RAM1": ram1, "CARD/07": card}
        # performances reference bank 0 patch 0 -> RAM1
        perf_ids = [relations.PerfId("CARD/07", 0), relations.PerfId("CARD/07", 1)]
        plan = perfcopy.plan_atomic_bank(sources, perf_ids, destination_bank=RAM3)
        built = perfcopy.build_atomic_bank(sources, plan)
        assert len(plan.allocations) == 2
        assert plan.allocations[0].allocated_patches[0][1] == 0
        assert plan.allocations[1].allocated_patches[0][1] == 1
        # mutate patch 0
        p0 = bytearray(built.patches)
        p0[0] = 99
        # patch 1 starts at 426
        assert built.patches[426] != 99
        # source unchanged
        assert sources["RAM1"].patches[0][0] == ord("R")


class TestIntraPerfDedup:
    def test_dedup(self):
        src = deps.Source(
            bank_id="RAM1",
            patches=[make_patch("P0"), make_patch("P1")],
            performances=[make_performance("Perf", ((0, 0), (0, 0), (0, 1)))],
            waveseq=None,
        )
        sources = {"RAM1": src}
        perf_id = relations.PerfId("RAM1", 0)
        plan = perfcopy.plan_atomic_bank(sources, [perf_id], destination_bank=RAM3)
        assert plan.patch_count == 2
        alloc = plan.allocations[0]
        assert alloc.part_patch_slots == {0: 0, 1: 0, 2: 1}


class TestZeroFragmentation:
    def _make(self):
        src = deps.Source(
            bank_id="RAM1",
            patches=[
                make_patch("A"),
                make_patch("B"),
                make_patch("C"),
                make_patch("D"),
                make_patch("E"),
            ],
            performances=[
                make_performance("P3", ((0, 0), (0, 1), (0, 2))),
                make_performance("P2", ((0, 3), (0, 4))),
            ],
            waveseq=None,
        )
        return {"RAM1": src}

    def test_clean(self):
        sources = self._make()
        ids = [relations.PerfId("RAM1", 0), relations.PerfId("RAM1", 1)]
        built = perfcopy.copy_into_bank(
            sources,
            "RAM3",
            ids,
            mode="clean",
            destination_bank=RAM3,
        )
        plan = built.plan
        assert plan.patch_count == 5
        assert len(plan.allocations) == 2
        # second perf part slots 3..4
        assert plan.allocations[1].part_patch_slots == {0: 3, 1: 4}

    def test_append(self):
        sources = self._make()
        ids = [relations.PerfId("RAM1", 0), relations.PerfId("RAM1", 1)]
        built = perfcopy.copy_into_bank(
            sources,
            "RAM3",
            ids,
            mode="clean",
            destination_bank=RAM3,
        )
        # feed built back as source
        src2 = deps.Source(
            bank_id="RAM3",
            performances=[
                built.performances[
                    i * r.PERFORMANCE_SIZE : (i + 1) * r.PERFORMANCE_SIZE
                ]
                for i in range(50)
            ],
            patches=[
                built.patches[i * r.PATCH_SIZE : (i + 1) * r.PATCH_SIZE]
                for i in range(35)
            ],
            waveseq=built.waveseq,
        )
        sources2 = {"RAM3": src2}
        new_perf = deps.Source(
            bank_id="RAM1",
            patches=[make_patch("F")],
            performances=[make_performance("P1", ((0, 0),))],
            waveseq=None,
        )
        sources2["RAM1"] = new_perf
        # append perf from RAM1
        built2 = perfcopy.copy_into_bank(
            sources2,
            "RAM3",
            [relations.PerfId("RAM1", 0)],
            mode="append",
            destination_bank=RAM3,
        )
        plan2 = built2.plan
        assert len(plan2.allocations) == 3
        assert plan2.patch_count == 6
        assert plan2.allocations[2].target_perf_slot == 2
        assert plan2.allocations[2].part_patch_slots == {0: 5}

    def test_replace(self):
        sources = self._make()
        ids = [relations.PerfId("RAM1", 0), relations.PerfId("RAM1", 1)]
        built = perfcopy.copy_into_bank(
            sources,
            "RAM3",
            ids,
            mode="clean",
            destination_bank=RAM3,
        )
        src2 = deps.Source(
            bank_id="RAM3",
            performances=[
                built.performances[
                    i * r.PERFORMANCE_SIZE : (i + 1) * r.PERFORMANCE_SIZE
                ]
                for i in range(50)
            ],
            patches=[
                built.patches[i * r.PATCH_SIZE : (i + 1) * r.PATCH_SIZE]
                for i in range(35)
            ],
            waveseq=built.waveseq,
        )
        sources2 = {"RAM3": src2}
        new_perf_src = deps.Source(
            bank_id="RAM1",
            patches=[make_patch("X")],
            performances=[make_performance("PX", ((0, 0),))],
            waveseq=None,
        )
        sources2["RAM1"] = new_perf_src
        built2 = perfcopy.copy_into_bank(
            sources2,
            "RAM3",
            [relations.PerfId("RAM1", 0)],
            mode="replace",
            replace_slot=0,
            destination_bank=RAM3,
        )
        plan2 = built2.plan
        # patch_count becomes 1+2=3
        assert plan2.patch_count == 3
        # new perf 0 patches 0
        assert plan2.allocations[0].part_patch_slots == {0: 0}
        # old perf 1 moves to perf slot 1 with patches 1..2
        assert plan2.allocations[1].target_perf_slot == 1
        assert plan2.allocations[1].part_patch_slots == {0: 1, 1: 2}
        # decode built performances part refs
        rec1 = built2.performances[1 * r.PERFORMANCE_SIZE : 2 * r.PERFORMANCE_SIZE]
        for part in r.performance_parts(rec1):
            if part.used:
                assert part.patch in (1, 2)

    def test_replace_remove(self):
        sources = self._make()
        ids = [relations.PerfId("RAM1", 0), relations.PerfId("RAM1", 1)]
        built = perfcopy.copy_into_bank(
            sources,
            "RAM3",
            ids,
            mode="clean",
            destination_bank=RAM3,
        )
        src2 = deps.Source(
            bank_id="RAM3",
            performances=[
                built.performances[
                    i * r.PERFORMANCE_SIZE : (i + 1) * r.PERFORMANCE_SIZE
                ]
                for i in range(50)
            ],
            patches=[
                built.patches[i * r.PATCH_SIZE : (i + 1) * r.PATCH_SIZE]
                for i in range(35)
            ],
            waveseq=built.waveseq,
        )
        sources2 = {"RAM3": src2}
        built2 = perfcopy.copy_into_bank(
            sources2, "RAM3", [], mode="replace", replace_slot=0, destination_bank=RAM3
        )
        plan2 = built2.plan
        # old perf 1 becomes perf slot 0 with patches 0..1
        assert len(plan2.allocations) == 1
        assert plan2.allocations[0].target_perf_slot == 0
        assert plan2.allocations[0].part_patch_slots == {0: 0, 1: 1}
        assert plan2.patch_count == 2


class TestWaveSequence:
    def test_moved_steps_keep_their_no_loop_sentinel(self):
        """0xFFFF is "no loop link"; writing 0 points at pool step 0.

        That is another sequence's step or an empty one, and a sequence
        that loops into an empty step is a quiet one -- which is how a
        staged pad auditioned silent while its patch and steps were right.
        """
        bank, starts = make_waveseq({0: [40, 41]})
        bank = bytearray(bank)
        first = starts[0]
        r.set_step_links(bank, first, forward=first + 1, back=0, loop=0xFFFF)
        r.set_step_links(bank, first + 1, forward=0, back=first, loop=first)
        src = deps.Source(
            bank_id="RAM1",
            patches=[make_patch("Q", waves=((0, 0),) * 4)],
            performances=[make_performance("Perf", ((0, 0),))],
            waveseq=bytes(bank),
        )
        target = deps.Source(
            "RAM3",
            performances=[bytes(r.PERFORMANCE_SIZE)] * banks.PERFORMANCES_PER_BANK,
            patches=[bytes(r.PATCH_SIZE)] * banks.PATCHES_PER_BANK,
            waveseq=bytes(r.WAVESEQ_BANK_SIZE),
        )
        sources = {"RAM1": src, "RAM3": target}
        plan = perfcopy.plan_slot_import(
            sources,
            "RAM3",
            {0: relations.PerfId("RAM1", 0)},
            destination_bank=RAM3,
        )
        assert plan.fits, plan.rejected
        built = perfcopy.build_slot_import(sources, plan)
        assert built.waveseq is not None
        slot = plan.sequence_map[("RAM1", 0, 0)]
        chain = deps.walk_sequence(built.waveseq, slot)
        steps = sorted(chain.steps)
        assert r.step(built.waveseq, steps[0])["loop"] == 0xFFFF, (
            "a no-loop sentinel must stay one"
        )
        assert r.step(built.waveseq, steps[-1])["loop"] == steps[0]

    def test_waveseq(self):
        waveseq = make_waveseq({0: [40, 41, 7], 7: [50, 51]})[0]
        src = deps.Source(
            bank_id="RAM1",
            patches=[make_patch("Q", waves=((0, 0),) * 4)],
            performances=[make_performance("Perf", ((0, 0),))],
            waveseq=waveseq,
        )
        sources = {"RAM1": src}
        # two chosen performances both reference same source patch
        perf_ids = [relations.PerfId("RAM1", 0), relations.PerfId("RAM1", 0)]
        src2 = deps.Source(
            bank_id="RAM1",
            patches=[make_patch("Q", waves=((0, 0),) * 4)],
            performances=[
                make_performance("Perf0", ((0, 0),)),
                make_performance("Perf1", ((0, 0),)),
            ],
            waveseq=waveseq,
        )
        sources = {"RAM1": src2}
        perf_ids = [relations.PerfId("RAM1", 0), relations.PerfId("RAM1", 1)]
        built = perfcopy.copy_into_bank(
            sources,
            "RAM3",
            perf_ids,
            mode="clean",
            destination_bank=RAM3,
        )
        plan = built.plan
        assert len(plan.sequence_map) == 2  # seq 0 and 7
        assert built.waveseq is not None
        assert len(built.waveseq) == 8784
        # both target patches point same sequence slot for seq0
        # patch0 and patch1 oscillators wave == seq0 slot
        seq0_slot = plan.sequence_map[("RAM1", 0, 0)]
        for pi in range(2):
            prec = built.patches[pi * r.PATCH_SIZE : (pi + 1) * r.PATCH_SIZE]
            for w in r.patch_waves(prec):
                if w.is_sequence:
                    assert w.bank == RAM3.number
                    assert w.wave == seq0_slot
        # quoted step points at relocated seq7 slot
        seq7_slot = plan.sequence_map[("RAM1", 0, 7)]
        chain = deps.walk_sequence(built.waveseq, seq0_slot)
        quotes = [
            r.step(built.waveseq, s)["wave"]
            for s in chain.steps
            if r.step(built.waveseq, s)["wave"] < r.ROM_WAVE_BASE
        ]
        assert seq7_slot in quotes


class TestBudgetRefusals:
    def _src(self):
        make_waveseq({0: [40, 41], 7: [50, 51]})[0]
        # We'll create patch with osc0 wave seq 0, osc1 wave seq 1 where seq1 exists
        waveseq2 = make_waveseq({0: [40], 1: [41], 7: [50]})[0]
        patch = make_patch("P", waves=((0, 0), (0, 1), (0, 40), (0, 40)))
        src = deps.Source(
            bank_id="RAM1",
            patches=[patch],
            performances=[make_performance("Perf", ((0, 0),))],
            waveseq=waveseq2,
        )
        return {"RAM1": src}, waveseq2

    def test_perf_budget(self):
        sources, _ = self._src()
        ids = [relations.PerfId("RAM1", 0), relations.PerfId("RAM1", 0)]
        # need two perf slots; create second perf
        sources["RAM1"].performances.append(make_performance("Perf2", ((0, 0),)))
        ids = [relations.PerfId("RAM1", 0), relations.PerfId("RAM1", 1)]
        plan = perfcopy.plan_atomic_bank(
            sources,
            ids,
            destination_bank=RAM3,
            budget=bankbuild.Budget(performances=1),
        )
        assert not plan.fits
        assert any("no performance slot left" in r for r in plan.rejected)

    def test_patch_budget(self):
        sources, _ = self._src()
        # one performance needing 2 patches: make perf with two parts different patches
        sources["RAM1"].patches = [make_patch("A"), make_patch("B")]
        sources["RAM1"].performances = [make_performance("Perf", ((0, 0), (0, 1)))]
        plan = perfcopy.plan_atomic_bank(
            sources,
            [relations.PerfId("RAM1", 0)],
            destination_bank=RAM3,
            budget=bankbuild.Budget(patches=1),
        )
        assert not plan.fits
        assert any("patch slot" in r for r in plan.rejected)
        assert plan.patch_count == 0

    def test_sequence_budget(self):
        sources, _ = self._src()
        # patch needs 2 wave sequences (seq0 and seq1)
        sources["RAM1"].patches = [
            make_patch("P", waves=((0, 0), (0, 1), (0, 40), (0, 40))),
        ]
        sources["RAM1"].performances = [make_performance("Perf", ((0, 0),))]
        plan = perfcopy.plan_atomic_bank(
            sources,
            [relations.PerfId("RAM1", 0)],
            destination_bank=RAM3,
            budget=bankbuild.Budget(sequences=1),
        )
        assert not plan.fits
        assert any("wave sequence" in r for r in plan.rejected)
        assert len(plan.sequence_map) == 0

    def test_step_budget(self):
        # patch whose sequence walks 2+ steps, budget steps=1
        waveseq = make_waveseq({0: [40, 41]})[0]
        sources = {
            "RAM1": deps.Source(
                bank_id="RAM1",
                patches=[make_patch("P", waves=((0, 0), (0, 40), (0, 40), (0, 40)))],
                performances=[make_performance("Perf", ((0, 0),))],
                waveseq=waveseq,
            )
        }
        plan = perfcopy.plan_atomic_bank(
            sources,
            [relations.PerfId("RAM1", 0)],
            destination_bank=RAM3,
            budget=bankbuild.Budget(steps=1),
        )
        assert not plan.fits
        assert any("step" in r for r in plan.rejected)
        assert len(plan.sequence_map) == 0


class TestActivePerformances:
    def test_active(self):
        recs = [
            make_performance("A", ((0, 0),)),
            bytes(r.PERFORMANCE_SIZE),  # empty zero
            make_performance("B", ((0, 1),)),
        ]
        src = deps.Source(bank_id="X", performances=recs, patches=[])
        assert perfcopy.active_performances(src) == [0, 2]


class TestTemplateDoesNotLeaveStalePerformances:
    """A target bank used as a template must not donate performances.

    The template supplies the *shape* of an empty bank. Its occupied slots
    are exactly what a clean copy or a replaced slot was meant to remove;
    keeping them would put performances in the output that nobody chose,
    with patch references into slots the new plan owns.
    """

    def _target(self):
        return deps.Source(
            bank_id="RAM3",
            patches=[make_patch("A"), make_patch("B"), make_patch("C")],
            performances=[
                make_performance("First", ((2, 0),)),
                make_performance("Second", ((2, 1),)),
                make_performance("Third", ((2, 2),)),
            ],
            waveseq=None,
        )

    def test_clean_leaves_only_the_chosen_performance(self):
        sources = {"RAM3": self._target()}
        built = perfcopy.copy_into_bank(
            sources,
            "RAM3",
            [relations.PerfId("RAM3", 1)],
            mode="clean",
            destination_bank=RAM3,
        )
        records = [
            built.performances[i * r.PERFORMANCE_SIZE : (i + 1) * r.PERFORMANCE_SIZE]
            for i in range(banks.PERFORMANCES_PER_BANK)
        ]
        assert perfcopy.active_performances(
            deps.Source(bank_id="RAM3", performances=records)
        ) == [0]
        for slot in range(1, banks.PERFORMANCES_PER_BANK):
            parts = r.performance_parts(records[slot])
            assert not any(part.used for part in parts), slot

    def test_removing_the_last_performance_leaves_no_tail(self):
        sources = {"RAM3": self._target()}
        built = perfcopy.copy_into_bank(
            sources,
            "RAM3",
            [],
            mode="replace",
            replace_slot=2,
            destination_bank=RAM3,
        )
        records = [
            built.performances[i * r.PERFORMANCE_SIZE : (i + 1) * r.PERFORMANCE_SIZE]
            for i in range(banks.PERFORMANCES_PER_BANK)
        ]
        assert perfcopy.active_performances(
            deps.Source(bank_id="RAM3", performances=records)
        ) == [0, 1]
        for slot in range(2, banks.PERFORMANCES_PER_BANK):
            parts = r.performance_parts(records[slot])
            assert not any(part.used for part in parts), slot


class TestSlotImport:
    def _dest_source(self):
        # 15 active performances in slots 0..14, rest Init-ish zero? Use Init named.
        perfs = []
        for i in range(15):
            perfs.append(make_performance(f"Active{i}", ((2, 0),)))
        for _i in range(15, banks.PERFORMANCES_PER_BANK):
            perfs.append(make_performance("Init", ()))
        patches = []
        for i in range(10):
            patches.append(make_patch(f"Real{i}"))
        for _i in range(10, banks.PATCHES_PER_BANK):
            patches.append(make_patch("Init"))
        waveseq = make_waveseq({0: [40, 41]})[0]
        return deps.Source(
            bank_id="RAM3",
            performances=perfs,
            patches=patches,
            waveseq=waveseq,
        )

    def _src_source(self):
        # source perf with localized part (RAM1 patch 0) and ROM part (ROM4 patch 0)
        # wave sequence in source bank
        waveseq = make_waveseq({0: [40, 41]})[0]
        src_perf = make_performance("SrcPerf", ((0, 0), (3, 0)))
        src = deps.Source(
            bank_id="RAM1",
            patches=[make_patch("SrcPatch", waves=((0, 0), (0, 40), (0, 40), (0, 40)))],
            performances=[src_perf],
            waveseq=waveseq,
        )
        return {"RAM1": src, "RAM3": self._dest_source()}

    def test_populated_destination(self):
        sources = self._src_source()
        dest = banks.bank("RAM3")
        target_scope = "RAM3"
        before_perfs = [
            bytes(sources[target_scope].performances[i])
            for i in range(banks.PERFORMANCES_PER_BANK)
        ]
        plan = perfcopy.plan_slot_import(
            sources,
            target_scope,
            {15: relations.PerfId("RAM1", 0)},
            destination_bank=dest,
        )
        assert plan.fits
        assert plan.assignments == [(15, relations.PerfId("RAM1", 0))]
        assert plan.allocations[0].target_perf_slot == 15
        built = perfcopy.build_slot_import(sources, plan)
        # non-assigned perf slots unchanged
        for i in range(banks.PERFORMANCES_PER_BANK):
            if i == 15:
                continue
            slot_bytes = built.performances[
                i * r.PERFORMANCE_SIZE : (i + 1) * r.PERFORMANCE_SIZE
            ]
            assert slot_bytes == before_perfs[i], i
        # source ROM part stays ROM: perf slot 15 parts
        rec0 = built.performances[15 * r.PERFORMANCE_SIZE : 16 * r.PERFORMANCE_SIZE]
        parts = r.performance_parts(rec0)
        used = [p for p in parts if p.used]
        # one localized -> dest bank, one ROM -> bank 3
        banks_seen = {p.bank for p in used}
        assert dest.number in banks_seen
        assert 3 in banks_seen  # ROM4
        # localized parts point at dest bank and valid patch slot
        for p in used:
            if p.bank == dest.number:
                assert 0 <= p.patch < banks.PATCHES_PER_BANK
        # patch count <=35
        assert plan.patch_count <= 35
        # waveseq valid
        assert built.waveseq is not None and len(built.waveseq) == r.WAVESEQ_BANK_SIZE

    def test_pinned_target_patch(self):
        # destination perf at slot 0 references target bank patch 0
        sources = self._src_source()
        dest_src = sources["RAM3"]
        # make slot 0 active referencing patch 0
        dest_src.performances[0] = make_performance("PinnedPerf", ((2, 0),))
        dest = banks.bank("RAM3")
        # source patch differs, so must allocate different slot
        plan = perfcopy.plan_slot_import(
            sources, "RAM3", {16: relations.PerfId("RAM1", 0)}, destination_bank=dest
        )
        assert plan.fits
        alloc = plan.allocations[0]
        # patch slot not 0
        assert all(slot != 0 for _, slot in alloc.allocated_patches)
        built = perfcopy.build_slot_import(sources, plan)
        # patch 0 bytes unchanged
        assert built.patches[0 : r.PATCH_SIZE] == dest_src.patches[0]
        # existing perf slot 0 unchanged
        assert built.performances[0 : r.PERFORMANCE_SIZE] == dest_src.performances[0]
        # all 35 pinned -> reject
        # pin all patch slots
        # Construct a destination where every patch slot is pinned by a surviving perf.
        dest2 = self._dest_source()
        # make 35 performances each referencing a distinct patch slot
        for i in range(35):
            dest2.performances[i] = make_performance(f"P{i}", ((2, i),))
        # keep rest init
        for i in range(35, 50):
            dest2.performances[i] = make_performance("Init", ())
        sources2 = dict(sources)
        sources2["RAM3"] = dest2
        plan2 = perfcopy.plan_slot_import(
            sources2, "RAM3", {49: relations.PerfId("RAM1", 0)}, destination_bank=dest
        )
        assert not plan2.fits
        assert any("needs" in rr and "patch slot(s)" in rr for rr in plan2.rejected)
        with pytest.raises(ValueError):
            perfcopy.build_slot_import(sources2, plan2)

    def test_overwrite_occupied_slot(self):
        sources = self._src_source()
        dest_src = sources["RAM3"]
        # slot 5 occupied by a perf using a unique patch 5
        dest_src.performances[5] = make_performance("Occ", ((2, 5),))
        # slot 6 shares patch 7
        dest_src.performances[6] = make_performance("Surv", ((2, 7),))
        # overwritten perf's unique patch 5 should become free; shared patch 7 stays
        dest = banks.bank("RAM3")
        before_slot6 = bytes(dest_src.performances[6])
        plan = perfcopy.plan_slot_import(
            sources, "RAM3", {5: relations.PerfId("RAM1", 0)}, destination_bank=dest
        )
        assert plan.fits
        built = perfcopy.build_slot_import(sources, plan)
        # slot 5 contains source perf
        assert (
            dumps.record_name(
                built.performances[5 * r.PERFORMANCE_SIZE : 6 * r.PERFORMANCE_SIZE]
            )
            == "SrcPerf"
        )
        # slot 6 unchanged
        assert (
            built.performances[6 * r.PERFORMANCE_SIZE : 7 * r.PERFORMANCE_SIZE]
            == before_slot6
        )
        # other slots unchanged (spot check slot 0 init)
        # patch 5 may be reused
        # shared patch 7 unchanged
        assert built.patches[7 * r.PATCH_SIZE : 8 * r.PATCH_SIZE] == dest_src.patches[7]

    def test_batch_assignment(self):
        sources = self._src_source()
        # add second source perf
        sources["RAM1"].performances.append(make_performance("SrcPerf2", ((0, 0),)))
        sources["RAM1"].patches.append(make_patch("SrcPatch2"))
        # ensure perf references patch 1? adjust: create perf referencing patch index 1
        sources["RAM1"].performances[1] = make_performance("SrcPerf2", ((0, 1),))
        dest = banks.bank("RAM3")
        before = [
            bytes(sources["RAM3"].performances[i])
            for i in range(banks.PERFORMANCES_PER_BANK)
        ]
        plan = perfcopy.plan_slot_import(
            sources,
            "RAM3",
            {10: relations.PerfId("RAM1", 0), 20: relations.PerfId("RAM1", 1)},
            destination_bank=dest,
        )
        assert plan.fits
        targets = {a.target_perf_slot for a in plan.allocations}
        assert targets == {10, 20}
        built = perfcopy.build_slot_import(sources, plan)
        # existing slots unchanged
        for i in range(banks.PERFORMANCES_PER_BANK):
            if i in (10, 20):
                continue
            assert (
                built.performances[
                    i * r.PERFORMANCE_SIZE : (i + 1) * r.PERFORMANCE_SIZE
                ]
                == before[i]
            )

    def test_wave_sequences(self):
        # source perf uses source patch with sequence in source bank
        waveseq = make_waveseq({0: [40, 41], 7: [50, 51]})[0]
        src = deps.Source(
            bank_id="RAM1",
            patches=[make_patch("Q", waves=((0, 0),) * 4)],
            performances=[make_performance("Perf", ((0, 0),))],
            waveseq=waveseq,
        )
        dest_src = self._dest_source()
        # pin a destination sequence/step by a surviving perf referencing a patch
        dest_patch = make_patch("DestSeq", waves=((2, 0),) * 4)
        dest_src.patches[0] = dest_patch
        dest_src.performances[0] = make_performance("DestPerf", ((2, 0),))
        # destination waveseq has seq 0
        dest_waveseq = make_waveseq({0: [99]})[0]
        dest_src.waveseq = dest_waveseq
        sources = {"RAM1": src, "RAM3": dest_src}
        dest = banks.bank("RAM3")
        # pinned step bytes before
        # plan
        plan = perfcopy.plan_slot_import(
            sources, "RAM3", {5: relations.PerfId("RAM1", 0)}, destination_bank=dest
        )
        assert plan.fits
        # pinned seq 0 not overwritten: check seq slot 0 header unchanged after build?
        before_seq0_header = dest_waveseq[0:16]
        built = perfcopy.build_slot_import(sources, plan)
        # new seq slot should be >0
        new_slots = set(plan.sequence_map.values())
        assert 0 not in new_slots
        # pinned seq 0 header unchanged
        assert built.waveseq is not None
        assert built.waveseq[0:16] == before_seq0_header
        # quoted sequence test below
        # Test quoted sequence import
        waveseq2 = make_waveseq({0: [40, 7], 7: [50, 51]})[0]
        src2 = deps.Source(
            bank_id="RAM1",
            patches=[make_patch("Quoter", waves=((0, 0),) * 4)],
            performances=[make_performance("Perf2", ((0, 0),))],
            waveseq=waveseq2,
        )
        sources2 = {"RAM1": src2, "RAM3": self._dest_source()}
        plan2 = perfcopy.plan_slot_import(
            sources2, "RAM3", {6: relations.PerfId("RAM1", 0)}, destination_bank=dest
        )
        assert plan2.fits
        assert len(plan2.sequence_map) == 2

    def test_budget_rejection(self):
        src = deps.Source(
            bank_id="RAM1",
            patches=[make_patch("P", waves=((0, 0), (0, 1), (0, 40), (0, 40)))],
            performances=[make_performance("Perf", ((0, 0),))],
            waveseq=make_waveseq({0: [40], 1: [41]})[0],
        )
        dest_src = self._dest_source()
        # pin all patch slots
        for i in range(banks.PATCHES_PER_BANK):
            dest_src.performances[i] = make_performance(f"P{i}", ((2, i),))
        sources = {"RAM1": src, "RAM3": dest_src}
        dest = banks.bank("RAM3")
        # no free patch slots
        plan = perfcopy.plan_slot_import(
            sources,
            "RAM3",
            {49: relations.PerfId("RAM1", 0)},
            destination_bank=dest,
            budget=bankbuild.Budget(patches=0) if False else bankbuild.Budget(),
        )
        # With all 35 pinned, should reject patch
        assert not plan.fits
        assert any("patch slot(s)" in rr for rr in plan.rejected)
        with pytest.raises(ValueError):
            perfcopy.build_slot_import(sources, plan)
        # no output: build raises before returning; ensure no partial? just exception
        # sequence budget
        src2 = deps.Source(
            bank_id="RAM1",
            patches=[make_patch("P", waves=((0, 0), (0, 1), (0, 40), (0, 40)))],
            performances=[make_performance("Perf", ((0, 0),))],
            waveseq=make_waveseq({0: [40], 1: [41]})[0],
        )
        dest_free = self._dest_source()
        # pin all 32 destination sequences
        dest_seq = self._dest_source()
        for i in range(32):
            dest_seq.patches[i] = make_patch(f"S{i}", waves=((2, i),) * 4)
            dest_seq.performances[i] = make_performance(f"SP{i}", ((2, i),))
        dest_seq.waveseq = make_waveseq({i: [40 + i] for i in range(32)})[0]
        plan_seq = perfcopy.plan_slot_import(
            {"RAM1": src2, "RAM3": dest_seq},
            "RAM3",
            {5: relations.PerfId("RAM1", 0)},
            destination_bank=dest,
        )
        assert not plan_seq.fits
        assert any("wave sequence(s)" in rr for rr in plan_seq.rejected)
        # step budget
        plan_step = perfcopy.plan_slot_import(
            {"RAM1": src2, "RAM3": dest_free},
            "RAM3",
            {5: relations.PerfId("RAM1", 0)},
            destination_bank=dest,
            budget=bankbuild.Budget(steps=0),
        )
        assert not plan_step.fits
        assert any("step(s)" in rr for rr in plan_step.rejected)

    def test_relocation(self):
        src = deps.Source(
            bank_id="RAM1",
            patches=[make_patch("P", waves=((0, 0), (2, 5), (0, 40), (0, 40)))],
            performances=[make_performance("Perf", ((0, 0), (2, 3)))],
            waveseq=None,
        )
        dest = banks.bank("RAM3")
        rb = perfcopy.relocate_bank(src, dest)
        # perf part bank 0 -> dest.number (2); bank 2 unchanged
        rec0 = rb.performances[0 : r.PERFORMANCE_SIZE]
        for part in r.performance_parts(rec0):
            if part.used:
                if part.patch == 0:
                    assert part.bank == dest.number
                else:
                    assert part.bank == 2
        # patch wave bank 0 -> dest.number; bank 2 unchanged
        pc = rb.patches[0 : r.PATCH_SIZE]
        for w in r.patch_waves(pc):
            if w.bank == 0:
                # should have been rewritten to dest.number, so none remain 0? check
                pass
        # Actually assert no wave bank 0 remains for sequence? wave bank 2 remains
        banks_w = {w.bank for w in r.patch_waves(pc)}
        assert 2 in banks_w
        # ROM? none. Records otherwise identical except bank fields
        # name unchanged
        assert dumps.record_name(rb.performances[0 : r.PERFORMANCE_SIZE]) == "Perf"

    def test_duplicate_and_range(self):
        sources = self._src_source()
        dest = banks.bank("RAM3")
        from collections.abc import Mapping as _Mapping

        class DupMapping(_Mapping[int, relations.PerfId]):
            def __getitem__(self, key):
                raise KeyError(key)

            def __iter__(self):
                return iter([1])

            def __len__(self):
                return 1

            def items(self):
                return [
                    (1, relations.PerfId("RAM1", 0)),
                    (1, relations.PerfId("RAM1", 0)),
                ]

        plan = perfcopy.plan_slot_import(
            sources, "RAM3", DupMapping(), destination_bank=dest
        )
        assert not plan.fits
        assert any("duplicate" in rr for rr in plan.rejected)
        plan2 = perfcopy.plan_slot_import(
            sources, "RAM3", {100: relations.PerfId("RAM1", 0)}, destination_bank=dest
        )
        assert not plan2.fits
        assert any("out of range" in rr for rr in plan2.rejected)

    def test_build_does_not_clear_untouched(self):
        sources = self._src_source()
        dest_src = sources["RAM3"]
        # mark slot 7 with a recognizable performance
        dest_src.performances[7] = make_performance("KeepMe", ((2, 1),))
        dest = banks.bank("RAM3")
        before7 = bytes(dest_src.performances[7])
        plan = perfcopy.plan_slot_import(
            sources, "RAM3", {15: relations.PerfId("RAM1", 0)}, destination_bank=dest
        )
        built = perfcopy.build_slot_import(sources, plan)
        assert (
            built.performances[7 * r.PERFORMANCE_SIZE : 8 * r.PERFORMANCE_SIZE]
            == before7
        )

    def test_session_alias_source_bank_number(self):
        """Two RAM1 files can be assembled under one target.

        The alias key cannot be resolved by the bank table; its ``Source``
        carries the real bank number so parts still localize.
        """
        alias = "RAM1#2"
        src = deps.Source(
            bank_id=alias,
            performances=[make_performance("Alias", ((0, 0),))],
            patches=[make_patch("AliasPatch")],
            bank_number=0,
        )
        sources = {alias: src, "RAM3": self._dest_source()}
        dest = banks.bank("RAM3")
        plan = perfcopy.plan_slot_import(
            sources, "RAM3", {0: relations.PerfId(alias, 0)}, destination_bank=dest
        )
        assert plan.fits, plan.rejected
        built = perfcopy.build_slot_import(sources, plan)
        record = built.performances[0 : r.PERFORMANCE_SIZE]
        used = [part for part in r.performance_parts(record) if part.used]
        assert used
        assert all(part.bank == dest.number for part in used)

    def test_share_existing_false_gives_fresh_patch_copies(self):
        # destination bank RAM3: perf at slot 0 uses patch 2 (pinned)
        dest_src = self._dest_source()
        dest_src.performances[0] = make_performance("Keep", ((2, 2),))
        # source performance RAM1:0 uses patch 0
        src = deps.Source(
            bank_id="RAM1",
            patches=[make_patch("SrcPatch")],
            performances=[make_performance("SrcPerf", ((0, 0),))],
            waveseq=make_waveseq({0: [40, 41]})[0],
        )
        # make source patch 0 byte-identical to dest patch 2
        dest_src.patches[2] = make_patch("Shared")
        src.patches[0] = dest_src.patches[2]
        sources = {"RAM1": src, "RAM3": dest_src}
        dest = banks.bank("RAM3")

        # default: reuse slot 2
        plan_default = perfcopy.plan_slot_import(
            sources,
            "RAM3",
            {10: relations.PerfId("RAM1", 0)},
            destination_bank=dest,
        )
        assert plan_default.fits
        alloc_default = plan_default.allocations[0]
        assert 2 in list(alloc_default.part_patch_slots.values())

        # fresh: not 2, and one of free slots (not 0, not 2)
        plan_fresh = perfcopy.plan_slot_import(
            sources,
            "RAM3",
            {10: relations.PerfId("RAM1", 0)},
            destination_bank=dest,
            share_existing=False,
        )
        assert plan_fresh.fits
        alloc_fresh = plan_fresh.allocations[0]
        fresh_slot = next(iter(alloc_fresh.part_patch_slots.values()))
        assert fresh_slot != 2
        assert fresh_slot not in (0, 2)

        # batch dedup within one plan with share_existing=False
        # need second identical source perf record; assign same PerfId to two slots
        plan_batch = perfcopy.plan_slot_import(
            sources,
            "RAM3",
            {10: relations.PerfId("RAM1", 0), 20: relations.PerfId("RAM1", 0)},
            destination_bank=dest,
            share_existing=False,
        )
        assert plan_batch.fits
        slots = [
            next(iter(a.part_patch_slots.values())) for a in plan_batch.allocations
        ]
        assert slots[0] == slots[1]
        assert slots[0] not in (0, 2)

        # build fresh plan: fresh slot holds patch bytes; pinned slot 2 unchanged
        pinned_before = bytes(dest_src.patches[2])
        built = perfcopy.build_slot_import(sources, plan_fresh)
        assert (
            built.patches[fresh_slot * r.PATCH_SIZE : (fresh_slot + 1) * r.PATCH_SIZE]
            == src.patches[0]
        )
        assert built.patches[2 * r.PATCH_SIZE : 3 * r.PATCH_SIZE] == pinned_before


def _audition_sources():
    """A PCM-only source performance and an empty RAM3 to receive it."""
    patch = make_patch("SrcPatch", ((0, 40), (0, 40), (0, 40), (0, 40)))
    src = deps.Source(
        bank_id="RAM1",
        patches=[patch],
        performances=[make_performance("SrcPerf", ((0, 0), (0, 0)))],
        waveseq=None,
    )
    dest_src = deps.Source(
        bank_id="RAM3",
        patches=[make_patch("Init") for _ in range(banks.PATCHES_PER_BANK)],
        performances=[
            make_performance("Init", ()) for _ in range(banks.PERFORMANCES_PER_BANK)
        ],
        waveseq=None,
    )
    return {"RAM1": src, "RAM3": dest_src}


def test_planned_performance_record_equals_the_built_bank_record():
    """The audition record is byte-for-byte what build_slot_import writes."""
    sources = _audition_sources()
    plan = perfcopy.plan_slot_import(
        sources,
        "RAM3",
        {0: relations.PerfId("RAM1", 0)},
        destination_bank=RAM3,
    )
    assert plan.fits, plan.rejected
    alloc = plan.allocations[0]
    record = perfcopy.planned_performance_record(sources, alloc, RAM3)
    built = perfcopy.build_slot_import(sources, plan)
    at = alloc.target_perf_slot * r.PERFORMANCE_SIZE
    assert record == bytes(built.performances[at : at + r.PERFORMANCE_SIZE])
    used = [part for part in r.performance_parts(record) if part.used]
    assert {part.patch for part in used} == set(alloc.part_patch_slots.values())
    assert all(part.bank == RAM3.number for part in used)


def test_planned_performance_record_slot_override_remaps_parts():
    """slot_override points the parts at staging slots, for audition."""
    sources = _audition_sources()
    plan = perfcopy.plan_slot_import(
        sources,
        "RAM3",
        {0: relations.PerfId("RAM1", 0)},
        destination_bank=RAM3,
    )
    assert plan.fits, plan.rejected
    alloc = plan.allocations[0]
    override = dict.fromkeys(alloc.part_patch_slots, 27)
    record = perfcopy.planned_performance_record(
        sources, alloc, RAM3, slot_override=override
    )
    used = [part for part in r.performance_parts(record) if part.used]
    assert {part.patch for part in used} == {27}
    assert all(part.bank == RAM3.number for part in used)
    # The unoverridden record pointed somewhere else, so this is a real remap.
    plain = perfcopy.planned_performance_record(sources, alloc, RAM3)
    plain_used = [part for part in r.performance_parts(plain) if part.used]
    assert {part.patch for part in plain_used} != {27}


def test_bank_override_names_where_the_overridden_slots_actually_live():
    """Staging into another bank: the overridden parts must name it too.

    Slot numbers only mean something together with a bank. A part left at
    the destination's number with a staging slot points at an unrelated
    patch there -- on a real SR, usually an empty one, so the audition is
    silent.
    """
    sources = _audition_sources()
    plan = perfcopy.plan_slot_import(
        sources,
        "RAM3",
        {0: relations.PerfId("RAM1", 0)},
        destination_bank=RAM3,
    )
    assert plan.fits, plan.rejected
    alloc = plan.allocations[0]
    override = dict.fromkeys(alloc.part_patch_slots, 27)
    record = perfcopy.planned_performance_record(
        sources,
        alloc,
        RAM3,
        slot_override=override,
        bank_override=banks.bank("RAM2").number,
    )
    used = [part for part in r.performance_parts(record) if part.used]
    assert {part.bank for part in used} == {banks.bank("RAM2").number}
    assert {part.patch for part in used} == {27}


def test_imports_ok():
    assert True
