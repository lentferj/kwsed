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

"""The wire format. Names here are invented, per CLAUDE.md."""

import pytest

from kws import messages as m


class TestNibbles:
    """Two MIDI bytes carry one data byte, LOW nibble first.

    A different scheme from the Korg X5 family's eight-to-seven packing, and
    reading one for the other yields a stream that still decodes.
    """

    def test_round_trip(self):
        for data in (b"", b"\x00", b"\xff", bytes(range(256)),
                     b"Woven Bell"):
            assert m.from_nibbles(m.to_nibbles(data)) == data

    def test_low_nibble_comes_first(self):
        assert m.to_nibbles(bytes([0xAB])) == bytes([0x0B, 0x0A])

    def test_one_byte_becomes_two(self):
        assert len(m.to_nibbles(bytes(10))) == 20

    def test_a_trailing_half_byte_is_dropped(self):
        """Half a byte is not a byte; inventing the other half would put a
        plausible value at the end of a record."""
        assert m.from_nibbles(bytes([0x01])) == b""
        assert m.from_nibbles(bytes([0x01, 0x02, 0x03])) == bytes([0x21])

    def test_a_byte_with_bit_seven_set_is_refused(self):
        with pytest.raises(m.ProtocolError):
            m.from_nibbles(bytes([0x80, 0x00]))


class TestDataOffset:
    """Each message kind starts its data in a different place.

    One byte out pairs every nibble with the wrong partner, which does not
    fail -- it yields plausible, wrong bytes. That cost this project its
    longest detour.
    """

    def test_a_bulk_dump_starts_after_the_bank_byte(self):
        assert m.DATA_OFFSET[m.Function.ALL_PERFORMANCE_DUMP] == 6
        assert m.DATA_OFFSET[m.Function.ALL_PATCH_DUMP] == 6

    def test_a_single_dump_starts_one_later(self):
        """It carries a number as well as a bank."""
        assert m.DATA_OFFSET[m.Function.PERFORMANCE_DUMP] == 7

    def test_the_packet_uses_the_right_one(self):
        payload = [3] + list(m.to_nibbles(b"Woven Bell"))
        packet = m.parse(m.frame(m.Function.ALL_PERFORMANCE_DUMP, payload))
        assert packet.data()[:10] == b"Woven Bell"
        assert packet.bank == 3

    def test_a_non_dump_has_no_offset(self):
        packet = m.parse(m.frame(m.Function.WRITE_COMPLETED))
        with pytest.raises(m.ProtocolError):
            packet.data()


class TestChannel:
    def test_the_channel_is_zero_based(self):
        assert m.channel_byte(0) == 0x30
        assert m.channel_byte(4) == 0x34

    def test_round_trip(self):
        for channel in range(16):
            assert m.channel_of(m.channel_byte(channel)) == channel

    def test_out_of_range_is_refused_not_masked(self):
        for channel in (-1, 16):
            with pytest.raises(m.ProtocolError):
                m.channel_byte(channel)


class TestFrames:
    def test_the_header_is_the_documented_one(self):
        assert m.frame(m.Function.BULK_REQUEST, [2], channel=4) == bytes(
            [0xF0, 0x42, 0x34, 0x28, 0x1C, 0x02, 0xF7])

    def test_another_korg_model_is_not_ours(self):
        """36h is the X5 family; 28h is the Wavestation."""
        assert m.parse([0xF0, 0x42, 0x30, 0x36, 0x4C, 0xF7]) is None

    def test_foreign_traffic_is_none_not_an_error(self):
        assert m.parse([0xF0, 0x41, 0x10, 0x00, 0x10, 0x12, 0xF7]) is None
        assert m.parse([]) is None

    def test_errors_are_recognised(self):
        for code in (0x22, 0x24, 0x26):
            assert m.parse(m.frame(code)).is_error


class TestNames:
    def test_a_nul_terminated_name_stops_at_the_nul(self):
        """Performances store 15 characters and a NUL."""
        raw = b"Ski Jam        \x00"
        assert m.decode_name(raw) == "Ski Jam"

    def test_a_space_padded_name_is_stripped(self):
        """Patches store 16, space-padded."""
        assert m.decode_name(b"Wave Dance      ") == "Wave Dance"

    def test_round_trip(self):
        assert m.decode_name(m.encode_name("Woven Bell")) == "Woven Bell"

    def test_a_long_name_is_truncated(self):
        assert len(m.encode_name("x" * 40)) == m.NAME_LEN


class TestIdentity:
    def test_our_own_request_is_not_taken_for_a_reply(self):
        assert m.identity_reply_fields(m.identity_request()) is None

    def test_a_wavestation_reply_is_recognised(self):
        reply = bytes([0xF0, 0x7E, 0x04, 0x06, 0x02, 0x42, 0x28, 0x00,
                       0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xF7])
        fields = m.identity_reply_fields(reply)
        assert fields["channel"] == 4 and fields["is_wavestation"]

    def test_another_korg_is_not_a_wavestation(self):
        reply = bytes([0xF0, 0x7E, 0x02, 0x06, 0x02, 0x42, 0x36, 0x00,
                       0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xF7])
        fields = m.identity_reply_fields(reply)
        assert fields["is_korg"] and not fields["is_wavestation"]
