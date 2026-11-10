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

"""Tests for kws/importers.py."""

from __future__ import annotations

import os
import struct

import pytest

from kws import banks, deps, dumps, importers, multi
from kws import messages as m
from kws import records as r
from tests.test_bankbuild import make_patch, make_performance, make_waveseq


def _bulk_perf_bytes(name="Glass Meridian"):
    return make_performance(name, ((0, 0),))


def _bulk_patch_bytes(name="Amber Drift"):
    return make_patch(name)


def test_single_bank_syx(tmp_path):
    ws, _ = make_waveseq({0: [40, 41]})
    # bank wire byte 0 (RAM1)
    # simpler: use dumps? build records
    perfs = [_bulk_perf_bytes() for _ in range(50)]
    patches = [_bulk_patch_bytes() for _ in range(35)]
    perf_msg = m.frame_bulk(m.Function.ALL_PERFORMANCE_DUMP, 0, b"".join(perfs))
    patch_msg = m.frame_bulk(m.Function.ALL_PATCH_DUMP, 0, b"".join(patches))
    ws_msg = m.frame_bulk(m.Function.ALL_WAVESEQ_DUMP, 0, ws)
    path = tmp_path / "factory.syx"
    path.write_bytes(perf_msg + patch_msg + ws_msg)
    banks_list = importers.read_file(str(path))
    assert len(banks_list) == 1
    ib = banks_list[0]
    assert ib.scope == "RAM1"
    assert ib.format == "sysex"
    assert len(ib.source.performances) == 50
    assert len(ib.source.patches) == 35
    assert ib.source.waveseq is not None
    assert len(ib.source.waveseq) == r.WAVESEQ_BANK_SIZE
    assert m.decode_name(ib.source.performances[0]) == "Glass Meridian"
    assert m.decode_name(ib.source.patches[0]) == "Amber Drift"
    assert ib.label == "factory.syx [RAM1]"


def test_twelve_banks_syx(tmp_path):
    msgs = bytearray()
    for wire in range(12):
        recs = [_bulk_perf_bytes() for _ in range(50)]
        msgs += m.frame_bulk(m.Function.ALL_PERFORMANCE_DUMP, wire, b"".join(recs))
    path = tmp_path / "all.syx"
    path.write_bytes(bytes(msgs))
    out = importers.read_file(str(path))
    assert len(out) == 12
    scopes = [b.scope for b in out]
    assert len(set(scopes)) == 12


def test_single_dumps_slots(tmp_path):
    perf = make_performance("Glass Meridian", ((0, 0),))
    patch = make_patch("Amber Drift")
    # build performance dump
    perf_bytes = m.frame(
        m.Function.PERFORMANCE_DUMP,
        [0, 7] + list(m.to_nibbles(perf)) + [m.checksum(list(m.to_nibbles(perf)))],
    )
    patch_msg = m.frame(
        m.Function.SINGLE_PATCH_DUMP,
        [0, 3] + list(m.to_nibbles(patch)) + [m.checksum(list(m.to_nibbles(patch)))],
    )
    path = tmp_path / "single.syx"
    path.write_bytes(perf_bytes + patch_msg)
    out = importers.read_file(str(path))
    assert len(out) == 1
    ib = out[0]
    assert len(ib.source.performances) == 50
    assert len(ib.source.patches) == 35
    # slot 7 perf present, others empty
    assert ib.source.performances[7] == perf
    assert ib.source.performances[0] == bytes(181)
    assert ib.source.patches[3] == patch
    assert ib.source.patches[0] == bytes(426)


def test_midi_bank(tmp_path):
    perfs = [_bulk_perf_bytes() for _ in range(50)]
    patches = [_bulk_patch_bytes() for _ in range(35)]
    perf_msg = m.frame_bulk(m.Function.ALL_PERFORMANCE_DUMP, 0, b"".join(perfs))
    patch_msg = m.frame_bulk(m.Function.ALL_PATCH_DUMP, 0, b"".join(patches))
    sysex_event_data = perf_msg

    # VLQ length
    def vlq(n):
        out = bytearray()
        while True:
            out.insert(0, n & 0x7F)
            n >>= 7
            if n == 0:
                break
        for i in range(len(out) - 1):
            out[i] |= 0x80
        return bytes(out)

    trk = bytearray()
    # delta 0
    trk.append(0)
    trk.append(0xF0)
    trk += vlq(len(sysex_event_data))
    trk += sysex_event_data
    # delta 0, F0 for patch
    trk.append(0)
    trk.append(0xF0)
    trk += vlq(len(patch_msg))
    trk += patch_msg
    # end of track
    trk += bytes([0, 0xFF, 0x2F, 0])
    # header
    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, 480)
    track_chunk = b"MTrk" + struct.pack(">I", len(trk)) + bytes(trk)
    path = tmp_path / "bank.mid"
    path.write_bytes(header + track_chunk)
    out = importers.read_file(str(path))
    assert len(out) == 1
    assert out[0].scope == "RAM1"
    assert m.decode_name(out[0].source.performances[0]) == "Glass Meridian"


def test_midi_without_f0(tmp_path):
    perfs = [_bulk_perf_bytes() for _ in range(50)]
    perf_msg = m.frame_bulk(m.Function.ALL_PERFORMANCE_DUMP, 0, b"".join(perfs))
    # real-world: data is 42 30 28 ... without leading F0, but with trailing F7
    data = perf_msg[1:]  # remove F0, keep F7

    def vlq(n):
        out = bytearray()
        while True:
            out.insert(0, n & 0x7F)
            n >>= 7
            if n == 0:
                break
        for i in range(len(out) - 1):
            out[i] |= 0x80
        return bytes(out)

    trk = bytearray()
    trk.append(0)
    trk.append(0xF0)
    trk += vlq(len(data))
    trk += data
    trk += bytes([0, 0xFF, 0x2F, 0])
    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, 480)
    track_chunk = b"MTrk" + struct.pack(">I", len(trk)) + bytes(trk)
    path = tmp_path / "bank.mid"
    path.write_bytes(header + track_chunk)
    out = importers.read_file(str(path))
    assert len(out) == 1
    assert out[0].scope == "RAM1"


def test_unknown_empty_raises(tmp_path):
    path = tmp_path / "empty.syx"
    path.write_bytes(b"")
    with pytest.raises((importers.UnsupportedFormat, ValueError)):
        importers.read_file(str(path))
    path2 = tmp_path / "foreign.syx"
    path2.write_bytes(b"\x00\x01\x02")
    with pytest.raises((importers.UnsupportedFormat, ValueError)):
        importers.read_file(str(path2))


def test_unsupported_formats(tmp_path):
    for ext in (".lib", ".sqs"):
        path = tmp_path / f"x{ext}"
        path.write_bytes(b"")
        with pytest.raises(importers.UnsupportedFormat):
            importers.read_file(str(path))


def test_card_scope_and_resolve(tmp_path):
    from kws import relations as rel

    perf = make_performance("Kelp Forest", ((11, 0),))
    perfs = [make_performance("Empty", ()) for _ in range(50)]
    perfs[0] = perf
    perf_msg = m.frame_bulk(m.Function.ALL_PERFORMANCE_DUMP, 3, b"".join(perfs))
    # patch in card scope
    patch = make_patch("Amber Drift")
    patches = [make_patch("Empty") for _ in range(35)]
    patches[0] = patch
    patch_msg = m.frame_bulk(m.Function.ALL_PATCH_DUMP, 3, b"".join(patches))
    path = tmp_path / "card.syx"
    path.write_bytes(perf_msg + patch_msg)
    out = importers.read_file(str(path))
    assert len(out) == 1
    ib = out[0]
    assert ib.scope.startswith("CARD/")
    assert rel.resolve_part_scope(11, ib.scope) == ib.scope
    # ensure source map resolves
    srcs = {ib.scope: ib.source}
    assert srcs[ib.scope] is ib.source
