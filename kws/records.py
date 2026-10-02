# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# The record layouts here are interface facts. They were derived from dumps
# the instrument produced and cross-checked field by field against Edisyn's
# Wavestation support (Apache-2.0), which reads and writes the same records.
# No Edisyn code is used or derived; see LICENSE and docs/RESOLUTION_NOTES.md.
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

"""Inside the records: the fields that point at other records.

kwsed browses names, and for that the interior of a record does not matter.
Building a new bank out of pieces of old ones is the opposite: it is almost
entirely about the interior, because every reference has to be rewritten as
things move. This module decodes and rewrites exactly those references and
leaves everything else alone.

The three layouts, each of which divides its record exactly -- which is the
check that they are right, since a wrong split leaves a remainder:

**Performance, 181 bytes** ``16 name + 21 effects + 8 parts x 18``.

**Patch, 426 bytes** ``16 name + 74 settings + 4 oscillators x 84``.

**Wave sequence bank, 8784 bytes** ``32 headers x 16 + 501 steps x 16 +
32 names x 8``.

Two traps, both of which produce plausible wrong answers rather than errors:

**A part's bank number is split across two fields.** Four of its bits live
in ``bankNum`` and the top two bits of ``partMode`` carry the rest, so the
bank is ``bankNum + ((partMode >> 6) & 3) * 4``. Rewriting only ``bankNum``
yields a bank that loads cleanly and plays sounds from somewhere else.

**16-bit values are big-endian, while the nibble encoding on the wire is
low-nibble-first.** Reading a word the other way around gives numbers that
are all multiples of 256 and look like data. That mistake is what made an
early reading of this file's own author claim that 98% of patches use a wave
sequence; the real figure is about a fifth.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Sequence

__all__ = [
    "PERFORMANCE_SIZE",
    "PATCH_SIZE",
    "WAVESEQ_BANK_SIZE",
    "PARTS_PER_PERFORMANCE",
    "OSCILLATORS_PER_PATCH",
    "SEQUENCES_PER_BANK",
    "STEPS_PER_BANK",
    "PartRef",
    "WaveRef",
    "performance_parts",
    "set_performance_part",
    "patch_waves",
    "set_patch_wave",
    "sequence_names",
    "sequence_header",
    "step",
    "set_step_links",
    "set_step_wave",
    "ROM_WAVE_BASE",
    "UNUSED_PATCH",
]

# --- sizes -------------------------------------------------------------------

PERFORMANCE_SIZE = 181
PATCH_SIZE = 426
WAVESEQ_BANK_SIZE = 8784

PARTS_PER_PERFORMANCE = 8
OSCILLATORS_PER_PATCH = 4
SEQUENCES_PER_BANK = 32
STEPS_PER_BANK = 501

#: Performance: 16 name + 21 effects, then the parts.
PART_BASE = 37
PART_SIZE = 18

#: Patch: 16 name + 74 settings, then the oscillators.
OSC_BASE = 90
OSC_SIZE = 84

#: Wave sequence bank.
SEQ_BASE = 0
SEQ_SIZE = 16
STEP_BASE = SEQUENCES_PER_BANK * SEQ_SIZE  # 512
STEP_SIZE = 16
SEQNAME_BASE = STEP_BASE + STEPS_PER_BANK * STEP_SIZE  # 8528
SEQNAME_SIZE = 8

#: A wave number below this is a **wave sequence** in the oscillator's own
#: wave bank; at or above it, a wave in PCM ROM. The same rule applies to a
#: step's wave number, which is why a sequence can quote another sequence.
ROM_WAVE_BASE = 32

#: A part with this patch number is switched off. Stored as -1, which is
#: 255 once it is read as an unsigned byte.
UNUSED_PATCH = 255


def _u16(data: Sequence[int], at: int) -> int:
    """A 16-bit field. Big-endian -- see the module docstring."""
    return (data[at] << 8) | data[at + 1]


def _put_u16(data: bytearray, at: int, value: int) -> None:
    data[at] = (value >> 8) & 0xFF
    data[at + 1] = value & 0xFF


def _s8(value: int) -> int:
    return value - 256 if value > 127 else value


# --- performance -> patch ----------------------------------------------------


@dataclass(frozen=True)
class PartRef:
    """One of a performance's eight parts, and what it points at."""

    index: int
    bank: int
    patch: int

    @property
    def used(self) -> bool:
        return self.patch != UNUSED_PATCH and self.patch < 35


def performance_parts(record: Sequence[int]) -> List[PartRef]:
    """The eight parts of a performance, used or not."""
    if len(record) < PERFORMANCE_SIZE:
        raise ValueError(
            f"a performance is {PERFORMANCE_SIZE} bytes; got {len(record)}"
        )
    out = []
    for i in range(PARTS_PER_PERFORMANCE):
        at = PART_BASE + i * PART_SIZE
        low = record[at]
        mode = record[at + 4]
        out.append(
            PartRef(index=i, bank=low + ((mode >> 6) & 3) * 4, patch=record[at + 1])
        )
    return out


def set_performance_part(
    record: bytearray, index: int, *, bank: int, patch: int
) -> None:
    """Point one part at ``patch`` in ``bank``, both halves of the bank.

    The high two bits of the bank live in ``partMode`` alongside unrelated
    settings, so they are edited in place rather than written over.
    """
    if not 0 <= index < PARTS_PER_PERFORMANCE:
        raise ValueError(f"part {index} is outside 0-{PARTS_PER_PERFORMANCE - 1}")
    if not 0 <= bank <= 11:
        raise ValueError(f"bank {bank} is outside 0-11")
    at = PART_BASE + index * PART_SIZE
    record[at] = bank % 4
    record[at + 4] = (record[at + 4] & 0x3F) | ((bank // 4) << 6)
    record[at + 1] = patch & 0xFF


# --- patch -> wave / wave sequence -------------------------------------------


@dataclass(frozen=True)
class WaveRef:
    """One oscillator's wave, which is either ROM or a wave sequence."""

    index: int
    bank: int
    wave: int

    @property
    def is_sequence(self) -> bool:
        return 0 <= self.wave < ROM_WAVE_BASE


def patch_waves(record: Sequence[int]) -> List[WaveRef]:
    """The four oscillators' wave references."""
    if len(record) < PATCH_SIZE:
        raise ValueError(f"a patch is {PATCH_SIZE} bytes; got {len(record)}")
    out = []
    for i in range(OSCILLATORS_PER_PATCH):
        at = OSC_BASE + i * OSC_SIZE
        out.append(
            WaveRef(
                index=i,
                bank=record[at + 2],
                wave=_u16(record, at + 3) + _s8(record[at + 83]),
            )
        )
    return out


def set_patch_wave(record: bytearray, index: int, *, bank: int, wave: int) -> None:
    """Point one oscillator at ``wave`` in ``bank``.

    The split between ``waveNum`` and ``waveNumExp`` is collapsed: the whole
    value goes in the word and the extension is zeroed, which is what the
    instrument's own editor does for anything in range.
    """
    if not 0 <= index < OSCILLATORS_PER_PATCH:
        raise ValueError(f"oscillator {index} is outside 0-{OSCILLATORS_PER_PATCH - 1}")
    at = OSC_BASE + index * OSC_SIZE
    record[at + 2] = bank & 0xFF
    _put_u16(record, at + 3, wave & 0xFFFF)
    record[at + 83] = 0


# --- wave sequence bank ------------------------------------------------------


def sequence_names(bank: Sequence[int]) -> List[str]:
    """The 32 wave-sequence names of a wave-sequence bank."""
    if len(bank) < WAVESEQ_BANK_SIZE:
        raise ValueError(
            f"a wave sequence bank is {WAVESEQ_BANK_SIZE} bytes; got {len(bank)}"
        )
    out = []
    for i in range(SEQUENCES_PER_BANK):
        at = SEQNAME_BASE + i * SEQNAME_SIZE
        raw = bytes(bank[at : at + SEQNAME_SIZE])
        out.append("".join(chr(b) if 32 <= b <= 126 else " " for b in raw).rstrip())
    return out


def sequence_header(bank: Sequence[int], number: int) -> Dict[str, Any]:
    """One wave sequence's header, including the step it starts at."""
    if not 0 <= number < SEQUENCES_PER_BANK:
        raise ValueError(f"sequence {number} is outside 0-{SEQUENCES_PER_BANK - 1}")
    at = SEQ_BASE + number * SEQ_SIZE
    return {
        "link": _u16(bank, at),
        "start_link": _u16(bank, at + 2),
        "loop_start": bank[at + 4],
        "loop_end": bank[at + 5],
        "loop_count": bank[at + 6],
        "start_step": bank[at + 7],
    }


def step(bank: Sequence[int], index: int) -> Dict[str, Any]:
    """One step out of the bank's shared pool of 501."""
    if not 0 <= index < STEPS_PER_BANK:
        raise ValueError(f"step {index} is outside 0-{STEPS_PER_BANK - 1}")
    at = STEP_BASE + index * STEP_SIZE
    return {
        "forward": _u16(bank, at),
        "back": _u16(bank, at + 2),
        "loop": _u16(bank, at + 4),
        "wave": bank[at + 7],
        "duration": _u16(bank, at + 12),
        "level": bank[at + 14],
    }


def set_step_wave(bank: bytearray, index: int, wave: int) -> None:
    """Repoint one step at a different wave, or a different **sequence**.

    Below :data:`ROM_WAVE_BASE` this field names a wave sequence in the
    step's own bank rather than a wave in ROM, so it needs renumbering when
    steps are moved into a new pool for exactly the same reason the links
    do -- and unlike the links, it survives a naive copy looking perfectly
    valid, because any value at all is a legal sequence number.
    """
    if not 0 <= index < STEPS_PER_BANK:
        raise ValueError(f"step {index} is outside 0-{STEPS_PER_BANK - 1}")
    if not 0 <= wave <= 0xFF:
        raise ValueError(f"{wave} does not fit in the step's wave byte")
    bank[STEP_BASE + index * STEP_SIZE + 7] = wave


def set_step_links(
    bank: bytearray, index: int, *, forward: int, back: int, loop: int
) -> None:
    """Repoint one step. Used when steps are moved into a new pool."""
    if not 0 <= index < STEPS_PER_BANK:
        raise ValueError(f"step {index} is outside 0-{STEPS_PER_BANK - 1}")
    at = STEP_BASE + index * STEP_SIZE
    _put_u16(bank, at, forward)
    _put_u16(bank, at + 2, back)
    _put_u16(bank, at + 4, loop)
