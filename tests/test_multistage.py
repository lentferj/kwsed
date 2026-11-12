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

"""Multi-source staging. Every fixture here is invented."""

import dataclasses

import pytest

from kws import banks, deps, multi, multistage, perfcopy, relations
from kws import records as r
from tests.test_bankbuild import make_patch, make_performance, make_waveseq

RAM3 = banks.bank("RAM3")
RAM1 = banks.bank("RAM1")
RAM2 = banks.bank("RAM2")


def _src(scope_id, patches=None, performances=None, waveseq=None):
    return deps.Source(
        scope_id=scope_id,
        patches=patches if patches is not None else [],
        performances=performances if performances is not None else [],
        waveseq=waveseq,
    )


def _make_base_multiset():
    parts = []
    for ch in range(1, 17):
        parts.append(multi.Part(channel=ch, bank=0, performance=50, level=100, flag=1))
    return multi.Multiset(number=0, name="Test", parts=tuple(parts))


def test_needs_staging_rom_self_contained_false():
    src = _src(
        "ROM4",
        patches=[make_patch("R4")],
        performances=[make_performance("P", ((3, 0),))],
    )
    # part bank=3 resolves to ROM4 (resident) not card
    sources = {"ROM4": src}
    perf = relations.PerfId("ROM4", 0)
    assert multistage.needs_staging(perf, sources) is False


def test_needs_staging_card_default_true():
    src = _src(
        "CARD/07",
        patches=[make_patch("C")],
        performances=[make_performance("P", ((11, 0),))],
    )
    sources = {"CARD/07": src}
    perf = relations.PerfId("CARD/07", 0)
    assert multistage.needs_staging(perf, sources) is True


def test_needs_staging_card_resident_false():
    src = _src(
        "CARD/07",
        patches=[make_patch("C")],
        performances=[make_performance("P", ((11, 0),))],
    )
    sources = {"CARD/07": src}
    perf = relations.PerfId("CARD/07", 0)
    assert (
        multistage.needs_staging(
            perf, sources, resident_card_scope="CARD/07", stage_all_cards=False
        )
        is False
    )


def test_needs_staging_ram_part_card_true():
    src = _src(
        "RAM1",
        patches=[make_patch("R")],
        performances=[make_performance("P", ((11, 0),))],
    )
    # part bank=11 resolves to CARD scope for a RAM1 perf.
    sources = {"RAM1": src}
    perf = relations.PerfId("RAM1", 0)
    assert multistage.needs_staging(perf, sources) is True


def test_needs_staging_force_atomic_true():
    src = _src(
        "ROM4",
        patches=[make_patch("R4")],
        performances=[make_performance("P", ((3, 0),))],
    )
    sources = {"ROM4": src}
    perf = relations.PerfId("ROM4", 0)
    assert multistage.needs_staging(perf, sources, force_atomic=True) is True


def _build_assembly_sources():
    # 3 HCard banks each with one perf using 2-3 patches (part bank=11)
    sources = {}
    for card in ("CARD/05", "CARD/12", "CARD/27"):
        patches = [make_patch(f"P{i}") for i in range(3)]
        perf = make_performance("Perf", ((11, 0), (11, 1), (11, 2)))
        sources[card] = _src(card, patches=patches, performances=[perf])
    # 2 physical cards
    for card in ("CARD/c1", "CARD/c2"):
        patches = [make_patch(f"P{i}") for i in range(2)]
        perf = make_performance("Perf", ((11, 0), (11, 1)))
        sources[card] = _src(card, patches=patches, performances=[perf])
    # resident ROM4 performance referencing ROM4 patches (part bank=3)
    rom_patches = [make_patch("R0"), make_patch("R1")]
    rom_perf = make_performance("RomPerf", ((3, 0), (3, 1)))
    sources["ROM4"] = _src("ROM4", patches=rom_patches, performances=[rom_perf])
    return sources


def test_assembly():
    sources = _build_assembly_sources()
    base_ms = _make_base_multiset()
    desired = []
    channels_cards = ["CARD/05", "CARD/12", "CARD/27", "CARD/c1", "CARD/c2"]
    for ch, scope in enumerate(channels_cards):
        desired.append(
            multistage.DesiredChannel(channel=ch + 1, perf=relations.PerfId(scope, 0))
        )
    # ROM4 channel
    desired.append(
        multistage.DesiredChannel(channel=6, perf=relations.PerfId("ROM4", 0))
    )
    # pad remaining channels unused? base has performance=50 already
    plan = multistage.plan_multi_staging(
        base_ms, desired, sources, target_banks=("RAM3",), mode="append"
    )
    assert plan.fits
    # ROM4 channel not staged
    rom_res = next(r for r in plan.channels if r.channel == 6)
    assert rom_res.staged is False
    assert rom_res.target_bank == banks.bank("ROM4")
    assert rom_res.target_perf_slot == 0
    updated_rom_part = plan.updated_multiset.parts[5]
    assert updated_rom_part.bank == banks.bank("ROM4").sysex
    # 5 card performances staged
    staged_results = [r for r in plan.channels if r.staged]
    assert len(staged_results) == 5
    for res in staged_results:
        assert res.target_bank == banks.bank("RAM3")
    # slots 0..4
    slots = sorted(res.target_perf_slot for res in staged_results)
    assert slots == [0, 1, 2, 3, 4]
    for res in staged_results:
        part = plan.updated_multiset.parts[res.channel - 1]
        assert part.bank == banks.bank("RAM3").sysex
        assert part.performance == res.target_perf_slot
    # built banks
    assert list(plan.built_banks.keys()) == ["RAM3"]
    built = plan.built_banks["RAM3"]
    # patch count sum unique patches: 3+3+3+2+2 = 13
    assert built.plan.patch_count == 13
    # no overlap of patch slots
    all_slots: list[int] = []
    for res in staged_results:
        all_slots.extend(res.target_patch_slots)
    assert len(all_slots) == len(set(all_slots))
    # every built performance active parts bank == RAM3 number and patch inside range
    for i in range(5):
        rec_bytes = built.performances[
            i * r.PERFORMANCE_SIZE : (i + 1) * r.PERFORMANCE_SIZE
        ]
        for ref in r.performance_parts(rec_bytes):
            if ref.used:
                assert ref.bank == banks.bank("RAM3").number
                assert 0 <= ref.patch < built.plan.patch_count
    # decoding performance records confirms slots named by target_patch_slots
    # Map channel->result
    for res in staged_results:
        rec_bytes = built.performances[
            res.target_perf_slot * r.PERFORMANCE_SIZE : (res.target_perf_slot + 1)
            * r.PERFORMANCE_SIZE
        ]
        parts = [p for p in r.performance_parts(rec_bytes) if p.used]
        for p in parts:
            assert p.patch in res.target_patch_slots


def test_multi_bank_spillover():
    # 12 performances, each 4 distinct patches in CARD/05
    patches = [make_patch(f"P{i}") for i in range(4)]
    perfs = [
        make_performance(f"Perf{i}", ((11, 0), (11, 1), (11, 2), (11, 3)))
        for i in range(12)
    ]
    src = _src("CARD/05", patches=patches, performances=perfs)
    sources = {"CARD/05": src}
    base_ms = _make_base_multiset()
    desired = [
        multistage.DesiredChannel(channel=i + 1, perf=relations.PerfId("CARD/05", i))
        for i in range(12)
    ]
    plan = multistage.plan_multi_staging(
        base_ms, desired, sources, target_banks=("RAM1", "RAM2"), mode="clean"
    )
    assert plan.fits
    assert set(plan.built_banks.keys()) == {"RAM1", "RAM2"}
    # counts
    ram1_res = [x for x in plan.channels if x.target_bank == RAM1]
    ram2_res = [x for x in plan.channels if x.target_bank == RAM2]
    assert len(ram1_res) == 8
    assert len(ram2_res) == 4
    assert plan.built_banks["RAM1"].plan.patch_count == 32
    assert plan.built_banks["RAM2"].plan.patch_count == 16
    assert plan.built_banks["RAM1"].plan.patch_count <= 35
    assert plan.built_banks["RAM2"].plan.patch_count <= 35
    # wire bytes
    for x in ram1_res:
        part = plan.updated_multiset.parts[x.channel - 1]
        assert part.bank == RAM1.sysex  # 0
    for x in ram2_res:
        part = plan.updated_multiset.parts[x.channel - 1]
        assert part.bank == RAM2.sysex  # 1


def test_budget_exhaustion():
    patches = [make_patch(f"P{i}") for i in range(4)]
    perfs = [
        make_performance(f"Perf{i}", ((11, 0), (11, 1), (11, 2), (11, 3)))
        for i in range(12)
    ]
    src = _src("CARD/05", patches=patches, performances=perfs)
    sources = {"CARD/05": src}
    base_ms = _make_base_multiset()
    desired = [
        multistage.DesiredChannel(channel=i + 1, perf=relations.PerfId("CARD/05", i))
        for i in range(12)
    ]
    base_parts = list(base_ms.parts)
    plan = multistage.plan_multi_staging(
        base_ms, desired, sources, target_banks=("RAM1",), mode="clean"
    )
    assert not plan.fits
    assert any("patch" in r.lower() for r in plan.rejected)
    staged = [x for x in plan.channels if x.staged]
    rejected_res = [
        x for x in plan.channels if not x.staged and x.source_perf is not None
    ]
    assert len(staged) == 8
    assert len(rejected_res) == 4
    # remaining 4 results unchanged parts
    for x in rejected_res:
        ch = x.channel
        assert plan.updated_multiset.parts[ch - 1] == base_parts[ch - 1]
    assert "RAM1" in plan.built_banks
    assert plan.built_banks["RAM1"].plan.patch_count == 32


def test_rom_target_raises():
    sources: dict[str, deps.Source] = {}
    base_ms = _make_base_multiset()
    desired: list[multistage.DesiredChannel] = []
    with pytest.raises(ValueError):
        multistage.plan_multi_staging(base_ms, desired, sources, target_banks=("ROM4",))


def test_needs_staging_unloaded_card_true():
    """A card bank that was never captured cannot be left pointed at."""
    sources = {"RAM3": _src("RAM3")}
    assert multistage.needs_staging(relations.PerfId("CARD/07", 0), sources) is True


def test_needs_staging_unloaded_internal_false():
    """An internal scope resolves through banks.bank() even without records."""
    sources = {"RAM3": _src("RAM3")}
    assert multistage.needs_staging(relations.PerfId("ROM6", 0), sources) is False


def test_an_unloaded_card_channel_is_rejected_not_crashed():
    """Before the fix this raised LookupError from banks.bank('CARD/07')."""
    base = _make_base_multiset()
    parts = list(base.parts)
    parts[0] = dataclasses.replace(
        parts[0], bank=banks.bank("CARD").sysex, performance=0
    )
    base = dataclasses.replace(base, parts=tuple(parts))
    sources = {"RAM3": _src("RAM3")}
    desired = [
        multistage.DesiredChannel(channel=1, perf=relations.PerfId("CARD/07", 0))
    ]
    plan = multistage.plan_multi_staging(base, desired, sources, target_banks=("RAM3",))
    assert plan.fits is False
    assert plan.built_banks == {}
    assert any("not loaded" in reason for reason in plan.rejected)
    assert plan.updated_multiset.parts[0].bank == banks.bank("CARD").sysex
    assert plan.updated_multiset.parts[0].performance == 0
