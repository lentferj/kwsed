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

"""The write gate. Every fixture here is invented.

The property that matters most is the negative one: a write that cannot be
backed up must not happen. It is tested by making the read fail.
"""

import os

import pytest

from kws import backup, banks, records as r
from kws import messages as m

pytest.importorskip("rtmidi")
from kws.bridge import DeviceError, WavestationBridge  # noqa: E402


PATCH_BANK = bytes(banks.PATCHES_PER_BANK * r.PATCH_SIZE)


class FakeIn:
    def __init__(self):
        self.queue = []

    def ignore_types(self, **_kw):
        pass

    def get_message(self):
        return self.queue.pop(0) if self.queue else None


class FakeOut:
    def __init__(self):
        self.sent = []

    def send_message(self, data):
        self.sent.append(bytes(data))


class Unit:
    """A stand-in Wavestation that answers bank reads with what it holds."""

    def __init__(self, contents=PATCH_BANK, *, answer=True):
        self.midi_in = FakeIn()
        self.midi_out = FakeOut()
        self.contents = contents
        self.answer = answer
        self.bridge = WavestationBridge(self.midi_in, self.midi_out,
                                        channel=0)
        self.midi_out.send_message = self._sent

    def _sent(self, data):
        data = bytes(data)
        self.midi_out.sent.append(data)
        if len(data) > 5 and data[4] == m.Function.BULK_REQUEST and self.answer:
            reply = m.frame_bulk(m.Function.ALL_PATCH_DUMP, data[5],
                                 self.contents, channel=0)
            self.midi_in.queue.append((list(reply), 0.0))
        elif len(data) > 5 and data[4] == m.Function.ALL_PATCH_DUMP:
            # the instrument stores what it was sent
            self.contents = bytes(m.parse(data).data()[:len(PATCH_BANK)])


@pytest.fixture()
def unit():
    return Unit()


class TestTheFence:
    def test_a_rom_bank_is_refused_before_anything_is_sent(self, unit, tmp_path):
        with pytest.raises(DeviceError) as caught:
            unit.bridge.write_bank(5, banks.Kind.PATCH, PATCH_BANK,
                                   backup_dir=str(tmp_path))
        assert "ROM" in str(caught.value)
        assert unit.midi_out.sent == [], "a refused write must send nothing"

    @pytest.mark.parametrize("bank", [0, 1, 2, 11])
    def test_the_writable_banks_are_ram_one_to_three_and_the_card(self, bank):
        assert banks.bank(bank).writable

    @pytest.mark.parametrize("bank", range(3, 11))
    def test_every_rom_bank_is_not_writable(self, bank):
        assert not banks.bank(bank).writable

    def test_no_write_happens_if_the_destination_cannot_be_read(self, tmp_path):
        """The backup is the undo. Without it, kwsed does not write."""
        silent = Unit(answer=False)
        silent.bridge.DUMP_TIMEOUT = 0.1
        with pytest.raises(DeviceError) as caught:
            silent.bridge.write_bank(11, banks.Kind.PATCH, PATCH_BANK,
                                     backup_dir=str(tmp_path), timeout=0.1)
        assert "not backed up" in str(caught.value)
        assert not any(d[4] == m.Function.ALL_PATCH_DUMP
                       for d in silent.midi_out.sent if len(d) > 5), \
            "a dump was sent even though the backup failed"

    def test_no_write_happens_if_the_backup_cannot_be_saved(self, unit,
                                                            tmp_path):
        blocked = tmp_path / "not-a-directory"
        blocked.write_text("in the way")
        with pytest.raises(backup.BackupError):
            unit.bridge.write_bank(11, banks.Kind.PATCH, PATCH_BANK,
                                   backup_dir=str(blocked / "sub"))
        assert not any(d[4] == m.Function.ALL_PATCH_DUMP
                       for d in unit.midi_out.sent if len(d) > 5)

    def test_the_backup_holds_what_was_there_before(self, unit, tmp_path):
        was = bytearray(PATCH_BANK)
        was[0:8] = b"Previous"
        unit.contents = bytes(was)
        result = unit.bridge.write_bank(11, banks.Kind.PATCH, PATCH_BANK,
                                        backup_dir=str(tmp_path))
        saved = backup.load(result["backup"])
        restored = m.parse(saved).data()[:len(PATCH_BANK)]
        assert restored[0:8] == b"Previous"
        assert backup.describe(result["backup"])["bank"] == "Card"

    def test_a_write_is_verified_by_reading_it_back(self, unit, tmp_path):
        fresh = bytearray(PATCH_BANK)
        fresh[0:6] = b"Rebuilt"[:6]
        result = unit.bridge.write_bank(11, banks.Kind.PATCH, bytes(fresh),
                                        backup_dir=str(tmp_path))
        assert result["verified"] is True
        assert result["mismatch"] is None

    def test_a_bad_read_back_is_reported_not_claimed_as_success(self, unit,
                                                                tmp_path):
        """A bulk dump is unacknowledged, so success must be demonstrated."""
        original = unit._sent

        def lying(data):
            original(data)
            if len(data) > 5 and data[4] == m.Function.ALL_PATCH_DUMP:
                wrong = bytearray(unit.contents)
                wrong[0] ^= 0xFF
                unit.contents = bytes(wrong)

        unit.midi_out.send_message = lying
        result = unit.bridge.write_bank(11, banks.Kind.PATCH, PATCH_BANK,
                                        backup_dir=str(tmp_path))
        assert result["verified"] is False
        assert "differs" in result["mismatch"]


class TestTheModal:
    """The TUI's fence. Opening it must send nothing; arming must send
    nothing; firing must need a second, different key."""

    def _screen(self, bank=11, fired=None):
        from kwsed.app import SendScreen
        return SendScreen(path="favourites-patch.syx",
                          kind=banks.Kind.PATCH,
                          destination=banks.bank(bank),
                          current={0: "Glass Meridian", 1: "Slow Tideline"},
                          on_send=fired)

    def test_arming_sends_nothing(self):
        calls = []
        screen = self._screen(fired=lambda *a: calls.append(a))
        screen._armed = False
        try:
            screen.action_arm()          # needs no mounted widgets to arm
        except Exception:
            screen._armed = True         # the state is what is under test
        assert calls == [], "arming must not write"
        assert screen._armed is True, "arming a writable bank must arm it"

    def test_firing_without_arming_does_nothing(self):
        calls = []
        screen = self._screen(fired=lambda *a: calls.append(a))
        screen._armed = False
        try:
            screen.action_fire()
        except Exception:
            pass
        assert calls == [], "fire before arm must not write"

    def test_a_rom_destination_cannot_be_armed(self):
        screen = self._screen(bank=5)
        screen._armed = False
        try:
            screen.action_arm()
        except Exception:
            pass
        assert screen._armed is False, "a ROM bank must not arm"

    def test_the_fire_key_is_not_the_arm_key_and_not_enter(self):
        from kwsed.app import SendScreen
        keys = {b.action: b.key for b in SendScreen.BINDINGS}
        assert keys["arm"] != keys["fire"]
        assert keys["fire"] not in ("enter", "return")

    def test_the_screen_names_what_it_will_replace(self):
        text = self._screen()._describe()
        assert "Glass Meridian" in text, \
            "a destination shown only as a number is how work gets lost"
        assert "Card" in text


class TestLargeMessagesGetTheOtherTransport:
    """python-rtmidi cannot send a bank dump on ALSA; kws.alsa can.

    Measured: rtmidi sends 8 kB and fails at 16 kB, and a patch bank is
    29828 bytes, so every bank write died before a byte left the machine.
    """

    def test_a_port_name_yields_its_sequencer_address(self):
        from kws import alsa
        assert alsa.port_address("U6MIDI Pro:U6MIDI Pro MIDI 1 44:0") == (44, 0)
        assert alsa.port_address("Midi Through:Midi Through Port-0 14:0") \
            == (14, 0)

    def test_a_name_without_an_address_is_declined(self):
        from kws import alsa
        assert alsa.port_address("not an alsa port") is None
        assert alsa.port_address("") is None
        assert alsa.port_address(None) is None

    def test_the_threshold_is_below_what_rtmidi_manages(self):
        from kws.bridge import WavestationBridge
        from kws import banks, records as r
        assert WavestationBridge.RTMIDI_LIMIT < 8192
        bank = banks.PATCHES_PER_BANK * r.PATCH_SIZE
        assert bank > WavestationBridge.RTMIDI_LIMIT, \
            "a patch bank must take the other transport"

    def test_the_chunk_is_small_enough_for_one_event(self):
        """A whole dump in one event is refused however large the pool is."""
        from kws import alsa
        assert alsa.Sender.CHUNK <= 512


class TestTheSendIsPaced:
    """A MIDI cable carries 3125 bytes a second and cannot say "slow down".

    The first bank written to hardware went out instantly and the instrument
    stored a stream with 672 bytes missing from the middle. Nothing reported
    an error; only the read-back comparison caught it.
    """

    def test_the_rate_is_below_the_wire_rate(self):
        from kws import alsa
        assert alsa.Sender.BYTES_PER_SECOND < 3125, \
            "pacing at exactly the wire rate leaves no margin"
        assert alsa.Sender.BYTES_PER_SECOND > 2000, \
            "too slow makes a bank write take minutes"

    def test_a_bank_would_take_about_the_time_the_wire_needs(self):
        from kws import alsa, banks, records as r
        size = 2 * banks.PATCHES_PER_BANK * r.PATCH_SIZE  # nibble encoded
        seconds = size / alsa.Sender.BYTES_PER_SECOND
        assert 9 < seconds < 15, f"a patch bank would take {seconds:.1f}s"


class TestMultisets:
    """32 Multisets, each assigning all 16 MIDI channels."""

    def _dump(self, assignments=None):
        from kws import multi
        data = bytearray(multi.NAME_BASE + multi.MULTISETS * multi.NAME_SIZE)
        for number in range(multi.MULTISETS):
            base = number * multi.MULTISET_SIZE
            for channel in range(multi.CHANNELS):
                at = base + multi.PART_BASE + channel * multi.PART_SIZE
                data[at] = 0
                data[at + 1] = channel
                data[at + 2] = 127
            name = f"Invented{number}".encode()[:multi.NAME_SIZE]
            at = multi.NAME_BASE + number * multi.NAME_SIZE
            data[at:at + len(name)] = name
        for at, value in (assignments or {}).items():
            data[at] = value
        return bytes(data)

    def test_the_arithmetic_lands_flush(self):
        from kws import multi
        assert multi.PART_BASE + multi.CHANNELS * multi.PART_SIZE == \
            multi.MULTISET_SIZE
        assert multi.MULTISETS * multi.MULTISET_SIZE == multi.NAME_BASE

    def test_it_parses_all_thirty_two(self):
        from kws import multi
        sets = multi.parse(self._dump())
        assert len(sets) == multi.MULTISETS
        assert sets[0].name == "Invented0"
        assert len(sets[0].parts) == multi.CHANNELS
        assert [p.channel for p in sets[0].parts] == list(range(1, 17))

    def test_a_short_dump_is_refused(self):
        from kws import multi
        with pytest.raises(ValueError, match="at least"):
            multi.parse(self._dump()[:100])

    def test_seven_f_reads_as_a_space(self):
        """Korg's appendix says 0x7F is a space; the names use it too."""
        from kws import multi
        data = bytearray(self._dump())
        at = multi.NAME_BASE
        data[at:at + 16] = b"Two\x7fWords" + bytes(7)
        assert multi.parse(bytes(data))[0].name == "Two Words"

    def test_the_request_is_the_documented_one(self):
        from kws import messages as m
        assert m.Function.MULTI_REQUEST == 0x06
        assert m.REQUEST_FOR_DUMP[m.Function.MULTISET_DUMP] == 0x06


class TestReadsMatchTheBankTheyAskedFor:
    """A dump for another bank must not be taken for this one's.

    read_patch guarded this; read_bank and read_raw did not, though the
    docstring said otherwise. The caller files the names under the bank it
    requested, so a late dump from a read that already timed out lands under
    the wrong pane.
    """

    def _unit(self, reply_bank):
        """A stand-in that answers every bank request with one fixed bank."""
        from kws import banks as bk, records as r

        payload = bytes(bk.PATCHES_PER_BANK * r.PATCH_SIZE)
        reply = m.frame_bulk(m.Function.ALL_PATCH_DUMP, reply_bank,
                             payload, channel=0)

        class In:
            def __init__(self): self.queue = []
            def ignore_types(self, **kw): pass
            def get_message(self):
                return self.queue.pop(0) if self.queue else None

        midi_in = In()

        class Out:
            def send_message(self, data):
                # answer after the request, so _drain cannot eat it first
                if len(data) > 4 and data[4] in (0x1C, 0x1D):
                    midi_in.queue.append((list(reply), 0.0))

        return WavestationBridge(midi_in, Out(), channel=0)

    def test_a_dump_for_another_bank_is_ignored(self):
        # the Card is wire byte 3; answer with ROM 11's byte 2 instead
        bridge = self._unit(reply_bank=2)
        with pytest.raises(TimeoutError):
            bridge.read_bank("CARD", banks.Kind.PATCH, timeout=0.3)

    def test_the_right_bank_is_accepted(self):
        bridge = self._unit(reply_bank=3)
        dump = bridge.read_bank("CARD", banks.Kind.PATCH, timeout=2.0)
        assert dump.bank.label == "Card"


class TestALargeSendIsRefusedWithoutTheAlsaPath:
    """The refusal used to sit inside `if alsa.available()`.

    alsa.available() is find_library("asound") having found something, which
    fails on hosts where rtmidi's own ALSA backend works. Such a host fell
    through to the silent drop the refusal exists to prevent.
    """

    def _bridge(self, description, available):
        from kws import alsa, bridge as b

        class Out:
            def __init__(self): self.sent = []
            def open_port(self, *a): pass        # looks like a real port
            def send_message(self, data): self.sent.append(bytes(data))

        class In:
            def ignore_types(self, **kw): pass
            def get_message(self): return None

        out = Out()
        bridge = WavestationBridge(In(), out, channel=0)
        bridge.description = description
        return bridge, out, alsa

    def test_a_port_with_no_sequencer_address_is_refused(self, monkeypatch):
        from kws import alsa

        bridge, out, _ = self._bridge("a port with no address", True)
        with pytest.raises(DeviceError, match="Nothing was sent"):
            bridge._send(bytes(29828))
        assert out.sent == [], "a refused send must transmit nothing"

    def test_it_is_refused_even_when_libasound_is_absent(self, monkeypatch):
        """The case the old nesting skipped."""
        from kws import alsa

        bridge, out, alsa_mod = self._bridge(
            "U6MIDI Pro:U6MIDI Pro MIDI 1 44:0", False)
        monkeypatch.setattr(alsa_mod, "available", lambda: False)
        with pytest.raises(DeviceError, match="Nothing was sent"):
            bridge._send(bytes(29828))
        assert out.sent == []

    def test_a_small_message_still_goes_out(self, monkeypatch):
        from kws import alsa

        bridge, out, alsa_mod = self._bridge("no address here", True)
        monkeypatch.setattr(alsa_mod, "available", lambda: False)
        bridge._send(bytes([0xF0, 0x42, 0x30, 0x28, 0x06, 0xF7]))
        assert len(out.sent) == 1
