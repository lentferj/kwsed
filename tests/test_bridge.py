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

"""The bridge's read and write paths, against a stand-in for the ports.

Everything here is synthetic: no MIDI port is opened and nothing is sent to
an instrument. The double answers from a queue, which is enough to exercise
the polling loops, the refusals and the write fence -- and the write fence is
the part that matters, because it is the only thing standing between a
mistake and a bank that cannot be got back.

`write_patch` had no test of its own before this file. `write_bank` has one,
and it is the more destructive of the two, but a single patch is the smaller
and more common operation, and it is the one whose backup-and-verify had
never been demonstrated.

Two things about the double that are easy to get wrong, and were:

* A single-patch request is answered with **one patch**, not with the bank.
  Answering with the whole bank makes every read succeed and every test
  pass while proving nothing.
* A reply queued *before* a request is discarded, because every request
  drains the input first. That drain is the reason the previous reply is not
  mistaken for this one's answer, so a test that wants a specific reply has
  to produce it in response to the request.
"""

import pytest

from kws import backup, banks
from kws import messages as m
from kws import multi as multi_mod
from kws import records as r
from kws.bridge import DeviceError, DeviceRefused, WavestationBridge

#: What a bank holds. A single patch is one record of this, not the bank.
PATCH_BANK = bytes(banks.PATCHES_PER_BANK * r.PATCH_SIZE)
ONE_PATCH = bytes(r.PATCH_SIZE)


class FakeIn:
    """Answers from a queue, and records nothing."""

    def __init__(self):
        self.queue = []

    def ignore_types(self, **_kw):
        pass

    def get_message(self):
        return self.queue.pop(0) if self.queue else None


class FakeOut:
    """Records what was sent, and can be told how to answer."""

    def __init__(self):
        self.sent = []
        self.on_send = None

    def send_message(self, data):
        data = bytes(data)
        self.sent.append(data)
        if self.on_send is not None:
            self.on_send(data)


def _multiset_dump():
    """A 0x62 message: 32 Multisets, each pointing at RAM 1 performances."""
    data = bytearray(multi_mod.NAME_BASE + multi_mod.MULTISETS * multi_mod.NAME_SIZE)
    for number in range(multi_mod.MULTISETS):
        base = number * multi_mod.MULTISET_SIZE
        for channel in range(multi_mod.CHANNELS):
            at = base + multi_mod.PART_BASE + channel * multi_mod.PART_SIZE
            data[at] = 0  # bank byte 0 is RAM 1
            data[at + 1] = channel  # performance 0-15
            data[at + 2] = 127  # full level
        name = f"Invented{number}".encode()[: multi_mod.NAME_SIZE]
        at = multi_mod.NAME_BASE + number * multi_mod.NAME_SIZE
        data[at : at + len(name)] = name
    # Like the real dump: a 513-byte unmodelled tail and the checksum over
    # the header and the nibbles.
    body = bytes(data) + b"\x69" * 512 + b"\x00"
    header = [0, 1]
    nibbles = list(m.to_nibbles(body))
    return bytes(
        m.frame(
            m.Function.MULTISET_DUMP,
            [*header, *nibbles, m.checksum([*header, *nibbles])],
        )
    )


class Unit:
    """A bridge over the two fakes, holding a patch bank it will answer from.

    It behaves like the instrument in the two ways that matter: a read
    returns what is in the slot, and a write **puts it there**, so the
    read-back after a write sees the new contents rather than the old.
    Without that second behaviour every write test passes while proving
    nothing, because the comparison is against the value that was there
    before.

    ``mode`` selects a way of being wrong, for the refusal tests:

    * ``"wrong_slot"`` -- a patch read answers with a different bank, which
      is how a late reply to an earlier request arrives.
    * ``"write_error"`` -- the write command is answered with an error.
    * ``"read_error"`` -- a patch read is answered with an error, which is
      the refusal that must not be waited out.
    * ``"foreign"`` -- an identity request is answered with another
      manufacturer's message.
    """

    def __init__(self, contents=PATCH_BANK, *, answer=True, mode=None):
        self.midi_in = FakeIn()
        self.midi_out = FakeOut()
        self.contents = contents
        self.answer = answer
        self.mode = mode
        #: The decoded System Setup payload the double answers with. Long
        #: enough for the card-name field at the constants' current offset.
        self.system_setup = bytearray(64)
        #: The 0x62 message the double answers a multi request with, and
        #: replaces when a multi write is sent (so the read-back sees it).
        self.multi_message = _multiset_dump()
        self.bridge = WavestationBridge(self.midi_in, self.midi_out, channel=0)
        # Replacing a method on the instance is the point of this double.
        self.midi_out.send_message = self._sent  # type: ignore[method-assign]

    def _sent(self, data):
        """Answer one outgoing message, the way the instrument would.

        Dispatched on the function code rather than as a chain of tests,
        because the chain was both harder to read and over the complexity
        bar for a test file.
        """
        data = bytes(data)
        self.midi_out.sent.append(data)
        if not self.answer:
            return
        handler = self._HANDLERS.get(self._code(data))
        if handler is not None:
            reply = handler(self, data)
            if reply is not None:
                self.midi_in.queue.append((list(reply), 0.0))

    @staticmethod
    def _code(data):
        """The function code of a Korg message, or None for anything else.

        An identity request is checked first because it is only six bytes
        long, so the general "read byte 4" rule would read its sub-ID
        instead of its manufacturer byte.
        """
        if data[:2] == bytes([0xF0, 0x7E]):
            return data[1]
        return data[4] if len(data) > 5 else None

    def _identity(self, data):
        if self.mode == "foreign":
            return bytes([0xF0, 0x41, 0x10, 0x00, 0x10, 0x12, 0xF7])
        return bytes(
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

    def _patch_request(self, data):
        if self.mode == "read_error":
            return m.frame(m.Function.WRITE_ERROR, channel=0)
        # a wrong-slot reply names a different bank, which must not be taken
        # for this one's answer
        bank = 11 if self.mode == "wrong_slot" else data[5]
        number = data[6]
        # the patch at the slot that was asked for, not always the first one
        at = number * r.PATCH_SIZE
        return m.single_patch_dump(
            bank, number, self.contents[at : at + r.PATCH_SIZE], channel=0
        )

    def _patch_dump(self, data):
        """The instrument stores what it was sent."""
        packet = m.parse(data)
        assert packet is not None
        at = data[5] * r.PATCH_SIZE
        self.contents = (
            self.contents[:at]
            + bytes(packet.data()[: r.PATCH_SIZE])
            + self.contents[at + r.PATCH_SIZE :]
        )

    def _patch_write(self, data):
        if self.mode == "write_error":
            return m.frame(m.Function.WRITE_ERROR, channel=0)
        return m.frame(m.Function.WRITE_COMPLETED, channel=0)

    def _multi_request(self, data):
        if self.mode == "read_error":
            return m.frame(m.Function.WRITE_ERROR, channel=0)
        return self.multi_message

    def _multi_dump_write(self, data):
        """A multi write is stored, so the read-back sees the new setup."""
        self.multi_message = bytes(data)

    def _system_setup_request(self, data):
        if self.mode == "read_error":
            return m.frame(m.Function.WRITE_ERROR, channel=0)
        if self.mode == "short_setup":
            return m.frame(m.Function.SYSTEM_SETUP_DUMP, [0, 0], channel=0)
        return m.frame(
            m.Function.SYSTEM_SETUP_DUMP,
            list(m.to_nibbles(bytes(self.system_setup))),
            channel=0,
        )

    _HANDLERS = {
        0x7E: _identity,
        m.Function.SINGLE_PATCH_REQUEST: _patch_request,
        m.Function.SINGLE_PATCH_DUMP: _patch_dump,
        m.Function.PATCH_WRITE: _patch_write,
        m.Function.MULTI_REQUEST: _multi_request,
        m.Function.MULTISET_DUMP: _multi_dump_write,
        m.Function.SYSTEM_SETUP_REQUEST: _system_setup_request,
    }


class TestIdentify:
    def test_it_adopts_the_channel_the_unit_reports(self):
        """The one number that cannot be guessed, and the reason every
        other request is answered on it."""
        fields = Unit().bridge.identify(timeout=0.5)
        assert fields is not None
        assert fields["channel"] == 4
        assert fields["is_wavestation"]

    def test_foreign_traffic_is_skipped_not_mistaken(self):
        """Another manufacturer's identity reply is not ours, and a patch
        read still works afterwards."""
        unit = Unit(mode="foreign")
        assert unit.bridge.identify(timeout=0.5) is None
        assert len(unit.bridge.read_patch("CARD", 3, timeout=1.0)) == (r.PATCH_SIZE)

    def test_silence_is_none_not_an_error(self):
        assert Unit(answer=False).bridge.identify(timeout=0.05) is None


class TestReadPatch:
    def test_it_reads_one_patch(self):
        unit = Unit()
        got = unit.bridge.read_patch("CARD", 3, timeout=1.0)
        assert len(got) == r.PATCH_SIZE
        assert unit.midi_out.sent, "a request went out"

    def test_a_reply_for_another_slot_does_not_count(self):
        """The guard that stops a late answer being filed under the wrong
        slot -- a read of Card 12 coming back holding Card 3's patch."""
        unit = Unit(mode="wrong_slot")
        with pytest.raises(TimeoutError):
            unit.bridge.read_patch("CARD", 3, timeout=0.2)

    def test_the_right_slot_is_accepted(self):
        unit = Unit()
        assert len(unit.bridge.read_patch("CARD", 3, timeout=1.0)) == r.PATCH_SIZE

    def test_an_error_reply_raises_rather_than_waiting_out_the_clock(self):
        unit = Unit(mode="read_error")
        with pytest.raises(DeviceRefused):
            unit.bridge.read_patch("CARD", 3, timeout=5.0)

    def test_a_foreign_message_is_skipped(self):
        """Foreign traffic during a read is skipped, not mistaken for the
        answer."""
        unit = Unit(mode="foreign")
        assert len(unit.bridge.read_patch("CARD", 3, timeout=1.0)) == (r.PATCH_SIZE)


class TestWritePatch:
    """The fence: back up, send, read back, compare.

    A single patch is a smaller operation than a bank, but the thing that
    makes a write safe is the same and is the same size -- the backup and
    the read-back. This is the first test of that fence for a patch.
    """

    def test_it_backs_up_what_was_there_before_sending(self, tmp_path):
        was = bytearray(PATCH_BANK)
        # in slot 3, which is the slot this test writes
        at = 3 * r.PATCH_SIZE
        was[at : at + 8] = b"Previous"
        unit = Unit(bytes(was))
        fresh = bytearray(ONE_PATCH)
        fresh[0:6] = b"Rebuilt"[:6]
        result = unit.bridge.write_patch(
            "CARD", 3, bytes(fresh), backup_dir=str(tmp_path)
        )
        assert result["verified"] is True
        packet = m.parse(backup.load(result["backup"]).blocks["dump"])
        assert packet is not None
        assert packet.data()[:8] == b"Previous", "the backup holds the old patch"
        assert backup.describe(result["backup"])["bank"] == "Card-patch03"

    def test_it_sends_the_dump_then_the_write_command(self, tmp_path):
        """Two messages, because one is not enough: the dump lands in the
        edit buffer and the command commits it."""
        unit = Unit()
        unit.bridge.write_patch("CARD", 3, ONE_PATCH, backup_dir=str(tmp_path))
        functions = [d[4] for d in unit.midi_out.sent if len(d) > 5]
        assert m.Function.SINGLE_PATCH_DUMP in functions
        assert m.Function.PATCH_WRITE in functions
        assert functions.index(m.Function.SINGLE_PATCH_DUMP) < functions.index(
            m.Function.PATCH_WRITE
        ), "the buffer is filled before it is committed"

    def test_it_reports_the_units_own_status_reply(self, tmp_path):
        result = Unit().bridge.write_patch(
            "CARD", 3, ONE_PATCH, backup_dir=str(tmp_path)
        )
        assert result["status"] == "write completed"

    def test_a_write_error_is_reported_not_claimed(self, tmp_path):
        unit = Unit(mode="write_error")
        result = unit.bridge.write_patch("CARD", 3, ONE_PATCH, backup_dir=str(tmp_path))
        assert result["status"] == "write error"

    def test_a_rom_bank_is_refused_before_anything_is_sent(self, tmp_path):
        unit = Unit()
        with pytest.raises(DeviceError, match="ROM"):
            unit.bridge.write_patch("ROM4", 3, ONE_PATCH, backup_dir=str(tmp_path))
        assert unit.midi_out.sent == []

    def test_a_patch_of_the_wrong_size_is_refused(self, tmp_path):
        unit = Unit()
        with pytest.raises(ValueError, match="426 bytes"):
            unit.bridge.write_patch("CARD", 3, bytes(100), backup_dir=str(tmp_path))
        assert unit.midi_out.sent == []

    def test_a_number_outside_the_bank_is_refused(self, tmp_path):
        unit = Unit()
        with pytest.raises(ValueError, match="35 patches"):
            unit.bridge.write_patch("CARD", 35, ONE_PATCH, backup_dir=str(tmp_path))
        assert unit.midi_out.sent == []

    def test_nothing_is_sent_when_the_destination_cannot_be_read(self, tmp_path):
        """The backup is the undo. Without it, kwsed does not write."""
        unit = Unit(answer=False)
        # a module constant in kws.bridge, read by name at the call site
        import kws.bridge as bridge_mod

        bridge_mod.SINGLE_OBJECT_TIMEOUT = 0.1
        with pytest.raises(DeviceError, match="nothing was sent"):
            unit.bridge.write_patch("CARD", 3, ONE_PATCH, backup_dir=str(tmp_path))
        assert not any(
            d[4] == m.Function.SINGLE_PATCH_DUMP
            for d in unit.midi_out.sent
            if len(d) > 5
        )

    def test_a_read_back_that_does_not_match_is_reported(self, tmp_path):
        """A bulk dump is unacknowledged, so success must be demonstrated."""
        unit = Unit()
        original = unit._sent

        def lying(data):
            original(data)
            if len(data) > 5 and data[4] == m.Function.SINGLE_PATCH_DUMP:
                # corrupt what the instrument stores, so the read-back
                # disagrees with what was sent
                slot = data[5] * r.PATCH_SIZE
                wrong = bytearray(unit.contents[slot : slot + r.PATCH_SIZE])
                wrong[0] ^= 0xFF
                unit.contents = (
                    unit.contents[:slot]
                    + bytes(wrong)
                    + unit.contents[slot + r.PATCH_SIZE :]
                )

        unit.midi_out.send_message = lying  # type: ignore[method-assign]
        result = unit.bridge.write_patch("CARD", 3, ONE_PATCH, backup_dir=str(tmp_path))
        assert result["verified"] is False
        assert "differs" in result["mismatch"]

    def test_verify_false_skips_the_read_back(self, tmp_path):
        result = Unit().bridge.write_patch(
            "CARD", 3, ONE_PATCH, backup_dir=str(tmp_path), verify=False
        )
        assert result["verified"] is None
        assert result["mismatch"] is None


class TestReadMulti:
    def test_it_returns_the_raw_message(self):
        packet = m.parse(Unit().bridge.read_multi(timeout=1.0))
        assert packet is not None
        assert packet.function == m.Function.MULTISET_DUMP

    def test_the_other_two_replies_are_read_and_discarded(self):
        """One request draws three; only the last carries the Multisets, and
        the first two must not be mistaken for the answer to the next
        request."""
        unit = Unit()
        for function in (m.Function.MULTI_SETUP_DUMP, m.Function.MULTI_SETUP_EXPANDED):
            unit.midi_in.queue.append((list(m.frame(function, [0, 1])), 0.0))
        packet = m.parse(unit.bridge.read_multi(timeout=1.0))
        assert packet is not None
        assert packet.function == m.Function.MULTISET_DUMP

    def test_silence_is_a_timeout(self):
        with pytest.raises(TimeoutError):
            Unit(answer=False).bridge.read_multi(timeout=0.05)

    def test_an_error_reply_raises(self):
        unit = Unit(mode="read_error")
        with pytest.raises(DeviceRefused):
            unit.bridge.read_multi(timeout=5.0)


class TestSelect:
    def test_it_switches_to_performance_mode_then_selects(self):
        unit = Unit()
        unit.bridge.select(banks.slot("CARD", banks.Kind.PERFORMANCE, 7))
        assert len(unit.midi_out.sent) == 4
        mode, msb, lsb, pc = unit.midi_out.sent
        # global parameter 4 (MIDI mode) = +1 (POLY, single Performance)
        assert mode == bytes(
            [0xF0, 0x42, 0x30, 0x28, 0x41, 0x04, 0x00, 0x2B, 0x31, 0x00, 0xF7]
        )
        assert msb[0] == 0xB0 and msb[1] == 0x00 and msb[2] == 0x00
        assert lsb[0] == 0xB0 and lsb[1] == 0x20
        assert lsb[2] == 1
        assert pc[0] == 0xC0 and pc[1] == 57

    def test_it_honours_an_explicit_channel(self):
        unit = Unit()
        unit.bridge.select(banks.slot("RAM1", banks.Kind.PERFORMANCE, 0), channel=9)
        mode = unit.midi_out.sent[0]
        assert mode[2] == 0x39, "the mode change carries the same channel"
        assert all(d[0] in (0xB9, 0xC9) for d in unit.midi_out.sent[1:])

    def test_a_channel_outside_zero_to_fifteen_is_refused(self):
        unit = Unit()
        with pytest.raises(ValueError, match="outside 0-15"):
            unit.bridge.select(
                banks.slot("RAM1", banks.Kind.PERFORMANCE, 0), channel=16
            )

    def test_multi_select_switches_to_multi_mode_then_selects(self):
        unit = Unit()
        unit.bridge.select_multi(16)
        mode, select = unit.midi_out.sent
        # global parameter 4 = +2 (MULTI, 16-channel)
        assert mode == bytes(
            [0xF0, 0x42, 0x30, 0x28, 0x41, 0x04, 0x00, 0x2B, 0x32, 0x00, 0xF7]
        )
        assert select == bytes([0xF0, 0x42, 0x30, 0x28, 0x63, 0x10, 0xF7])

    def test_multi_select_refuses_an_unknown_multiset(self):
        unit = Unit()
        with pytest.raises(m.ProtocolError, match="outside 0-31"):
            unit.bridge.select_multi(32)

    def test_set_midi_mode_sends_the_documented_value(self):
        unit = Unit()
        unit.bridge.set_midi_mode(2, channel=5)
        assert unit.midi_out.sent == [
            bytes([0xF0, 0x42, 0x35, 0x28, 0x41, 0x04, 0x00, 0x2B, 0x32, 0x00, 0xF7])
        ]

    @pytest.mark.parametrize("mode", [-1, 4])
    def test_set_midi_mode_refuses_an_unknown_mode(self, mode):
        with pytest.raises(ValueError, match="outside 0-3"):
            Unit().bridge.set_midi_mode(mode)


class TestClose:
    def test_it_closes_both_ports(self):
        closed = []

        class ClosingIn(FakeIn):
            def close_port(self):
                closed.append("in")

        class ClosingOut(FakeOut):
            def close_port(self):
                closed.append("out")

        WavestationBridge(ClosingIn(), ClosingOut(), channel=0).close()
        assert sorted(closed) == ["in", "out"]

    def test_a_port_that_raises_does_not_stop_the_other(self):
        class BadIn(FakeIn):
            def close_port(self):
                raise RuntimeError("already gone")

        class GoodOut(FakeOut):
            def close_port(self):
                pass

        # must not raise: teardown that raises takes the app down with it
        WavestationBridge(BadIn(), GoodOut(), channel=0).close()


class TestTheDrain:
    def test_a_message_already_waiting_is_discarded_before_a_request(self):
        """The reason every request drains first: a reply left over from the
        previous one is otherwise taken for this one's answer."""
        unit = Unit()
        stale = m.single_patch_dump(3, 0, unit.contents[: r.PATCH_SIZE], channel=0)
        unit.midi_in.queue.append((list(stale), 0.0))
        unit.bridge._drain()
        assert unit.midi_in.queue == [], "the stale reply is gone"


class TestReadCardName:
    """The System Setup dump is the only place a card's name is exposed.

    Its offset is the one wire fact docs/HCARD.md leaves open, so these
    tests pin the contract around it: a name at the constants, nothing at
    all when the field is empty, and ``None`` rather than an exception when
    the unit stays silent. A refusal is still a refusal.
    """

    def test_a_blank_field_is_none_not_an_empty_string(self):
        assert Unit().bridge.read_card_name(timeout=0.1) is None

    def test_the_name_at_the_constants_comes_back(self):
        unit = Unit()
        name = b"WPC-13 Perf 3"
        unit.system_setup[: len(name)] = name
        assert unit.bridge.read_card_name(timeout=0.1) == "WPC-13 Perf 3"

    def test_a_garbage_field_is_none_not_a_plausible_name(self):
        """The offset is unconfirmed, so binary in the field is "unknown".

        The first sixteen decoded bytes used to be returned whatever they
        were; a control-byte field is now no usable name rather than a
        name that files a capture under the wrong bank.
        """
        unit = Unit()
        unit.system_setup[:16] = bytes(range(1, 17))
        assert unit.bridge.read_card_name(timeout=0.1) is None

    def test_a_name_shorter_than_four_characters_is_none(self):
        unit = Unit()
        unit.system_setup[:2] = b"AB"
        assert unit.bridge.read_card_name(timeout=0.1) is None

    def test_the_request_is_the_system_setup_one(self):
        unit = Unit()
        unit.bridge.read_card_name(timeout=0.1)
        assert unit.midi_out.sent[-1][4] == m.Function.SYSTEM_SETUP_REQUEST

    def test_silence_is_none_rather_than_a_timeout_error(self):
        assert Unit(answer=False).bridge.read_card_name(timeout=0.01) is None

    def test_a_refusal_is_still_raised(self):
        with pytest.raises(DeviceRefused):
            Unit(mode="read_error").bridge.read_card_name(timeout=0.1)

    def test_a_dump_too_short_for_the_field_is_none(self):
        assert Unit(mode="short_setup").bridge.read_card_name(timeout=0.1) is None

    def test_the_raw_setup_dump_is_available_for_the_probe(self):
        unit = Unit()
        unit.system_setup[0:4] = b"ABCD"
        data = unit.bridge.read_system_setup(timeout=0.1)
        assert data is not None
        assert data[:4] == b"ABCD"


class TestReadBankTimeout:
    """The Card is the one bank that only answers when the card is fitted."""

    def test_the_card_bank_names_the_empty_slot_first(self):
        unit = Unit(answer=False)
        with pytest.raises(TimeoutError) as caught:
            unit.bridge.read_bank("CARD", banks.Kind.PATCH, timeout=0.05)
        message = str(caught.value)
        assert "Program Card" in message
        assert "empty slot" in message

    def test_another_bank_keeps_the_generic_message(self):
        unit = Unit(answer=False)
        with pytest.raises(TimeoutError) as caught:
            unit.bridge.read_bank("RAM1", banks.Kind.PATCH, timeout=0.05)
        assert "Program Card" not in str(caught.value)


class TestWriteMulti:
    def test_the_frame_carries_the_checksum_over_header_and_nibbles(self, tmp_path):
        unit = Unit()
        before = m.parse(unit.multi_message)
        assert before is not None
        data = before.data()
        sets = multi_mod.parse(data)
        encoded = multi_mod.encode(sets)
        result = unit.bridge.write_multi(encoded, backup_dir=str(tmp_path))
        assert result["verified"] is True
        sent = [
            message
            for message in unit.midi_out.sent
            if len(message) > 5 and message[4] == m.Function.MULTISET_DUMP
        ][-1]
        payload = sent[5:-1]
        header = list(payload[:2])
        nibbles = list(payload[2:-1])
        assert payload[-1] == m.checksum(header + nibbles)
        # The unit's header is 00 01, so the header-inclusive sum differs
        # from the nibbles-only one: a checksum that omitted the header
        # would be rejected by the unit with CHECKSUM ERROR.
        assert payload[-1] != m.checksum(nibbles)
        after = m.parse(sent)
        assert after is not None
        decoded = after.data()
        assert decoded[: len(encoded)] == encoded
        assert decoded[len(encoded) :] == data[len(encoded) :]

    def test_a_failed_pre_write_read_sends_no_dump(self, tmp_path):
        unit = Unit(answer=False)
        with pytest.raises(DeviceError):
            unit.bridge.write_multi(
                b"\x00" * 3264, backup_dir=str(tmp_path), timeout=0.05
            )
        assert not [
            message
            for message in unit.midi_out.sent
            if len(message) > 5 and message[4] == m.Function.MULTISET_DUMP
        ]
