# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# The System Exclusive frame layout, the model ID, the function codes, the
# bank list and the nibble encoding are transcribed as **facts** from Korg's
# documentation, from System Exclusive dumps the instrument produced, and
# from the Wavestation support in Edisyn (Sean Luke and contributors,
# Apache-2.0). No Edisyn code is used or derived here -- see LICENSE and
# docs/RESOLUTION_NOTES.md on why that distinction is kept strictly.
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

"""The Korg Wavestation SR System Exclusive wire format.

Frame::

    F0 42 3n 28 <function> [bank] [number] <nibble data ...> F7

* ``42h`` Korg; ``28h`` the Wavestation family.
* ``3n`` -- format 3, channel *n*, sharing one byte. A caller passing "1"
  meaning channel 1 gets ``31``, which is channel **2**.

**The data is nibble-encoded, not bit-packed.** Two MIDI bytes carry one
data byte, **low nibble first**. That is a different scheme from the Korg
X5/X5D family, which packs eight bytes to seven -- so a reader that assumes
one Korg convention covers the other gets a stream that is wrong in a way
that still decodes.

**And the data begins at a different offset for each message.** A bulk dump
carries a bank byte; a single-object dump carries a bank *and* a number. Get
that offset wrong by one and every nibble pairs with its neighbour instead
of its partner, which does not fail -- it yields bytes that are plausible
and wrong. That cost this project its longest detour: the bulk performance
dump was read from offset 5 instead of 6 and produced no recognisable names
at any record size, which looked like a wrong record size and was not. See
docs/RESOLUTION_NOTES.md §3.

The arithmetic confirms the offsets. With the data taken from offset 6, the
performance bulk dump yields 9050 bytes, exactly 50 records of 181; the
patch bulk dump yields 14910, exactly 35 of 426. Both remainders are zero,
and every record then begins with a legible name.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "SYSEX_START",
    "SYSEX_END",
    "KORG_ID",
    "WAVESTATION_ID",
    "FORMAT_NIBBLE",
    "Function",
    "NAME_LEN",
    "ProtocolError",
    "channel_byte",
    "channel_of",
    "to_nibbles",
    "from_nibbles",
    "frame",
    "parse",
    "Packet",
    "decode_name",
    "encode_name",
    "identity_request",
    "identity_reply_fields",
    "checksum",
    "frame_bulk",
    "bulk_request",
    "bank_request",
    "single_patch_request",
    "single_patch_dump",
    "patch_write_command",
    "REQUEST_FOR_DUMP",
    "DATA_OFFSET",
]

SYSEX_START = 0xF0
SYSEX_END = 0xF7

#: Korg's MIDI manufacturer ID.
KORG_ID = 0x42

#: Model ID of the Wavestation family (WS, EX, A/D, SR).
WAVESTATION_ID = 0x28

#: High nibble of the third byte; the low nibble is the MIDI channel.
FORMAT_NIBBLE = 0x30

#: Names are at most 16 bytes. Performances use 15 characters and a NUL;
#: patches use all 16, space-padded. :func:`decode_name` handles both rather
#: than making the caller know which it has.
NAME_LEN = 16


class ProtocolError(ValueError):
    """A frame is not what it claims to be."""


class Function:
    """Function codes.

    The **dump** codes are confirmed against dumps the instrument produced
    and against the sizes Edisyn's Wavestation support expects.

    The **status replies** below are not. They are transcribed from Korg's
    family convention: no dump in hand contains one, this unit has never
    returned one, and Edisyn implements none of them. Treat a match on one
    as a strong hint rather than a fact -- see docs/RESOLUTION_NOTES.md
    section 8.4.
    """

    # Requests
    #: Each kind of bank has its **own** request. kwsed had only the patch
    #: one for its whole life and waited for performance dumps that were
    #: never asked for -- which read as "the unit decides what to send".
    PATCH_BANK_REQUEST = 0x1C  #: + bank -> ALL_PATCH_DUMP
    PERFORMANCE_BANK_REQUEST = 0x1D  #: + bank -> ALL_PERFORMANCE_DUMP
    WAVESEQ_BANK_REQUEST = 0x0C  #: + bank -> ALL_WAVESEQ_DUMP
    SINGLE_PATCH_REQUEST = 0x10  #: + bank + number -> SINGLE_PATCH_DUMP
    SINGLE_PERFORMANCE_REQUEST = 0x19  #: + bank + number
    PATCH_WRITE = 0x11  #: + bank + number: commit the buffer
    PERFORMANCE_WRITE = 0x1A  #: + bank + number: commit the buffer
    MULTI_REQUEST = 0x06  #: no argument -> 0x55, 0x5E and 0x62
    ALL_DATA_REQUEST = 0x0F  #: no argument -> ALL_DATA_DUMP
    SYSTEM_SETUP_REQUEST = 0x0E  #: no argument -> SYSTEM_SETUP_DUMP
    BULK_REQUEST = PATCH_BANK_REQUEST  #: the old name for the patch one

    # Dumps
    SINGLE_PATCH_DUMP = 0x40  #: one patch: bank, number, 426 bytes
    PERFORMANCE_DUMP = 0x49  #: one performance: bank, number, 181 bytes
    ALL_PATCH_DUMP = 0x4C  #: one bank: 35 patches of 426
    ALL_PERFORMANCE_DUMP = 0x4D  #: one bank: 50 performances of 181
    ALL_WAVESEQ_DUMP = 0x54  #: one bank: 32 sequences over a 501-step pool
    ALL_DATA_DUMP = 0x50  #: system, patches, performances, wave seqs
    SYSTEM_SETUP_DUMP = 0x51
    MULTI_SETUP_DUMP = 0x55  #: the three multi messages always travel
    MULTI_SETUP_EXPANDED = 0x5E  #: together, in this order
    MULTISET_DUMP = 0x62  #: ...and this one carries all 32 Multisets

    # Status replies, as Korg's Reference Guide appendix 4 lists them.
    WRITE_COMPLETED = 0x21
    WRITE_ERROR = 0x22
    DATA_LOAD_COMPLETED = 0x23
    #: Sent when a message arrives whose checksum failed.
    DATA_LOAD_ERROR = 0x24

    NAMES = {
        0x10: "object request",
        0x19: "single performance request",
        0x1C: "patch bank request",
        0x1D: "performance bank request",
        0x0C: "wave sequence bank request",
        0x0E: "system setup request",
        0x0F: "all data request",
        0x49: "performance dump",
        0x4C: "all patch dump",
        0x4D: "all performance dump",
        0x54: "all wave sequence dump",
        0x50: "all data dump",
        0x51: "system setup dump",
        0x55: "multi mode setup dump",
        0x5E: "multi mode setup expanded dump",
        0x62: "multi mode setup SR expanded dump",
        0x06: "multi mode setup dump request",
        0x40: "single patch dump",
        0x11: "patch write command",
        0x1A: "performance write command",
        0x21: "write completed",
        0x22: "write error",
        0x23: "data load completed",
        0x24: "data load error",
    }

    #: Korg's appendix lists write error and data load error. 0x26 is a
    #: data format error on the X5 family and is kept here because a reply
    #: that means trouble should not be mistaken for a dump, but this unit
    #: has never sent one.
    ERRORS = frozenset({0x22, 0x24, 0x26})

    @classmethod
    def name(cls, code: int) -> str:
        return cls.NAMES.get(code, f"function {code:#04x}")


#: Where the nibble data starts in each message, counted from ``F0``.
#:
#: Not one number, and that is the point. A bulk dump carries a bank byte, a
#: single-object dump a bank and a number, so the payload begins in a
#: different place. One byte out pairs every nibble with the wrong partner.
DATA_OFFSET = {
    Function.ALL_PATCH_DUMP: 6,
    Function.ALL_PERFORMANCE_DUMP: 6,
    Function.ALL_WAVESEQ_DUMP: 6,
    Function.PERFORMANCE_DUMP: 7,
    Function.SINGLE_PATCH_DUMP: 7,
    # The Multiset dump carries two bytes of its own before the data, where
    # the bank dumps carry one. Reading it at 6 yields 3778 bytes that still
    # look like data -- the names even decode -- so this is the difference
    # between a right answer and a plausible wrong one.
    Function.MULTISET_DUMP: 7,
}


# --- channel -----------------------------------------------------------------


def channel_byte(channel: int) -> int:
    """0-based MIDI channel to the third byte of a frame."""
    value = int(channel)
    if not 0 <= value <= 15:
        raise ProtocolError(f"MIDI channel {value} is outside 0-15 (0-based)")
    return FORMAT_NIBBLE | value


def channel_of(byte: int) -> int:
    """The 0-based channel a frame's third byte carries."""
    if byte & 0xF0 != FORMAT_NIBBLE:
        raise ProtocolError(
            f"{byte:#04x} is not a format-3 byte; kwsed speaks format 3 only"
        )
    return byte & 0x0F


# --- nibble encoding ---------------------------------------------------------


def from_nibbles(data: Sequence[int]) -> bytes:
    """Nibble pairs to bytes, **low nibble first**.

    A trailing odd byte is dropped: half a byte is not a byte, and inventing
    the missing nibble would put a plausible value at the end of a record.
    """
    out = bytearray()
    for index in range(0, len(data) - 1, 2):
        low, high = data[index], data[index + 1]
        for part in (low, high):
            if not 0 <= part <= 0x0F:
                raise ProtocolError(
                    f"{part:#04x} is not a nibble; nibble data is 0x00-0x0F"
                )
        out.append(low | (high << 4))
    return bytes(out)


def to_nibbles(data: Sequence[int]) -> bytes:
    """Bytes to nibble pairs. The inverse of :func:`from_nibbles`."""
    out = bytearray()
    for byte in data:
        out.append(byte & 0x0F)
        out.append((byte >> 4) & 0x0F)
    return bytes(out)


# --- frames ------------------------------------------------------------------


class Packet:
    """A parsed Wavestation message."""

    __slots__ = ("channel", "function", "payload")

    def __init__(self, channel: int, function: int, payload: bytes) -> None:
        self.channel = channel
        self.function = function
        self.payload = payload

    @property
    def is_error(self) -> bool:
        return self.function in Function.ERRORS

    @property
    def function_name(self) -> str:
        return Function.name(self.function)

    def data(self) -> bytes:
        """The decoded payload of a dump, from the right offset.

        The offset comes from :data:`DATA_OFFSET`, keyed on the function --
        not passed in by the caller, because a caller that has to remember
        whether this message carries a number is a caller that will
        eventually forget.
        """
        try:
            offset = DATA_OFFSET[self.function]
        except KeyError:
            raise ProtocolError(
                f"{self.function_name} is not a dump kwsed decodes"
            ) from None
        # payload starts after F0 42 3n 28 fn, so five bytes are already
        # gone; DATA_OFFSET counts from F0.
        return from_nibbles(self.payload[offset - 5 :])

    #: Functions whose payload opens with a bank byte. The Multiset dump
    #: is decoded but excluded: its two header bytes are not a bank.
    _BANKED = frozenset(
        {
            Function.ALL_PATCH_DUMP,
            Function.ALL_PERFORMANCE_DUMP,
            Function.ALL_WAVESEQ_DUMP,
            Function.PERFORMANCE_DUMP,
            Function.SINGLE_PATCH_DUMP,
        }
    )

    @property
    def bank(self) -> Optional[int]:
        """The bank byte a dump carries, if it carries one."""
        if self.function in self._BANKED and self.payload:
            return self.payload[0]
        return None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Packet(channel={self.channel}, "
            f"function={self.function:#04x} {self.function_name!r}, "
            f"{len(self.payload)} bytes)"
        )

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Packet)
            and other.channel == self.channel
            and other.function == self.function
            and other.payload == self.payload
        )


def checksum(data: Sequence[int]) -> int:
    """The trailing byte of a bulk dump: the 7-bit sum of its nibble data.

    kwsed read dumps for a long time without noticing this, because reading
    does not need it -- the record arithmetic divides exactly whether or not
    the last byte is counted. It matters the moment a dump is *built*: a
    file without it is one byte short of what the instrument sends, and the
    instrument will not load it.

    Confirmed against every bulk dump in the owner's library: the last byte
    before F7 equals ``sum(nibble data) & 0x7F`` in each one.
    """
    return sum(data) & 0x7F


def frame_bulk(
    function: int, bank: int, data: Sequence[int], *, channel: int = 0
) -> bytes:
    """A bulk dump, nibble-encoded and checksummed, ready to save or send.

    ``data`` is plain bytes; the nibble encoding and the trailing checksum
    are applied here so that no caller has to remember either.
    """
    nibbles = list(to_nibbles(data))
    return frame(function, [bank] + nibbles + [checksum(nibbles)], channel=channel)


def frame(function: int, payload: Sequence[int] = (), *, channel: int = 0) -> bytes:
    """Build a Wavestation System Exclusive message."""
    for byte in payload:
        if not 0 <= byte <= 0x7F:
            raise ProtocolError(
                f"{byte:#04x} is not a 7-bit data byte; every byte between "
                f"F0 and F7 must have bit 7 clear"
            )
    if not 0 <= function <= 0x7F:
        raise ProtocolError(f"function {function:#04x} is not a 7-bit value")
    return bytes(
        [
            SYSEX_START,
            KORG_ID,
            channel_byte(channel),
            WAVESTATION_ID,
            function,
            *payload,
            SYSEX_END,
        ]
    )


def parse(message: Sequence[int]) -> Optional[Packet]:
    """Parse one Wavestation message, or ``None`` if it is not one.

    ``None`` rather than an exception for foreign traffic -- including other
    Korg models, which share the manufacturer byte and differ in the model
    byte. A shared MIDI chain carries plenty of both.
    """
    data = bytes(message)
    if len(data) < 6:
        return None
    if data[0] != SYSEX_START or data[-1] != SYSEX_END:
        return None
    if data[1] != KORG_ID or data[3] != WAVESTATION_ID:
        return None
    if data[2] & 0xF0 != FORMAT_NIBBLE:
        return None
    return Packet(channel=data[2] & 0x0F, function=data[4], payload=data[5:-1])


# --- names -------------------------------------------------------------------


def decode_name(data: Sequence[int]) -> str:
    """Up to :data:`NAME_LEN` bytes to a string.

    Stops at a NUL and strips trailing spaces. Both are needed: performance
    names are 15 characters and a NUL, patch names are 16 and space-padded,
    and a reader that handles only one turns the other into a name with a
    stray character on the end.
    """
    raw = bytes(data)[:NAME_LEN]
    end = raw.find(0)
    if end >= 0:
        raw = raw[:end]
    return "".join(chr(byte) if 32 <= byte <= 126 else " " for byte in raw).rstrip()


def encode_name(name: str, *, length: int = NAME_LEN) -> List[int]:
    """A string to exactly ``length`` bytes, space-padded."""
    trimmed = name[:length].ljust(length)
    return [ord(c) if 32 <= ord(c) <= 126 else 32 for c in trimmed]


# --- requests ----------------------------------------------------------------


def identity_request() -> bytes:
    """``F0 7E 7F 06 01 F7`` -- not a Korg message at all."""
    return bytes([SYSEX_START, 0x7E, 0x7F, 0x06, 0x01, SYSEX_END])


def identity_reply_fields(message: Sequence[int]) -> Optional[Dict[str, Any]]:
    """Fields of an Identity Reply, or ``None``.

    Matches the reply sub-ID (``02``) and never the request (``01``), which
    is what stops a MIDI-Thru loop being taken for a device.
    """
    data = bytes(message)
    if len(data) < 12 or data[0] != SYSEX_START or data[-1] != SYSEX_END:
        return None
    if data[1] != 0x7E or data[3] != 0x06 or data[4] != 0x02:
        return None
    return {
        "channel": data[2],
        "manufacturer": data[5],
        "family": (data[6], data[7]),
        "is_korg": data[5] == KORG_ID,
        "is_wavestation": data[5] == KORG_ID and data[6] == WAVESTATION_ID,
    }


#: Which request asks for which dump.
REQUEST_FOR_DUMP = {
    Function.ALL_PATCH_DUMP: Function.PATCH_BANK_REQUEST,
    Function.MULTISET_DUMP: Function.MULTI_REQUEST,
    Function.ALL_PERFORMANCE_DUMP: Function.PERFORMANCE_BANK_REQUEST,
    Function.ALL_WAVESEQ_DUMP: Function.WAVESEQ_BANK_REQUEST,
}


def bank_request(bank: int, dump: int, channel: int = 0) -> bytes:
    """Ask for one bank of one kind. Read-only, makes no sound."""
    try:
        function = REQUEST_FOR_DUMP[dump]
    except KeyError:
        raise ProtocolError(
            f"there is no bank request for {Function.name(dump)}"
        ) from None
    return frame(function, [bank], channel=channel)


def bulk_request(bank: int, channel: int = 0) -> bytes:
    """Ask for a bank of **patches**. Read-only, makes no sound.

    Kept for callers that predate :func:`bank_request`. It asks for patches,
    which is what it always did.
    """
    return frame(Function.PATCH_BANK_REQUEST, [bank], channel=channel)


def single_patch_request(bank: int, number: int, channel: int = 0) -> bytes:
    """Ask for one patch. Read-only, makes no sound."""
    return frame(Function.SINGLE_PATCH_REQUEST, [bank, number], channel=channel)


def single_patch_dump(
    bank: int, number: int, data: Sequence[int], *, channel: int = 0
) -> bytes:
    """One patch, addressed to a slot. **Lands in the edit buffer.**

    On its own this changes nothing in memory: Korg's appendix says a single
    patch dump is placed in the edit buffer, and it takes a separate write
    command to put it in the slot.
    """
    nibbles = list(to_nibbles(data))
    return frame(
        Function.SINGLE_PATCH_DUMP,
        [bank, number] + nibbles + [checksum(nibbles)],
        channel=channel,
    )


def patch_write_command(bank: int, number: int, channel: int = 0) -> bytes:
    """Commit the edit buffer to a patch slot. **Destructive.**

    Edisyn sends this and notes that it does not work; its message passes
    the function code and the bank where the bank and the number belong.
    The documented form is bank then number.
    """
    return frame(Function.PATCH_WRITE, [bank, number], channel=channel)
