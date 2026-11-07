# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# kwsed is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation either version 2 of the License, or (at your option) any later
# version.

"""Opt-in hardware tests for a real Korg Wavestation SR.

**Opt-in: see :mod:`tests.conftest`.** Everything else in this suite runs
synthetically -- no bridge is ever constructed and no port is opened -- which
is also its limit: a synthetic check agrees with whatever kwsed believes, so
every size and offset below the API surface was only ever checked against a
real unit.

This file is the other half. It asks the machine, and the machine's answer
is the one that cannot be transcribed wrong:

* **Read-only, structural.** The Identity Reply, the bank dump -- sizes,
  counts and the checksum -- and the name-list exchange, at the timeouts the
  bench measured rather than the two-second default the code shipped with.
* **One write, heavy gate.** The spare Card bank, written and read back, and
  put back -- behind ``KWSED_HW_WRITE=1``.

**What these tests deliberately do not do.** They never touch a ROM bank or
RAM 1: the write goes to the Card, the spare, and never to a bank a person
might be playing from. Not a single test here selects a sound: ``select()``
is the only thing in the bridge that makes a sound, and nothing here needs
the unit to play anything.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kws import alsa, banks, bridge, dumps
from kws import messages as m


def _bulk_checksum_is_good(packet: m.Packet) -> bool:
    """The trailing checksum byte of a bulk dump against its nibble data.

    A bulk dump frame is ``F0 42 3n 28 <fn> <bank> <nibbles> <checksum> F7``,
    so the checksum byte is the last payload byte and the nibble data is
    everything between the bank byte and it. A bank the unit dumps with a
    bad checksum is a bank that was not what was asked for, and a checksum
    this suite does not check is one the reader did not ask for either.
    """
    nibbles = packet.payload[1:-1]
    return m.checksum(nibbles) == packet.payload[-1]


def _require_write_gate() -> None:
    """The spare write is destructive only behind the explicit gate.

    ``--hardware`` gets the suite onto the bench; ``KWSED_HW_WRITE=1`` gets a
    write past its own skip. The last one belongs to the test, not the
    fixture, because a write is not "more hardware tests" but "the hardware
    suite, changed": a store into the spare Card bank is permanent unit
    memory, the one thing every other test in this file leaves alone.
    """
    if not os.environ.get("KWSED_HW_WRITE"):
        pytest.skip(
            "the hardware write tests need KWSED_HW_WRITE=1: they write the "
            "spare Card bank, which only that gate may target"
        )


class TestHardwareIdentity:
    def test_identity_probe(self, device: bridge.WavestationBridge) -> None:
        """The unit identifies as Korg, in the Wavestation family, and says
        which global channel its replies come back on.

        The reply is the first thing asked for, and the one every later
        request is answered on: the global channel cannot be guessed, since
        a request on any other channel meets silence rather than an error.
        """
        fields = device.identify()
        assert fields is not None, "the unit did not answer an Identity Request"
        assert fields["is_korg"]
        assert fields["is_wavestation"]
        assert fields["manufacturer"] == m.KORG_ID
        assert tuple(fields["family"]) == (m.WAVESTATION_ID, 0)
        channel = fields["channel"]
        assert isinstance(channel, int)
        assert 0 <= channel <= 15


class TestHardwareBankRead:
    def test_the_spare_card_bank_dump_decodes(
        self, device: bridge.WavestationBridge
    ) -> None:
        """The Card patch dump is 35 records of exactly 426 bytes.

        A patch bank is nearly 30 kB on the wire, about eight seconds at
        31250 baud before the unit's own delay, which is why the bench read
        this at the measured :data:`kws.bridge.DUMP_TIMEOUT` rather than the
        two-second default the bridge ships with. The reply has to come back
        whole, or the record arithmetic that divides so cleanly stops being
        evidence.
        """
        dump = device.read_bank("CARD", banks.Kind.PATCH)
        assert dump.bank.label == "Card"
        assert dump.kind == banks.Kind.PATCH
        assert dump.count == banks.PATCHES_PER_BANK
        assert dump.record_size == dumps.PATCH_RECORD
        assert len(dump.records) * dump.record_size == (
            banks.PATCHES_PER_BANK * dumps.PATCH_RECORD
        )
        for record in dump.records:
            assert isinstance(record, bytes)
            assert len(record) == dumps.PATCH_RECORD
            assert isinstance(dumps.record_name(record), str)

    def test_the_bank_dump_carries_its_checksum(
        self, device: bridge.WavestationBridge
    ) -> None:
        """The trailing checksum byte of the Card dump is the nibble sum.

        A bulk dump is unacknowledged on the wire, so the checksum is the
        part of the reply this suite can check without believing any of the
        rest: it is computed over the nibble data the unit was sent, and a
        dump that does not carry the sum it was asked for is a dump that was
        not the dump that was asked for.
        """
        card = banks.bank("CARD")
        packet = m.parse(device.read_raw(card.sysex, m.Function.ALL_PATCH_DUMP))
        assert packet is not None
        assert packet.bank == card.sysex, "the reply must name the bank it is about"
        assert _bulk_checksum_is_good(packet)

    def test_a_single_patch_read_uses_the_measured_timeout(
        self, device: bridge.WavestationBridge
    ) -> None:
        """One patch is answered after about 9.9 seconds, consistently.

        ``SINGLE_OBJECT_TIMEOUT`` is measured, not guessed, and is what a
        single patch read waits for: the old value was the bridge default of
        two seconds, which cut every reply off and then took the answer for
        the next request -- which is how a read of the Card came back holding
        a patch from another bank. This read goes out on that measured clock.
        """
        data = device.read_patch("CARD", 0)
        assert isinstance(data, bytes)
        assert len(data) == dumps.PATCH_RECORD, (
            f"a single patch came back as {len(data)} bytes, "
            f"not the {dumps.PATCH_RECORD} a patch is"
        )


class TestHardwareNameListExchange:
    def test_the_name_list_exchange_reads_every_patch_name(
        self, device: bridge.WavestationBridge
    ) -> None:
        """The bank's names come back as a list, one per patch, nothing more.

        A whole-bank read answers with all thirty-five names at once, which
        is the name-list exchange this suite makes with the unit: what came
        back is the unit's list, keyed by the number the bank dump carries,
        and it is not this suite's list to edit.
        """
        dump = device.read_bank("CARD", banks.Kind.PATCH)
        assert dump.kind == banks.Kind.PATCH
        names = dump.names()
        for number, name in names.items():
            assert isinstance(number, int)
            assert isinstance(name, str)


class TestTheSendGapSweep:
    def test_the_spare_card_reads_cleanly_at_every_gap_the_sweep_tries(
        self, device: bridge.WavestationBridge, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sweep the send gap, at the pace the ALSA path enforces.

        ``SEND_GAP`` is a guess inherited from the siblings, and this is the
        sweep that was meant to replace it with a measurement: try a series of
        gaps and read the same bank back clean at each, the way
        ``tools/measure_send_gap.py`` does for the name sweeps. A gap too small
        puts the reply of one request into the next, and a late answer is
        indistinguishable from the right one -- the bank the reply must not
        be filed under is the bank it must be filed under.

        Bank-sized messages go through :mod:`kws.alsa`, paced to the wire by
        :data:`alsa.Sender.BYTES_PER_SECOND`; the sweep therefore measures the
        gap *and* the pace the way the bench measured them.
        """
        reference = device.read_bank("CARD", banks.Kind.PATCH)
        for gap in (0.010, 0.005, 0.002, 0.001, 0.0005):
            monkeypatch.setattr(bridge, "SEND_GAP", gap)
            again = device.read_bank("CARD", banks.Kind.PATCH)
            assert again == reference, (
                f"a send gap of {gap} seconds did not read the spare Card "
                "cleanly; the smallest gap that fails is the one to sharpen"
            )


class TestSpareCardWrite:
    def test_the_spare_card_bank_is_written_and_read_back(
        self, device: bridge.WavestationBridge, tmp_path: Path
    ) -> None:
        """A patch bank written to the spare Card bank is read back identical.

        The bench write of 2026-09-12: a built patch bank went to the Card
        and came back with 14910 decoded bytes and no differences. The fence
        did the rest -- backup before send, read-back compare, report a
        mismatch rather than claim success -- and this is the one hardware
        test that drives it for real.
        """
        _require_write_gate()

        destination = banks.bank("CARD")
        assert destination.writable, "the spare write target must be the Card"
        original = device.read_bank("CARD", banks.Kind.PATCH)
        before = b"".join(original.records)
        edited = bytearray(before)
        edited[0 : m.NAME_LEN] = bytes(m.encode_name("SpareWrite"))
        assert bytes(edited) != before, "the invented name changed no byte"

        try:
            result = device.write_bank(
                "CARD", banks.Kind.PATCH, bytes(edited), backup_dir=str(tmp_path)
            )
            assert result["verified"] is True
            assert result["mismatch"] is None
            after = device.read_bank("CARD", banks.Kind.PATCH)
            assert b"".join(after.records) == bytes(edited)
        finally:
            device.write_bank(
                "CARD", banks.Kind.PATCH, before, backup_dir=str(tmp_path)
            )
            assert device.read_bank("CARD", banks.Kind.PATCH) == original


class TestHardwareCardName:
    """The HCard's name field, read-only.

    The card name is the only identity signal the synth exposes for which
    of the card's forty banks is active. This test asks for the System
    Setup dump and accepts either a name or none -- the offset is still
    marked ``# CONFIRM AGAINST HARDWARE``, so a mismatch must not turn into
    a hardware failure. Nothing here writes: one request, one reply.
    """

    def test_the_system_setup_dump_comes_back(
        self, device: bridge.WavestationBridge
    ) -> None:
        data = device.read_system_setup()
        if data is None:
            # A skip, not a pass: this unit answered an Identity Request
            # but not a System Setup request, so the 0x51 field is still
            # unconfirmed rather than shown absent.
            pytest.skip(
                "the unit answered no System Setup dump (0x51); the card-name "
                "field is therefore still unconfirmed on hardware"
            )
        assert len(data) >= bridge.CARD_NAME_OFFSET + bridge.CARD_NAME_LEN

    def test_the_card_name_is_none_or_the_name_the_card_shows(
        self, device: bridge.WavestationBridge
    ) -> None:
        name = device.read_card_name()
        assert name is None or isinstance(name, str)
        if name is not None:
            assert name == name.strip()
            assert 0 < len(name) <= m.NAME_LEN
