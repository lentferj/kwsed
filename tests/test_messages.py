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
        for data in (b"", b"\x00", b"\xff", bytes(range(256)), b"Woven Bell"):
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
        assert packet is not None
        assert packet.data()[:10] == b"Woven Bell"
        assert packet.bank == 3

    def test_a_non_dump_has_no_offset(self):
        packet = m.parse(m.frame(m.Function.WRITE_COMPLETED))
        assert packet is not None
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
            [0xF0, 0x42, 0x34, 0x28, 0x1C, 0x02, 0xF7]
        )

    def test_another_korg_model_is_not_ours(self):
        """36h is the X5 family; 28h is the Wavestation."""
        assert m.parse([0xF0, 0x42, 0x30, 0x36, 0x4C, 0xF7]) is None

    def test_foreign_traffic_is_none_not_an_error(self):
        assert m.parse([0xF0, 0x41, 0x10, 0x00, 0x10, 0x12, 0xF7]) is None
        assert m.parse([]) is None

    def test_errors_are_recognised(self):
        for code in (0x22, 0x24, 0x26):
            packet = m.parse(m.frame(code))
            assert packet is not None
            assert packet.is_error


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
        reply = bytes(
            [
                0xF0,
                0x7E,
                0x04,
                0x06,
                0x02,
                0x42,
                0x28,
                0x00,
                0x00,
                0x00,
                0x00,
                0x00,
                0x00,
                0x00,
                0xF7,
            ]
        )
        fields = m.identity_reply_fields(reply)
        assert fields is not None
        assert fields["channel"] == 4 and fields["is_wavestation"]

    def test_another_korg_is_not_a_wavestation(self):
        reply = bytes(
            [
                0xF0,
                0x7E,
                0x02,
                0x06,
                0x02,
                0x42,
                0x36,
                0x00,
                0x00,
                0x00,
                0x00,
                0x00,
                0x00,
                0x00,
                0xF7,
            ]
        )
        fields = m.identity_reply_fields(reply)
        assert fields is not None
        assert fields["is_korg"] and not fields["is_wavestation"]


class TestChecksum:
    """The trailing byte of a bulk dump.

    Reading never needed it -- the record arithmetic divides exactly whether
    or not it is counted -- which is why kwsed went a long time without
    noticing. Building needs it: a dump without it is a byte short of what
    the instrument sends, and will not load.
    """

    def test_it_is_the_seven_bit_sum(self):
        from kws import messages as m

        assert m.checksum([1, 2, 3]) == 6
        assert m.checksum([0x7F, 0x7F]) == 0x7E, "it wraps at seven bits"
        assert m.checksum([]) == 0

    def test_a_built_bulk_dump_matches_the_instrument_s_shape(self):
        from kws import messages as m

        data = bytes(range(256)) * 4
        frame = m.frame_bulk(m.Function.ALL_PATCH_DUMP, 11, data, channel=4)
        assert frame[0] == m.SYSEX_START and frame[-1] == m.SYSEX_END
        assert frame[4] == m.Function.ALL_PATCH_DUMP
        assert frame[5] == 11, "the bank byte is plain, not nibble encoded"
        body = frame[6:-1]
        assert body[-1] == m.checksum(body[:-1])
        assert len(frame) == 6 + 2 * len(data) + 1 + 1, "header, nibbles, checksum, F7"

    def test_it_round_trips_through_the_parser(self):
        from kws import messages as m

        data = bytes([9, 8, 7, 6, 5])
        frame = m.frame_bulk(m.Function.ALL_PATCH_DUMP, 3, data)
        packet = m.parse(frame)
        assert packet is not None and packet.bank == 3
        # the checksum rides along in the data, so it is trimmed by length
        assert packet.data()[: len(data)] == data


class TestMultiSelect:
    """Appendix 4 §2.1.20/§2.1.21: 0-15 uses 0x5B, 16-31 the SR 0x63."""

    @pytest.mark.parametrize(
        ("number", "function"),
        [(0, 0x5B), (15, 0x5B), (16, 0x63), (31, 0x63)],
    )
    def test_the_documented_function_and_number(self, number, function):
        assert m.multi_select(number) == bytes(
            [0xF0, 0x42, 0x30, 0x28, function, number, 0xF7]
        )

    def test_the_channel_rides_in_the_format_byte(self):
        assert m.multi_select(16, channel=5)[2] == 0x35

    @pytest.mark.parametrize("number", [-1, 32])
    def test_an_unknown_multiset_is_refused(self, number):
        with pytest.raises(m.ProtocolError, match="outside 0-31"):
            m.multi_select(number)
