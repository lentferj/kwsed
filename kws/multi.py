# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# The message type and the fact that the three multi messages travel together
# are documented in appendix 4 of Korg's Wavestation SR Reference Guide. The
# record layout below was derived from dumps the instrument produced and is
# not in that appendix in this form.
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

"""Multi mode: 32 Multisets, each assigning all 16 MIDI channels.

**"Multi" names two different things on this instrument**, and they are not
interchangeable. A **performance** combines up to eight parts, each a patch
with its own key range -- that is the multi-timbral *sound*. A **Multiset**
is the MIDI multi mode proper: which performance answers on each of the 16
channels, and at what level. This module is about the second.

The dump is message type ``0x62`` and the request is ``0x06``. Asking once
draws three replies -- ``0x55``, ``0x5E`` then ``0x62`` -- which Korg's
appendix says always travel together; only the last carries all 32
Multisets.

The layout was derived rather than read, and the arithmetic is the evidence:
``22 + 16 * 4 = 86`` lands flush on the record boundary, and ``32 * 86 =
2752`` is exactly where the names begin.

Record layout (86 bytes):
    byte 0        first byte of the FX head (raw; meaning still open)
    bytes 1-21    the 21-byte FX block (kws.fx.FxBlock)
    bytes 22-85   16 parts x 4 bytes, except that the 16th part's fourth
                  byte is the FX control channel, not a flag

The FX control channel is **the last byte of the record**, stored as
channel - 1 (0 = channel 1, 15 = channel 16). Measured on the bench
2026-11-13: MULTI:2 set to 7 stored 6, set to 16 stored 15; the panel's
parameter-change echo is parameter 29 (``MULTISET_FX_CONTROL_CHAN``), a
global *address* that edits the current Multiset's value. KB stores 16
(measured 2026-11-13).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Sequence

from kws.fx import FxBlock

__all__ = [
    "MULTISETS",
    "MULTISET_SIZE",
    "CHANNELS",
    "Part",
    "Multiset",
    "parse",
    "encode",
]

#: Confirmed twice: 32 name slots in the dump, and Korg's two Multiset
#: Select messages, one for 0-15 and an SR-expanded one for 16-31.
MULTISETS = 32

MULTISET_SIZE = 86
CHANNELS = 16


#: Settings first, then one four-byte part per MIDI channel.
PART_BASE = 22
PART_SIZE = 4

NAME_BASE = MULTISETS * MULTISET_SIZE  # 2752
NAME_SIZE = 16


@dataclass(frozen=True)
class Part:
    """One MIDI channel's assignment within a Multiset."""

    channel: int  #: 1-16, as a person counts them
    bank: int  #: the performance's bank
    performance: int  #: 0-49 within that bank
    level: int  #: 0-127
    flag: int  #: unidentified; 1 on channels 1-15, 0 on 16

    @property
    def used(self) -> bool:
        return self.performance < 50


@dataclass(frozen=True)
class Multiset:
    number: int
    name: str
    parts: tuple[Part, ...]
    #: Byte 0 of the 22-byte head, kept raw. It is **not** the FX control
    #: channel: the panel's FXControlChan is global parameter 29, measured
    #: on the bench 2026-11-13 (the unit echoes "+1".."+16"), and changing
    #: it does not alter these records. Byte 0 is part of the copied FX
    #: head; its meaning is still open.
    header_byte: int = 0
    #: The Multiset's own 21-byte FX block.
    fx: FxBlock = field(default_factory=FxBlock.empty)
    #: The FX control channel, stored in the **last byte** of the record
    #: (offset 85) as channel - 1. Measured on the bench 2026-11-13:
    #: MULTI:2 set to 7 stored 6, set to 16 stored 15. The 16th part's
    #: fourth byte is this field, not a flag. KB's stored value is still
    #: to be measured; the raw byte is preserved.
    control_channel: int = 0

    def __post_init__(self) -> None:
        if not (0 <= self.header_byte <= 127):
            raise ValueError(
                f"header byte must be a 7-bit byte, not {self.header_byte}"
            )
        if not (0 <= self.control_channel <= 127):
            raise ValueError(
                f"control channel must be a 7-bit byte, not {self.control_channel}"
            )

    @property
    def display(self) -> str:
        return f"{self.number:02d} {self.name}" if self.name else f"{self.number:02d}"


#: The stored control byte for **KB** (the keyboard), measured 2026-11-13
#: after setting MULTI:2 to KB and storing it. Channels 1-16 store 0-15.
CONTROL_CHANNEL_KB = 16


def control_channel_text(raw: int) -> str:
    """The panel's name for a stored FX control byte."""
    if raw == CONTROL_CHANNEL_KB:
        return "KB"
    if 0 <= raw < 16:
        return str(raw + 1)
    return f"{raw} (raw)"


def _name(raw: Sequence[int]) -> str:
    out = []
    for byte in raw:
        if byte == 0:
            break
        # 0x7F is a space; Korg's appendix says so for parameter change
        # messages and the stored names use it the same way.
        out.append(" " if byte == 0x7F else chr(byte) if 32 <= byte <= 126 else " ")
    return "".join(out).rstrip()


def parse(data: Sequence[int]) -> List[Multiset]:
    """Decode the data of a ``0x62`` dump into 32 Multisets.

    Refuses data too short to hold them rather than returning a plausible
    few: a half-read dump that yields eight Multisets and stops is the kind
    of output that looks like an answer.
    """
    needed = NAME_BASE + MULTISETS * NAME_SIZE
    if len(data) < needed:
        raise ValueError(
            f"a Multiset dump decodes to at least {needed} bytes; got {len(data)}"
        )
    out = []
    for number in range(MULTISETS):
        base = number * MULTISET_SIZE
        parts = []
        for channel in range(CHANNELS):
            at = base + PART_BASE + channel * PART_SIZE
            parts.append(
                Part(
                    channel=channel + 1,
                    bank=data[at],
                    performance=data[at + 1],
                    level=data[at + 2],
                    # The 16th part's fourth byte is the Multiset's FX
                    # control channel, not a flag.
                    flag=data[at + 3] if channel < CHANNELS - 1 else 0,
                )
            )
        at = NAME_BASE + number * NAME_SIZE
        out.append(
            Multiset(
                number=number,
                name=_name(data[at : at + NAME_SIZE]),
                parts=tuple(parts),
                header_byte=data[base],
                fx=FxBlock.from_bytes(bytes(data[base + 1 : base + 22])),
                control_channel=int(data[base + MULTISET_SIZE - 1]),
            )
        )
    return out


def encode(multisets: Sequence[Multiset]) -> bytes:  # noqa: C901
    """Encode 32 Multisets back into the plain data of a 0x62 dump.

    Byte 0 is preserved raw, bytes 1-21 are the FX block, and the last
    byte is the FX control channel (channel - 1); the 16th part's flag
    slot is that control channel. (The first :data:`PART_BASE` bytes of
    each Multiset's record are otherwise preserved from the parsed dump; see
    :attr:`Multiset.header_byte`); the four bytes of each of the 16 channels
    and the name slot are written from the Multiset. Round-trips through
    :func:`parse` byte-identically when nothing is edited.
    """
    needed = NAME_BASE + MULTISETS * NAME_SIZE
    data = bytearray(needed)
    by_number = {entry.number: entry for entry in multisets}
    for number in range(MULTISETS):
        entry = by_number.get(number)
        if entry is None:
            continue
        base = number * MULTISET_SIZE
        data[base] = entry.header_byte & 0x7F
        fx_bytes = entry.fx.to_bytes()
        data[base + 1 : base + 22] = fx_bytes
        for part in entry.parts:
            at = base + PART_BASE + (part.channel - 1) * PART_SIZE
            data[at] = part.bank & 0x7F
            data[at + 1] = part.performance & 0x7F
            data[at + 2] = part.level & 0x7F
            if part.channel < CHANNELS:
                data[at + 3] = part.flag & 0x7F
        data[base + MULTISET_SIZE - 1] = entry.control_channel & 0x7F
        at = NAME_BASE + number * NAME_SIZE
        encoded_name = []
        for char in entry.name:
            code = ord(char)
            if char == " ":
                encoded_name.append(0x7F)
            elif 32 <= code <= 126:
                encoded_name.append(code)
            else:
                # Stop at the terminator: no bytes are written after the NUL.
                break
            if len(encoded_name) >= NAME_SIZE:
                break
        # Pad the remainder with NUL.
        while len(encoded_name) < NAME_SIZE:
            encoded_name.append(0)
        for index, byte in enumerate(encoded_name[:NAME_SIZE]):
            data[at + index] = byte
    return bytes(data)
