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

"""Container and patch decoder for SoundDiver ``.LIB`` files.

The IFF container, the ``LENT`` blocks and the Wavestation **patch**
data block are decoded.  A patch entry carries 410 bytes: a two-byte
split-off piece of the common section, the other 70 common bytes, the
remaining two common bytes, and four 84-byte oscillator blocks whose
bytes are stored in the adaptation's order.  The oscillator order below
was derived by matching 42 real library patches against bank records;
the few positions that are constant in every example decode as zero,
which is what the adaptation itself stores there.

A performance entry is 158 bytes, but it is SoundDiver's normalized
representation of the part values, not the raw 181-byte performance
record (the values do not even share a byte multiset), so performances
are listed but not converted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Tuple, cast

from kws import messages as m
from kws import records as rec

__all__ = [
    "LibBank",
    "LibEntry",
    "SoundDiverError",
    "decode_patch",
    "read_banks",
    "read_entries",
]

#: raw oscillator byte -> position inside the 84-byte block in the LIB.
#: Derived from 42 real patch pairs; the four blocks all use this order.
_OSC_PERMUTATION: Tuple[int, ...] = (
    1,
    2,
    4,
    5,
    6,
    3,
    8,
    9,
    11,
    12,
    10,
    17,
    18,
    19,
    20,
    21,
    22,
    13,
    25,
    23,
    30,
    31,
    32,
    33,
    35,
    36,
    37,
    38,
    39,
    40,
    41,
    42,
    43,
    45,
    65,
    66,
    67,
    68,
    69,
    70,
    71,
    72,
    47,
    56,
    64,
    80,
    34,
    48,
    52,
    53,
    54,
    55,
    58,
    60,
    61,
    62,
    63,
    73,
    78,
    79,
    74,
    75,
    76,
    77,
    82,
    81,
    57,
    59,
    44,
    24,
    49,
    50,
    51,
    0,
    26,
    14,
    15,
    16,
    46,
    27,
    28,
    29,
    7,
    83,
)

_PATCH_DATA_SIZE = 410
_PERFORMANCE_DATA_SIZE = 158
_SLOT_COMMENT = re.compile(r"^(RAM[123]|CARD)[-/](\d{1,2})$")


class SoundDiverError(ValueError):
    """A ``.LIB`` object this module cannot turn into a bank record."""


@dataclass(frozen=True)
class LibBank:
    """The decodable patches of one ``RAM1``/``RAM2``/``RAM3`` bank."""

    scope: str
    patches: List[bytes]
    note: str


@dataclass(frozen=True)
class LibEntry:
    index: int
    name: str
    location: str
    comment: str
    header: bytes
    data: bytes
    uses: bytes


def _decode_block_text(block: bytes) -> str:
    return block.decode("latin-1").replace("\x00", "")


def _apply_block(block_id: int, block_data: bytes, state: dict[str, object]) -> None:
    if block_id == 1:
        state["name"] = _decode_block_text(block_data)
    elif block_id == 2:
        state["location"] = _decode_block_text(block_data)
    elif block_id == 3:
        state["data"] = block_data
    elif block_id == 4:
        state["uses"] = block_data
    elif block_id == 6:
        state["comment"] = _decode_block_text(block_data)


def _parse_tnel(body: bytes, index: int) -> LibEntry:
    if len(body) < 12:
        raise ValueError("truncated TNEL body in .lib file")
    header = body[:12]
    rest = body[12:]

    state: dict[str, object] = {
        "name": "",
        "location": "",
        "comment": "",
        "data": b"",
        "uses": b"",
    }

    i = 0
    n = len(rest)
    while i < n:
        if i >= n:
            break
        block_id = rest[i]
        i += 1
        if block_id == 0:
            # End of data. The 1-2 bytes that may follow are not a length.
            break
        if i >= n:
            raise ValueError("truncated block header in .lib file")
        l1 = rest[i]
        i += 1
        if l1 & 0x80:
            if i >= n:
                raise ValueError("truncated block length in .lib file")
            l2 = rest[i]
            i += 1
            length = ((l1 & 0x7F) << 7) | l2
        else:
            length = l1
        if i + length > n:
            raise ValueError("block overflow in .lib file")
        block_data = rest[i : i + length]
        i += length
        _apply_block(block_id, block_data, state)

    return LibEntry(
        index=index,
        name=str(state["name"]),
        location=str(state["location"]),
        comment=str(state["comment"]),
        header=header,
        data=cast(bytes, state["data"]),
        uses=cast(bytes, state["uses"]),
    )


def read_entries(path: str) -> List[LibEntry]:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise ValueError(f"cannot read .lib file: {exc}") from exc

    if len(data) < 12:
        raise ValueError("not a valid .lib file: truncated header")
    if data[:4] != b"MROF":
        raise ValueError("not a valid .lib file: missing MROF container")

    size = int.from_bytes(data[4:8], "little")
    if size < 4 or 8 + size > len(data):
        raise ValueError("truncated MROF container in .lib file")
    if data[8:12] != b"BLSS":
        raise ValueError("not a valid .lib file: bad form type")

    bound = 8 + size
    entries: List[LibEntry] = []
    index = 0
    pos = 12
    while pos < bound:
        if pos + 8 > bound:
            raise ValueError("truncated chunk header in .lib file")
        chunk_id = data[pos : pos + 4]
        chunk_size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        content_start = pos + 8
        content_end = content_start + chunk_size
        if content_end > bound or content_end > len(data):
            raise ValueError("chunk overflow in .lib file")

        body = data[content_start:content_end]
        if chunk_id == b"TNEL":
            entries.append(_parse_tnel(body, index))
            index += 1

        pos = content_end

    return entries


def decode_patch(entry: LibEntry) -> bytes:
    """Turn one 410-byte library patch into a 426-byte bank patch."""
    if len(entry.data) != _PATCH_DATA_SIZE:
        raise SoundDiverError(
            f"a SoundDiver patch entry carries {_PATCH_DATA_SIZE} bytes; "
            f"this one has {len(entry.data)}"
        )
    data = entry.data
    record = bytearray(rec.PATCH_SIZE)
    record[:16] = bytes(m.encode_name(entry.name))
    record[16:86] = data[2:72]
    record[86:88] = data[0:2]
    record[88:90] = data[72:74]
    for osc in range(4):
        lib_base = 74 + osc * 84
        raw_base = rec.OSC_BASE + osc * rec.OSC_SIZE
        for raw_offset, lib_offset in enumerate(_OSC_PERMUTATION):
            record[raw_base + raw_offset] = data[lib_base + lib_offset]
    return bytes(record)


def _decode_patch_banks(
    entries: List[LibEntry],
) -> Tuple[Dict[str, Dict[int, bytes]], int]:
    banks: Dict[str, Dict[int, bytes]] = {}
    duplicates = 0
    for entry in entries:
        if len(entry.data) != _PATCH_DATA_SIZE:
            continue
        match = _SLOT_COMMENT.match(entry.comment.strip())
        if match is None:
            continue
        scope = match.group(1)
        slot = int(match.group(2))
        if not 0 <= slot < 35:
            continue
        slots = banks.setdefault(scope, {})
        if slot in slots:
            duplicates += 1
        slots[slot] = decode_patch(entry)
    return banks, duplicates


def read_banks(path: str) -> List[LibBank]:
    """Decode a ``.LIB``'s patches into per-bank slot lists.

    Only the patches are converted.  The performance entries are counted
    in the bank's note so the caller can say what stayed behind.
    """
    entries = read_entries(path)
    banks, duplicates = _decode_patch_banks(entries)
    performances = sum(
        1 for entry in entries if len(entry.data) == _PERFORMANCE_DATA_SIZE
    )
    if not banks:
        raise SoundDiverError(
            "no decodable Wavestation patches in this .LIB; performances use "
            "SoundDiver's normalized part values and are not converted yet"
        )
    notes: List[str] = []
    if performances:
        notes.append(
            f"{performances} performance entr"
            f"{'y was' if performances == 1 else 'ies were'} not decoded"
        )
    if duplicates:
        notes.append(f"{duplicates} duplicate slot(s) kept the last entry")
    note = "; ".join(notes)
    result: List[LibBank] = []
    for scope, slots in sorted(banks.items()):
        patches = [slots.get(number, bytes(rec.PATCH_SIZE)) for number in range(35)]
        result.append(LibBank(scope=scope, patches=patches, note=note))
    return result
