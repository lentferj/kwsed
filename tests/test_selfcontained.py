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

from kws import bankbuild as bb
from kws import banks, containment, deps, dumps, perfcopy, relations
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


ROM_BANK = banks.bank(3)  # ROM 4: display number 3, never writable
OVERFLOW = banks.bank("RAM3")


def _assert_self_contained(built, bank):
    """Every used part of every performance names ``bank`` or ROM.

    This is the self-containment invariant of a split output: no part may
    still point at a *writable* bank other than the one the performance
    lives in, because nothing guarantees what that bank holds.
    """
    for slot in range(banks.PERFORMANCES_PER_BANK):
        record = _performance(built, slot)
        if perfcopy.is_empty_performance(record):
            continue
        for part in _used_parts(record):
            ok = part.bank == bank.number or not banks.bank(part.bank).writable
            assert ok, f"slot {slot} part {part.index} -> bank {part.bank}"


class TestRomPartsStayInRom:
    """A ROM part is neither carried nor external; it costs no slot."""

    def _sources(self):
        return {
            "RAM1": _source(
                performances=[
                    make_performance("Mixed Reach", ((0, 0), (ROM_BANK.number, 0))),
                    make_performance("Rom Only", ((4, 2),)),
                ],
                patches=[make_patch("Tide Gate")],
            )
        }

    def test_planning_with_the_rom_source_absent_reports_no_external(self):
        sources = self._sources()
        for carry in (True, False):
            plan = perfcopy.plan_atomic_bank(
                sources,
                [relations.PerfId("RAM1", 0)],
                destination_bank=CARD,
                carry_external=carry,
            )
            assert plan.fits, plan.rejected
            assert plan.external_patches == []
            assert plan.patch_count == 1

    def test_a_rom_only_part_points_at_its_rom_bank_when_built(self):
        sources = self._sources()
        plan = perfcopy.plan_atomic_bank(
            sources, [relations.PerfId("RAM1", 1)], destination_bank=CARD
        )
        assert plan.fits, plan.rejected
        assert plan.patch_count == 0
        built = perfcopy.build_atomic_bank(sources, plan)
        part = _used_parts(_performance(built, 0))[0]
        assert part.bank == 4  # ROM 5's display number, as authored
        assert not banks.bank(part.bank).writable

    def test_overflow_import_carries_no_rom_patches(self):
        sources = self._sources()
        plan = perfcopy.plan_overflow_import(
            sources, "RAM1", CARD, OVERFLOW, mode=MODE_1TO1
        )
        assert plan.fits, plan.rejected
        assert plan.atomic.external_patches == []
        built = perfcopy.build_overflow_import(sources, plan)
        assert built.patch_count == 0


class TestSelfContainedSplit:
    """Contained performances go to the destination; the rest overflow."""

    def _sources(self):
        # RAM 2's patch names a wave sequence that lives in RAM 2's own
        # wave-sequence bank, so the overflow copy must relocate it.
        seq_bank, _starts = make_waveseq({0: [50, 51]})
        shared = make_patch("Harbor Slow")
        own_reef = make_patch("Harbor Reef")
        foreign = bytearray(make_patch("Tide Cross"))
        r.set_patch_wave(foreign, 0, bank=1, wave=0)
        performances = [
            make_performance("Solo Shore", ((0, 0),)),  # self only
            make_performance("Cross Reach", ((0, 0), (1, 0))),  # own + RAM 2
            make_performance("Rom Drift", ((ROM_BANK.number, 0),)),  # ROM only
            make_performance("Solo Reef", ((0, 1),)),  # self only, own patch
        ]
        sources = {
            "RAM1": _source(performances=performances, patches=[shared, own_reef]),
            "RAM2": deps.Source(
                scope_id="RAM2",
                performances=[],
                patches=[bytes(foreign)],
                waveseq=seq_bank,
            ),
        }
        return sources, shared

    def test_split_places_each_group_and_duplicates_shared_content(self):
        sources, shared = self._sources()
        plan = perfcopy.plan_self_contained_split(
            sources, "RAM1", CARD, OVERFLOW, mode=MODE_PACKED
        )
        assert plan.fits, plan.rejected
        built = perfcopy.build_split_import(sources, plan)

        assert [alloc.name for alloc in plan.destination.allocations] == [
            "Solo Shore",
            "Rom Drift",
            "Solo Reef",
        ]
        assert [alloc.name for alloc in plan.overflow.allocations] == ["Cross Reach"]
        assert built.destination_patches == 2
        assert built.overflow_patches == 2

        _assert_self_contained(built.destination, CARD)
        _assert_self_contained(built.overflow, OVERFLOW)

        # The shared own patch exists in both banks: content used by
        # performances that land in both groups is duplicated.
        assert _patch(built.destination, 0) == shared
        assert _patch(built.overflow, 0) == shared

        # The overflow patch's sequence lives in the overflow bank's
        # wave-sequence bank and its reference was rewritten there.
        wave = r.patch_waves(_patch(built.overflow, 1))[0]
        assert wave.is_sequence
        assert (wave.bank, wave.wave) == (OVERFLOW.number, 0)
        assert built.overflow.waveseq is not None
        chain = deps.walk_sequence(built.overflow.waveseq, 0, scope_id=OVERFLOW.id)
        waves = [r.step(built.overflow.waveseq, step)["wave"] for step in chain.steps]
        assert waves == [50, 51]

    def test_as_is_keeps_each_source_slot(self):
        sources, _shared = self._sources()
        plan = perfcopy.plan_self_contained_split(
            sources, "RAM1", CARD, OVERFLOW, mode=perfcopy.ImportMode.AS_IS
        )
        assert plan.fits, plan.rejected
        assert [alloc.target_perf_slot for alloc in plan.destination.allocations] == [
            0,
            2,
            3,
        ]
        assert [alloc.target_perf_slot for alloc in plan.overflow.allocations] == [1]

    def test_the_same_bank_twice_is_refused(self):
        sources, _shared = self._sources()
        with pytest.raises(ValueError, match="overflow bank is the destination"):
            perfcopy.plan_self_contained_split(sources, "RAM1", CARD, CARD)

    def test_a_group_that_cannot_fit_is_rejected_naming_the_bank(self):
        sources, _shared = self._sources()
        tight = bb.Budget(patches=1)
        plan = perfcopy.plan_self_contained_split(
            sources, "RAM1", CARD, OVERFLOW, budget=tight
        )
        assert not plan.fits
        assert any(
            "Card" in message and "do not fit 1 patch slots" in message
            for message in plan.rejected
        )
        with pytest.raises(ValueError, match="Card"):
            perfcopy.build_split_import(sources, plan)


class TestSplitPartition:
    """One split spread across as many banks as it needs."""

    def _sources(self, groups):
        """A source bank whose performances name ``groups`` of patches.

        ``groups`` is a list of lists of (bank, patch) tuples, one per
        performance. Every patch record is distinct, so a group's patch
        count is its distinct content count.
        """
        performances = []
        patches: list[bytes] = []
        for index, parts in enumerate(groups):
            performances.append(make_performance(f"Split {index}", tuple(parts)))
        for index in range(40):
            patches.append(make_patch(f"Part {index}"))
        return {
            "RAM1": deps.Source(
                scope_id="RAM1",
                performances=performances,
                patches=patches,
                waveseq=make_waveseq({})[0],
            ),
            "RAM2": deps.Source(
                scope_id="RAM2",
                performances=[],
                patches=list(patches),
                waveseq=make_waveseq({})[0],
            ),
        }

    def test_groups_are_ordered_and_each_is_self_contained(self):
        # Six performances, each naming four own patches plus one RAM 2
        # patch. Two banks hold 35 patches each, so the non-contained set
        # needs more than one overflow bank.
        groups = []
        for index in range(6):
            parts = [(0, index * 4 + k) for k in range(4)]
            parts.append((1, index))
            groups.append(parts)
        sources = self._sources(groups)
        offered = [banks.bank("CARD"), banks.bank("RAM3"), banks.bank("RAM2")]
        partition = perfcopy.partition_self_contained_split(
            sources, "RAM1", offered, mode=MODE_1TO1
        )
        assert partition.fits, partition.rejected
        seen = [slot for group in partition.groups for slot in group.performances]
        assert seen == sorted(seen)  # source order across the whole split
        for group in partition.groups:
            assert group.plan is not None
            built = perfcopy.build_atomic_bank(sources, group.plan)
            for slot in group.performances:
                at = slot * r.PERFORMANCE_SIZE
                for part in _used_parts(
                    built.performances[at : at + r.PERFORMANCE_SIZE]
                ):
                    assert (
                        part.bank in (group.target_bank.number,)
                        or not banks.bank(part.bank).writable
                    )
        # Nothing here is contained, so the first group is an overflow
        # group and the offered order is respected.
        assert partition.groups[0].target_bank.id == "RAM3"
        # A contained performance takes the destination bank instead.
        groups[0] = [(0, 34)]
        sources = self._sources(groups)
        partition = perfcopy.partition_self_contained_split(
            sources, "RAM1", offered, mode=MODE_1TO1
        )
        assert partition.fits, partition.rejected
        assert partition.groups[0].target_bank.id == "CARD"
        assert partition.groups[0].performances == (0,)

    def test_a_patch_shared_by_two_groups_is_duplicated(self):
        # Two performances share a RAM 2 patch and differ only in their own
        # patches, so they land in different groups; the shared patch must
        # exist in both banks.
        groups = [[(0, 0), (1, 30)], [(0, 1), (1, 30)]]
        for index in range(2, 34):
            groups.append([(0, index)])
        sources = self._sources(groups)
        offered = [banks.bank("CARD"), banks.bank("RAM3"), banks.bank("RAM2")]
        partition = perfcopy.partition_self_contained_split(
            sources, "RAM1", offered, mode=MODE_1TO1
        )
        assert partition.fits, partition.rejected
        built = perfcopy.build_split_partition(sources, partition)
        holders = []
        for group, bytes_built in zip(partition.groups, built):
            names = set()
            for slot in group.performances:
                at = slot * r.PERFORMANCE_SIZE
                for part in _used_parts(
                    bytes_built.performances[at : at + r.PERFORMANCE_SIZE]
                ):
                    if part.bank == group.target_bank.number:
                        names.add(part.patch)
            holders.append(names)
        assert len(holders) >= 2
        assert holders[0] & holders[1] or len(holders) == 1

    def test_contained_overflow_is_rejected_with_the_bank_count(self):
        groups = [[(0, 0)] for _ in range(60)]  # 60 self-only performances
        sources = self._sources(groups)
        partition = perfcopy.partition_self_contained_split(
            sources, "RAM1", [banks.bank("CARD"), banks.bank("RAM3")], mode=MODE_1TO1
        )
        assert not partition.fits
        assert any("need" in text and "banks" in text for text in partition.rejected)

    def test_every_group_fits_a_bank(self):
        """Packing never leaves a group over the budget it must fit."""
        groups = [[(0, index)] for index in range(34)]
        groups += [[(0, index), (1, index)] for index in range(34, 40)]
        sources = self._sources(groups)
        partition = perfcopy.partition_self_contained_split(
            sources,
            "RAM1",
            [banks.bank("CARD"), banks.bank("RAM3"), banks.bank("RAM2")],
            mode=MODE_1TO1,
        )
        assert partition.fits, partition.rejected
        for group in partition.groups:
            assert group.plan is not None
            assert group.plan.patch_count <= banks.PATCHES_PER_BANK
            assert len(group.performances) <= banks.PERFORMANCES_PER_BANK
