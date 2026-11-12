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

"""The whole-bank copy's remap: which parts move, and which stay put.

A bank copied to another bank would still play its patches from the bank it
was copied from unless the parts that name the source itself are rewritten.
The interesting assertions are the negative ones: a part pointing at ROM or
at the Card must *not* follow the copy, because a RAM-to-RAM copy that drags
third-party references along is not a copy, it is a hijack.
"""

import pytest

from kws import records as r


def make_perf_bank(parts):
    """A one-record performance bank; ``parts`` maps part index -> (bank, patch)."""
    rec = bytearray(r.PERFORMANCE_SIZE)
    rec[0:6] = b"Invent"
    for index, (bank, patch) in parts.items():
        r.set_performance_part(rec, index, bank=bank, patch=patch)
    return bytes(rec)


def parts_of(bank, record=0):
    return r.performance_parts(bank[record * r.PERFORMANCE_SIZE :])


class TestTheRemap:
    def test_a_self_reference_follows_the_copy(self):
        source = make_perf_bank({0: (0, 7), 3: (0, 12)})
        out = r.remap_performance_banks(source, 0, 1)
        parts = parts_of(out)
        assert (parts[0].bank, parts[0].patch) == (1, 7)
        assert (parts[3].bank, parts[3].patch) == (1, 12)

    def test_a_card_destination_sets_both_halves_of_the_split_field(self):
        """The Card is bank 11, which needs the high bits in partMode."""
        source = make_perf_bank({0: (0, 5)})
        out = r.remap_performance_banks(source, 0, 11)
        assert parts_of(out)[0].bank == 11
        # and back again, which reads the high bits as well as writes them
        back = r.remap_performance_banks(out, 11, 0)
        assert parts_of(back)[0].bank == 0

    def test_a_card_source_is_remapped_the_same_way(self):
        source = make_perf_bank({1: (11, 3), 2: (0, 9)})
        out = r.remap_performance_banks(source, 11, 2)
        parts = parts_of(out)
        assert (parts[1].bank, parts[1].patch) == (2, 3)
        # a part naming RAM 1 is untouched: only self-references move
        assert (parts[2].bank, parts[2].patch) == (0, 9)

    def test_third_party_references_stay_put(self):
        source = make_perf_bank({0: (0, 1), 1: (3, 2), 2: (11, 4), 4: (1, 6)})
        out = r.remap_performance_banks(source, 0, 1)
        parts = parts_of(out)
        assert (parts[1].bank, parts[1].patch) == (3, 2)  # ROM 4
        assert (parts[2].bank, parts[2].patch) == (11, 4)  # the Card
        assert (parts[4].bank, parts[4].patch) == (1, 6)  # already RAM 2

    def test_an_unused_part_is_remapped_like_any_other(self):
        source = make_perf_bank({0: (0, r.UNUSED_PATCH)})
        out = r.remap_performance_banks(source, 0, 1)
        assert not parts_of(out)[0].used
        assert parts_of(out)[0].patch == r.UNUSED_PATCH

    def test_every_record_in_the_bank_is_remapped(self):
        record = make_perf_bank({0: (0, 1)})
        source = record * 50
        out = r.remap_performance_banks(source, 0, 1)
        for number in range(50):
            assert parts_of(out, number)[0].bank == 1

    def test_the_input_is_not_modified(self):
        source = make_perf_bank({0: (0, 1)})
        snapshot = bytes(source)
        r.remap_performance_banks(source, 0, 1)
        assert source == snapshot

    def test_nothing_else_changes(self):
        """Name, effects and bytes outside the parts are bit-identical."""
        source = make_perf_bank({0: (0, 1), 2: (4, 3)})
        out = r.remap_performance_banks(source, 0, 1)
        parts_region = slice(
            r.PART_BASE, r.PART_BASE + r.PARTS_PER_PERFORMANCE * r.PART_SIZE
        )
        for at in range(r.PERFORMANCE_SIZE):
            if parts_region.start <= at < parts_region.stop:
                continue  # the parts, checked by value below
            assert out[at] == source[at], f"byte {at} changed"
        parts = parts_of(out)
        assert (parts[0].bank, parts[0].patch) == (1, 1)  # moved with the copy
        assert (parts[2].bank, parts[2].patch) == (4, 3)  # ROM 5, untouched
        # the parts that were zeroed name RAM 1 by default, so they follow
        # the copy too -- which is what the unit's own BANK COPY does
        assert all(part.bank == 1 for part in parts if part.index not in (0, 2))


class TestRefusals:
    def test_a_bank_outside_zero_to_eleven_is_refused(self):
        bank = bytes(r.PERFORMANCE_SIZE)
        for bad in (-1, 12):
            with pytest.raises(ValueError, match="outside 0-11"):
                r.remap_performance_banks(bank, bad, 0)
            with pytest.raises(ValueError, match="outside 0-11"):
                r.remap_performance_banks(bank, 0, bad)

    def test_the_same_bank_on_both_sides_is_refused(self):
        with pytest.raises(ValueError, match="the same bank"):
            r.remap_performance_banks(bytes(r.PERFORMANCE_SIZE), 3, 3)

    def test_a_length_that_is_not_whole_records_is_refused(self):
        for size in (0, r.PERFORMANCE_SIZE - 1, r.PERFORMANCE_SIZE + 1):
            with pytest.raises(ValueError, match="181 bytes per record"):
                r.remap_performance_banks(bytes(size), 0, 1)
