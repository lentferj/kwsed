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

"""Container format readers: ZIP archives, FAT12 disk images."""

from __future__ import annotations

import io
import os
import zipfile
from typing import List, Optional, Tuple

from kws import fat12
from kws.assembler import assemble, relabel_disk_banks
from kws.exceptions import UnsupportedFormat
from kws.messages import Packet
from kws.types import ImportedBank

__all__ = [
    "read_img_file",
    "read_img_data",
    "read_zip_file",
    "read_zip_data",
    "looks_like_img",
    "looks_like_zip",
    "looks_like_flux",
]


def read_img_data(data: bytes, label: str) -> List[ImportedBank]:
    """Read Wavestation banks from a FAT12 disk image byte stream."""
    if not fat12.is_fat12(data):
        raise UnsupportedFormat("not a FAT12 floppy image")
    files = fat12.read_files(data)
    result: List[ImportedBank] = []
    for name in sorted(files):
        content = files[name]
        ext = os.path.splitext(name)[1].lower()
        packets = None
        stream_fmt = "sysex"
        if ext in (".syx", ".sysex", ".bnk"):
            from kws import dumps

            packets = dumps.read_syx_data(content)
        elif ext in (".mid", ".midi"):
            try:
                from kws.importer_midi import read_midi_data

                packets = read_midi_data(content)
            except UnsupportedFormat:
                packets = None
            stream_fmt = "midi"
        elif b"\xf0\x42" in content:
            from kws import dumps

            packets = dumps.read_syx_data(content)
        else:
            continue
        if not packets:
            continue
        assembled = assemble(packets, f"{label} / {name}", stream_fmt)
        result.extend(relabel_disk_banks(assembled, label, name))
    if not result:
        raise UnsupportedFormat("no Wavestation banks in disk image")
    return result


def read_img_file(path: str) -> List[ImportedBank]:
    """Read Wavestation banks from a FAT12 disk image file (.img)."""
    with open(path, "rb") as handle:
        raw = handle.read()
    return read_img_data(raw, os.path.basename(path))


def _zip_parse_member(content: bytes, ext: str) -> Optional[Tuple[List[Packet], str]]:
    if ext in (".syx", ".sysex", ".bnk"):
        from kws import dumps

        return dumps.read_syx_data(content), "sysex"
    if ext in (".mid", ".midi"):
        try:
            from kws.importer_midi import read_midi_data

            return read_midi_data(content), "midi"
        except UnsupportedFormat:
            return None
    if b"\xf0\x42" not in content:
        return None
    from kws import dumps

    return dumps.read_syx_data(content), "sysex"


def _zip_member_banks(
    zf: zipfile.ZipFile, info: zipfile.ZipInfo, label: str
) -> List[ImportedBank]:
    if info.is_dir():
        return []
    name = info.filename
    ext = os.path.splitext(name)[1].lower()
    if ext == ".7z":
        return []
    if ext == ".img":
        try:
            member = zf.read(info)
        except (OSError, zipfile.BadZipFile, RuntimeError):
            return []
        try:
            return read_img_data(member, f"{label} / {name}")
        except UnsupportedFormat:
            return []
    try:
        content = zf.read(info)
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return []
    parsed = _zip_parse_member(content, ext)
    if parsed is None:
        return []
    packets, stream_fmt = parsed
    if not packets:
        return []
    assembled = assemble(packets, f"{label} / {name}", stream_fmt)
    return relabel_disk_banks(assembled, label, name)


def read_zip_data(data: bytes, label: str) -> List[ImportedBank]:
    """Read Wavestation banks from a ZIP archive byte stream."""
    result: List[ImportedBank] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in sorted(zf.infolist(), key=lambda item: item.filename):
                result.extend(_zip_member_banks(zf, info, label))
    except zipfile.BadZipFile as exc:
        raise UnsupportedFormat("not a zip archive") from exc
    if not result:
        raise UnsupportedFormat(
            "no Wavestation banks in zip (KryoFlux .7z flux archives are "
            "not decoded; use the .img files)"
        )
    return result


def read_zip_file(path: str) -> List[ImportedBank]:
    """Read Wavestation banks from a ZIP archive file."""
    with open(path, "rb") as handle:
        raw = handle.read()
    return read_zip_data(raw, os.path.basename(path))


def looks_like_img(data: bytes) -> bool:
    """Quick check if data looks like a FAT12 disk image."""
    return fat12.is_fat12(data)


def looks_like_zip(data: bytes) -> bool:
    """Quick check if data looks like a ZIP archive."""
    return data.startswith(b"PK\x03\x04")


def looks_like_flux(data: bytes) -> bool:
    """Quick check if data looks like a KryoFlux flux archive (.7z)."""
    return data.startswith(b"7z\xbc\xaf\x27\x1c")
