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
# WARRANTY; without even the implied warranty of MERCHANTABILITY OR FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.

"""Public API for reading banks from files into something ``kws.perfcopy`` can consume.

A file may hold several banks; each becomes one :class:`ImportedBank`. The
SysEx and MIDI readers share one assembler, so a bank reads the same
regardless of how it was carried.
"""

from __future__ import annotations

import os
from typing import List

from kws import messages as m
from kws.assembler import assemble
from kws.exceptions import UnsupportedFormat
from kws.importer_container import (
    looks_like_flux,
    looks_like_img,
    looks_like_zip,
    read_img_file,
    read_zip_file,
)
from kws.importer_lib import looks_like_lib, read_lib_file
from kws.importer_midi import looks_like_midi, read_midi_data, read_midi_file
from kws.importer_sqs import looks_like_sqs, read_sqs_file

# Import from format-specific modules
from kws.importer_syx import looks_like_syx, read_syx_data, read_syx_file
from kws.types import ImportedBank

__all__ = [
    "ImportedBank",
    "UnsupportedFormat",
    "read_file",
    "describe_file",
    "read_midi_data",
    "read_midi_file",
    "read_syx_file",
    "read_syx_data",
    "read_sqs_file",
    "read_lib_file",
    "read_bnk_file",
    "read_img_file",
    "read_zip_file",
    "looks_like_syx",
    "looks_like_midi",
    "looks_like_sqs",
    "looks_like_lib",
    "looks_like_bnk",
    "looks_like_img",
    "looks_like_zip",
    "detect_format",
]


# --- Format detection ---


def _sniff_format(raw: bytes) -> str:
    head = raw[:8]
    if head.startswith(b"MThd"):
        return "midi"
    if head.startswith(b"MROF"):
        return "lib"
    if head.startswith(b"LIST") or head.startswith(b"FORM"):
        return "sqs"
    if raw.startswith(b"\xf0"):
        return "sysex"
    raise UnsupportedFormat("could not determine file format")


def detect_format(path: str) -> str:
    """Detect the format of a file by extension and content sniffing."""
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as handle:
        raw = handle.read()
    if ext == ".lib" or raw.startswith(b"MROF"):
        return "lib"
    if ext == ".img" or looks_like_img(raw):
        return "img"
    if ext == ".zip" or looks_like_zip(raw):
        return "zip"
    if ext == ".7z" or looks_like_flux(raw):
        return "flux"
    if ext == ".sqs" or looks_like_sqs(raw):
        return "sqs"
    if ext in (".syx", ".sysex") or looks_like_syx(raw):
        return "sysex"
    if ext in (".mid", ".midi") or looks_like_midi(raw):
        return "midi"
    if ext == ".bnk" or (b"\xf0\x42" in raw):
        return "sysex"
    return _sniff_format(raw)


def looks_like_bnk(data: bytes) -> bool:
    """Quick check if data looks like a .bnk bundle (has F0 42 prefix)."""
    return b"\xf0\x42" in data


# --- Public API ---


def read_file(path: str) -> List[ImportedBank]:  # noqa: C901
    """Main entry point: read a file and return a list of ImportedBank objects.

    Supports: .syx/.sysex (SysEx), .mid/.midi (Standard MIDI File),
    .sqs (SoundQuest), .lib (SoundDiver), .img (FAT12 disk image),
    .zip (ZIP archive), .bnk (SysEx bundle).
    """
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, "rb") as handle:
            head = handle.read(4)
            handle.seek(0)
            raw = handle.read()
    except OSError:
        head = b""
        raw = b""

    if ext == ".lib" or head == b"MROF":
        return read_lib_file(path)
    if ext == ".img" or looks_like_img(raw):
        return read_img_file(path)
    if ext == ".zip" or looks_like_zip(raw):
        return read_zip_file(path)
    if ext == ".7z" or looks_like_flux(raw):
        raise UnsupportedFormat(
            "KryoFlux flux archives are not decoded; use the .img files "
            "in the same download"
        )
    if ext == ".sqs" or looks_like_sqs(raw):
        packets = read_sqs_file(path)
        if not packets:
            raise UnsupportedFormat("no Wavestation messages found")
        return assemble(packets, path, "sqs")
    if ext == ".bnk" or (
        ext not in (".syx", ".sysex", ".mid", ".midi", ".sqs", ".lib")
        and b"\xf0\x42" in raw
    ):
        packets = read_syx_data(raw)
        if not packets:
            raise UnsupportedFormat("no Wavestation messages found")
        return assemble(packets, path, "sysex")
    if ext in (".syx", ".sysex") or looks_like_syx(raw):
        packets = read_syx_file(path)
        if not packets:
            raise UnsupportedFormat("no Wavestation messages found")
        return assemble(packets, path, "sysex")
    if ext in (".mid", ".midi") or looks_like_midi(raw):
        packets = read_midi_file(path)
        if not packets:
            raise UnsupportedFormat("no Wavestation messages found")
        return assemble(packets, path, "midi")
    # Fallback to sniffing
    sniffed = _sniff_format(raw)
    if sniffed == "sqs":
        packets = read_sqs_file(path)
        return assemble(packets, path, "sqs")
    if sniffed == "midi":
        packets = read_midi_file(path)
        return assemble(packets, path, "midi")
    if sniffed == "sysex":
        packets = read_syx_file(path)
        return assemble(packets, path, "sysex")
    if sniffed == "lib":
        return read_lib_file(path)
    raise UnsupportedFormat(f"unsupported format {sniffed}")


def read_bnk_file(path: str) -> List[ImportedBank]:
    """Read a .bnk file (SysEx bundle with optional prefix)."""
    with open(path, "rb") as handle:
        raw = handle.read()
    packets = read_syx_data(raw)
    if not packets:
        raise UnsupportedFormat("no Wavestation messages found")
    return assemble(packets, path, "sysex")


def describe_file(path: str) -> str:
    """Return a human-readable summary of ``path`` as a string."""
    ext = os.path.splitext(path)[1].lower()
    sniffed = None
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
        try:
            sniffed = _sniff_format(raw)
        except (UnsupportedFormat, ValueError):
            sniffed = None
    except OSError:
        sniffed = None

    if ext == ".lib" or sniffed == "lib":
        from kws import sounddiver

        entries = sounddiver.read_entries(path)
        lines: List[str] = []
        for entry in entries:
            lines.append(
                f"{entry.index}\t{entry.name}\t{entry.location}\t"
                f"{entry.comment}\t{len(entry.data)} bytes"
            )
        lines.append("")
        lines.append(
            "patch entries (158/242/410 bytes), performance entries "
            "(53..165 bytes) and wave-sequence entries are decoded; "
            "multiset entries are not converted"
        )
        return "\n".join(lines)

    banks_list = read_file(path)
    rows: List[str] = []
    if ext in (".img", ".zip"):
        for ib in banks_list:
            used_perfs = sum(1 for perf in ib.source.performances if any(perf))
            used_patches = sum(1 for patch in ib.source.patches if any(patch))
            rows.append(
                f"{ib.label}  "
                f"[{used_perfs} performances, {used_patches} patches, "
                f"waveseq={'yes' if ib.source.waveseq else 'no'}]"
            )
        return "\n".join(rows)
    for ib in banks_list:
        rows.append(ib.label)
        if ib.notes:
            rows.append(f"  note: {ib.notes}")
        rows.extend(_describe_bank_perfs(ib))
    return "\n".join(rows)


def _describe_bank_perfs(ib: ImportedBank) -> List[str]:
    out: List[str] = []
    for slot, perf in enumerate(ib.source.performances):
        if not perf or not any(perf):
            continue
        name = m.decode_name(perf)
        if name.strip():
            out.append(f"  {slot}: {name}")
    return out
