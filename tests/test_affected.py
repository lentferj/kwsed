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

"""The affected-performance analyser. Every fixture name here is invented."""

from typing import List

from kws import affected, banks, deps
from tests.test_bankbuild import make_patch, make_performance

RAM1 = banks.bank("RAM1").number
RAM2 = banks.bank("RAM2").number
RAM3 = banks.bank("RAM3").number
ROM5 = banks.bank("ROM5").number
ROM11 = banks.bank("ROM11").number
CARD = banks.bank("CARD").number

# Display positions (what banks.bank, PartRef.bank and written_banks use):
# RAM 2 = 1, RAM 3 = 2, ROM 5 = 4, ROM 11 = 10, Card = 11.
# Wire bytes (Bank.sysex) differ: RAM 3 = 4, Card = 3, ROM 11 = 2.
assert (RAM1, RAM2, RAM3, ROM5, ROM11, CARD) == (0, 1, 2, 4, 10, 11)


def _source(scope_id: str, performances: List[bytes]) -> deps.Source:
    return deps.Source(
        scope_id=scope_id,
        performances=performances,
        patches=[make_patch("Invented Patch") for _ in range(3)],
    )


def test_own_bank_counts_when_it_is_written():
    source = _source("RAM1", [make_performance("Self Ref", ((RAM1, 2),))])
    got = affected.affected_performances({"RAM1": source}, {RAM1})
    assert got == {
        "RAM1": affected.AffectedBank(
            bank_id="RAM1",
            performances=(affected.AffectedPerformance(0, 1, (RAM1,)),),
            part_total=1,
            banks=(RAM1,),
        )
    }
    assert got["RAM1"].count == 1


def test_another_written_bank_counts_while_an_unwritten_one_does_not():
    source = _source(
        "RAM1",
        [make_performance("Cross Ref", ((RAM2, 3), (RAM1, 4)))],
    )
    got = affected.affected_performances({"RAM1": source}, {RAM2})
    assert got["RAM1"].performances == (affected.AffectedPerformance(0, 1, (RAM2,)),)
    assert got["RAM1"].part_total == 1
    assert got["RAM1"].banks == (RAM2,)


def test_rom_only_and_self_plus_rom_ignore_another_written_bank():
    source = _source(
        "RAM1",
        [
            make_performance("Rom Only", ((ROM5, 0),)),
            make_performance("Self Rom", ((RAM1, 1), (ROM5, 2))),
        ],
    )
    assert affected.affected_performances({"RAM1": source}, {RAM2}) == {}
    # ROM is not special-cased: the caller's set decides.
    got = affected.affected_performances({"RAM1": source}, {ROM5})
    assert got["RAM1"].performances == (
        affected.AffectedPerformance(0, 1, (ROM5,)),
        affected.AffectedPerformance(1, 1, (ROM5,)),
    )


def test_empty_slots_are_ignored():
    source = _source(
        "RAM1",
        [
            make_performance("", ()),
            make_performance("Kept", ((RAM1, 1),)),
            make_performance("", ()),
        ],
    )
    got = affected.affected_performances({"RAM1": source}, {RAM1})
    assert got["RAM1"].performances == (affected.AffectedPerformance(1, 1, (RAM1,)),)


def test_banks_absent_from_sources_are_not_reported():
    source = _source("RAM1", [make_performance("Outward", ((RAM2, 0),))])
    got = affected.affected_performances({"RAM1": source}, [RAM2, RAM1, RAM2])
    assert list(got) == ["RAM1"]
    assert affected.affected_performances({}, {RAM1, RAM2}) == {}


def test_multiple_banks_are_sorted_and_summarised():
    ram1 = _source(
        "RAM1",
        [
            make_performance("Harbour Glass", ((RAM1, 0), (RAM2, 1))),
            make_performance("Amber Drift", ((RAM2, 2), (ROM5, 3))),
        ],
    )
    ram2 = _source("RAM2", [make_performance("Crossing Deck", ((RAM2, 4),))])
    card = _source("CARD", [make_performance("Kelp Forest", ((RAM1, 5), (RAM2, 6)))])
    # Deliberately not in bank-id order.
    sources = {"RAM2": ram2, "CARD": card, "RAM1": ram1}

    got = affected.affected_performances(sources, (RAM2, RAM1))
    assert list(got) == ["CARD", "RAM1", "RAM2"]
    assert got["RAM1"].performances == (
        affected.AffectedPerformance(0, 2, (RAM1, RAM2)),
        affected.AffectedPerformance(1, 1, (RAM2,)),
    )
    assert got["RAM1"].count == 2 and got["RAM1"].part_total == 3
    assert got["RAM1"].banks == (RAM1, RAM2)
    assert got["RAM2"].banks == (RAM2,)
    assert got["CARD"].banks == (RAM1, RAM2)

    assert affected.affected_summary(got) == (
        "Card: 1 performances, 2 parts, naming RAM 1, RAM 2; "
        "RAM 1: 2 performances, 3 parts, naming RAM 1, RAM 2; "
        "RAM 2: 1 performances, 1 parts, naming RAM 2"
    )


def test_empty_summary():
    assert affected.affected_summary({}) == ""


def test_determinism_and_alias_resolution():
    first = deps.Source(
        scope_id="RAM1#session",
        bank_number=RAM1,
        performances=[make_performance("Alias One", ((RAM2, 0),))],
    )
    second = deps.Source(
        scope_id="CARD/07",
        bank_number=banks.bank("CARD").number,
        performances=[make_performance("Alias Two", ((RAM1, 1),))],
    )
    forward = affected.affected_performances(
        {"RAM1#session": first, "CARD/07": second}, {RAM1, RAM2}
    )
    backward = affected.affected_performances(
        {"CARD/07": second, "RAM1#session": first}, [RAM2, RAM1, RAM1]
    )
    assert list(forward) == ["CARD", "RAM1"]
    assert forward == backward
    assert forward["RAM1"].bank_id == "RAM1"
    assert forward["CARD"].bank_id == "CARD"
    for entry in forward.values():
        assert [p.slot for p in entry.performances] == sorted(
            p.slot for p in entry.performances
        )
        assert list(entry.banks) == sorted(set(entry.banks))


def test_card_part_is_matched_by_position_not_wire_byte():
    """Card is display position 11, wire byte 3; the analyser uses positions."""
    source = _source("RAM2", [make_performance("Invented Card", ((CARD, 4),))])
    got = affected.affected_performances({"RAM2": source}, {CARD})
    assert list(got) == ["RAM2"]
    entry = got["RAM2"]
    assert entry.count == 1
    assert entry.part_total == 1
    assert entry.performances == (affected.AffectedPerformance(0, 1, (CARD,)),)
    assert entry.banks == (CARD,)
    # Writing ROM 11 (position 10) must not touch a Card part, even though the
    # Card's wire byte is 3 and could be mistaken for another position.
    assert affected.affected_performances({"RAM2": source}, {ROM11}) == {}


def test_split_bank_field_is_matched_by_position_not_wire_byte():
    """RAM 3 spans two byte halves: position 2, wire byte 4."""
    source = _source("RAM1", [make_performance("Split Field", ((RAM3, 6),))])
    got = affected.affected_performances({"RAM1": source}, {RAM3})
    assert got["RAM1"].performances == (affected.AffectedPerformance(0, 1, (RAM3,)),)
    assert got["RAM1"].banks == (RAM3,)
    # Position 4 is ROM 5; 4 is only RAM 3's *wire* byte, not its position.
    # A {4} write must therefore not count a RAM 3 part.
    assert affected.affected_performances({"RAM1": source}, {ROM5}) == {}


def test_mixed_self_card_rom_counts_only_the_named_position():
    """Self is RAM 2 = position 1, never 2 (that is RAM 3).

    The one performance below, in RAM 2, names self (1), Card (11) and
    ROM 5 (4). So {11} affects exactly the Card part (parts == 1), while
    {2} affects nothing: position 2 is RAM 3, which no part names here.
    The wire bytes (Card 3, RAM 3 4) and the self position 1 are deliberately
    different, so the assertion is unambiguous about which one is compared.
    """
    parts = ((RAM2, 0), (CARD, 4), (ROM5, 3))
    source = _source("RAM2", [make_performance("Mixed Banks", parts)])
    got = affected.affected_performances({"RAM2": source}, {CARD})
    assert got["RAM2"].performances == (affected.AffectedPerformance(0, 1, (CARD,)),)
    assert got["RAM2"].part_total == 1
    assert got["RAM2"].banks == (CARD,)
    assert affected.affected_performances({"RAM2": source}, {RAM3}) == {}
    # The self part does name position 1, so {1} affects it -- the exact
    # number matters, not a nearby one.
    self_got = affected.affected_performances({"RAM2": source}, {RAM2})
    assert self_got["RAM2"].performances == (
        affected.AffectedPerformance(0, 1, (RAM2,)),
    )
