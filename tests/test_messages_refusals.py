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

"""The refusals in the wire format.

Every rule here is a place where the code declines to guess. A wrong
guess in this module does not raise -- it produces bytes that look like
data, or names that look right, which is the failure this project keeps
running into. So the refusals are worth testing on their own, separately
from the decoding that motivated them.
"""

import pytest

from kws import messages as m


class TestFramesRefuseWhatCannotTravel:
    """Everything between F0 and F7 must be seven bits."""

    def test_a_payload_byte_with_bit_seven_set_is_refused(self):
        with pytest.raises(m.ProtocolError, match="7-bit data byte"):
            m.frame(m.Function.PATCH_WRITE, [0x80])

    def test_a_function_that_is_not_seven_bits_is_refused(self):
        with pytest.raises(m.ProtocolError, match="not a 7-bit value"):
            m.frame(0x100)

    def test_a_negative_byte_is_refused(self):
        with pytest.raises(m.ProtocolError, match="7-bit data byte"):
            m.frame(m.Function.PATCH_WRITE, [-1])


class TestParseDeclinesForeignTraffic:
    """parse() answers None for anything that is not ours, never raises.

    A shared MIDI chain carries other manufacturers, other Korg models and
    the occasional identity reply, and none of them is an error.
    """

    def test_a_message_too_short_to_have_a_header_is_none(self):
        assert m.parse([0xF0, 0x42, 0x34, 0x28]) is None

    def test_a_message_that_starts_but_does_not_end_is_none(self):
        assert m.parse([0xF0, 0x42, 0x34, 0x28, 0x4C]) is None

    def test_a_message_that_ends_but_does_not_start_is_none(self):
        assert m.parse([0x01, 0x42, 0x34, 0x28, 0x4C, 0xF7]) is None

    def test_a_non_format_three_channel_byte_is_none(self):
        assert m.parse([0xF0, 0x42, 0x04, 0x28, 0x4C, 0xF7]) is None

    def test_an_empty_message_is_none(self):
        assert m.parse([]) is None


class TestPacketEquality:
    def test_two_packets_with_the_same_fields_are_equal(self):
        frame = m.frame(m.Function.ALL_PATCH_DUMP, [3, 0])
        assert m.parse(frame) == m.parse(frame)

    def test_a_different_bank_is_not_equal(self):
        one = m.parse(m.frame(m.Function.ALL_PATCH_DUMP, [3, 0]))
        other = m.parse(m.frame(m.Function.ALL_PATCH_DUMP, [4, 0]))
        assert one != other

    def test_comparison_with_a_foreign_object_is_false_not_an_error(self):
        assert m.parse(m.frame(m.Function.ALL_PATCH_DUMP, [3])) != "not a packet"

    def test_the_repr_names_the_function_and_its_length(self):
        packet = m.parse(m.frame(m.Function.ALL_PATCH_DUMP, [3, 0, 0]))
        text = repr(packet)
        assert "all patch dump" in text
        assert "3 bytes" in text


class TestIdentityReplies:
    """Only sub-ID 02 is a reply. 01 is the request, which is what stops a
    MIDI-Thru loop being taken for a device."""

    def test_a_reply_too_short_is_none(self):
        assert m.identity_reply_fields([0xF0, 0x7E, 0x04, 0x06, 0x02, 0x42]) is None

    def test_the_request_itself_is_not_a_reply(self):
        assert m.identity_reply_fields(m.identity_request()) is None

    def test_a_reply_from_another_manufacturer_is_none(self):
        assert (
            m.identity_reply_fields(
                [0xF0, 0x7F, 0x04, 0x06, 0x02, 0x41, 0x28, 0, 0, 0, 0, 0, 0, 0xF7]
            )
            is None
        )

    def test_a_reply_that_is_not_terminated_is_none(self):
        assert (
            m.identity_reply_fields(
                [0xF0, 0x7E, 0x04, 0x06, 0x02, 0x42, 0x28, 0, 0, 0, 0, 0, 0, 0x01]
            )
            is None
        )


class TestBankRequestRefusesADumpItHasNoRequestFor:
    def test_a_single_object_dump_has_no_bank_request(self):
        with pytest.raises(m.ProtocolError, match="no bank request"):
            m.bank_request(3, m.Function.SINGLE_PATCH_DUMP)

    def test_a_status_reply_has_no_bank_request(self):
        with pytest.raises(m.ProtocolError, match="no bank request"):
            m.bank_request(3, m.Function.WRITE_COMPLETED)


class TestTheRequestBuilders:
    """One line each, and the shape is the whole contract."""

    def test_a_bank_request_carries_its_request_code_and_the_bank(self):
        assert m.bank_request(3, m.Function.ALL_PATCH_DUMP) == bytes(
            [0xF0, 0x42, 0x30, 0x28, m.Function.PATCH_BANK_REQUEST, 0x03, 0xF7]
        )

    def test_a_bulk_request_is_the_patch_one(self):
        """The old name for it, kept because callers predate the split."""
        assert m.bulk_request(4) == m.bank_request(4, m.Function.ALL_PATCH_DUMP)

    def test_a_single_patch_request_carries_bank_then_number(self):
        frame = m.single_patch_request(3, 5)
        assert frame[4] == m.Function.SINGLE_PATCH_REQUEST
        assert frame[5] == 3, "bank first"
        assert frame[6] == 5, "then the number"

    def test_the_patch_write_command_carries_bank_then_number(self):
        """Documented as bank then number; Edisyn's is the other way round
        and does not work."""
        frame = m.patch_write_command(11, 7)
        assert frame[4] == m.Function.PATCH_WRITE
        assert (frame[5], frame[6]) == (11, 7)

    def test_every_builder_honours_the_channel(self):
        for frame in (
            m.bank_request(0, m.Function.ALL_PATCH_DUMP, 4),
            m.single_patch_request(0, 0, 4),
            m.patch_write_command(0, 0, 4),
        ):
            assert frame[2] == m.channel_byte(4)


class TestSinglePatchDump:
    def test_it_addresses_a_slot_and_carries_a_checksum(self):
        data = bytes(range(256))
        frame = m.single_patch_dump(3, 5, data)
        assert frame[4] == m.Function.SINGLE_PATCH_DUMP
        assert (frame[5], frame[6]) == (3, 5)
        assert frame[-2] == m.checksum(list(m.to_nibbles(data)))

    def test_it_decodes_back_to_what_went_in(self):
        data = bytes(range(200, 256))
        packet = m.parse(m.single_patch_dump(3, 5, data))
        assert packet is not None
        assert packet.data()[: len(data)] == data
        assert packet.bank == 3
