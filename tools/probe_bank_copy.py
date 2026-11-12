#!/usr/bin/env python3
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
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.

"""Ask the unit's own MIDI/Global BANK COPY whether it remaps a bank.

kwsed's :func:`kws.copy_bank` rewrites every part that names the source
bank to the destination, because a copied bank would otherwise still play
its patches from where it came from. The panel function is documented but
has no SysEx request, so the only way to know whether *it* remaps is to
perform the copy by hand and read the destination back. This tool makes
that a three-command experiment:

    tools/probe_bank_copy.py prepare RAM2 --yes
    # on the unit: MIDI/GLOBAL -> BANK COPY, RAM 2 -> RAM 3, execute
    tools/probe_bank_copy.py check RAM2 RAM3

The probe bank has one performance whose part 0 names the source bank
itself and part 1 names ROM 4. A remap moves part 0 and leaves part 1
alone; a copy that does not remap leaves both as they were.

``check`` exits 0 when the panel remapped, 3 when it did not, and 4 when
the destination matches neither the probe nor its remap -- a difference
worth looking at before believing either answer. ``prepare`` writes only
with ``--yes``; ``check`` and ``show`` are read-only.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kws import backup, banks
from kws import messages as m
from kws import records as rec

__all__ = [
    "MOVE_TYPES",
    "bank_move_frames",
    "build_probe_bank",
    "classify",
    "part_table",
    "read_probe_bank",
]

PROBE_PERFORMANCE = "PROBE COPY"
PROBE_PATCH = "PROBE PATCH"
PROBE_SEQUENCE = "PROBESEQ"
PROBE_SLOT = 0
PROBE_PATCH_SLOT = 1
#: The probe's third-party reference: ROM 4, which must not move.
THIRD_PARTY_BANK = 3

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_REMAPPED = 3
EXIT_UNEXPECTED = 4

_BUNDLE_FUNCTIONS = (
    ("waveseq", m.Function.ALL_WAVESEQ_DUMP),
    ("patches", m.Function.ALL_PATCH_DUMP),
    ("performances", m.Function.ALL_PERFORMANCE_DUMP),
)

_SIZES = {
    "waveseq": rec.WAVESEQ_BANK_SIZE,
    "patches": banks.PATCHES_PER_BANK * rec.PATCH_SIZE,
    "performances": banks.PERFORMANCES_PER_BANK * rec.PERFORMANCE_SIZE,
}


def build_probe_bank(source: banks.Bank) -> Dict[str, bytes]:
    """The three decoded blobs of a bank that names ``source`` and ROM 4.

    Every unused part is written as :data:`kws.records.UNUSED_PATCH`; an
    all-zero record would otherwise name patch 0 of RAM 1 in all eight
    parts, and a remap would drag those along, hiding the one reference
    the experiment is about.
    """
    perf_bank = bytearray(_SIZES["performances"])
    for number in range(banks.PERFORMANCES_PER_BANK):
        at = number * rec.PERFORMANCE_SIZE
        record = bytearray(perf_bank[at : at + rec.PERFORMANCE_SIZE])
        for index in range(rec.PARTS_PER_PERFORMANCE):
            rec.set_performance_part(record, index, bank=0, patch=rec.UNUSED_PATCH)
        if number == PROBE_SLOT:
            # A performance name is fifteen characters and a NUL.
            record[0:15] = bytes(m.encode_name(PROBE_PERFORMANCE, length=15))
            record[15] = 0
            rec.set_performance_part(record, 0, bank=source.number, patch=1)
            rec.set_performance_part(record, 1, bank=THIRD_PARTY_BANK, patch=2)
        perf_bank[at : at + rec.PERFORMANCE_SIZE] = record

    patch_bank = bytearray(_SIZES["patches"])
    at = PROBE_PATCH_SLOT * rec.PATCH_SIZE
    patch_bank[at : at + 16] = bytes(m.encode_name(PROBE_PATCH, length=16))

    waveseq = bytearray(_SIZES["waveseq"])
    at = rec.SEQNAME_BASE
    waveseq[at : at + rec.SEQNAME_SIZE] = bytes(
        m.encode_name(PROBE_SEQUENCE, length=rec.SEQNAME_SIZE)
    )

    return {
        "performances": bytes(perf_bank),
        "patches": bytes(patch_bank),
        "waveseq": bytes(waveseq),
    }


def _normalise(blobs: Dict[str, bytes]) -> Dict[str, bytes]:
    """Trim every kind to its whole-bank size, as the app's reads do.

    A wave-sequence dump can carry more than the bank the model uses; the
    performance and patch dumps divide exactly and are checked instead.
    """
    out: Dict[str, bytes] = {}
    for kind, size in _SIZES.items():
        data = blobs.get(kind, b"")
        if kind != "waveseq" and len(data) != size:
            raise RuntimeError(
                f"the {kind} dump decoded to {len(data)} bytes; a whole bank is {size}"
            )
        out[kind] = data[:size]
    return out


def read_probe_bank(device: Any, entry: banks.Bank) -> Dict[str, bytes]:
    """One bank's three kinds, decoded and trimmed."""
    blobs: Dict[str, bytes] = {}
    for kind, function in _BUNDLE_FUNCTIONS:
        raw = device.read_raw(entry.sysex, function)
        packet = m.parse(raw)
        if packet is None:
            raise RuntimeError(f"{entry.label} {kind} is not a valid frame")
        blobs[kind] = bytes(packet.data())
    return _normalise(blobs)


def classify(
    source: banks.Bank,
    destination: banks.Bank,
    probe: Dict[str, bytes],
    got: Dict[str, bytes],
) -> str:
    """``remapped``, ``unremapped`` or ``unexpected`` for a destination."""
    remapped = dict(probe)
    remapped["performances"] = rec.remap_performance_banks(
        probe["performances"], source.number, destination.number
    )
    if all(got[kind] == remapped[kind] for kind in _SIZES):
        return "remapped"
    if all(got[kind] == probe[kind] for kind in _SIZES):
        return "unremapped"
    return "unexpected"


def part_table(performances: bytes, slot: int = PROBE_SLOT) -> List[str]:
    """The named parts of one performance, in the probe's own numbering."""
    at = slot * rec.PERFORMANCE_SIZE
    record = performances[at : at + rec.PERFORMANCE_SIZE]
    lines = [f'performance {slot:02d} "{m.decode_name(record)}"']
    for part in rec.performance_parts(record):
        if part.patch == rec.UNUSED_PATCH:
            lines.append(f"  part {part.index} unused")
            continue
        label = banks.bank(part.bank).label
        lines.append(f"  part {part.index} -> {label}:{part.patch:02d}")
    return lines


def _build_device(args: argparse.Namespace) -> Any:
    from kwsed.cli import _build_device

    return _build_device(args)


#: BANK_COPY_TYPE (346), as the unit's own page cycles it. Measured on the
#: bench: 0 copies performances, 1 patches, 2 wave sequences, 3 all three.
MOVE_TYPES = m.BANK_MOVE_TYPES

#: The four parameter changes the panel's MOVE BANK DATA? sends. The names
#: are Korg's, as Edisyn spells them: 213 source bank, 346 copy type, 379
#: destination bank, 472 execute. The bank values are the System Exclusive
#: bytes, the same ones a request carries.
MOVE_SOURCE_BANK = m.BANK_MOVE_SOURCE_BANK
MOVE_TYPE = m.BANK_MOVE_TYPE
MOVE_DEST_BANK = m.BANK_MOVE_DEST_BANK
EXECUTE_BANK_MOVE = m.EXECUTE_BANK_MOVE


def bank_move_frames(
    source: banks.Bank,
    destination: banks.Bank,
    kind: str,
    channel: int,
) -> List[bytes]:
    """The unit's own bank move, as the panel expresses it."""
    return m.bank_move(source.sysex, destination.sysex, kind=kind, channel=channel)


def _backup_destination(device: Any, entry: banks.Bank, where: str) -> str:
    """One envelope of the destination's three kinds, before the move."""
    blocks: Dict[str, bytes] = {}
    for kind, function in _BUNDLE_FUNCTIONS:
        raw = device.read_raw(entry.sysex, function)
        packet = m.parse(raw)
        if packet is None:
            raise RuntimeError(f"{entry.label} {kind} is not a valid frame")
        blocks[kind] = bytes(raw)
    saved = backup.save(
        where,
        kind="bank-bundle",
        slot=entry.label,
        name=entry.label,
        device_id=str(device.channel),
        source="probe_bank_copy",
        blocks=blocks,
    )
    return saved


def _move(device: Any, args: argparse.Namespace) -> int:
    """Perform the unit's own BANK MOVE, without the panel.

    This is the discovery the probe produced: the panel's operation is four
    documented parameter changes, so it can be replayed. It is here to make
    the experiment repeatable, not as kwsed's copy path -- the unit gives no
    per-kind verify, and on the bench it did not clear the source despite
    the LCD's "MOVE BANK DATA?" prompt.
    """
    source = banks.bank(args.source)
    destination = banks.bank(args.destination)
    if not source.writable or not destination.writable:
        print("the probe moves between writable banks only")
        return EXIT_ERROR
    if source.number == destination.number:
        print("source and destination are the same bank")
        return EXIT_ERROR
    if not args.yes:
        print(
            f"would move {source.label} onto {destination.label} "
            f"({args.type}); nothing has been sent. Add --yes to execute."
        )
        return EXIT_ERROR
    where = args.backup_dir or backup.default_dir()
    saved = _backup_destination(device, destination, where)
    for frame in bank_move_frames(source, destination, args.type, device.channel):
        device._send(frame)
        time.sleep(0.08)
    time.sleep(args.settle)
    got = read_probe_bank(device, destination)
    print(f"moved {source.label} -> {destination.label} ({args.type})")
    print(f"destination backed up first: {saved}")
    print("\n".join(part_table(got["performances"])))
    print(
        f"source {source.label} after the move: "
        f'"{m.decode_name(read_probe_bank(device, source)["performances"])}"'
    )
    return EXIT_OK


def _prepare(device: Any, args: argparse.Namespace) -> int:
    entry = banks.bank(args.bank)
    if not entry.writable:
        print(f"{entry.label} is ROM; the probe writes a writable bank only")
        return EXIT_ERROR
    if not args.yes:
        print(
            f"would write the probe bank to {entry.label}; nothing has been "
            "sent. Add --yes to write it."
        )
        return EXIT_ERROR
    probe = build_probe_bank(entry)
    where = args.backup_dir or backup.default_dir()
    result = device.write_bank_bundle(
        entry.number,
        performances=probe["performances"],
        patches=probe["patches"],
        waveseq=probe["waveseq"],
        backup_dir=where,
    )
    if not isinstance(result, dict) or result.get("verified") is not True:
        print(f"the write was not verified: {result}")
        return EXIT_ERROR
    got = read_probe_bank(device, entry)
    print(f"wrote the probe bank to {entry.label} (backup: {result['backup']})")
    print("\n".join(part_table(got["performances"])))
    print()
    print("Now, on the unit: MIDI/GLOBAL -> BANK COPY")
    print(f"  source {entry.label}, destination <the bank to check>, execute")
    print("  mode ALL or PERFORMANCE -- both carry the performance bank")
    print(f"Then: {os.path.basename(sys.argv[0])} check {entry.id} <destination>")
    return EXIT_OK


def _check(device: Any, args: argparse.Namespace) -> int:
    source = banks.bank(args.source)
    destination = banks.bank(args.destination)
    if not source.writable:
        print(f"{source.label} is ROM; the probe lives in a writable bank")
        return EXIT_ERROR
    probe = build_probe_bank(source)
    got = read_probe_bank(device, destination)
    verdict = classify(source, destination, probe, got)
    print(f"source {source.label}, destination {destination.label}")
    print("\n".join(part_table(got["performances"])))
    for kind in ("patches", "waveseq"):
        same = got[kind] == probe[kind]
        print(
            f"{kind}: {'identical to the probe' if same else 'DIFFERS from the probe'}"
        )
    if verdict == "remapped":
        print("VERDICT: the panel remapped the self-reference (as kwsed does)")
        return EXIT_OK
    if verdict == "unremapped":
        print("VERDICT: the panel did NOT remap; the copy still names the source")
        return EXIT_NOT_REMAPPED
    print(
        "VERDICT: unexpected -- the destination matches neither the probe nor its remap"
    )
    return EXIT_UNEXPECTED


def _show(device: Any, args: argparse.Namespace) -> int:
    entry = banks.bank(args.bank)
    got = read_probe_bank(device, entry)
    print(entry.label)
    print("\n".join(part_table(got["performances"])))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", default=None, help="MIDI output port to use")
    common.add_argument("--recv-port", default=None, help="MIDI input port to use")
    common.add_argument(
        "--device-channel",
        type=int,
        default=None,
        help="the unit's global channel, 1-16 (default: the saved setting)",
    )
    common.add_argument(
        "--demo", action="store_true", help="run against the built-in demo device"
    )
    common.add_argument(
        "--scan", action="store_true", help="re-probe the ports before connecting"
    )
    common.add_argument(
        "--backup-dir",
        default=None,
        help="where the prepare write saves its backup envelope",
    )
    parser = argparse.ArgumentParser(
        prog="probe_bank_copy.py",
        description=(
            "prepare a probe bank, let the unit's panel BANK COPY move it, "
            "and read the destination back to see whether the panel remapped "
            "the performance's references."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser(
        "prepare",
        parents=[common],
        help="write the probe bank to a writable bank (needs --yes)",
    )
    prep.add_argument("bank", help="writable source bank: RAM1, RAM2, RAM3 or CARD")
    prep.add_argument(
        "--yes", action="store_true", help="actually write; without it nothing is sent"
    )
    check = sub.add_parser(
        "check",
        parents=[common],
        help="read a destination back and say whether the panel remapped it",
    )
    check.add_argument("source", help="the bank the probe was written to")
    check.add_argument("destination", help="the bank the panel copied it into")
    show = sub.add_parser(
        "show", parents=[common], help="print a bank's probe performance (read-only)"
    )
    show.add_argument("bank", help="bank to read")
    move = sub.add_parser(
        "move",
        parents=[common],
        help="perform the unit's own BANK MOVE, without the panel (needs --yes)",
    )
    move.add_argument("source", help="source bank")
    move.add_argument("destination", help="destination bank")
    move.add_argument(
        "--type",
        choices=tuple(MOVE_TYPES),
        default="all",
        help="what to move (default: all three kinds)",
    )
    move.add_argument(
        "--settle",
        type=float,
        default=1.0,
        help="seconds to wait after EXECUTE_BANK_MOVE (default: 1.0)",
    )
    move.add_argument(
        "--yes", action="store_true", help="actually move; without it nothing is sent"
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    device = _build_device(args)
    if args.command == "prepare":
        return _prepare(device, args)
    if args.command == "check":
        return _check(device, args)
    if args.command == "move":
        return _move(device, args)
    return _show(device, args)


if __name__ == "__main__":
    sys.exit(main())
