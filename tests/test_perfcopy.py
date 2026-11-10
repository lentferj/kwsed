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


def test_imports_ok():
    assert True
