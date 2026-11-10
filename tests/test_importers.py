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

from kws import banks, deps, dumps, importers, multi, records
from kws import messages as m
from kws import records as r
from kws import sounddiver as sounddiver_mod
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


def _lib_length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    return bytes([0x80 | (length >> 7), length & 0x7F])


def _lib_entry(
    index: int,
    name: str,
    location: str = "1",
    data: bytes = b"\x00" * 8,
    comment: str | None = None,
) -> bytes:
    if comment is None:
        comment = f"RAM1-{index:02d}"
    encoded_comment = comment.encode("ascii")
    blocks = (
        b"\x01"
        + _lib_length(len(name))
        + name.encode("ascii")
        + b"\x02"
        + _lib_length(len(location))
        + location.encode("ascii")
        + b"\x03"
        + _lib_length(len(data))
        + data
        + b"\x06"
        + _lib_length(len(encoded_comment))
        + encoded_comment
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

    with pytest.raises(importers.UnsupportedFormat, match="no decodable"):
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


def _lib_patch_data(raw: bytes, oscs: tuple[int, ...] = (0, 1, 2, 3)) -> bytes:
    """Encode a bank patch the way the SoundDiver adaptation does."""
    from kws import sounddiver

    data = bytearray(74 + 84 * len(oscs))
    data[2:72] = raw[16:86]
    data[0:2] = raw[86:88]
    data[72:74] = raw[88:90]
    for stored, raw_osc in enumerate(oscs):
        for raw_offset, lib_offset in enumerate(sounddiver._OSC_PERMUTATION):
            data[74 + stored * 84 + lib_offset] = raw[90 + raw_osc * 84 + raw_offset]
    return bytes(data)


def _lib_performance_data(raw: bytes, used: int) -> bytes:
    """Encode a bank performance: common, used parts, unused placeholders."""
    common = raw[16:37]
    data = bytearray(37 + 16 * used)
    data[0] = common[2]
    data[1] = common[3]
    data[2] = common[4]
    data[3] = common[0]
    data[4] = common[5]
    data[5:12] = common[6:13]
    data[12] = common[1]
    data[13:21] = common[13:21]
    pos = 21
    for part in range(8):
        at = 37 + part * 18
        if part < used:
            data[pos : pos + 18] = raw[at : at + 18]
            pos += 18
        else:
            data[pos : pos + 2] = raw[at : at + 2]
            pos += 2
    return bytes(data)


def test_lib_patch_decodes_into_its_comment_slot(tmp_path):
    raw = make_patch("Kelp Forest")
    perf = make_performance("Glass Meridian", ((0, 3), (0, 4)))
    path = tmp_path / "patches.lib"
    path.write_bytes(
        _lib_file(
            _lib_entry(
                0,
                "Kelp Forest",
                data=_lib_patch_data(raw),
                comment="RAM1-03",
            )
            + _lib_entry(
                1,
                "Glass Meridian",
                data=_lib_performance_data(perf, 2),
                comment="RAM1-04",
            )
        )
    )

    out = importers.read_file(str(path))
    assert len(out) == 1
    bank = out[0]
    assert bank.scope == "RAM1"
    assert m.decode_name(bank.source.patches[3]) == "Kelp Forest"
    assert bank.source.patches[3][16:426] == raw[16:426]
    assert bank.source.patches[0] == bytes(r.PATCH_SIZE)
    assert m.decode_name(bank.source.performances[4]) == "Glass Meridian"
    assert "1 patch and 1 performance decoded" in bank.notes


def test_lib_one_and_two_oscillator_patches(tmp_path):
    one = make_patch("Kelp Forest")
    two = make_patch("Amber Drift")
    path = tmp_path / "oscillators.lib"
    path.write_bytes(
        _lib_file(
            _lib_entry(
                0, "Kelp Forest", data=_lib_patch_data(one, (0,)), comment="RAM1-00"
            )
            + _lib_entry(
                1,
                "Amber Drift",
                data=_lib_patch_data(two, (0, 2)),
                comment="RAM1-01",
            )
        )
    )

    bank = importers.read_file(str(path))[0]
    first = bank.source.patches[0]
    second = bank.source.patches[1]
    assert first[16:90] == one[16:90]
    assert first[90:174] == one[90:174]
    assert not any(first[174:])
    assert second[16:90] == two[16:90]
    assert second[90:174] == two[90:174]
    assert second[258:342] == two[258:342]
    assert not any(second[174:258])
    assert not any(second[342:])


def test_lib_performance_decodes_used_parts_and_placeholders(tmp_path):
    raw = bytearray(make_performance("Glass Meridian", ((0, 3), (0, 4))))
    raw[16:37] = bytes(range(21))
    path = tmp_path / "performances.lib"
    path.write_bytes(
        _lib_file(
            _lib_entry(
                0,
                "Glass Meridian",
                data=_lib_performance_data(bytes(raw), 2),
                comment="RAM1-07",
            )
        )
    )

    bank = importers.read_file(str(path))[0]
    decoded = bank.source.performances[7]
    assert m.decode_name(decoded) == "Glass Meridian"
    assert decoded[16:37] == raw[16:37]
    assert decoded[37:55] == raw[37:55]
    assert decoded[55:73] == raw[55:73]
    for part in range(2, 8):
        at = 37 + part * 18
        assert decoded[at : at + 2] == raw[at : at + 2]
        assert not any(decoded[at + 2 : at + 18])


def _lib_sequence_data(raw_bank: bytes, slot: int) -> bytes:
    """Encode one raw sequence the way the LIB stores it (relative links)."""
    header = bytearray(
        raw_bank[records.SEQ_BASE + slot * 16 : records.SEQ_BASE + (slot + 1) * 16]
    )
    start = records.sequence_header(raw_bank, slot)["link"]
    chain = []
    step = start
    seen = set()
    while 0 < step < records.STEPS_PER_BANK and step not in seen:
        seen.add(step)
        content = raw_bank[
            records.STEP_BASE + step * 16 : records.STEP_BASE + (step + 1) * 16
        ]
        chain.append(bytearray(content))
        step = (content[0] << 8) | content[1]
    data = bytearray(16 + 16 * len(chain))
    data[:16] = header
    data[0:2] = len(chain).to_bytes(2, "big")
    for index, step_bytes in enumerate(chain):
        forward = index + 1 if index + 1 < len(chain) else 0
        back = index if index else 0
        offset = 16 + index * 16
        data[offset : offset + 16] = step_bytes
        data[offset : offset + 2] = forward.to_bytes(2, "big")
        data[offset + 2 : offset + 4] = back.to_bytes(2, "big")
    return bytes(data)


def test_lib_wave_sequences_are_assembled(tmp_path):
    raw, starts = make_waveseq({0: [40, 41], 1: [50, 51, 52], 2: [60]})
    path = tmp_path / "sequences.lib"
    path.write_bytes(
        _lib_file(
            b"".join(
                _lib_entry(
                    slot,
                    records.sequence_names(raw)[slot] or f"Seq{slot}",
                    data=_lib_sequence_data(raw, slot),
                    comment=f"RAM1-{slot:02d}",
                )
                for slot in range(3)
            )
        )
    )
    entries = sounddiver_mod.read_entries(str(path))
    bank = sounddiver_mod.build_waveseq(entries, "RAM1")
    assert bank is not None
    assert records.sequence_names(bank)[:3] == records.sequence_names(raw)[:3]
    for slot in range(3):
        header = records.sequence_header(bank, slot)
        step = header["link"]
        chain = []
        seen = set()
        while 0 < step < records.STEPS_PER_BANK and step not in seen:
            seen.add(step)
            data = bank[
                records.STEP_BASE + step * 16 : records.STEP_BASE + (step + 1) * 16
            ]
            chain.append(data[6:16])
            step = (data[0] << 8) | data[1]
        raw_start = starts[slot]
        raw_chain = []
        seen = set()
        while 0 < raw_start < records.STEPS_PER_BANK and raw_start not in seen:
            seen.add(raw_start)
            data = raw[
                records.STEP_BASE + raw_start * 16 : records.STEP_BASE
                + (raw_start + 1) * 16
            ]
            raw_chain.append(data[6:16])
            raw_start = (data[0] << 8) | data[1]
        assert chain == raw_chain
        assert header["loop_start"] == records.sequence_header(raw, slot)["loop_start"]
        assert header["loop_end"] == records.sequence_header(raw, slot)["loop_end"]
        assert header["loop_count"] == records.sequence_header(raw, slot)["loop_count"]


def test_lib_wave_sequences_absent_for_a_scope(tmp_path):
    raw, _ = make_waveseq({0: [40]})
    path = tmp_path / "sequences.lib"
    path.write_bytes(
        _lib_file(
            _lib_entry(
                0,
                records.sequence_names(raw)[0] or "Seq0",
                data=_lib_sequence_data(raw, 0),
                comment="RAM1-00",
            )
        )
    )
    entries = sounddiver_mod.read_entries(str(path))
    assert sounddiver_mod.build_waveseq(entries, "RAM2") is None


def test_lib_wave_sequence_step_budget_is_enforced():
    entry = sounddiver_mod.LibEntry(
        index=0,
        name="Too Many",
        location="",
        comment="RAM1-00",
        header=bytes(12),
        data=bytes(16 + 16 * 16),
        uses=b"",
    )
    entries = [
        sounddiver_mod.LibEntry(
            index=index,
            name=entry.name,
            location=entry.location,
            comment=f"RAM1-{index:02d}",
            header=entry.header,
            data=entry.data,
            uses=entry.uses,
        )
        for index in range(32)
    ]
    with pytest.raises(sounddiver_mod.SoundDiverError, match="budget"):
        sounddiver_mod.build_waveseq(entries, "RAM1")


def test_lib_patch_performance_and_sequence_end_to_end(tmp_path):
    from tests.test_bankbuild import make_patch, make_performance, make_waveseq

    raw_patch = make_patch("Kelp Forest", ((0, 0), (0, 40), (0, 40), (0, 40)))
    raw_perf = make_performance("Glass Meridian", parts=((0, 0),))
    raw_ws, _ = make_waveseq({0: [40, 41]})

    path = tmp_path / "endtoend.lib"
    path.write_bytes(
        _lib_file(
            _lib_entry(
                0,
                "Kelp Forest",
                data=_lib_patch_data(raw_patch),
                comment="RAM1-00",
            )
            + _lib_entry(
                0,
                "Glass Meridian",
                data=_lib_performance_data(raw_perf, 1),
                comment="RAM1-00",
            )
            + _lib_entry(
                0,
                records.sequence_names(raw_ws)[0] or "Seq0",
                data=_lib_sequence_data(raw_ws, 0),
                comment="RAM1-00",
            )
        )
    )

    out = importers.read_file(str(path))
    assert len(out) == 1
    assert out[0].source.waveseq is not None
    assert len(out[0].source.waveseq) == records.WAVESEQ_BANK_SIZE


def _disk_bank_bytes(name: str = "Glass Meridian") -> bytes:
    ws, _ = make_waveseq({0: [40, 41]})
    perf_rows = b"".join(
        [make_performance(name, ((0, 0),))] + [bytes(r.PERFORMANCE_SIZE)] * 49
    )
    patch_rows = b"".join(
        make_patch("Kelp Forest") for _ in range(banks.PATCHES_PER_BANK)
    )
    return (
        m.frame_bulk(m.Function.ALL_PERFORMANCE_DUMP, 0, perf_rows)
        + m.frame_bulk(m.Function.ALL_PATCH_DUMP, 0, patch_rows)
        + m.frame_bulk(m.Function.ALL_WAVESEQ_DUMP, 0, ws)
    )


def test_fat12_image_bank_is_imported(tmp_path):
    from tests.test_fat12 import make_fat12_image

    path = tmp_path / "disk.img"
    path.write_bytes(
        make_fat12_image({"TEST.SYX": _disk_bank_bytes(), "README.TXT": b"hi"})
    )

    out = importers.read_file(str(path))
    assert len(out) == 1
    assert out[0].format == "img"
    assert out[0].scope == "RAM1"
    assert out[0].label == "disk.img / TEST.SYX [RAM1]"
    assert len(out[0].source.performances) == 50
    assert len(out[0].source.patches) == 35
    assert out[0].source.waveseq is not None


def test_zip_reads_images_and_direct_sysex_and_skips_flux(tmp_path):
    import zipfile

    from tests.test_fat12 import make_fat12_image

    path = tmp_path / "set.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("disk.img", make_fat12_image({"TEST.SYX": _disk_bank_bytes()}))
        archive.writestr("direct.syx", _disk_bank_bytes("Amber Drift"))
        archive.writestr("flux.7z", b"7z\xbc\xaf\x27\x1c not real flux")
        archive.writestr("notes.txt", b"ignore")

    out = importers.read_file(str(path))
    labels = {bank.label for bank in out}
    assert "set.zip / disk.img / TEST.SYX [RAM1]" in labels
    assert "set.zip / direct.syx [RAM1]" in labels
    assert len(out) == 2


def test_bnk_bundle_with_a_prefix_is_read_as_sysex(tmp_path):
    path = tmp_path / "bank.bnk"
    path.write_bytes(b"FR" + _disk_bank_bytes())
    out = importers.read_file(str(path))
    assert len(out) == 1
    assert out[0].scope == "RAM1"
    assert m.decode_name(out[0].source.performances[0]) == "Glass Meridian"


def test_flux_archive_is_refused(tmp_path):
    path = tmp_path / "flux.7z"
    path.write_bytes(b"7z\xbc\xaf\x27\x1c0000")
    with pytest.raises(importers.UnsupportedFormat, match="flux"):
        importers.read_file(str(path))


def test_fat12_image_without_banks_is_refused(tmp_path):
    from tests.test_fat12 import make_fat12_image

    path = tmp_path / "empty.img"
    path.write_bytes(make_fat12_image({"README.TXT": b"nothing"}))
    with pytest.raises(importers.UnsupportedFormat, match="no Wavestation"):
        importers.read_file(str(path))
