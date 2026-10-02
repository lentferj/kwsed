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
    rec[0 : len(name)] = name.encode()
    for i, (bank, wave) in enumerate(waves):
        r.set_patch_wave(rec, i, bank=bank, wave=wave)
    return bytes(rec)


def make_performance(name="Invented", parts=((0, 0),)):
    rec = bytearray(r.PERFORMANCE_SIZE)
    rec[0 : len(name)] = name.encode()
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
        name = f"Seq{seq}".encode()[: r.SEQNAME_SIZE]
        at = r.SEQNAME_BASE + seq * r.SEQNAME_SIZE
        bank[at : at + len(name)] = name
    return bytes(bank)


class TestRecords:
    def test_sizes_divide_exactly(self):
        assert 16 + 21 + 8 * r.PART_SIZE == r.PERFORMANCE_SIZE
        assert 16 + 74 + 4 * r.OSC_SIZE == r.PATCH_SIZE
        assert (
            r.SEQUENCES_PER_BANK * r.SEQ_SIZE
            + r.STEPS_PER_BANK * r.STEP_SIZE
            + r.SEQUENCES_PER_BANK * r.SEQNAME_SIZE
        ) == r.WAVESEQ_BANK_SIZE

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
        rec[at] = 0b00111111  # every non-bank bit set
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
        r.set_step_links(bank, 3, forward=1, back=0, loop=0)  # last -> first
        chain = deps.walk_sequence(bank, 0)
        assert chain.cost == 3, "the walk must stop rather than spin"


class TestPlan:
    def _sources(self):
        seqs = make_waveseq({0: [40, 41], 1: [42, 43, 44]})
        a = deps.Source(
            bank_id="A",
            patches=[
                make_patch("One", ((0, 0), (0, 40), (0, 40), (0, 40))),
                make_patch("Two", ((0, 1), (0, 40), (0, 40), (0, 40))),
                make_patch("Three"),
            ],
            performances=[
                make_performance("P-ok", ((0, 0), (0, 1))),
                make_performance("P-no", ((0, 0), (0, 34))),
            ],
            waveseq=seqs,
        )
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
        built = bb.build(
            src,
            made,
            template_patches=bytes(35 * r.PATCH_SIZE),
            template_performances=bytes(50 * r.PERFORMANCE_SIZE),
            template_waveseq=bytes(r.WAVESEQ_BANK_SIZE),
            destination_bank=11,
        )
        # patch 1 of A was chosen first, so it is patch 0 of the new bank
        assert made.patch_map[("A", 1)] == 0
        slot = 0
        rec = built.performances[
            slot * r.PERFORMANCE_SIZE : (slot + 1) * r.PERFORMANCE_SIZE
        ]
        for part in r.performance_parts(rec):
            if part.used:
                assert part.bank == 11, "a part still points at its old bank"
                assert part.patch < len(made.patch_map)

    def test_relocated_sequences_keep_their_steps(self):
        src = TestPlan()._sources()
        made = bb.plan(src, [("A", 0), ("A", 1)])
        built = bb.build(
            src,
            made,
            template_patches=bytes(35 * r.PATCH_SIZE),
            template_performances=bytes(50 * r.PERFORMANCE_SIZE),
            template_waveseq=bytes(r.WAVESEQ_BANK_SIZE),
        )
        for (bank_id, wave_bank, seq), slot in made.sequence_map.items():
            before = deps.walk_sequence(src[bank_id].waveseq, seq)
            after = deps.walk_sequence(built.waveseq, slot)
            assert before.cost == after.cost
            for old, new in zip(before.steps, after.steps):
                was = r.step(src[bank_id].waveseq, old)["wave"]
                now = r.step(built.waveseq, new)["wave"]
                if was < r.ROM_WAVE_BASE:
                    # not a wave at all: a quote of another sequence, which
                    # is renumbered with everything else it points at
                    assert now == made.sequence_map[(bank_id, wave_bank, was)]
                else:
                    assert now == was

    def test_untouched_template_slots_survive(self):
        src = TestPlan()._sources()
        made = bb.plan(src, [("A", 0)])
        template = bytearray(35 * r.PATCH_SIZE)
        template[34 * r.PATCH_SIZE : 34 * r.PATCH_SIZE + 4] = b"KEEP"
        built = bb.build(
            src,
            made,
            template_patches=bytes(template),
            template_performances=bytes(50 * r.PERFORMANCE_SIZE),
            template_waveseq=bytes(r.WAVESEQ_BANK_SIZE),
        )
        assert built.patches[34 * r.PATCH_SIZE : 34 * r.PATCH_SIZE + 4] == b"KEEP"


def _build(sources, made):
    """The three templates a build needs, all empty. Invented, as ever."""
    return bb.build(
        sources,
        made,
        template_patches=bytes(35 * r.PATCH_SIZE),
        template_performances=bytes(50 * r.PERFORMANCE_SIZE),
        template_waveseq=bytes(r.WAVESEQ_BANK_SIZE),
    )


class TestASequenceThatQuotesAnother:
    """A step's wave number below 32 plays a *sequence*, not a wave.

    That is the one reference in the format that survives being copied
    wrong: any byte is a legal sequence number, so a stale one addresses
    some other sequence in the destination and the bank stays well-formed.
    The checksum agrees, the read-back after a write agrees, and the sound
    is missing. Everything in this class exists because of that.
    """

    def _sources(self):
        """Sequence 0 ends by quoting sequence 7; 7 quotes nothing."""
        waveseq = make_waveseq({0: [40, 41, 7], 7: [50, 51]})
        return {
            "A": deps.Source(
                bank_id="A",
                patches=[make_patch("Quoter", waves=((0, 0),) * 4)],
                performances=[],
                waveseq=waveseq,
            )
        }

    def test_walk_sequence_reports_the_quote_against_its_bank(self):
        src = self._sources()
        chain = deps.walk_sequence(src["A"].waveseq, 0, bank_id="A")
        assert chain.bank_id == "A"
        assert chain.quotes == (("A", 7),), (
            "a quote is only actionable as 'sequence N of this bank'"
        )

    def test_the_closure_pulls_in_what_is_quoted(self):
        src = self._sources()
        order, chains, unresolved = deps.quoted_closure(src, [("A", 0, 0)])
        assert not unresolved
        assert order == [("A", 0, 0), ("A", 0, 7)], (
            "the quoted sequence must travel, and after the one quoting it"
        )
        assert chains[("A", 0, 7)].steps

    def test_a_circular_quote_terminates(self):
        """Two sequences that quote each other are legal and must not hang."""
        waveseq = make_waveseq({0: [40, 7], 7: [50, 0]})
        src = {
            "A": deps.Source(
                bank_id="A", waveseq=waveseq, patches=[make_patch(waves=((0, 0),) * 4)]
            )
        }
        order, _, unresolved = deps.quoted_closure(src, [("A", 0, 0)])
        assert not unresolved
        assert sorted(order) == [("A", 0, 0), ("A", 0, 7)]

    def test_the_plan_carries_the_quoted_sequence(self):
        src = self._sources()
        made = bb.plan(src, [("A", 0)], include_performances=False)
        assert made.fits
        assert ("A", 0, 7) in made.sequence_map, (
            "the plan took the patch but left a sequence it needs behind"
        )

    def test_the_built_quote_points_at_the_quoted_sequences_new_slot(self):
        src = self._sources()
        made = bb.plan(src, [("A", 0)], include_performances=False)
        built = _build(src, made)

        where = made.sequence_map[("A", 0, 7)]
        chain = deps.walk_sequence(built.waveseq, made.sequence_map[("A", 0, 0)])
        quotes = [
            r.step(built.waveseq, s)["wave"]
            for s in chain.steps
            if r.step(built.waveseq, s)["wave"] < r.ROM_WAVE_BASE
        ]
        assert quotes == [where], (
            f"the step should quote slot {where}, the quoted sequence's new "
            f"home, not its number in the source bank"
        )

    def test_no_step_in_a_built_bank_quotes_an_empty_slot(self):
        """The failure this whole class is about, stated as the invariant."""
        src = self._sources()
        made = bb.plan(src, [("A", 0)], include_performances=False)
        built = _build(src, made)

        filled = set(made.sequence_map.values())
        for slot in filled:
            for index in deps.walk_sequence(built.waveseq, slot).steps:
                wave = r.step(built.waveseq, index)["wave"]
                if wave < r.ROM_WAVE_BASE:
                    assert wave in filled, (
                        f"slot {slot} quotes sequence {wave}, which the build "
                        f"zeroed; that step plays nothing"
                    )

    def test_two_wave_banks_do_not_share_one_relocated_step(self):
        """One physical step cannot hold two different quote targets.

        A patch may name the same sequence number in two different wave
        banks. Both resolve to the one wave sequence bank a Source holds, so
        the chains walk identical steps -- but they are separate entries with
        separate destination slots, and each needs its quote pointed at its
        own copy of what it quotes. Relocating by source bank and step alone
        gave them one shared step, and whichever was written second took the
        other's target.
        """
        waveseq = make_waveseq({0: [40, 7], 7: [50, 51]})
        src = {
            "A": deps.Source(
                bank_id="A",
                waveseq=waveseq,
                patches=[
                    make_patch("Two banks", waves=((0, 0), (2, 0), (0, 40), (0, 40)))
                ],
            )
        }
        made = bb.plan(src, [("A", 0)], include_performances=False)
        assert ("A", 0, 0) in made.sequence_map
        assert ("A", 2, 0) in made.sequence_map

        built = _build(src, made)
        for wave_bank in (0, 2):
            slot = made.sequence_map[("A", wave_bank, 0)]
            want = made.sequence_map[("A", wave_bank, 7)]
            got = [
                r.step(built.waveseq, s)["wave"]
                for s in deps.walk_sequence(built.waveseq, slot).steps
                if r.step(built.waveseq, s)["wave"] < r.ROM_WAVE_BASE
            ]
            assert got == [want], (
                f"wave bank {wave_bank}'s copy quotes {got}, not {want}"
            )

    def test_the_budget_counts_the_quoted_sequences_steps(self):
        """A quote costs slots and steps, and the plan has to know."""
        src = self._sources()
        tight = bb.Budget(sequences=1)
        made = bb.plan(src, [("A", 0)], budget=tight, include_performances=False)
        assert made.rejected, "one slot cannot hold a sequence and the one it quotes"


class TestAChainThatRunsOffTheEnd:
    def test_an_out_of_range_forward_link_stops_the_walk(self):
        """Three files in an ordinary library of 828 do this.

        They end a chain with a forward link of 0x0700 rather than 0, in
        banks that are otherwise well formed. Before this stop the walk
        raised out of records.step and took the bank builder with it.
        """
        bank = bytearray(make_waveseq({0: [40, 41]}))
        last = deps.walk_sequence(bank, 0).steps[-1]
        r.set_step_links(bank, last, forward=0x0700, back=0, loop=0)

        chain = deps.walk_sequence(bytes(bank), 0)
        assert chain.steps == (1, 2), "the walk should stop, not raise"
