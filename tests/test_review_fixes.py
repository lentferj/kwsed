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

"""Regressions for what a code review found. Fixtures here are invented."""

import pytest

from kws import banks, deps, records as r
from kws import bankbuild as bb
from kws import messages as m


def _waveseq(chains):
    bank = bytearray(r.WAVESEQ_BANK_SIZE)
    for seq, steps in chains.items():
        head = r.SEQ_BASE + seq * r.SEQ_SIZE
        bank[head] = (steps[0] >> 8) & 0xFF
        bank[head + 1] = steps[0] & 0xFF
        for index, step in enumerate(steps):
            nxt = steps[index + 1] if index + 1 < len(steps) else 0
            r.set_step_links(bank, step, forward=nxt, back=0, loop=0)
            bank[r.STEP_BASE + step * r.STEP_SIZE + 7] = 40 + step
    return bytes(bank)


def _patch(name, wave):
    rec = bytearray(r.PATCH_SIZE)
    rec[0 : len(name)] = name.encode()
    for index in range(4):
        r.set_patch_wave(rec, index, bank=0, wave=wave if index == 0 else 40)
    return bytes(rec)


def _occupied(waveseq):
    return sum(
        1
        for i in range(1, r.STEPS_PER_BANK)
        if any(
            waveseq[r.STEP_BASE + i * r.STEP_SIZE : r.STEP_BASE + (i + 1) * r.STEP_SIZE]
        )
    )


class TestSharedStepsAreCountedAndCopiedOnce:
    """plan() counts a shared step once, so build() must copy it once.

    They disagreed: plan deduplicated into a set while build kept a
    per-sequence map and an ever-rising index, so a step two sequences share
    was laid out twice. On a bank near the 501-step ceiling the plan then
    accepted a set that did not fit, and past step 500 the writes land on
    the sequence-name table.
    """

    def _sources(self):
        source = deps.Source(
            bank_id="A",
            performances=[],
            patches=[_patch("One", 0), _patch("Two", 1)],
            waveseq=_waveseq({0: [1, 2, 3], 1: [2, 3]}),
        )
        return {"A": source}

    def _build(self, sources, made):
        return bb.build(
            sources,
            made,
            template_patches=bytes(35 * r.PATCH_SIZE),
            template_performances=bytes(50 * r.PERFORMANCE_SIZE),
            template_waveseq=bytes(r.WAVESEQ_BANK_SIZE),
        )

    def test_the_plan_and_the_build_agree(self):
        sources = self._sources()
        made = bb.plan(sources, [("A", 0), ("A", 1)])
        built = self._build(sources, made)
        assert _occupied(built.waveseq) == made.steps_used

    def test_a_shared_step_is_stored_once(self):
        sources = self._sources()
        made = bb.plan(sources, [("A", 0), ("A", 1)])
        built = self._build(sources, made)
        # two chains of 3 and 2 sharing two steps: three slots, not five
        assert _occupied(built.waveseq) == 3

    def test_both_chains_still_resolve(self):
        """Sharing must not cost either sequence its own steps."""
        sources = self._sources()
        made = bb.plan(sources, [("A", 0), ("A", 1)])
        built = self._build(sources, made)
        for (bank_id, _wave_bank, seq), slot in made.sequence_map.items():
            before = deps.walk_sequence(sources[bank_id].waveseq, seq)
            after = deps.walk_sequence(built.waveseq, slot)
            assert [
                r.step(sources[bank_id].waveseq, s)["wave"] for s in before.steps
            ] == [r.step(built.waveseq, s)["wave"] for s in after.steps]

    def test_two_source_banks_do_not_share_step_numbers(self):
        """Step 7 of one bank is not step 7 of another."""
        sources = {
            "A": deps.Source(
                bank_id="A",
                performances=[],
                patches=[_patch("One", 0)],
                waveseq=_waveseq({0: [1, 2]}),
            ),
            "B": deps.Source(
                bank_id="B",
                performances=[],
                patches=[_patch("Two", 0)],
                waveseq=_waveseq({0: [1, 2]}),
            ),
        }
        made = bb.plan(sources, [("A", 0), ("B", 0)])
        built = self._build(sources, made)
        assert made.steps_used == 4, "the same index in two banks is two steps"
        assert _occupied(built.waveseq) == 4


class TestTheCommandLineNamesItsKind:
    def test_dump_takes_a_kind_and_defaults_to_patch(self):
        from kwsed.cli import build_parser

        assert build_parser().parse_args(["dump", "CARD"]).kind == banks.Kind.PATCH
        assert (
            build_parser().parse_args(["dump", "CARD", "performance"]).kind
            == banks.Kind.PERFORMANCE
        )

    def test_patch_pluralises_properly(self):
        """It read "all patchs" in the prompt for a destructive write."""
        from kwsed.cli import _plural

        assert _plural("patch") == "patches"
        assert _plural("performance") == "performances"
        assert _plural("waveseq") == "wave sequences"

    def test_the_command_line_module_imports(self):
        """A syntax error in cli.py once passed the whole suite."""
        import importlib

        import kwsed.cli

        importlib.reload(kwsed.cli)
        assert hasattr(kwsed.cli, "main")


class TestTheDemoResolvesWireBytes:
    def test_the_card_is_reported_as_the_card(self):
        """Byte 3 is the Card; banks.bank() would call it ROM 4."""
        from kwsed.demo import DemoDevice

        _, names = DemoDevice().read_bank_names("CARD")
        assert names
        assert banks.by_sysex(3).label == "Card"
