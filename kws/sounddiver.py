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

"""Container, patch and performance decoder for SoundDiver ``.LIB`` files.

The IFF container, the ``LENT`` blocks, the Wavestation **patch** and
**performance** data blocks are decoded.

A patch entry carries ``74 + 84 * n`` bytes, where ``n`` is 1, 2 or 4
oscillators (158, 242 or 410 bytes).  The common section is split around
a two-byte piece, and each oscillator block is stored in the adaptation's
byte order.  The order below was derived from real library/source pairs;
the few positions that are constant zero in every example decode as zero,
which is what the adaptation itself stores there.  A 2-oscillator patch's
two stored blocks are raw oscillators 0 and 2.

A performance entry carries ``37 + 16 * n`` bytes (53..165), where ``n``
is the number of used parts.  The first 21 bytes are the performance
common in a fixed byte order, then the ``n`` used parts as full 18-byte
raw part records, then ``8 - n`` two-byte placeholders for the unused
parts (their bank/patch).  The remaining 16 bytes of an unused part are
not stored; they decode as zero, which is safe because the part's patch
is unused.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple, cast

from kws import messages as m
from kws import records as rec
from kws.exceptions import SoundDiverError
from kws.types import LibBank, LibEntry

__all__ = [
    "LibBank",
    "LibEntry",
    "SoundDiverError",
    "decode_patch",
    "decode_performance",
    "read_banks",
    "read_entries",
    "build_waveseq",
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

#: data length -> number of stored oscillator blocks.
_PATCH_OSCILLATORS: Dict[int, int] = {158: 1, 242: 2, 410: 4}

#: raw oscillator slots used by a patch with 1, 2 or 4 oscillators.
_OSC_SLOTS: Dict[int, Tuple[int, ...]] = {
    1: (0,),
    2: (0, 2),
    4: (0, 1, 2, 3),
}

#: data length -> number of used parts, for a performance entry.
_PERFORMANCE_PARTS: Dict[int, int] = {37 + 16 * n: n for n in range(1, 9)}

_SLOT_COMMENT = re.compile(r"^(RAM[123]|CARD)[-/](\d{1,2})$")


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
    """Turn a 1-, 2- or 4-oscillator library patch into a bank patch."""
    count = _PATCH_OSCILLATORS.get(len(entry.data))
    if count is None:
        raise SoundDiverError(
            "a SoundDiver patch entry carries 158, 242 or 410 bytes; "
            f"this one has {len(entry.data)}"
        )
    data = entry.data
    record = bytearray(rec.PATCH_SIZE)
    record[:16] = bytes(m.encode_name(entry.name))
    record[16:86] = data[2:72]
    record[86:88] = data[0:2]
    record[88:90] = data[72:74]
    for stored, raw_osc in enumerate(_OSC_SLOTS[count]):
        lib_base = 74 + stored * rec.OSC_SIZE
        raw_base = rec.OSC_BASE + raw_osc * rec.OSC_SIZE
        for raw_offset, lib_offset in enumerate(_OSC_PERMUTATION):
            record[raw_base + raw_offset] = data[lib_base + lib_offset]
    return bytes(record)


def _decode_performance_common(data: bytes) -> bytes:
    common = bytearray(21)
    common[0] = data[3]
    common[1] = data[12]
    common[2] = data[0]
    common[3] = data[1]
    common[4] = data[2]
    common[5] = data[4]
    common[6:13] = data[5:12]
    common[13:21] = data[13:21]
    return bytes(common)


def decode_performance(entry: LibEntry) -> bytes:
    """Turn a 53..165-byte library performance into a bank performance."""
    count = _PERFORMANCE_PARTS.get(len(entry.data))
    if count is None:
        raise SoundDiverError(
            "a SoundDiver performance entry carries 37 + 16*n bytes "
            f"(53..165); this one has {len(entry.data)}"
        )
    data = entry.data
    record = bytearray(rec.PERFORMANCE_SIZE)
    name = entry.name.encode("latin-1", "replace")[:15]
    record[: len(name)] = name
    record[16:37] = _decode_performance_common(data)
    pos = 21
    for part in range(rec.PARTS_PER_PERFORMANCE):
        at = rec.PART_BASE + part * rec.PART_SIZE
        if part < count:
            record[at : at + rec.PART_SIZE] = data[pos : pos + rec.PART_SIZE]
            pos += rec.PART_SIZE
        else:
            record[at : at + 2] = data[pos : pos + 2]
            pos += 2
    if pos != len(data):
        raise SoundDiverError("performance entry length and part count disagree")
    return bytes(record)


def _decode_banks(
    entries: List[LibEntry],
) -> Tuple[
    Dict[str, Dict[int, bytes]],
    Dict[str, Dict[int, bytes]],
    int,
    int,
]:
    patches: Dict[str, Dict[int, bytes]] = {}
    performances: Dict[str, Dict[int, bytes]] = {}
    duplicate_patches = 0
    duplicate_performances = 0
    for entry in entries:
        match = _SLOT_COMMENT.match(entry.comment.strip())
        if match is None:
            continue
        scope = match.group(1)
        slot = int(match.group(2))
        if len(entry.data) in _PATCH_OSCILLATORS:
            if not 0 <= slot < 35:
                continue
            slots = patches.setdefault(scope, {})
            if slot in slots:
                duplicate_patches += 1
            slots[slot] = decode_patch(entry)
        elif len(entry.data) in _PERFORMANCE_PARTS:
            if not 0 <= slot < 50:
                continue
            slots = performances.setdefault(scope, {})
            if slot in slots:
                duplicate_performances += 1
            slots[slot] = decode_performance(entry)
    return patches, performances, duplicate_patches, duplicate_performances


def _is_sequence_entry_data(data: bytes) -> bool:
    return len(data) >= 32 and len(data) % 16 == 0


def _decode_sequence_slots(
    entries: List[LibEntry],
) -> Tuple[Dict[str, Dict[int, LibEntry]], int]:
    """Select the last wave-sequence entry per slot, per scope."""
    sequence_slots: Dict[str, Dict[int, LibEntry]] = {}
    duplicates = 0
    for entry in entries:
        match = _SLOT_COMMENT.match(entry.comment.strip())
        if match is None:
            continue
        if not _is_sequence_entry_data(entry.data):
            continue
        scope = match.group(1)
        slot = int(match.group(2))
        if not 0 <= slot <= 31:
            continue
        slots = sequence_slots.setdefault(scope, {})
        if slot in slots:
            duplicates += 1
        slots[slot] = entry
    return sequence_slots, duplicates


def read_banks(path: str) -> List[LibBank]:
    """Decode a ``.LIB``'s patches, performances and wave sequences."""
    entries = read_entries(path)
    patches, performances, dup_patches, dup_performances = _decode_banks(entries)

    sequence_slots, dup_sequences = _decode_sequence_slots(entries)

    seq_scopes = set(sequence_slots)
    scopes = sorted(set(patches) | set(performances) | seq_scopes)
    if not scopes:
        raise SoundDiverError(
            "no decodable Wavestation patches, performances or wave sequences "
            "in this .LIB"
        )
    patch_count = sum(len(slots) for slots in patches.values())
    performance_count = sum(len(slots) for slots in performances.values())
    seq_count = sum(len(slots) for slots in sequence_slots.values())
    seq_steps = sum(
        (len(entry.data) - 16) // 16
        for slots in sequence_slots.values()
        for entry in slots.values()
    )
    recognised = set(_PATCH_OSCILLATORS) | set(_PERFORMANCE_PARTS)
    other = sum(
        1
        for entry in entries
        if entry.data
        and len(entry.data) not in recognised
        and not _is_sequence_entry_data(entry.data)
    )
    notes: List[str] = [
        f"{patch_count} patch{'' if patch_count == 1 else 'es'} and "
        f"{performance_count} performance"
        f"{'' if performance_count == 1 else 's'} decoded"
    ]
    if seq_count:
        notes.append(
            f"{seq_count} wave sequence{'s' if seq_count != 1 else ''} "
            f"({seq_steps} step{'s' if seq_steps != 1 else ''})"
        )
    duplicates = dup_patches + dup_performances + dup_sequences
    if duplicates:
        notes.append(f"{duplicates} duplicate slot(s) kept the last entry")
    if other:
        notes.append(
            f"{other} other entr{'y' if other == 1 else 'ies'} "
            "(multisets, ...) not converted"
        )
    note = "; ".join(notes)
    result: List[LibBank] = []
    for scope in scopes:
        patch_slots = patches.get(scope, {})
        performance_slots = performances.get(scope, {})
        result.append(
            LibBank(
                scope=scope,
                performances=[
                    performance_slots.get(number, bytes(rec.PERFORMANCE_SIZE))
                    for number in range(50)
                ],
                patches=[
                    patch_slots.get(number, bytes(rec.PATCH_SIZE))
                    for number in range(35)
                ],
                waveseq=build_waveseq(entries, scope),
                note=note,
            )
        )
    return result


def _decode_sequence_header(entry: LibEntry) -> Tuple[bytes, List[bytes]]:
    """Split a wave-sequence entry into its 16-byte header and steps."""
    data = entry.data
    n = (len(data) - 16) // 16
    if len(data) != 16 + 16 * n or n < 1:
        raise SoundDiverError(
            "a SoundDiver wave-sequence entry carries 16 + 16*n bytes; "
            f"this one has {len(data)}"
        )
    header = data[:16]
    steps = [data[16 + i * 16 : 16 + (i + 1) * 16] for i in range(n)]
    return header, steps


def build_waveseq(entries: List[LibEntry], scope: str = "RAM1") -> Optional[bytes]:
    """Assemble a wave-sequence bank from ``.LIB`` entries."""
    pattern = re.compile(rf"^{re.escape(scope)}-(\d{{1,2}})$")
    selected: Dict[int, LibEntry] = {}
    for entry in entries:
        match = pattern.match(entry.comment.strip())
        if match is None:
            continue
        if len(entry.data) < 32 or len(entry.data) % 16 != 0:
            continue
        slot = int(match.group(1))
        if not 0 <= slot <= 31:
            continue
        # keep the last entry per slot
        selected[slot] = entry

    if not selected:
        return None

    bank = bytearray(rec.WAVESEQ_BANK_SIZE)
    cursor = 1
    for slot in sorted(selected):
        entry = selected[slot]
        header, steps = _decode_sequence_header(entry)
        n = len(steps)
        if cursor + n > rec.STEPS_PER_BANK:
            raise SoundDiverError(
                "wave-sequence step budget exceeded: "
                f"need {cursor + n}, have {rec.STEPS_PER_BANK}"
            )
        b = cursor
        # header
        at_header = rec.SEQ_BASE + slot * rec.SEQ_SIZE
        rec._put_u16(bank, at_header, b)
        rec._put_u16(bank, at_header + 2, b)
        bank[at_header + 4 : at_header + 16] = header[4:16]

        for i, step_bytes in enumerate(steps):
            f = (step_bytes[0] << 8) | step_bytes[1]
            bb = (step_bytes[2] << 8) | step_bytes[3]
            bank_step_pos = rec.STEP_BASE + (b + i) * rec.STEP_SIZE
            rec._put_u16(bank, bank_step_pos, b + f if f else 0)
            rec._put_u16(bank, bank_step_pos + 2, b + bb - 1 if bb else 0)
            bank[bank_step_pos + 4 : bank_step_pos + 6] = step_bytes[4:6]
            bank[bank_step_pos + 6 : bank_step_pos + 16] = step_bytes[6:16]
        at_name = rec.SEQNAME_BASE + slot * rec.SEQNAME_SIZE
        bank[at_name : at_name + rec.SEQNAME_SIZE] = bytes(
            m.encode_name(entry.name, length=8)
        )

        cursor += n

    return bytes(bank)
