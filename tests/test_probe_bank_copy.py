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
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.

"""The hardware probe's pure parts, tested before they judge a real copy.

The probe's whole job is to say whether the unit's panel BANK COPY remapped
a performance's references. A classifier that cannot tell the three answers
apart would give a confident wrong one on the bench, so the bank it builds
and the verdict it reaches are checked here against synthetic bytes.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from kws import banks
from kws import messages as m
from kws import records as rec

_PATH = Path(__file__).resolve().parents[1] / "tools" / "probe_bank_copy.py"
_spec = importlib.util.spec_from_file_location("probe_bank_copy_under_test", _PATH)
assert _spec is not None and _spec.loader is not None
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def _parts(blob: bytes, slot: int = 0) -> Any:
    at = slot * rec.PERFORMANCE_SIZE
    return rec.performance_parts(blob[at : at + rec.PERFORMANCE_SIZE])


class TestTheProbeBank:
    def test_it_has_whole_bank_sizes(self) -> None:
        built = probe.build_probe_bank(banks.bank("RAM2"))
        assert len(built["performances"]) == (
            banks.PERFORMANCES_PER_BANK * rec.PERFORMANCE_SIZE
        )
        assert len(built["patches"]) == banks.PATCHES_PER_BANK * rec.PATCH_SIZE
        assert len(built["waveseq"]) == rec.WAVESEQ_BANK_SIZE

    def test_slot_zero_names_the_source_and_rom_four(self) -> None:
        source = banks.bank("RAM2")
        built = probe.build_probe_bank(source)
        parts = _parts(built["performances"])
        assert (parts[0].bank, parts[0].patch) == (source.number, 1)
        assert (parts[1].bank, parts[1].patch) == (3, 2), "ROM 4 must not move"

    def test_every_other_part_is_unused(self) -> None:
        built = probe.build_probe_bank(banks.bank("RAM2"))
        for slot in range(banks.PERFORMANCES_PER_BANK):
            for part in _parts(built["performances"], slot):
                if slot == 0 and part.index in (0, 1):
                    continue
                assert part.patch == rec.UNUSED_PATCH, (slot, part.index)

    def test_the_names_are_legible(self) -> None:
        built = probe.build_probe_bank(banks.bank("CARD"))
        assert m.decode_name(built["performances"]) == probe.PROBE_PERFORMANCE
        at = probe.PROBE_PATCH_SLOT * rec.PATCH_SIZE
        assert (
            m.decode_name(built["patches"][at : at + rec.PATCH_SIZE])
            == probe.PROBE_PATCH
        )
        assert rec.sequence_names(built["waveseq"])[0] == probe.PROBE_SEQUENCE


class TestTheVerdict:
    def _built(self, source: str = "RAM2") -> Any:
        return probe.build_probe_bank(banks.bank(source))

    def test_a_remapped_copy_is_recognised(self) -> None:
        source = banks.bank("RAM2")
        destination = banks.bank("RAM3")
        built = self._built()
        got = dict(built)
        got["performances"] = rec.remap_performance_banks(
            built["performances"], source.number, destination.number
        )
        assert probe.classify(source, destination, built, got) == "remapped"

    def test_an_unremapped_copy_is_recognised(self) -> None:
        source = banks.bank("RAM2")
        destination = banks.bank("RAM3")
        built = self._built()
        assert probe.classify(source, destination, built, dict(built)) == ("unremapped")

    def test_any_other_difference_is_unexpected(self) -> None:
        source = banks.bank("RAM2")
        destination = banks.bank("RAM3")
        built = self._built()
        got = dict(built)
        got["patches"] = bytes([built["patches"][0] ^ 0xFF]) + built["patches"][1:]
        assert probe.classify(source, destination, built, got) == "unexpected"

    def test_the_card_direction_is_the_same_question(self) -> None:
        source = banks.bank("CARD")
        destination = banks.bank("RAM3")
        built = self._built("CARD")
        got = dict(built)
        got["performances"] = rec.remap_performance_banks(
            built["performances"], source.number, destination.number
        )
        assert probe.classify(source, destination, built, got) == "remapped"


class TestThePartTable:
    def test_it_names_the_two_references(self) -> None:
        built = probe.build_probe_bank(banks.bank("RAM2"))
        lines = probe.part_table(built["performances"])
        assert lines[0] == 'performance 00 "PROBE COPY"'
        assert "  part 0 -> RAM 2:01" in lines
        assert "  part 1 -> ROM 4:02" in lines
        assert "  part 2 unused" in lines


class TestTheMoveFrames:
    """The panel's MOVE BANK DATA? as the four documented changes.

    The numbers are Korg's parameter table (Edisyn names them); the bank
    values are System Exclusive bytes, not display positions -- RAM 3 is
    byte 4, and using its position (2) would move the wrong bank.
    """

    def test_the_type_values_are_the_ones_measured(self) -> None:
        assert probe.MOVE_TYPES == {
            "performance": 0,
            "patch": 1,
            "waveseq": 2,
            "all": 3,
        }

    def test_the_four_changes_are_source_type_destination_execute(self) -> None:
        frames = probe.bank_move_frames(
            banks.bank("RAM2"), banks.bank("RAM3"), "all", channel=4
        )
        assert len(frames) == 4
        expected = [(213, "+1"), (346, "+3"), (379, "+4"), (472, "+1")]
        for frame, (parameter, value) in zip(frames, expected):
            packet = m.parse(frame)
            assert packet is not None
            assert packet.channel == 4
            function = (
                m.Function.PARAMETER_CHANGE_SR
                if parameter >= 407
                else m.Function.PARAMETER_CHANGE
            )
            assert packet.function == function
            assert packet.payload[0] == parameter & 0x7F
            assert packet.payload[1] == (parameter >> 7) & 0x7F
            assert bytes(packet.payload[2:-1]).decode("ascii") == value

    def test_a_card_source_uses_the_card_sysex_byte(self) -> None:
        frames = probe.bank_move_frames(
            banks.bank("CARD"), banks.bank("RAM3"), "performance", channel=4
        )
        packet = m.parse(frames[0])
        assert packet is not None
        assert bytes(packet.payload[2:-1]).decode("ascii") == "+3"
        packet = m.parse(frames[1])
        assert packet is not None
        assert bytes(packet.payload[2:-1]).decode("ascii") == "0"

    def test_an_unknown_type_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no move type"):
            probe.bank_move_frames(
                banks.bank("RAM2"), banks.bank("RAM3"), "everything", channel=0
            )
