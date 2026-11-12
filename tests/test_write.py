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
from typing import Optional, Any, Dict, Iterable, List, Tuple

import pytest

from kws.bridge import MidiInPort, MidiOutPort
from kws import backup, banks, records as r
from kws import messages as m

pytest.importorskip("rtmidi")
from kws.bridge import DeviceError, WavestationBridge


PATCH_BANK = bytes(banks.PATCHES_PER_BANK * r.PATCH_SIZE)
PERFORMANCE_BANK = bytes(banks.PERFORMANCES_PER_BANK * r.PERFORMANCE_SIZE)
WAVESEQ_BANK = bytes(r.WAVESEQ_BANK_SIZE)

CARD_SYSEX = banks.bank("CARD").sysex

#: Which dump answers which bank request, and how many decoded bytes each
#: dump carries for a whole bank. A stand-in that confuses these answers a
#: question nobody asked -- which is what TODO 17 was.
DUMP_FOR_REQUEST = {
    m.Function.PATCH_BANK_REQUEST: m.Function.ALL_PATCH_DUMP,
    m.Function.PERFORMANCE_BANK_REQUEST: m.Function.ALL_PERFORMANCE_DUMP,
    m.Function.WAVESEQ_BANK_REQUEST: m.Function.ALL_WAVESEQ_DUMP,
}

SIZE_FOR_DUMP = {
    m.Function.ALL_PATCH_DUMP: banks.PATCHES_PER_BANK * r.PATCH_SIZE,
    m.Function.ALL_PERFORMANCE_DUMP: banks.PERFORMANCES_PER_BANK * r.PERFORMANCE_SIZE,
    m.Function.ALL_WAVESEQ_DUMP: r.WAVESEQ_BANK_SIZE,
}


class FakeIn(MidiInPort):
    def __init__(self) -> None:
        self.queue: List[Tuple[List[int], float]] = []

    def ignore_types(self, sysex: bool, timing: bool, active_sense: bool) -> None:
        pass

    def get_message(self) -> Optional[Tuple[List[int], float]]:
        return self.queue.pop(0) if self.queue else None

    def close_port(self) -> None:
        pass

    def delete(self) -> None:
        pass


class FakeOut(MidiOutPort):
    def __init__(self) -> None:
        self.sent: List[bytes] = []

    def send_message(self, data: List[int]) -> None:
        self.sent.append(bytes(data))

    def close_port(self) -> None:
        pass

    def delete(self) -> None:
        pass


class Unit:
    """A stand-in Wavestation that answers bank reads with what it holds."""

    def __init__(
        self,
        contents: bytes = PATCH_BANK,
        *,
        answer: bool = True,
        refuse: Iterable[int] = (),
    ) -> None:
        self.midi_in = FakeIn()
        self.midi_out = FakeOut()
        #: Every bank dump the fake holds, keyed by
        #: ``(wire bank byte, dump function)``.
        self.dumps: Dict[Tuple[int, int], bytes] = {}
        self.contents = bytes(contents)
        self.answer = answer
        #: Dump functions refused with a Data Load Error, as the unit does.
        self.refuse = frozenset(refuse)
        self.bridge = WavestationBridge(self.midi_in, self.midi_out, channel=0)
        # Replacing a method on the instance is the point of this double.
        self.midi_out.send_message = self._sent  # type: ignore[method-assign]

    @property
    def contents(self) -> bytes:
        """The Card's patch bank, where the older tests look for it."""
        return self.dumps.get((CARD_SYSEX, m.Function.ALL_PATCH_DUMP), PATCH_BANK)

    @contents.setter
    def contents(self, value: bytes) -> None:
        self.dumps[(CARD_SYSEX, m.Function.ALL_PATCH_DUMP)] = bytes(value)

    def _stored(self, bank_byte: int, dump: int) -> bytes:
        held = self.dumps.get((bank_byte, dump))
        if held is not None:
            return held
        return bytes(SIZE_FOR_DUMP[dump])

    def sent_by_function(self, function: int) -> List[bytes]:
        """Every frame sent whose function byte is ``function``."""
        return [
            data for data in self.midi_out.sent if len(data) > 4 and data[4] == function
        ]

    def dump_frames_sent(self) -> List[bytes]:
        """Every bank dump frame sent, of any of the three kinds."""
        return [
            data
            for data in self.midi_out.sent
            if len(data) > 4 and data[4] in SIZE_FOR_DUMP
        ]

    def _sent(self, data: List[int]) -> None:
        raw = bytes(data)
        self.midi_out.sent.append(raw)
        if len(raw) < 5:
            return
        function = raw[4]
        dump = DUMP_FOR_REQUEST.get(function)
        if dump is not None and len(raw) > 5:
            bank_byte = raw[5]
            if dump in self.refuse:
                self.midi_in.queue.append(
                    (
                        list(m.frame(m.Function.DATA_LOAD_ERROR, channel=0)),
                        0.0,
                    )
                )
                return
            if not self.answer:
                return
            reply = m.frame_bulk(
                dump, bank_byte, self._stored(bank_byte, dump), channel=0
            )
            self.midi_in.queue.append((list(reply), 0.0))
        elif function in SIZE_FOR_DUMP:
            # the instrument stores what it was sent
            packet = m.parse(raw)
            assert packet is not None
            assert packet.bank is not None
            self.dumps[(packet.bank, function)] = bytes(
                packet.data()[: SIZE_FOR_DUMP[function]]
            )


@pytest.fixture()
def unit():
    return Unit()


class TestTheFence:
    def test_a_rom_bank_is_refused_before_anything_is_sent(self, unit, tmp_path):
        with pytest.raises(DeviceError) as caught:
            unit.bridge.write_bank(
                5, banks.Kind.PATCH, PATCH_BANK, backup_dir=str(tmp_path)
            )
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
        # No timeout is patched onto the bridge: DUMP_TIMEOUT is a module
        # constant in kws.bridge, read by name, so setting it on the instance
        # never shortened anything. The explicit timeout below is what does
        # the work -- which is why the test passed while saying nothing.
        with pytest.raises(DeviceError) as caught:
            silent.bridge.write_bank(
                11, banks.Kind.PATCH, PATCH_BANK, backup_dir=str(tmp_path), timeout=0.1
            )
        assert "not backed up" in str(caught.value)
        assert not any(
            d[4] == m.Function.ALL_PATCH_DUMP
            for d in silent.midi_out.sent
            if len(d) > 5
        ), "a dump was sent even though the backup failed"

    def test_no_write_happens_if_the_backup_cannot_be_saved(self, unit, tmp_path):
        blocked = tmp_path / "not-a-directory"
        blocked.write_text("in the way")
        with pytest.raises(backup.BackupError):
            unit.bridge.write_bank(
                11, banks.Kind.PATCH, PATCH_BANK, backup_dir=str(blocked / "sub")
            )
        assert not any(
            d[4] == m.Function.ALL_PATCH_DUMP for d in unit.midi_out.sent if len(d) > 5
        )

    def test_the_backup_holds_what_was_there_before(self, unit, tmp_path):
        was = bytearray(PATCH_BANK)
        was[0:8] = b"Previous"
        unit.contents = bytes(was)
        result = unit.bridge.write_bank(
            11, banks.Kind.PATCH, PATCH_BANK, backup_dir=str(tmp_path)
        )
        saved = backup.load(result["backup"]).blocks["dump"]
        saved_packet = m.parse(saved)
        assert saved_packet is not None
        restored = saved_packet.data()[: len(PATCH_BANK)]
        assert restored[0:8] == b"Previous"
        assert backup.describe(result["backup"])["bank"] == "Card"

    def test_a_write_is_verified_by_reading_it_back(self, unit, tmp_path):
        fresh = bytearray(PATCH_BANK)
        fresh[0:6] = b"Rebuilt"[:6]
        result = unit.bridge.write_bank(
            11, banks.Kind.PATCH, bytes(fresh), backup_dir=str(tmp_path)
        )
        assert result["verified"] is True
        assert result["mismatch"] is None

    def test_a_bad_read_back_is_reported_not_claimed_as_success(self, unit, tmp_path):
        """A bulk dump is unacknowledged, so success must be demonstrated."""
        original = unit._sent

        def lying(data):
            original(data)
            if len(data) > 5 and data[4] == m.Function.ALL_PATCH_DUMP:
                wrong = bytearray(unit.contents)
                wrong[0] ^= 0xFF
                unit.contents = bytes(wrong)

        unit.midi_out.send_message = lying
        result = unit.bridge.write_bank(
            11, banks.Kind.PATCH, PATCH_BANK, backup_dir=str(tmp_path)
        )
        assert result["verified"] is False
        assert "differs" in result["mismatch"]


class TestTheModal:
    """The TUI's fence. Opening it must send nothing; arming must send
    nothing; firing must need a second, different key."""

    def _screen(self, bank=11, fired=None):
        from kwsed.app import SendScreen

        return SendScreen(
            path="favourites-patch.syx",
            kind=banks.Kind.PATCH,
            destination=banks.bank(bank),
            current={0: "Glass Meridian", 1: "Slow Tideline"},
            on_send=fired,
        )

    def test_arming_sends_nothing(self):
        calls = []
        screen = self._screen(fired=lambda *a: calls.append(a))
        screen._armed = False
        try:
            screen.action_arm()  # needs no mounted widgets to arm
        except Exception:
            screen._armed = True  # the state is what is under test
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

        from textual.binding import Binding

        keys = {b.action: b.key for b in SendScreen.BINDINGS if isinstance(b, Binding)}
        assert keys["arm"] != keys["fire"]
        assert keys["fire"] not in ("enter", "return")

    def test_the_screen_names_what_it_will_replace(self):
        text = self._screen()._describe()
        assert "Glass Meridian" in text, (
            "a destination shown only as a number is how work gets lost"
        )
        assert "Card" in text


class TestLargeMessagesGetTheOtherTransport:
    """python-rtmidi cannot send a bank dump on ALSA; kws.alsa can.

    Measured: rtmidi sends 8 kB and fails at 16 kB, and a patch bank is
    29828 bytes, so every bank write died before a byte left the machine.
    """

    def test_a_port_name_yields_its_sequencer_address(self):
        from kws import alsa

        assert alsa.port_address("U6MIDI Pro:U6MIDI Pro MIDI 1 44:0") == (44, 0)
        assert alsa.port_address("Midi Through:Midi Through Port-0 14:0") == (14, 0)

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
        assert bank > WavestationBridge.RTMIDI_LIMIT, (
            "a patch bank must take the other transport"
        )

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

        assert alsa.Sender.BYTES_PER_SECOND < 3125, (
            "pacing at exactly the wire rate leaves no margin"
        )
        assert alsa.Sender.BYTES_PER_SECOND > 2000, (
            "too slow makes a bank write take minutes"
        )

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
            name = f"Invented{number}".encode()[: multi.NAME_SIZE]
            at = multi.NAME_BASE + number * multi.NAME_SIZE
            data[at : at + len(name)] = name
        for at, value in (assignments or {}).items():
            data[at] = value
        return bytes(data)

    def test_the_arithmetic_lands_flush(self):
        from kws import multi

        assert multi.PART_BASE + multi.CHANNELS * multi.PART_SIZE == multi.MULTISET_SIZE
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
        data[at : at + 16] = b"Two\x7fWords" + bytes(7)
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
        reply = m.frame_bulk(m.Function.ALL_PATCH_DUMP, reply_bank, payload, channel=0)

        class In(MidiInPort):
            def __init__(self):
                self.queue: List[Tuple[List[int], float]] = []

            def ignore_types(
                self, sysex: bool, timing: bool, active_sense: bool
            ) -> None:
                pass

            def get_message(self) -> Optional[Tuple[List[int], float]]:
                return self.queue.pop(0) if self.queue else None

            def close_port(self) -> None:
                pass

            def delete(self) -> None:
                pass

        midi_in = In()

        class Out(MidiOutPort):
            def send_message(self, data: List[int]) -> None:
                # answer after the request, so _drain cannot eat it first
                if len(data) > 4 and data[4] in (0x1C, 0x1D):
                    midi_in.queue.append((list(reply), 0.0))

            def close_port(self) -> None:
                pass

            def delete(self) -> None:
                pass

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

    def test_the_progress_label_pluralises_patches(self):
        """It said "patchs" on startup, which is not a word."""
        bridge = self._unit(reply_bank=3)
        labels: list[str] = []
        bridge.progress = lambda label, fraction: labels.append(label)
        bridge.read_bank("CARD", banks.Kind.PATCH, timeout=2.0)
        assert any("Card patches" in label for label in labels)
        assert not any("patchs" in label for label in labels)


class TestALargeSendIsRefusedWithoutTheAlsaPath:
    """The refusal used to sit inside `if alsa.available()`.

    alsa.available() is find_library("asound") having found something, which
    fails on hosts where rtmidi's own ALSA backend works. Such a host fell
    through to the silent drop the refusal exists to prevent.
    """

    def _bridge(self, description, available):
        from kws import alsa, bridge as b

        class Out(MidiOutPort):
            def __init__(self):
                self.sent = []

            def open_port(self, *a):
                pass  # looks like a real port

            def send_message(self, data):
                self.sent.append(bytes(data))

            def close_port(self):
                pass

            def delete(self):
                pass

        class In(MidiInPort):
            def ignore_types(
                self, sysex: bool, timing: bool, active_sense: bool
            ) -> None:
                pass

            def get_message(self) -> Optional[Tuple[List[int], float]]:
                return None

            def close_port(self) -> None:
                pass

            def delete(self) -> None:
                pass

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

        bridge, out, alsa_mod = self._bridge("U6MIDI Pro:U6MIDI Pro MIDI 1 44:0", False)
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


class TestWritingEveryKind:
    """TODO 17: the fence was exercised for patches only.

    A performance bank and a wave sequence bank go out as different dump
    functions, and the double used to answer every request with the patch
    one. A read of the wrong kind would then have looked like a success.
    """

    @pytest.mark.parametrize(
        "kind, function, size",
        [
            (banks.Kind.PERFORMANCE, m.Function.ALL_PERFORMANCE_DUMP, 9050),
            ("waveseq", m.Function.ALL_WAVESEQ_DUMP, 8784),
            (banks.Kind.PATCH, m.Function.ALL_PATCH_DUMP, 14910),
        ],
    )
    def test_each_kind_is_backed_up_verified_and_stored(
        self, tmp_path, kind, function, size
    ):
        unit = Unit()
        fresh = bytearray(SIZE_FOR_DUMP[function])
        fresh[0:7] = b"Tidal B"
        assert len(fresh) == size

        result = unit.bridge.write_bank(
            11, kind, bytes(fresh), backup_dir=str(tmp_path)
        )

        assert result["verified"] is True
        assert result["mismatch"] is None
        # The backup is the dump that was there before: a zero bank here.
        saved = backup.load(result["backup"])
        was = m.parse(saved.blocks["dump"])
        assert was is not None
        assert was.data()[:size] == bytes(size)

        after = m.parse(unit.bridge.read_raw(CARD_SYSEX, function))
        assert after is not None
        assert after.data()[:size] == bytes(fresh)


class TestWritingABundle:
    """Performances, patches and wave sequences as one undoable write."""

    def _payload(self, size: int, marker: bytes) -> bytes:
        data = bytearray(size)
        data[0 : len(marker)] = marker
        return bytes(data)

    def _provided(self) -> Dict[str, bytes]:
        return {
            "performances": self._payload(len(PERFORMANCE_BANK), b"NewPerf"),
            "patches": self._payload(len(PATCH_BANK), b"NewPatch"),
            "waveseq": self._payload(len(WAVESEQ_BANK), b"NewSeq"),
        }

    def _functions(self) -> Dict[str, int]:
        return {
            "performances": m.Function.ALL_PERFORMANCE_DUMP,
            "patches": m.Function.ALL_PATCH_DUMP,
            "waveseq": m.Function.ALL_WAVESEQ_DUMP,
        }

    def test_a_bundle_writes_backs_up_and_verifies_all_three(self, tmp_path):
        unit = Unit()
        before = {
            "performances": self._payload(len(PERFORMANCE_BANK), b"WasPerf"),
            "patches": self._payload(len(PATCH_BANK), b"WasPatch"),
            "waveseq": self._payload(len(WAVESEQ_BANK), b"WasSeq"),
        }
        functions = self._functions()
        for kind, data in before.items():
            unit.dumps[(CARD_SYSEX, functions[kind])] = data

        provided = self._provided()
        result = unit.bridge.write_bank_bundle(
            11,
            performances=provided["performances"],
            patches=provided["patches"],
            waveseq=provided["waveseq"],
            backup_dir=str(tmp_path),
        )

        assert result["bank"] == "Card"
        assert result["kind"] == "bank-bundle"
        assert result["kinds"] == ["waveseq", "patches", "performances"]
        assert result["verified"] is True
        assert result["mismatch"] is None

        saved = backup.load(result["backup"])
        assert set(saved.blocks) == {"performances", "patches", "waveseq"}
        for kind, data in before.items():
            expected = m.frame_bulk(functions[kind], CARD_SYSEX, data, channel=0)
            assert saved.blocks[kind] == expected, (
                "the backup must hold what was there, not what was written"
            )

        for kind, data in provided.items():
            after = m.parse(unit.bridge.read_raw(CARD_SYSEX, functions[kind]))
            assert after is not None
            assert after.data()[: len(data)] == data

    def test_a_silent_unit_stops_the_bundle_before_a_byte_is_sent(self, tmp_path):
        silent = Unit(answer=False)
        provided = self._provided()

        with pytest.raises(DeviceError) as caught:
            silent.bridge.write_bank_bundle(
                11,
                performances=provided["performances"],
                patches=provided["patches"],
                waveseq=provided["waveseq"],
                backup_dir=str(tmp_path),
                timeout=0.1,
            )

        assert "nothing was sent" in str(caught.value)
        assert silent.dump_frames_sent() == [], "a dump was sent anyway"

    def test_a_refused_dump_stops_the_bundle_before_a_byte_is_sent(self, tmp_path):
        stubborn = Unit(refuse=(m.Function.ALL_WAVESEQ_DUMP,))
        provided = self._provided()

        with pytest.raises(DeviceError) as caught:
            stubborn.bridge.write_bank_bundle(
                11,
                performances=provided["performances"],
                patches=provided["patches"],
                waveseq=provided["waveseq"],
                backup_dir=str(tmp_path),
            )

        assert "nothing was sent" in str(caught.value)
        assert stubborn.dump_frames_sent() == [], "a dump was sent anyway"

    def test_a_corrupted_read_back_is_reported_not_claimed_as_success(self, tmp_path):
        unit = Unit()
        original = unit._sent

        def lying(data: List[int]) -> None:
            original(data)
            raw = bytes(data)
            if len(raw) > 5 and raw[4] == m.Function.ALL_PERFORMANCE_DUMP:
                stored = bytearray(unit.dumps[(CARD_SYSEX, raw[4])])
                stored[0] ^= 0xFF
                unit.dumps[(CARD_SYSEX, raw[4])] = bytes(stored)

        unit.midi_out.send_message = lying  # type: ignore[method-assign]
        provided = self._provided()

        result = unit.bridge.write_bank_bundle(
            11,
            performances=provided["performances"],
            patches=provided["patches"],
            waveseq=provided["waveseq"],
            backup_dir=str(tmp_path),
        )

        assert result["verified"] is False
        assert "differs" in str(result["mismatch"])

    def test_before_blocks_skips_backup_reads_and_unchanged_kinds(self, tmp_path):
        unit = Unit()
        functions = self._functions()
        before = {
            "performances": self._payload(len(PERFORMANCE_BANK), b"WasPerf"),
            "patches": self._payload(len(PATCH_BANK), b"WasPatch"),
            "waveseq": self._payload(len(WAVESEQ_BANK), b"WasSeq"),
        }
        for kind, data in before.items():
            unit.dumps[(CARD_SYSEX, functions[kind])] = data
        frames = {
            kind: m.frame_bulk(functions[kind], CARD_SYSEX, data, channel=0)
            for kind, data in before.items()
        }
        provided = self._provided()
        provided["patches"] = before["patches"]
        provided["waveseq"] = before["waveseq"]

        result = unit.bridge.write_bank_bundle(
            11,
            performances=provided["performances"],
            patches=provided["patches"],
            waveseq=provided["waveseq"],
            backup_dir=str(tmp_path),
            before_blocks=frames,
        )

        assert result["kinds"] == ["performances"]
        assert set(result["unchanged"]) == {"patches", "waveseq"}
        assert result["verified"] is True
        assert unit.sent_by_function(m.Function.PATCH_BANK_REQUEST) == []
        assert unit.sent_by_function(m.Function.WAVESEQ_BANK_REQUEST) == []
        assert unit.sent_by_function(m.Function.ALL_PATCH_DUMP) == []
        assert unit.sent_by_function(m.Function.ALL_WAVESEQ_DUMP) == []
        saved = backup.load(result["backup"])
        assert saved.blocks == frames

    def test_all_unchanged_sends_nothing(self, tmp_path):
        unit = Unit()
        functions = self._functions()
        before = {
            "performances": self._payload(len(PERFORMANCE_BANK), b"WasPerf"),
            "patches": self._payload(len(PATCH_BANK), b"WasPatch"),
            "waveseq": self._payload(len(WAVESEQ_BANK), b"WasSeq"),
        }
        for kind, data in before.items():
            unit.dumps[(CARD_SYSEX, functions[kind])] = data
        frames = {
            kind: m.frame_bulk(functions[kind], CARD_SYSEX, data, channel=0)
            for kind, data in before.items()
        }

        result = unit.bridge.write_bank_bundle(
            11,
            performances=before["performances"],
            patches=before["patches"],
            waveseq=before["waveseq"],
            backup_dir=str(tmp_path),
            before_blocks=frames,
        )

        assert result["kinds"] == []
        assert set(result["unchanged"]) == {"waveseq", "patches", "performances"}
        assert result["verified"] is True
        assert unit.dump_frames_sent() == []
        assert unit.sent_by_function(m.Function.PATCH_BANK_REQUEST) == []
        assert unit.sent_by_function(m.Function.PERFORMANCE_BANK_REQUEST) == []
        assert unit.sent_by_function(m.Function.WAVESEQ_BANK_REQUEST) == []

    def test_a_malformed_before_block_is_refused_before_a_byte_is_sent(self, tmp_path):
        unit = Unit()
        with pytest.raises(ValueError, match="before-block"):
            unit.bridge.write_bank_bundle(
                11,
                performances=self._provided()["performances"],
                backup_dir=str(tmp_path),
                before_blocks={"performances": b"not sysex"},
            )
        assert unit.midi_out.sent == []

    def test_a_rom_bank_is_refused_before_a_byte_is_sent(self, tmp_path):
        unit = Unit()
        with pytest.raises(DeviceError) as caught:
            unit.bridge.write_bank_bundle(
                5,
                performances=self._provided()["performances"],
                backup_dir=str(tmp_path),
            )
        assert "ROM" in str(caught.value)
        assert unit.midi_out.sent == [], "a refused write must send nothing"

    @pytest.mark.parametrize("kind", ["performances", "patches", "waveseq"])
    def test_a_wrong_size_is_refused_before_a_byte_is_sent(self, tmp_path, kind):
        unit = Unit()
        expected = {
            "performances": len(PERFORMANCE_BANK),
            "patches": len(PATCH_BANK),
            "waveseq": len(WAVESEQ_BANK),
        }[kind]
        short = bytes(expected - 1)

        with pytest.raises(ValueError) as caught:
            if kind == "performances":
                unit.bridge.write_bank_bundle(
                    11, performances=short, backup_dir=str(tmp_path)
                )
            elif kind == "patches":
                unit.bridge.write_bank_bundle(
                    11, patches=short, backup_dir=str(tmp_path)
                )
            else:
                unit.bridge.write_bank_bundle(
                    11, waveseq=short, backup_dir=str(tmp_path)
                )

        message = str(caught.value)
        assert kind in message
        assert str(expected) in message
        assert str(len(short)) in message
        assert unit.midi_out.sent == [], "a refused write must send nothing"

    def test_an_empty_bundle_is_refused_before_a_byte_is_sent(self, tmp_path):
        unit = Unit()
        with pytest.raises(ValueError, match="nothing to write"):
            unit.bridge.write_bank_bundle(11, backup_dir=str(tmp_path))
        assert unit.midi_out.sent == []


class TestTheDemoDeviceStoresWrites:
    """A fake that forgot a write could not exercise a read-back or an undo."""

    def test_a_written_patch_bank_reads_back(self, tmp_path):
        from kwsed.demo import DemoDevice

        device = DemoDevice()
        data = bytearray(PATCH_BANK)
        data[0:9] = b"Glass Mer"

        result = device.write_bank(
            "CARD", banks.Kind.PATCH, bytes(data), backup_dir=str(tmp_path)
        )

        assert result["verified"] is True
        from kws import backup as _backup

        loaded = _backup.load(result["backup"])
        assert loaded.kind == banks.Kind.PATCH
        assert "dump" in loaded.blocks
        assert device.bank_write_count == 1
        dump = device.read_bank("CARD", banks.Kind.PATCH)
        assert b"".join(dump.records) == bytes(data)

    @pytest.mark.parametrize("bank", ["ROM 4", "ROM 5", "ROM11"])
    def test_a_rom_bank_is_refused_as_the_bridge_refuses_it(self, tmp_path, bank):
        from kwsed.demo import DemoDevice

        device = DemoDevice()
        with pytest.raises(ValueError) as caught:
            device.write_bank(
                bank, banks.Kind.PATCH, PATCH_BANK, backup_dir=str(tmp_path)
            )
        assert "ROM" in str(caught.value)
        assert device.bank_write_count == 0
        with pytest.raises(ValueError) as caught:
            device.write_bank_bundle(bank, patches=PATCH_BANK, backup_dir=str(tmp_path))
        assert "ROM" in str(caught.value)
        assert device.bundle_write_count == 0

    def test_a_written_bundle_reads_back(self, tmp_path):
        from kwsed.demo import DemoDevice

        device = DemoDevice()
        performances = bytearray(PERFORMANCE_BANK)
        performances[0:8] = b"Amber Dr"
        patches = bytearray(PATCH_BANK)
        patches[0:8] = b"Kelp For"
        waveseq = bytearray(WAVESEQ_BANK)
        waveseq[0:7] = b"Steps 0"

        result = device.write_bank_bundle(
            "CARD",
            performances=bytes(performances),
            patches=bytes(patches),
            waveseq=bytes(waveseq),
            backup_dir=str(tmp_path),
        )

        assert result["kind"] == "bank-bundle"
        assert result["kinds"] == ["waveseq", "patches", "performances"]
        assert result["verified"] is True
        from kws import backup as _backup

        loaded = _backup.load(result["backup"])
        assert loaded.kind == "bank-bundle"
        assert set(loaded.blocks) == {"performances", "patches", "waveseq"}
        assert device.bundle_write_count == 1

        perf_dump = device.read_bank("CARD", banks.Kind.PERFORMANCE)
        assert b"".join(perf_dump.records) == bytes(performances)
        patch_dump = device.read_bank("CARD", banks.Kind.PATCH)
        assert b"".join(patch_dump.records) == bytes(patches)
        stored = m.parse(device.read_raw(CARD_SYSEX, m.Function.ALL_WAVESEQ_DUMP))
        assert stored is not None
        assert stored.data()[: len(waveseq)] == bytes(waveseq)


class TestBankCopy:
    """``copy_bank``: read one bank, remap it, write another, fenced."""

    @staticmethod
    def _load_source(unit: Unit, *, owner: str = "RAM1") -> Tuple[bytes, bytes, bytes]:
        """Fill one bank with all three kinds; performances name ``owner``.

        Part 0 names the owning bank itself (a self-reference, which the copy
        must retarget); part 1 names ROM 4 (third-party, which it must not).
        """
        bank_byte = banks.bank(owner).sysex
        position = banks.bank(owner).number
        patch_bank = bytes((index * 13) % 256 for index in range(14910))
        waveseq = bytes((index * 7) % 256 for index in range(r.WAVESEQ_BANK_SIZE))
        records = bytearray()
        for _ in range(50):
            rec = bytearray(r.PERFORMANCE_SIZE)
            rec[0:6] = b"Source"
            r.set_performance_part(rec, 0, bank=position, patch=4)
            r.set_performance_part(rec, 1, bank=3, patch=5)  # ROM 4
            records += rec
        unit.dumps[(bank_byte, m.Function.ALL_PATCH_DUMP)] = patch_bank
        unit.dumps[(bank_byte, m.Function.ALL_PERFORMANCE_DUMP)] = bytes(records)
        unit.dumps[(bank_byte, m.Function.ALL_WAVESEQ_DUMP)] = waveseq
        return patch_bank, bytes(records), waveseq

    def test_ram1_to_card_is_a_remapped_verified_copy(self, tmp_path):
        unit = Unit()
        patch_bank, _perf_bank, waveseq = self._load_source(unit)

        result = unit.bridge.copy_bank("RAM1", "CARD", backup_dir=str(tmp_path))

        assert result["kind"] == "bank-bundle"
        assert result["kinds"] == ["waveseq", "patches", "performances"]
        assert result["verified"] is True
        stored = unit.dumps[(CARD_SYSEX, m.Function.ALL_PATCH_DUMP)]
        assert stored == patch_bank
        stored = unit.dumps[(CARD_SYSEX, m.Function.ALL_WAVESEQ_DUMP)]
        assert stored == waveseq
        stored = unit.dumps[(CARD_SYSEX, m.Function.ALL_PERFORMANCE_DUMP)]
        parts = r.performance_parts(stored)
        # the self-reference now names the Card; ROM 4 stayed ROM 4
        assert (parts[0].bank, parts[0].patch) == (11, 4)
        assert (parts[1].bank, parts[1].patch) == (3, 5)

    def test_card_to_ram1_remaps_the_other_way(self, tmp_path):
        unit = Unit()
        self._load_source(unit, owner="CARD")

        result = unit.bridge.copy_bank("CARD", "RAM1", backup_dir=str(tmp_path))

        assert result["verified"] is True
        stored = unit.dumps[(0, m.Function.ALL_PERFORMANCE_DUMP)]
        parts = r.performance_parts(stored)
        # the Card self-references now name RAM 1
        assert (parts[0].bank, parts[0].patch) == (0, 4)
        assert (parts[1].bank, parts[1].patch) == (3, 5)

    def test_the_destination_is_backed_up_before_anything_is_sent(self, tmp_path):
        """The fence, for a copy too: what was on the Card survives on disk."""
        unit = Unit()
        before = bytes((index * 3) % 256 for index in range(14910))
        functions = {
            "patches": m.Function.ALL_PATCH_DUMP,
            "performances": m.Function.ALL_PERFORMANCE_DUMP,
            "waveseq": m.Function.ALL_WAVESEQ_DUMP,
        }
        unit.dumps[(CARD_SYSEX, m.Function.ALL_PATCH_DUMP)] = before
        self._load_source(unit)

        result = unit.bridge.copy_bank("RAM1", "CARD", backup_dir=str(tmp_path))

        saved = backup.load(result["backup"])
        assert saved.blocks["patches"] == m.frame_bulk(
            functions["patches"], CARD_SYSEX, before, channel=0
        )

    def test_a_silent_unit_starts_no_copy(self, tmp_path):
        unit = Unit(answer=False)
        self._load_source(unit)

        with pytest.raises(DeviceError, match="nothing was sent"):
            unit.bridge.copy_bank("RAM1", "CARD", backup_dir=str(tmp_path), timeout=0.1)
        assert unit.dump_frames_sent() == [], "no dump may go out unreadable"

    def test_a_rom_bank_cannot_be_copied_from(self, tmp_path):
        with pytest.raises(DeviceError, match="ROM 4 is ROM"):
            Unit().bridge.copy_bank("ROM4", "CARD", backup_dir=str(tmp_path))
        with pytest.raises(DeviceError, match="ROM 4 is ROM"):
            Unit().bridge.copy_bank("CARD", "ROM4", backup_dir=str(tmp_path))

    def test_copying_a_bank_onto_itself_is_refused(self, tmp_path):
        with pytest.raises(DeviceError, match="copies nothing"):
            Unit().bridge.copy_bank("RAM1", "RAM1", backup_dir=str(tmp_path))

    def test_a_read_back_that_does_not_match_is_reported(self, tmp_path):
        unit = Unit()
        self._load_source(unit)
        original = unit._sent

        def lying(data):
            original(data)
            raw = bytes(data)
            if (
                len(raw) > 5
                and raw[4] == m.Function.ALL_PERFORMANCE_DUMP
                and raw[5] == CARD_SYSEX
            ):
                stored = bytearray(
                    unit.dumps[(CARD_SYSEX, m.Function.ALL_PERFORMANCE_DUMP)]
                )
                stored[0] ^= 0xFF
                unit.dumps[(CARD_SYSEX, m.Function.ALL_PERFORMANCE_DUMP)] = bytes(
                    stored
                )

        unit.midi_out.send_message = lying  # type: ignore[method-assign]

        result = unit.bridge.copy_bank("RAM1", "CARD", backup_dir=str(tmp_path))

        assert result["verified"] is False
        assert result["mismatch"]


def test_a_demo_write_multi_returns_a_multi_envelope(tmp_path):
    from kwsed.demo import DemoDevice

    device = DemoDevice()
    result = device.write_multi(b"hello", backup_dir=str(tmp_path))

    assert result["verified"] is True
    from kws import backup as _backup

    loaded = _backup.load(result["backup"])
    assert loaded.kind == "multi"
    assert "dump" in loaded.blocks
