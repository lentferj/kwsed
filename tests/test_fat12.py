# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors

"""Tests for the FAT12 floppy-image reader."""

from __future__ import annotations

import os
import struct

import pytest

from kws.fat12 import Fat12Error, is_fat12, read_files

BPS = 512
SPC = 2
TOTAL = 1440
ROOT_ENTRIES = 112
FATSZ = 3
MEDIA = 0xF9


def _fat_set(fat: bytearray, n: int, v: int) -> None:
    o = (n * 3) // 2
    v &= 0xFFF
    if n & 1:
        fat[o] = (fat[o] & 0x0F) | ((v & 0x0F) << 4)
        fat[o + 1] = (v >> 4) & 0xFF
    else:
        fat[o] = v & 0xFF
        fat[o + 1] = (fat[o + 1] & 0xF0) | ((v >> 8) & 0x0F)


def _short_entry(name: str, attr: int, start: int, size: int) -> bytes:
    base, _, ext = name.partition(".")
    base = base.upper().ljust(8)[:8]
    ext = ext.upper().ljust(3)[:3]
    entry = bytearray(32)
    entry[0:8] = base.encode("ascii")
    entry[8:11] = ext.encode("ascii")
    entry[11] = attr
    struct.pack_into("<H", entry, 26, start)
    struct.pack_into("<I", entry, 28, size)
    return bytes(entry)


def _lfn_entries(long_name: str) -> bytes:
    units = long_name.encode("utf-16-le") + b"\x00\x00"
    while len(units) % 26 != 0:
        units += b"\xff\xff"
    chunks = [units[i : i + 26] for i in range(0, len(units), 26)]
    out = bytearray()
    n = len(chunks)
    for i in range(n - 1, -1, -1):
        seq = i + 1
        if i == n - 1:
            seq |= 0x40
        entry = bytearray(32)
        entry[0] = seq
        entry[1:11] = chunks[i][0:10]
        entry[11] = 0x0F
        entry[12] = 0x00
        entry[13] = 0x00
        entry[14:26] = chunks[i][10:22]
        struct.pack_into("<H", entry, 26, 0)
        entry[28:32] = chunks[i][22:26]
        out += entry
    return bytes(out)


def _find_free(root: bytearray) -> int:
    for off in range(0, len(root), 32):
        if root[off] in (0x00, 0xE5):
            return off
    raise RuntimeError("root full")


def _find_free_run(root: bytearray, n: int) -> int:
    run = 0
    start = 0
    for off in range(0, len(root), 32):
        if root[off] in (0x00, 0xE5):
            if run == 0:
                start = off
            run += 1
            if run >= n:
                return start
        else:
            run = 0
    raise RuntimeError("root full")


def make_fat12_image(files: dict[str, bytes]) -> bytes:
    bps = BPS
    spc = SPC
    total = TOTAL
    root_entries = ROOT_ENTRIES
    fatsz = FATSZ
    root_sectors = (root_entries * 32 + bps - 1) // bps

    bs = bytearray(bps)
    bs[0:3] = b"\xeb\x3c\x90"
    bs[3:11] = b"MSWIN4.1"
    struct.pack_into("<H", bs, 11, bps)
    bs[13] = spc
    struct.pack_into("<H", bs, 14, 1)
    bs[16] = 2
    struct.pack_into("<H", bs, 17, root_entries)
    struct.pack_into("<H", bs, 19, total)
    bs[21] = MEDIA
    struct.pack_into("<H", bs, 22, fatsz)
    struct.pack_into("<H", bs, 24, 9)
    struct.pack_into("<H", bs, 26, 2)
    bs[54:62] = b"FAT12   "
    bs[510:512] = b"\x55\xaa"

    cluster_bytes = spc * bps
    cluster_cursor = 2
    fat = bytearray(fatsz * bps)
    fat[0] = MEDIA
    fat[1] = 0xFF
    fat[2] = 0xFF

    root = bytearray(root_sectors * bps)
    data = bytearray((total - (1 + 2 * fatsz + root_sectors)) * bps)

    def alloc(n: int) -> int:
        nonlocal cluster_cursor
        if n <= 0:
            return 0
        start = cluster_cursor
        for i in range(n):
            c = cluster_cursor + i
            nxt = c + 1 if i < n - 1 else 0xFFF
            _fat_set(fat, c, nxt)
        cluster_cursor += n
        return start

    for name, content in files.items():
        if len(content) == 0:
            start = 0
        else:
            n = (len(content) + cluster_bytes - 1) // cluster_bytes
            start = alloc(n)
            for i in range(n):
                chunk = content[i * cluster_bytes : (i + 1) * cluster_bytes]
                off = (start - 2 + i) * cluster_bytes
                data[off : off + len(chunk)] = chunk

        short_ok = (
            "." in name
            and " " not in name
            and len(name.split(".")[0]) <= 8
            and len(name.split(".")[-1]) <= 3
        )
        if short_ok:
            entry = _short_entry(name, 0x20, start, len(content))
            slot = _find_free(root)
            root[slot : slot + 32] = entry
        else:
            ext = name.rsplit(".", 1)[-1].upper()[:3].ljust(3) if "." in name else "   "
            base = "LONGFILE"[:8].ljust(8)
            # make dotted short name
            dotted = base.rstrip() + "." + ext.strip() if ext.strip() else base.rstrip()
            lfn = _lfn_entries(name)
            entry = _short_entry(dotted, 0x20, start, len(content))
            need = len(lfn) // 32 + 1
            slot = _find_free_run(root, need)
            root[slot : slot + len(lfn)] = lfn
            root[slot + len(lfn) : slot + len(lfn) + 32] = entry

    image = bytearray(total * bps)
    image[0:bps] = bs
    off = 1 * bps
    image[off : off + fatsz * bps] = fat
    off = (1 + fatsz) * bps
    image[off : off + fatsz * bps] = fat
    root_start_sector = 1 + 2 * fatsz
    off = root_start_sector * bps
    image[off : off + root_sectors * bps] = root
    data_start_sector = root_start_sector + root_sectors
    off = data_start_sector * bps
    image[off : off + len(data)] = data
    return bytes(image)


def test_is_fat12_true() -> None:
    img = make_fat12_image({"ALPHA.SYX": b"\x11" * 100})
    assert is_fat12(img) is True


def test_is_fat12_false() -> None:
    assert is_fat12(b"") is False
    assert is_fat12(os.urandom(1024)) is False
    img = make_fat12_image({"ALPHA.SYX": b"\x11" * 100})
    assert is_fat12(img[:511]) is False
    assert is_fat12(b"\x00" * 512) is False


def test_read_files_basic() -> None:
    data1 = b"hello world" * 50
    data2 = b"\xaa\xbb" * 100
    img = make_fat12_image({"ALPHA.SYX": data1, "BETA.BNK": data2})
    files = read_files(img)
    assert files == {"ALPHA.SYX": data1, "BETA.BNK": data2}


def test_read_files_multicluster() -> None:
    data = b"Z" * 3000
    img = make_fat12_image({"BIGFILE.BIN": data})
    files = read_files(img)
    assert files["BIGFILE.BIN"] == data


def test_read_files_zero_byte() -> None:
    img = make_fat12_image({"EMPTY.TXT": b""})
    files = read_files(img)
    assert files["EMPTY.TXT"] == b""


def test_read_files_lfn() -> None:
    data = b"L" * 200
    img = make_fat12_image({"Glass Meridian.syx": data})
    files = read_files(img)
    assert "Glass Meridian.syx" in files
    assert files["Glass Meridian.syx"] == data


def test_corrupt_fat_chain() -> None:
    img = bytearray(make_fat12_image({"ALPHA.SYX": b"\x11" * 3000}))
    o = (2 * 3) // 2
    val = 0xF00
    img[512 + o] = val & 0xFF
    img[512 + o + 1] = (img[512 + o + 1] & 0xF0) | ((val >> 8) & 0x0F)
    with pytest.raises(Fat12Error):
        read_files(bytes(img))


def test_tail_ignored() -> None:
    img = make_fat12_image({"ALPHA.SYX": b"\x11" * 100})
    tail = img + b"\x00" * (9 * 1024)
    assert read_files(tail) == read_files(img)
