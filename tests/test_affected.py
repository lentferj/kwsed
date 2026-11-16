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
ROM5 = banks.bank("ROM5").number


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
