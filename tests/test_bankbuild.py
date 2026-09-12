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

"""Records, dependencies and building. Every fixture here is invented."""

import pytest

from kws import bankbuild as bb
from kws import deps
from kws import records as r


def make_patch(name="Invented", waves=((0, 40), (0, 40), (0, 40), (0, 40))):
    rec = bytearray(r.PATCH_SIZE)
    rec[0:len(name)] = name.encode()
    for i, (bank, wave) in enumerate(waves):
        r.set_patch_wave(rec, i, bank=bank, wave=wave)
    return bytes(rec)


def make_performance(name="Invented", parts=((0, 0),)):
    rec = bytearray(r.PERFORMANCE_SIZE)
    rec[0:len(name)] = name.encode()
    for i in range(r.PARTS_PER_PERFORMANCE):
        r.set_performance_part(rec, i, bank=0, patch=r.UNUSED_PATCH)
    for i, (bank, patch) in enumerate(parts):
        r.set_performance_part(rec, i, bank=bank, patch=patch)
    return bytes(rec)


def make_waveseq(chains):
    """A wave sequence bank: ``chains`` maps sequence -> list of wave numbers."""
    bank = bytearray(r.WAVESEQ_BANK_SIZE)
    step = 1
    for seq, waves in chains.items():
        head = r.SEQ_BASE + seq * r.SEQ_SIZE
        bank[head] = (step >> 8) & 0xFF
        bank[head + 1] = step & 0xFF
        first = step
        for k, wave in enumerate(waves):
            at = r.STEP_BASE + step * r.STEP_SIZE
            bank[at + 7] = wave
            nxt = step + 1 if k + 1 < len(waves) else 0
            r.set_step_links(bank, step, forward=nxt, back=0, loop=0)
            step += 1
        name = f"Seq{seq}".encode()[:r.SEQNAME_SIZE]
        at = r.SEQNAME_BASE + seq * r.SEQNAME_SIZE
        bank[at:at + len(name)] = name
    return bytes(bank)


class TestRecords:
    def test_sizes_divide_exactly(self):
        assert 16 + 21 + 8 * r.PART_SIZE == r.PERFORMANCE_SIZE
        assert 16 + 74 + 4 * r.OSC_SIZE == r.PATCH_SIZE
        assert (r.SEQUENCES_PER_BANK * r.SEQ_SIZE
                + r.STEPS_PER_BANK * r.STEP_SIZE
                + r.SEQUENCES_PER_BANK * r.SEQNAME_SIZE) == r.WAVESEQ_BANK_SIZE

    @pytest.mark.parametrize("bank", range(12))
    def test_part_bank_survives_its_split(self, bank):
        """Four bits live in bankNum and two in the top of partMode."""
        rec = bytearray(make_performance())
        r.set_performance_part(rec, 0, bank=bank, patch=7)
        got = r.performance_parts(rec)[0]
        assert got.bank == bank and got.patch == 7

    def test_rewriting_a_part_leaves_partmode_settings_alone(self):
        rec = bytearray(make_performance())
        at = r.PART_BASE + 4
        rec[at] = 0b00111111          # every non-bank bit set
        r.set_performance_part(rec, 0, bank=11, patch=3)
        assert rec[at] & 0x3F == 0b00111111, "unrelated settings were clobbered"
        assert r.performance_parts(rec)[0].bank == 11

    def test_wave_words_are_big_endian(self):
        rec = bytearray(make_patch())
        r.set_patch_wave(rec, 0, bank=2, wave=0x0102)
        at = r.OSC_BASE + 3
        assert rec[at] == 0x01 and rec[at + 1] == 0x02
        assert r.patch_waves(rec)[0].wave == 0x0102

    def test_a_wave_under_32_is_a_sequence(self):
        rec = bytearray(make_patch(waves=((1, 5), (0, 40), (0, 40), (0, 40))))
        waves = r.patch_waves(rec)
        assert waves[0].is_sequence and waves[0].bank == 1
        assert not waves[1].is_sequence

    def test_an_unused_part_is_not_a_dependency(self):
        rec = make_performance(parts=((0, 4),))
        assert deps.performance_dependencies(rec) == [(0, 4)]


class TestSequences:
    def test_walk_follows_the_chain(self):
        bank = make_waveseq({0: [40, 41, 42]})
        assert deps.walk_sequence(bank, 0).cost == 3

    def test_a_looping_chain_terminates(self):
        """A wave sequence that repeats is normal here, not corruption."""
        bank = bytearray(make_waveseq({0: [40, 41, 42]}))
        r.set_step_links(bank, 3, forward=1, back=0, loop=0)   # last -> first
        chain = deps.walk_sequence(bank, 0)
        assert chain.cost == 3, "the walk must stop rather than spin"


class TestPlan:
    def _sources(self):
        seqs = make_waveseq({0: [40, 41], 1: [42, 43, 44]})
        a = deps.Source(
            bank_id="A",
            patches=[make_patch("One", ((0, 0), (0, 40), (0, 40), (0, 40))),
                     make_patch("Two", ((0, 1), (0, 40), (0, 40), (0, 40))),
                     make_patch("Three")],
            performances=[make_performance("P-ok", ((0, 0), (0, 1))),
                          make_performance("P-no", ((0, 0), (0, 34)))],
            waveseq=seqs)
        return {"A": a}

    def test_patches_are_numbered_without_gaps(self):
        made = bb.plan(self._sources(), [("A", 2), ("A", 0)])
        assert sorted(made.patch_map.values()) == [0, 1]

    def test_only_fully_satisfied_performances_are_taken(self):
        made = bb.plan(self._sources(), [("A", 0), ("A", 1)])
        names = [i for _, i in made.performances]
        assert 0 in names, "every part of P-ok is present"
        assert 1 not in names, "P-no needs patch 34, which was not chosen"

    def test_the_patch_budget_is_reported_not_silently_applied(self):
        src = self._sources()
        src["A"].patches = [make_patch(f"P{i}") for i in range(40)]
        made = bb.plan(src, [("A", i) for i in range(40)])
        assert len(made.patch_map) == 35, "a bank holds 35 patches"
        assert len(made.rejected) == 5, "the other five must be reported"
        assert all("no patch slot left" in why for why in made.rejected)

    def test_a_missing_wave_sequence_bank_rejects_with_a_reason(self):
        src = self._sources()
        src["A"].waveseq = None
        made = bb.plan(src, [("A", 0)])
        assert not made.patch_map
        assert "wave sequence bank is not loaded" in made.rejected[0]


class TestBuild:
    def test_references_point_into_the_new_bank(self):
        src = TestPlan()._sources()
        made = bb.plan(src, [("A", 1), ("A", 0)])
        built = bb.build(src, made,
                         template_patches=bytes(35 * r.PATCH_SIZE),
                         template_performances=bytes(50 * r.PERFORMANCE_SIZE),
                         template_waveseq=bytes(r.WAVESEQ_BANK_SIZE),
                         destination_bank=11)
        # patch 1 of A was chosen first, so it is patch 0 of the new bank
        assert made.patch_map[("A", 1)] == 0
        slot = 0
        rec = built.performances[slot * r.PERFORMANCE_SIZE:
                                 (slot + 1) * r.PERFORMANCE_SIZE]
        for part in r.performance_parts(rec):
            if part.used:
                assert part.bank == 11, "a part still points at its old bank"
                assert part.patch < len(made.patch_map)

    def test_relocated_sequences_keep_their_steps(self):
        src = TestPlan()._sources()
        made = bb.plan(src, [("A", 0), ("A", 1)])
        built = bb.build(src, made,
                         template_patches=bytes(35 * r.PATCH_SIZE),
                         template_performances=bytes(50 * r.PERFORMANCE_SIZE),
                         template_waveseq=bytes(r.WAVESEQ_BANK_SIZE))
        for (bank_id, _wb, seq), slot in made.sequence_map.items():
            before = deps.walk_sequence(src[bank_id].waveseq, seq)
            after = deps.walk_sequence(built.waveseq, slot)
            assert before.cost == after.cost
            for old, new in zip(before.steps, after.steps):
                assert (r.step(src[bank_id].waveseq, old)["wave"]
                        == r.step(built.waveseq, new)["wave"])

    def test_untouched_template_slots_survive(self):
        src = TestPlan()._sources()
        made = bb.plan(src, [("A", 0)])
        template = bytearray(35 * r.PATCH_SIZE)
        template[34 * r.PATCH_SIZE:34 * r.PATCH_SIZE + 4] = b"KEEP"
        built = bb.build(src, made,
                         template_patches=bytes(template),
                         template_performances=bytes(50 * r.PERFORMANCE_SIZE),
                         template_waveseq=bytes(r.WAVESEQ_BANK_SIZE))
        assert built.patches[34 * r.PATCH_SIZE:34 * r.PATCH_SIZE + 4] == b"KEEP"
