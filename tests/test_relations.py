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

"""Tests for kws/relations.py."""

from __future__ import annotations

from typing import Dict

from kws import banks, deps, multi
from kws import records as r
from kws import relations as rel
from tests.test_bankbuild import (
    make_patch,
    make_performance,
    make_waveseq,
)


def _source_with_waveseq():
    seqs = make_waveseq({0: [40, 41], 1: [42, 43, 44]})[0]
    src = deps.Source(
        bank_id="RAM1",
        patches=[
            make_patch("One", ((0, 0), (0, 40), (0, 40), (0, 40))),
            make_patch("Two", ((0, 1), (0, 40), (0, 40), (0, 40))),
        ],
        performances=[
            make_performance("PerfZero", ((0, 0), (0, 1))),
            make_performance("PerfOne", ((0, 0),)),
        ],
        waveseq=seqs,
    )
    return src


def test_resolve_part_scope():
    assert rel.resolve_part_scope(11, "CARD/07") == "CARD/07"
    assert rel.resolve_part_scope(11, "CARD/c1") == "CARD/c1"
    assert rel.resolve_part_scope(11, "RAM1") == "CARD"
    assert rel.resolve_part_scope(2, "CARD/07") == "RAM3"
    assert rel.resolve_part_scope(0, "RAM1") == "RAM1"


def test_forward_relations():
    src_ram = _source_with_waveseq()
    src_ram.bank_id = "RAM1"

    # CARD/07 source
    seqs_card = make_waveseq({0: [40]})[0]
    src_card = deps.Source(
        bank_id="CARD/07",
        patches=[make_patch("CardPatch", ((0, 0), (0, 40), (0, 40), (0, 40)))],
        performances=[make_performance("CardPerf", ((11, 0),))],
        waveseq=seqs_card,
    )

    sources = {"RAM1": src_ram, "CARD/07": src_card}

    # Multiset
    ms_parts = []
    for ch in range(1, 17):
        if ch == 1:
            ms_parts.append(
                multi.Part(channel=1, bank=0, performance=0, level=100, flag=1)
            )
        elif ch == 2:
            ms_parts.append(
                multi.Part(channel=2, bank=3, performance=0, level=90, flag=1)
            )
        else:
            ms_parts.append(
                multi.Part(channel=ch, bank=0, performance=50, level=0, flag=0)
            )
    ms = multi.Multiset(number=0, name="TestMulti", parts=tuple(ms_parts))
    graph = rel.RelationGraph(sources, [ms], active_card_scope="CARD/07")

    perf_rel = graph.performance_relation(rel.PerfId("RAM1", 0))
    assert len(perf_rel.parts) == 2
    assert len(perf_rel.unique_patches) == 2
    assert perf_rel.sequences  # non-empty
    assert perf_rel.step_cost > 0
    assert perf_rel.name == "PerfZero"

    mrels = graph.multi_relations(0)
    assert len(mrels) == 2
    channels = [m.channel for m in mrels]
    assert channels == [1, 2]
    card_rel = next(m for m in mrels if m.channel == 2)
    assert card_rel.perf == rel.PerfId("CARD/07", 0)

    # card scope resolves part bank 11 to CARD/07
    card_perf_rel = graph.performance_relation(rel.PerfId("CARD/07", 0))
    assert card_perf_rel.parts[0].source_patch.scope_id == "CARD/07"


def test_reverse_lookups():
    src_ram = _source_with_waveseq()
    sources = {"RAM1": src_ram}
    graph = rel.RelationGraph(sources)

    assert graph.performances_using_patch(rel.PatchId("RAM1", 0)) == [
        rel.PerfId("RAM1", 0),
        rel.PerfId("RAM1", 1),
    ]
    assert graph.shared_patches_in_bank("RAM1") == {0: [0, 1]}
    unreferenced = graph.unreferenced_patches_in_bank("RAM1")
    assert 0 not in unreferenced and 1 not in unreferenced
    assert all(p in unreferenced for p in range(2, 35))
    assert graph.multisets_using_performance(rel.PerfId("RAM1", 0)) == []


def test_multisets_using_performance():
    src_ram = _source_with_waveseq()
    sources = {"RAM1": src_ram}
    ms_parts = []
    for ch in range(1, 17):
        if ch == 1:
            ms_parts.append(
                multi.Part(channel=1, bank=0, performance=0, level=100, flag=1)
            )
        else:
            ms_parts.append(
                multi.Part(channel=ch, bank=0, performance=50, level=0, flag=0)
            )
    ms = multi.Multiset(number=0, name="M", parts=tuple(ms_parts))
    graph = rel.RelationGraph(sources, [ms])
    assert graph.multisets_using_performance(rel.PerfId("RAM1", 0)) == [(0, 1)]


def test_intra_perf_dedup():
    src = _source_with_waveseq()
    # performance with parts 0 and 1 both (0,0), part 2 (0,1)
    src.performances = [make_performance("Dedup", ((0, 0), (0, 0), (0, 1)))]
    sources = {"RAM1": src}
    graph = rel.RelationGraph(sources)
    perf_rel = graph.performance_relation(rel.PerfId("RAM1", 0))
    assert len(perf_rel.unique_patches) == 2


def test_duplicate_detection():
    # two patch slots identical bytes different names
    p0 = make_patch("Alpha", ((0, 40), (0, 40), (0, 40), (0, 40)))
    p1 = make_patch("Beta", ((0, 40), (0, 40), (0, 40), (0, 40)))
    p2 = make_patch("Gamma", ((0, 41), (0, 40), (0, 40), (0, 40)))
    src = deps.Source(
        bank_id="RAM1", patches=[p0, p1, p2], performances=[], waveseq=None
    )
    sources = {"RAM1": src}
    graph = rel.RelationGraph(sources)
    dups = graph.duplicate_patches_in_bank("RAM1")
    # grouped
    found = False
    for _digest, nums in dups.items():
        if sorted(nums) == [0, 1]:
            found = True
    assert found
    # p2 not in any group of >=2
    for nums in dups.values():
        assert 2 not in nums

    # include_name splits same content different name
    d0 = rel.patch_structural_digest(p0, "RAM1", sources, include_name=True)
    d1 = rel.patch_structural_digest(p1, "RAM1", sources, include_name=True)
    assert d0 != d1


def test_digest_invariance_relocation():
    # source A RAM1 waveseq seq0 3-step chain; patch P0 osc0 (bank=0,wave=0)
    seqs_a = make_waveseq({0: [40, 41, 42]})[0]
    p0 = make_patch("P0", ((0, 0), (0, 40), (0, 40), (0, 40)))
    src_a = deps.Source(bank_id="RAM1", patches=[p0], performances=[], waveseq=seqs_a)

    # source B RAM3 patch byte copy but osc0 rewritten bank=RAM3 number, wave=1;
    # waveseq identical chain relocated seq1
    p1_bytes = bytearray(p0)
    r.set_patch_wave(p1_bytes, 0, bank=banks.bank("RAM3").number, wave=1)
    p1 = bytes(p1_bytes)
    seqs_b = make_waveseq({1: [40, 41, 42]})[0]
    src_b = deps.Source(bank_id="RAM3", patches=[p1], performances=[], waveseq=seqs_b)

    sources = {"RAM1": src_a, "RAM3": src_b}
    d0 = rel.patch_structural_digest(p0, "RAM1", sources)
    d1 = rel.patch_structural_digest(p1, "RAM3", sources)
    assert d0 == d1

    # ROM wave patch: same digest when oscillator bank byte differs
    rom_patch_a = make_patch("Rom", ((0, 40), (0, 40), (0, 40), (0, 40)))
    # change bank byte in a copy manually? bank byte is at osc+2;
    # set_patch_wave with bank different but same wave
    rom_patch_b_bytes = bytearray(
        make_patch("Rom", ((0, 40), (0, 40), (0, 40), (0, 40)))
    )
    # manually alter bank byte for osc0
    rom_patch_b_bytes[r.OSC_BASE + 2] = 5
    rom_patch_b = bytes(rom_patch_b_bytes)
    # digest should ignore bank for ROM waves (wave>=32)
    da = rel.patch_structural_digest(rom_patch_a, "RAM1", sources)
    db = rel.patch_structural_digest(rom_patch_b, "RAM1", sources)
    assert da == db

    # changing ROM wave number changes digest
    rom_patch_c = make_patch("Rom2", ((0, 41), (0, 40), (0, 40), (0, 40)))
    dc = rel.patch_structural_digest(rom_patch_c, "RAM1", sources)
    assert dc != da

    # changing step duration changes digest
    seqs_c = make_waveseq({0: [40, 41, 42]})[0]
    # modify duration of first step
    bank = bytearray(seqs_c)
    # find first step link
    hdr = r.sequence_header(bank, 0)
    first = hdr["link"]
    r.set_step_links(
        bank, first, forward=r.step(bank, first)["forward"], back=0, loop=0
    )
    # set duration field (offset 12) to different
    at = r.STEP_BASE + first * r.STEP_SIZE + 12
    bank[at] = 0
    bank[at + 1] = 99
    seqs_c2 = bytes(bank)
    src_c = deps.Source(bank_id="RAM1", patches=[p0], performances=[], waveseq=seqs_c2)
    sources2 = {"RAM1": src_c, "RAM3": src_b}
    d0b = rel.patch_structural_digest(p0, "RAM1", sources2)
    assert d0b != d0


def test_quote_handling():
    # sequence 0 quotes sequence 1
    seqs = make_waveseq(
        {0: [0], 1: [40]}
    )  # wave 0 (<32) quotes seq 0? need wave number = seq number
    # Actually step wave <32 plays sequence number = wave. To quote seq 1, wave=1.
    seqs = make_waveseq({0: [1], 1: [40]})[0]
    p0 = make_patch("Q", ((0, 0), (0, 40), (0, 40), (0, 40)))
    src_a = deps.Source(bank_id="RAM1", patches=[p0], performances=[], waveseq=seqs)

    # relocated equivalent: seq chain in seq 2 (quoted seq 3)
    seqs_b = make_waveseq({2: [3], 3: [40]})[0]
    p1_bytes = bytearray(p0)
    r.set_patch_wave(p1_bytes, 0, bank=banks.bank("RAM3").number, wave=2)
    p1 = bytes(p1_bytes)
    src_b = deps.Source(bank_id="RAM3", patches=[p1], performances=[], waveseq=seqs_b)

    sources = {"RAM1": src_a, "RAM3": src_b}
    d0 = rel.patch_structural_digest(p0, "RAM1", sources)
    d1 = rel.patch_structural_digest(p1, "RAM3", sources)
    assert d0 == d1

    # different quoted chain -> different digest
    seqs_c = make_waveseq({0: [1], 1: [41]})[0]
    src_c = deps.Source(bank_id="RAM1", patches=[p0], performances=[], waveseq=seqs_c)
    sources2 = {"RAM1": src_c, "RAM3": src_b}
    d2 = rel.patch_structural_digest(p0, "RAM1", sources2)
    assert d2 != d0


def test_missing_bank():
    src = _source_with_waveseq()
    src.performances = [
        make_performance("P", ((5, 0),))
    ]  # patch bank 5 (ROM6) not loaded
    # Actually patch bank 5 resolves to ROM6, not in sources
    sources = {"RAM1": src}
    graph = rel.RelationGraph(sources)
    perf_rel = graph.performance_relation(rel.PerfId("RAM1", 0))
    assert perf_rel.missing  # non-empty


def test_multi_relations_empty_unknown():
    graph = rel.RelationGraph({})
    assert graph.multi_relations(999) == []


def test_performance_relation_missing_raises():
    graph = rel.RelationGraph({})
    import pytest

    with pytest.raises(LookupError):
        graph.performance_relation(rel.PerfId("RAM1", 0))


def test_init_performances_do_not_pollute_where_used_or_shared():
    src_ram1 = deps.Source(
        bank_id="RAM1",
        patches=[make_patch("P", ((0, 40), (0, 40), (0, 40), (0, 40)))],
        performances=[
            make_performance("RealPerf", ((0, 0),)),
            make_performance("Init", ((0, 0),)),
        ],
        waveseq=None,
    )
    src_ram2 = deps.Source(
        bank_id="RAM2",
        patches=[make_patch("P", ((0, 40), (0, 40), (0, 40), (0, 40)))],
        performances=[make_performance("Init", ((0, 0),))],
        waveseq=None,
    )
    sources = {"RAM1": src_ram1, "RAM2": src_ram2}
    graph = rel.RelationGraph(sources)
    assert graph.performances_using_patch(rel.PatchId("RAM1", 0)) == [
        rel.PerfId("RAM1", 0)
    ]
    assert graph.shared_patches_in_bank("RAM1") == {}


def test_rom11_bank_exp_2_resolves_to_rom11_not_ram3():
    rec_bytes = bytearray(make_performance("P", ()))
    rec_bytes[r.PART_BASE] = 2
    rec_bytes[r.PART_BASE + 1] = 16
    rec_bytes[r.PART_BASE + 4] = 0
    src = deps.Source(
        bank_id="RAM1", patches=[], performances=[bytes(rec_bytes)], waveseq=None
    )
    sources = {"RAM1": src}
    graph = rel.RelationGraph(sources)
    rela = graph.performance_relation(rel.PerfId("RAM1", 0))
    assert rela.parts[0].source_patch == rel.PatchId("ROM11", 16)
