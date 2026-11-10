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

"""Pure-stdlib reader for FAT12 floppy images (no MBR; boot sector at LBA 0).

Geometry reference: mpc2emu ``writers/fat12.py`` (``_GEOM``, ``_fat12_get``,
``_read_bpb``, ``_cluster_offset``). This module is an independent reader.
"""

from __future__ import annotations

import struct
from typing import Callable, Dict, List, Tuple

__all__ = ["Fat12Error", "is_fat12", "read_files"]


class Fat12Error(ValueError):
    """Raised for malformed or truncated FAT12 images."""


def _is_power_of_two(value: int) -> bool:
    return value > 0 and (value & (value - 1)) == 0


def is_fat12(data: bytes) -> bool:
    if len(data) < 512 or data[510] != 0x55 or data[511] != 0xAA:
        return False
    try:
        bps = struct.unpack_from("<H", data, 11)[0]
        reserved = struct.unpack_from("<H", data, 14)[0]
        root_entries = struct.unpack_from("<H", data, 17)[0]
        total16 = struct.unpack_from("<H", data, 19)[0]
        fatsz16 = struct.unpack_from("<H", data, 22)[0]
        total32 = struct.unpack_from("<I", data, 32)[0]
        fatsz32 = struct.unpack_from("<I", data, 36)[0]
    except struct.error:
        return False
    spc = data[13]
    nfats = data[16]
    if bps not in (512, 1024, 2048) or not _is_power_of_two(spc):
        return False
    if reserved < 1 or nfats not in (1, 2) or root_entries <= 0:
        return False
    total = total16 if total16 != 0 else total32
    fatsz = fatsz16 if fatsz16 != 0 else fatsz32
    if total <= 0 or fatsz <= 0:
        return False
    media = data[21]
    return data[54:59] == b"FAT12" or media in (0xF0, 0xF9)


def _parse_bpb(data: bytes) -> Tuple[int, int, int, int, int, int, int, int, int]:
    if len(data) < 512:
        raise Fat12Error("image too short for boot sector")
    if data[510] != 0x55 or data[511] != 0xAA:
        raise Fat12Error("missing boot signature")
    bps = struct.unpack_from("<H", data, 11)[0]
    spc = data[13]
    reserved = struct.unpack_from("<H", data, 14)[0]
    nfats = data[16]
    root_entries = struct.unpack_from("<H", data, 17)[0]
    if bps not in (512, 1024, 2048) or not _is_power_of_two(spc):
        raise Fat12Error("invalid geometry")
    if reserved < 1 or nfats not in (1, 2) or root_entries <= 0:
        raise Fat12Error("invalid geometry")
    total16 = struct.unpack_from("<H", data, 19)[0]
    media = data[21]
    fatsz16 = struct.unpack_from("<H", data, 22)[0]
    total32 = struct.unpack_from("<I", data, 32)[0]
    fatsz32 = struct.unpack_from("<I", data, 36)[0]
    total = total16 if total16 != 0 else total32
    if total <= 0:
        raise Fat12Error("invalid total sector count")
    fatsz = fatsz16 if fatsz16 != 0 else fatsz32
    if fatsz <= 0:
        raise Fat12Error("invalid FAT size")
    fat12_tag = data[54:59] == b"FAT12"
    if not (fat12_tag or media in (0xF0, 0xF9)):
        raise Fat12Error("not a FAT12 filesystem")
    root_sectors = (root_entries * 32 + bps - 1) // bps
    return bps, spc, reserved, nfats, root_entries, total, fatsz, media, root_sectors


def _fat12_get(fat: bytes, n: int) -> int:
    offset = (n * 3) // 2
    if offset + 1 >= len(fat):
        raise Fat12Error("FAT read out of range")
    if n & 1:
        return ((fat[offset] >> 4) | (fat[offset + 1] << 4)) & 0xFFF
    return (fat[offset] | ((fat[offset + 1] & 0x0F) << 8)) & 0xFFF


def _short_name(entry: bytes) -> str:
    base = entry[0:8].decode("ascii", "replace").rstrip(" ")
    ext = entry[8:11].decode("ascii", "replace").rstrip(" ")
    if ext:
        return base + "." + ext
    return base


def _lfn_chars(entry: bytes) -> bytes:
    return entry[1:11] + entry[14:26] + entry[28:32]


def _build_lfn(parts: List[Tuple[int, bytes]]) -> str:
    if not parts:
        return ""
    parts_sorted = sorted(parts, key=lambda item: item[0])
    raw = b"".join(item[1] for item in parts_sorted)
    text = raw.decode("utf-16-le", errors="replace")
    if "\x00" in text:
        text = text.split("\x00", 1)[0]
    text = text.rstrip("\uffff")
    return text


def _read_chain(
    data: bytes,
    fat: bytes,
    spc: int,
    bps: int,
    data_start: int,
    total: int,
    cluster_bytes: int,
    max_cluster: int,
    start: int,
    size: int,
) -> bytes:
    if size <= 0:
        return b""
    if start < 2 or start > max_cluster:
        raise Fat12Error("invalid start cluster")
    fs_bytes = total * bps
    out = bytearray()
    cluster = start
    visited: set[int] = set()
    while len(out) < size:
        if cluster < 2 or cluster > max_cluster:
            raise Fat12Error("cluster out of range")
        if cluster in visited:
            raise Fat12Error("FAT chain loop")
        visited.add(cluster)
        off = (data_start + (cluster - 2) * spc) * bps
        if off + cluster_bytes > fs_bytes:
            raise Fat12Error("truncated cluster data")
        out += data[off : off + cluster_bytes]
        nxt = _fat12_get(fat, cluster)
        if nxt >= 0xFF8:
            break
        if nxt in (0, 1, 0xFF7):
            raise Fat12Error("broken FAT chain")
        cluster = nxt
    if len(out) < size:
        raise Fat12Error("FAT chain ends before file size")
    return bytes(out[:size])


def _parse_root_directory(
    root: bytes, read_file: Callable[[int, int], bytes]
) -> Dict[str, bytes]:
    result: Dict[str, bytes] = {}
    lfn_parts: List[Tuple[int, bytes]] = []
    offset = 0
    while offset + 32 <= len(root):
        entry = root[offset : offset + 32]
        offset += 32
        if entry[0] == 0x00:
            break
        if entry[0] == 0xE5:
            lfn_parts = []
            continue
        attr = entry[11]
        if attr == 0x0F:
            lfn_parts.append((entry[0] & 0x3F, _lfn_chars(entry)))
            continue
        if attr & 0x08 or attr & 0x10:
            lfn_parts = []
            continue
        name = _build_lfn(lfn_parts) if lfn_parts else _short_name(entry)
        lfn_parts = []
        start = struct.unpack_from("<H", entry, 26)[0]
        size = struct.unpack_from("<I", entry, 28)[0]
        result[name] = read_file(start, size)
    return result


def read_files(data: bytes) -> Dict[str, bytes]:
    bps, spc, reserved, nfats, _root_entries, total, fatsz, _media, root_sectors = (
        _parse_bpb(data)
    )

    fs_bytes = total * bps
    if len(data) < fs_bytes:
        raise Fat12Error("truncated filesystem image")

    fat_start = reserved
    root_start = reserved + nfats * fatsz
    data_start = root_start + root_sectors

    if data_start >= total:
        raise Fat12Error("invalid filesystem geometry")
    n_clusters = (total - data_start) // spc
    if n_clusters <= 0:
        raise Fat12Error("no data clusters")

    fat_offset = fat_start * bps
    fat_size = fatsz * bps
    if fat_offset + fat_size > len(data):
        raise Fat12Error("truncated FAT")
    fat = data[fat_offset : fat_offset + fat_size]

    root_offset = root_start * bps
    root_size = root_sectors * bps
    if root_offset + root_size > len(data):
        raise Fat12Error("truncated root directory")
    root = data[root_offset : root_offset + root_size]

    cluster_bytes = spc * bps
    max_cluster = n_clusters + 1

    def read_file(start: int, size: int) -> bytes:
        return _read_chain(
            data,
            fat,
            spc,
            bps,
            data_start,
            total,
            cluster_bytes,
            max_cluster,
            start,
            size,
        )

    return _parse_root_directory(root, read_file)
