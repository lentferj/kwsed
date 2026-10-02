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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

__all__ = [
    "MULTISETS",
    "MULTISET_SIZE",
    "CHANNELS",
    "Part",
    "Multiset",
    "parse",
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

    @property
    def display(self) -> str:
        return f"{self.number:02d} {self.name}" if self.name else f"{self.number:02d}"


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
                    flag=data[at + 3],
                )
            )
        at = NAME_BASE + number * NAME_SIZE
        out.append(
            Multiset(
                number=number, name=_name(data[at : at + NAME_SIZE]), parts=tuple(parts)
            )
        )
    return out
