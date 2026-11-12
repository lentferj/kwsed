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

"""The dependency graph: what a patch drags along, and loading it from files.

Every fixture is invented. The interesting property throughout is that
nothing here raises for an incomplete library -- a missing wave-sequence
bank is reported, because the useful answer is "these four patches cannot
come, and this is why", not a traceback.
"""

import pytest

from kws import banks, deps
from kws import messages as m
from kws import records as r


def make_patch(waves=((0, 0), (0, 40), (0, 40), (0, 40)), name="Invented"):
    rec = bytearray(r.PATCH_SIZE)
    rec[0 : len(name)] = name.encode()
    for index, (bank, wave) in enumerate(waves):
        r.set_patch_wave(rec, index, bank=bank, wave=wave)
    return bytes(rec)


def make_waveseq(chains):
    """``chains`` maps a sequence number to the waves its steps play."""
    bank = bytearray(r.WAVESEQ_BANK_SIZE)
    step = 1
    for seq, waves in chains.items():
        head = r.SEQ_BASE + seq * r.SEQ_SIZE
        bank[head] = (step >> 8) & 0xFF
        bank[head + 1] = step & 0xFF
        for index, wave in enumerate(waves):
            at = r.STEP_BASE + step * r.STEP_SIZE
            bank[at + 7] = wave
            nxt = step + 1 if index + 1 < len(waves) else 0
            r.set_step_links(bank, step, forward=nxt, back=0, loop=0)
            step += 1
    return bytes(bank)


class TestSourceLookup:
    def test_a_patch_past_the_end_says_how_many_there_are(self):
        source = deps.Source(scope_id="A", patches=[make_patch()])
        with pytest.raises(LookupError, match="has 1 patches"):
            source.patch(3)

    def test_any_of_the_three_may_be_absent(self):
        source = deps.Source(scope_id="A")
        assert source.performances == []
        assert source.patches == []
        assert source.waveseq is None


class TestClosure:
    def _sources(self):
        return {
            "A": deps.Source(
                scope_id="A",
                patches=[make_patch(waves=((0, 0), (0, 1), (0, 40), (0, 40)))],
                waveseq=make_waveseq({0: [40, 41], 1: [42, 43, 44]}),
            )
        }

    def test_it_reports_what_the_patches_drag_along(self):
        found = deps.closure(self._sources(), [("A", 0)])
        sequences = found["sequences"]
        assert isinstance(sequences, list)
        assert found["patches"] == [("A", 0)]
        assert sorted(sequences) == [("A", 0, 0), ("A", 0, 1)]
        assert found["steps"] == 5
        assert found["missing"] == []

    def test_an_unloaded_patch_is_reported_not_raised(self):
        found = deps.closure(self._sources(), [("A", 0), ("A", 9)])
        assert found["patches"] == [("A", 0)]
        assert found["missing"] == ["A:009 (patch not loaded)"]

    def test_a_patch_from_an_unknown_bank_is_reported(self):
        found = deps.closure(self._sources(), [("Z", 0)])
        assert found["patches"] == []
        assert found["missing"] == ["Z:000 (patch not loaded)"]

    def test_a_missing_waveseq_bank_names_the_sequence_it_needed(self):
        """The library answer, not a traceback."""
        sources = self._sources()
        sources["A"].waveseq = None
        found = deps.closure(sources, [("A", 0)])
        missing = found["missing"]
        assert isinstance(missing, list)
        assert found["patches"] == [("A", 0)], "the patch itself still resolves"
        assert found["sequences"] == []
        assert len(missing) == 2, "both oscillators' sequences"
        assert "wave sequence bank is not loaded" in missing[0]

    def test_two_patches_naming_the_same_sequence_pay_for_it_once(self):
        """The dedup is per sequence and across patches, not per patch."""
        one = ((0, 0), (0, 40), (0, 40), (0, 40))
        sources = {
            "A": deps.Source(
                scope_id="A",
                patches=[make_patch(waves=one), make_patch(waves=one)],
                waveseq=make_waveseq({0: [40, 41]}),
            )
        }
        found = deps.closure(sources, [("A", 0), ("A", 1)])
        patches = found["patches"]
        assert isinstance(patches, list)
        assert len(patches) == 2
        assert found["sequences"] == [("A", 0, 0)], "named by both, listed once"
        assert found["steps"] == 2, "the chain is walked once, not twice"

    def test_a_patch_of_rom_waves_needs_no_sequences(self):
        rom = ((0, 40), (0, 41), (0, 40), (0, 41))
        sources = {
            "A": deps.Source(
                scope_id="A", patches=[make_patch(waves=rom)], waveseq=make_waveseq({})
            )
        }
        found = deps.closure(sources, [("A", 0)])
        assert found["sequences"] == []
        assert found["steps"] == 0
        assert found["missing"] == []


class TestQuotedClosureBounds:
    def test_a_sequence_number_past_the_bank_is_unresolved(self):
        """Cannot happen on real data; here so a truncated bank cannot walk
        the builder off the end."""
        sources = {"A": deps.Source(scope_id="A", waveseq=make_waveseq({0: [40]}))}
        order, _, unresolved = deps.quoted_closure(sources, [("A", 0, 200)])
        assert order == []
        assert unresolved == [("A", 0, 200)]

    def test_a_seed_whose_source_has_no_waveseq_bank_is_unresolved(self):
        sources = {"A": deps.Source(scope_id="A")}
        order, _, unresolved = deps.quoted_closure(sources, [("A", 0, 0)])
        assert order == []
        assert unresolved == [("A", 0, 0)]


class TestLoadSyx:
    def _write(self, tmp_path, name, function, payload, bank=3):
        path = tmp_path / name
        path.write_bytes(bytes(m.frame_bulk(function, bank, payload)))
        return path

    def test_it_collects_the_three_kinds_into_one_source(self, tmp_path):
        patch = self._write(
            tmp_path, "p.syx", m.Function.ALL_PATCH_DUMP, bytes(35 * r.PATCH_SIZE)
        )
        perf = self._write(
            tmp_path,
            "f.syx",
            m.Function.ALL_PERFORMANCE_DUMP,
            bytes(50 * r.PERFORMANCE_SIZE),
        )
        seq = tmp_path / "s.syx"
        seq.write_bytes(
            bytes(
                m.frame_bulk(m.Function.ALL_WAVESEQ_DUMP, 3, bytes(r.WAVESEQ_BANK_SIZE))
            )
        )
        source = deps.load_syx([str(patch), str(perf), str(seq)], "A")
        assert len(source.patches) == 35
        assert len(source.performances) == 50
        assert source.waveseq is not None
        assert len(source.waveseq) == r.WAVESEQ_BANK_SIZE

    def test_a_source_with_only_patches_is_normal(self, tmp_path):
        patch = self._write(
            tmp_path, "p.syx", m.Function.ALL_PATCH_DUMP, bytes(35 * r.PATCH_SIZE)
        )
        source = deps.load_syx([str(patch)], "A")
        assert len(source.patches) == 35
        assert source.performances == []
        assert source.waveseq is None

    def test_a_waveseq_dump_too_short_is_ignored_not_stored(self, tmp_path):
        """Better an absent bank than half of one."""
        seq = tmp_path / "s.syx"
        seq.write_bytes(bytes(m.frame_bulk(m.Function.ALL_WAVESEQ_DUMP, 3, bytes(100))))
        assert deps.load_syx([str(seq)], "A").waveseq is None

    def test_a_message_of_another_kind_is_passed_over(self, tmp_path):
        other = tmp_path / "other.syx"
        other.write_bytes(bytes(m.frame(m.Function.WRITE_COMPLETED, [0])))
        source = deps.load_syx([str(other)], "A")
        assert source.patches == [] and source.performances == []


class TestWalkSequenceStops:
    def test_a_chain_that_loops_back_on_itself_terminates(self):
        """Step 0 pointing at step 0 is the smallest possible loop."""
        bank = bytearray(make_waveseq({0: [40, 41]}))
        r.set_step_links(bank, 1, forward=0, back=0, loop=0)
        chain = deps.walk_sequence(bytes(bank), 0)
        assert chain.steps == (1,)

    def test_a_sequence_with_no_header_walks_to_nothing(self):
        bank = make_waveseq({0: [40]})
        chain = deps.walk_sequence(bank, 5)
        assert chain.steps == ()
        assert chain.cost == 0

    def test_the_limit_bounds_a_pathological_chain(self):
        """A pool-sized limit, so a link graph cannot spin forever."""
        bank = bytearray(make_waveseq({0: [40]}))
        # point every step at the next, wrapping at the pool edge
        for index in range(1, 20):
            r.set_step_links(bank, index, forward=index + 1, back=0, loop=0)
        chain = deps.walk_sequence(bytes(bank), 0, limit=5)
        assert chain.cost == 5


class TestRecordRefusals:
    """The bounds checks, which exist so a wrong input says so."""

    def test_a_short_performance_is_refused(self):
        with pytest.raises(ValueError, match="181 bytes"):
            r.performance_parts(bytes(100))

    def test_a_short_patch_is_refused(self):
        with pytest.raises(ValueError, match="426 bytes"):
            r.patch_waves(bytes(100))

    def test_a_short_waveseq_bank_is_refused(self):
        with pytest.raises(ValueError, match="8784 bytes"):
            r.sequence_names(bytes(100))

    def test_a_part_index_outside_eight_is_refused(self):
        rec = bytearray(r.PERFORMANCE_SIZE)
        with pytest.raises(ValueError, match="outside 0-7"):
            r.set_performance_part(rec, 8, bank=0, patch=0)

    def test_a_bank_outside_zero_to_eleven_is_refused(self):
        rec = bytearray(r.PERFORMANCE_SIZE)
        for bad in (-1, 12):
            with pytest.raises(ValueError, match="outside 0-11"):
                r.set_performance_part(rec, 0, bank=bad, patch=0)

    def test_an_oscillator_index_outside_four_is_refused(self):
        rec = bytearray(r.PATCH_SIZE)
        with pytest.raises(ValueError, match="outside 0-3"):
            r.set_patch_wave(rec, 4, bank=0, wave=0)

    def test_a_sequence_outside_thirty_two_is_refused(self):
        bank = bytes(r.WAVESEQ_BANK_SIZE)
        with pytest.raises(ValueError, match="outside 0-31"):
            r.sequence_header(bank, 32)

    def test_a_step_outside_the_pool_is_refused(self):
        bank = bytes(r.WAVESEQ_BANK_SIZE)
        with pytest.raises(ValueError, match="outside 0-500"):
            r.step(bank, r.STEPS_PER_BANK)
        with pytest.raises(ValueError, match="outside 0-500"):
            r.set_step_links(bytearray(bank), 501, forward=0, back=0, loop=0)

    def test_a_step_wave_that_will_not_fit_is_refused(self):
        bank = bytearray(r.WAVESEQ_BANK_SIZE)
        with pytest.raises(ValueError, match="does not fit"):
            r.set_step_wave(bank, 0, 0x100)

    def test_a_sequence_name_is_read_even_when_empty(self):
        bank = bytes(r.WAVESEQ_BANK_SIZE)
        assert r.sequence_names(bank) == [""] * r.SEQUENCES_PER_BANK

    def test_an_unused_part_is_recognised(self):
        rec = bytearray(r.PERFORMANCE_SIZE)
        r.set_performance_part(rec, 0, bank=0, patch=r.UNUSED_PATCH)
        assert not r.performance_parts(bytes(rec))[0].used

    def test_the_bank_split_survives_both_halves(self):
        rec = bytearray(r.PERFORMANCE_SIZE)
        for bank in range(12):
            r.set_performance_part(rec, 0, bank=bank, patch=1)
            assert r.performance_parts(bytes(rec))[0].bank == bank


class TestWaveRefBoundaries:
    def test_the_boundary_between_a_sequence_and_a_wave_is_thirty_two(self):
        rec = bytearray(make_patch())
        r.set_patch_wave(rec, 0, bank=0, wave=r.ROM_WAVE_BASE - 1)
        assert r.patch_waves(bytes(rec))[0].is_sequence
        r.set_patch_wave(rec, 0, bank=0, wave=r.ROM_WAVE_BASE)
        assert not r.patch_waves(bytes(rec))[0].is_sequence

    def test_a_wave_extension_byte_can_subtract(self):
        """waveNum is 16-bit and waveNumExp is signed, so 0x0100 - 1 = 255."""
        rec = bytearray(make_patch())
        r.set_patch_wave(rec, 0, bank=0, wave=0x0100)
        assert r.patch_waves(bytes(rec))[0].wave == 0x0100
        rec[90 + 0 * 84 + 83] = 0xFF  # -1
        assert r.patch_waves(bytes(rec))[0].wave == 0x00FF


def test_the_bank_list_is_what_the_wire_uses():
    """The mapping the whole project exists to get right."""
    assert banks.by_sysex(3).label == "Card"
    assert banks.by_sysex(11).label == "ROM 10"
    with pytest.raises(LookupError, match="no bank carries"):
        banks.by_sysex(12)
