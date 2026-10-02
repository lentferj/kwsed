# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# The record sizes were derived from System Exclusive dumps the instrument
# produced; the frame sizes agree with those Edisyn's Wavestation support
# expects (Apache-2.0). No Edisyn code is used here.
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

"""Reading the Wavestation's bulk dumps.

A performance bank is **50 records of 181 bytes**; a patch bank is **35 of
426**. Each record begins with its name.

Those sizes were derived, and the derivation is the evidence:

* Taking the nibble data from offset 6, a performance bulk dump yields 9050
  bytes and a patch bulk dump 14910.
* ``9050 / 50 = 181`` and ``14910 / 35 = 426``, both exact, nothing left
  over.
* Every record then starts with a legible name -- all 35 patch names in a
  bank, and all 50 performance names.
* A single-performance dump (function ``0x49``, 371 bytes on the wire)
  independently yields 181 bytes from offset 7, agreeing with the bulk
  dump's division.

**Nothing past the name is decoded.** The manuals to hand are scans with no
text layer, so the parameter layout has not been read. kwsed browses and
does not edit, so it does not need it -- see TODO.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from kws import banks
from kws import messages as m

__all__ = [
    "PERFORMANCE_RECORD",
    "PATCH_RECORD",
    "BulkDump",
    "parse_bulk",
    "names_from_dump",
    "parse_performance",
    "record_name",
    "read_syx_file",
]

#: Bytes per performance in a bank dump.
PERFORMANCE_RECORD = 181

#: Bytes per patch in a bank dump.
PATCH_RECORD = 426

_LAYOUT = {
    m.Function.ALL_PERFORMANCE_DUMP: (
        banks.Kind.PERFORMANCE,
        PERFORMANCE_RECORD,
        banks.PERFORMANCES_PER_BANK,
    ),
    m.Function.ALL_PATCH_DUMP: (banks.Kind.PATCH, PATCH_RECORD, banks.PATCHES_PER_BANK),
}


def record_name(record: Sequence[int]) -> str:
    """The name at the start of a record.

    Performances store 15 characters and a NUL, patches 16 space-padded.
    :func:`kws.messages.decode_name` handles both, so callers do not have to
    know which they are holding.
    """
    return m.decode_name(record)


@dataclass(frozen=True)
class BulkDump:
    """One decoded bank dump."""

    function: int
    bank: banks.Bank
    kind: str
    record_size: int
    records: Tuple[bytes, ...]

    @property
    def count(self) -> int:
        return len(self.records)

    def name(self, index: int) -> str:
        return record_name(self.records[index])

    def names(self) -> Dict[int, str]:
        """``{number: name}``, omitting records with no name.

        Omitted rather than stored as an empty string: "called nothing" and
        "not read" are different claims and only the second would be true.
        """
        out: Dict[int, str] = {}
        for index in range(self.count):
            name = self.name(index)
            if name:
                out[index] = name
        return out


def parse_bulk(packet: m.Packet) -> BulkDump:
    """Decode an all-performance or all-patch dump.

    Refuses a dump that does not divide exactly into records. The record
    size was derived rather than documented, so a unit or model using
    another would otherwise be sliced into plausible nonsense, names and
    all -- and a name is exactly the kind of output that looks like success.
    """
    if packet.function not in _LAYOUT:
        raise m.ProtocolError(f"{packet.function_name} is not a bank dump kwsed reads")
    kind, size, expected = _LAYOUT[packet.function]

    number = packet.bank
    if number is None:
        raise m.ProtocolError(f"{packet.function_name} carries no bank byte")
    # The dump's bank byte is a wire byte, not a display
    # position: byte 3 is the Card, not ROM 4.
    entry = banks.by_sysex(number)

    data = packet.data()
    if len(data) % size:
        raise m.ProtocolError(
            f"{packet.function_name} decoded to {len(data)} bytes, which is "
            f"not a whole number of {size}-byte records "
            f"({len(data) % size} left over). kwsed derived that size from "
            f"dumps it had; this is refused rather than decoded into "
            f"nonsense."
        )
    records = tuple(
        data[index * size : (index + 1) * size] for index in range(len(data) // size)
    )
    if len(records) != expected:
        raise m.ProtocolError(
            f"{packet.function_name} holds {len(records)} records; a bank "
            f"has {expected} {kind}s"
        )
    return BulkDump(
        function=packet.function,
        bank=entry,
        kind=kind,
        record_size=size,
        records=records,
    )


def names_from_dump(packet: m.Packet) -> Tuple[banks.Bank, str, Dict[int, str]]:
    """``(bank, kind, {number: name})`` from a bank dump."""
    dump = parse_bulk(packet)
    return dump.bank, dump.kind, dump.names()


def parse_performance(packet: m.Packet) -> bytes:
    """One performance (function ``0x49``) as its 181 raw bytes.

    Note the offset differs from the bulk dumps': this message carries a
    number as well as a bank, so its data begins one byte later. That is
    handled by :data:`kws.messages.DATA_OFFSET` rather than here, so there
    is one place that knows it.
    """
    if packet.function != m.Function.PERFORMANCE_DUMP:
        raise m.ProtocolError(f"{packet.function_name} is not a performance dump")
    data = packet.data()
    if len(data) < PERFORMANCE_RECORD:
        raise m.ProtocolError(
            f"performance dump decoded to {len(data)} bytes, fewer than the "
            f"{PERFORMANCE_RECORD} a performance occupies"
        )
    return data[:PERFORMANCE_RECORD]


def read_syx_file(path: str) -> List[m.Packet]:
    """Every Wavestation message in a ``.syx`` file, in order.

    Foreign messages are skipped rather than raising: a file somebody
    assembled may carry other devices' data.
    """
    with open(path, "rb") as handle:
        raw = handle.read()
    out: List[m.Packet] = []
    cursor = 0
    while True:
        start = raw.find(b"\xf0", cursor)
        if start < 0:
            break
        end = raw.find(b"\xf7", start)
        if end < 0:
            break
        packet = m.parse(raw[start : end + 1])
        if packet is not None:
            out.append(packet)
        cursor = end + 1
    return out
