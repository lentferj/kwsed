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


def _sqs_file(body: bytes, *, even: bool = True) -> bytes:
    """A minimal SoundQuest LIST/FORM/BANK/BODY container around ``body``."""
    body_chunk = b"BODY" + struct.pack(">I", len(body)) + body
    if even and len(body) & 1:
        body_chunk += b"\x00"
    form = b"FORM" + struct.pack(">I", 4 + len(body_chunk)) + b"BANK" + body_chunk
    return b"LIST" + struct.pack(">I", len(form)) + form


def _lib_file(chunks: bytes) -> bytes:
    content = b"BLSS" + chunks
    return b"MROF" + struct.pack("<I", len(content)) + content


def _lib_entry(
    index: int, name: str, location: str = "1", data: bytes = b"\x00" * 8
) -> bytes:
    comment = f"RAM1-{index:02d}".encode()
    blocks = (
        b"\x01"
        + bytes([len(name)])
        + name.encode("ascii")
        + b"\x02"
        + bytes([len(location)])
        + location.encode("ascii")
        + b"\x03"
        + bytes([len(data)])
        + data
        + b"\x06"
        + bytes([len(comment)])
        + comment
        + b"\x00\x00"
    )
    body = bytes(12) + blocks
    return b"TNEL" + struct.pack("<I", len(body)) + body


def test_sqs_sysex_bank(tmp_path):
    ws, _ = make_waveseq({0: [40, 41]})
    perfs = b"".join(_bulk_perf_bytes() for _ in range(50))
    patches = b"".join(_bulk_patch_bytes() for _ in range(35))
    frames = (
        m.frame_bulk(m.Function.ALL_PERFORMANCE_DUMP, 0, perfs)
        + m.frame_bulk(m.Function.ALL_PATCH_DUMP, 0, patches)
        + m.frame_bulk(m.Function.ALL_WAVESEQ_DUMP, 0, ws)
    )
    path = tmp_path / "bank.sqs"
    path.write_bytes(_sqs_file(frames))

    out = importers.read_file(str(path))
    assert len(out) == 1
    assert out[0].format == "sqs"
    assert out[0].scope == "RAM1"
    assert len(out[0].source.performances) == 50
    assert len(out[0].source.patches) == 35
    assert out[0].source.waveseq is not None


def test_sqs_without_a_leading_f0_is_reassembled(tmp_path):
    perfs = b"".join(_bulk_perf_bytes() for _ in range(50))
    frame = m.frame_bulk(m.Function.ALL_PERFORMANCE_DUMP, 0, perfs)
    path = tmp_path / "bank.sqs"
    path.write_bytes(_sqs_file(frame[1:]))

    out = importers.read_file(str(path))
    assert len(out) == 1
    assert m.decode_name(out[0].source.performances[0]) == "Glass Meridian"


def test_sqs_native_body_is_refused(tmp_path):
    path = tmp_path / "native.sqs"
    path.write_bytes(_sqs_file(bytes(64)))
    with pytest.raises(importers.UnsupportedFormat, match="MidiQuest-native"):
        importers.read_file(str(path))


def test_lib_entries_are_listed_and_conversion_refused(tmp_path):
    from kws import sounddiver

    path = tmp_path / "library.lib"
    path.write_bytes(
        _lib_file(_lib_entry(0, "Glass Meridian") + _lib_entry(1, "Amber Drift"))
    )

    entries = sounddiver.read_entries(str(path))
    assert [entry.name for entry in entries] == ["Glass Meridian", "Amber Drift"]
    assert entries[0].location == "1"
    assert entries[1].comment == "RAM1-01"
    assert len(entries[0].data) == 8

    with pytest.raises(importers.UnsupportedFormat, match="not decoded"):
        importers.read_file(str(path))

    described = importers.describe_file(str(path))
    assert "Glass Meridian" in described
    assert "Amber Drift" in described


def _all_data_frame(
    perf1: bytes, perf2: bytes, patch1: bytes, patch2: bytes, ws: bytes
) -> bytes:
    data = bytes(1813) + perf1 + perf2 + patch1 + patch2 + ws + ws
    nibbles = list(m.to_nibbles(data))
    return m.frame(m.Function.ALL_DATA_DUMP, nibbles + [m.checksum(nibbles)])


def test_all_data_reads_ram1_and_ram2(tmp_path):
    perf1 = b"".join(
        [make_performance("Glass Meridian", ((0, 0),))]
        + [bytes(r.PERFORMANCE_SIZE)] * 49
    )
    perf2 = b"".join(
        [make_performance("Amber Drift", ((0, 0),))] + [bytes(r.PERFORMANCE_SIZE)] * 49
    )
    patch1 = b"".join([make_patch("Kelp Forest")] + [bytes(r.PATCH_SIZE)] * 34)
    patch2 = b"".join([make_patch("Tidal Glass")] + [bytes(r.PATCH_SIZE)] * 34)
    ws, _ = make_waveseq({0: [40, 41]})

    path = tmp_path / "all.syx"
    path.write_bytes(_all_data_frame(perf1, perf2, patch1, patch2, ws))

    out = importers.read_file(str(path))
    assert [bank.scope for bank in out] == ["RAM1", "RAM2"]
    for bank in out:
        assert len(bank.source.performances) == 50
        assert len(bank.source.patches) == 35
        assert bank.source.waveseq is not None
    assert m.decode_name(out[0].source.performances[0]) == "Glass Meridian"
    assert m.decode_name(out[1].source.performances[0]) == "Amber Drift"
    assert m.decode_name(out[0].source.patches[0]) == "Kelp Forest"
    assert m.decode_name(out[1].source.patches[0]) == "Tidal Glass"


def test_all_data_with_an_unknown_layout_is_refused(tmp_path):
    data = bytes(5000) + bytes(65488)
    nibbles = list(m.to_nibbles(data))
    path = tmp_path / "all.syx"
    path.write_bytes(m.frame(m.Function.ALL_DATA_DUMP, nibbles + [m.checksum(nibbles)]))
    with pytest.raises(importers.UnsupportedFormat, match="all-data"):
        importers.read_file(str(path))
