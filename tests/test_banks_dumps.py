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

"""Banks and bulk dumps. Fixtures are built here, not captured."""

import pytest

from kws import banks
from kws import dumps
from kws import messages as m


class TestBanks:
    def test_there_are_twelve(self):
        assert len(banks.BANKS) == 12
        assert banks.bank_numbers() == list(range(12))

    def test_four_are_writable(self):
        writable = [b.label for b in banks.writable_banks()]
        assert writable == ["RAM 1", "RAM 2", "RAM 3", "Card"]

    def test_a_bank_holds_both_kinds(self):
        assert len(banks.slots(0, banks.Kind.PERFORMANCE)) == 50
        assert len(banks.slots(0, banks.Kind.PATCH)) == 35

    def test_performance_seven_is_not_patch_seven(self):
        """Different objects; the catalog keys on kind for this reason."""
        a = banks.slot(0, banks.Kind.PERFORMANCE, 7)
        b = banks.slot(0, banks.Kind.PATCH, 7)
        assert a != b and a.key != b.key

    def test_banks_are_reachable_by_number_or_id(self):
        assert banks.bank(11) is banks.bank("CARD")
        assert banks.bank(0) is banks.bank("RAM 1")

    def test_out_of_range_is_refused(self):
        with pytest.raises(LookupError, match="0-11"):
            banks.bank(12)
        with pytest.raises(LookupError, match="0-34"):
            banks.slot(0, banks.Kind.PATCH, 35)


def _bulk(function, bank_number, kind, size, count, names=None):
    raw = bytearray()
    for index in range(count):
        record = bytearray(size)
        label = (names or {}).get(index, f"Slot{index:02d}")
        encoded = label.encode("ascii")
        record[:len(encoded)] = encoded
        record[m.NAME_LEN] = 0x80 | (index & 0x7F)
        raw.extend(record)
    payload = [bank_number] + list(m.to_nibbles(bytes(raw)))
    return m.parse(m.frame(function, payload))


class TestBulkDumps:
    def test_a_performance_bank_decodes(self):
        dump = dumps.parse_bulk(_bulk(
            m.Function.ALL_PERFORMANCE_DUMP, 0, banks.Kind.PERFORMANCE,
            dumps.PERFORMANCE_RECORD, 50))
        assert dump.count == 50
        assert dump.bank.label == "RAM 1"
        assert dump.kind == banks.Kind.PERFORMANCE

    def test_a_patch_bank_decodes(self):
        dump = dumps.parse_bulk(_bulk(
            m.Function.ALL_PATCH_DUMP, 11, banks.Kind.PATCH,
            dumps.PATCH_RECORD, 35))
        assert dump.count == 35
        assert dump.bank.label == "Card"

    def test_names_survive_the_nibble_encoding(self):
        dump = dumps.parse_bulk(_bulk(
            m.Function.ALL_PERFORMANCE_DUMP, 0, banks.Kind.PERFORMANCE,
            dumps.PERFORMANCE_RECORD, 50, {0: "Woven Bell", 49: "Amber Pad"}))
        assert dump.name(0) == "Woven Bell"
        assert dump.name(49) == "Amber Pad"

    def test_a_high_bit_byte_survives(self):
        """Records carry one on purpose: all-7-bit data would let a broken
        nibble implementation pass."""
        dump = dumps.parse_bulk(_bulk(
            m.Function.ALL_PATCH_DUMP, 0, banks.Kind.PATCH,
            dumps.PATCH_RECORD, 35))
        assert dump.records[3][m.NAME_LEN] == 0x83

    def test_the_bank_comes_from_the_dump_not_the_request(self):
        dump = dumps.parse_bulk(_bulk(
            m.Function.ALL_PATCH_DUMP, 7, banks.Kind.PATCH,
            dumps.PATCH_RECORD, 35))
        assert dump.bank.number == 7


class TestRefusals:
    def test_a_wrong_function_is_refused(self):
        with pytest.raises(m.ProtocolError, match="not a bank dump"):
            dumps.parse_bulk(m.parse(m.frame(m.Function.WRITE_COMPLETED, [0])))

    def test_a_dump_that_does_not_divide_is_refused(self):
        payload = [0] + list(m.to_nibbles(bytes(50 * 181 + 7)))
        packet = m.parse(m.frame(m.Function.ALL_PERFORMANCE_DUMP, payload))
        with pytest.raises(m.ProtocolError, match="whole number"):
            dumps.parse_bulk(packet)

    def test_a_short_dump_is_refused(self):
        payload = [0] + list(m.to_nibbles(bytes(10 * 181)))
        packet = m.parse(m.frame(m.Function.ALL_PERFORMANCE_DUMP, payload))
        with pytest.raises(m.ProtocolError, match="has 50"):
            dumps.parse_bulk(packet)
