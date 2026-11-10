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

"""Reading banks from files into something ``kws.perfcopy`` can consume.

A file may hold several banks; each becomes one :class:`ImportedBank`. The
SysEx and MIDI readers share one assembler, so a bank reads the same
regardless of how it was carried.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from kws import banks, deps, dumps, multi
from kws import messages as m
from kws import records as rec

__all__ = [
    "ImportedBank",
    "UnsupportedFormat",
    "read_file",
    "describe_file",
]


@dataclass(frozen=True)
class ImportedBank:
    """One bank group read from a file."""

    label: str
    scope: str
    format: str
    source: deps.Source
    multisets: Tuple[multi.Multiset, ...] = ()
    notes: str = ""


class UnsupportedFormat(ValueError):
    """A file this reader cannot decode."""


def _empty_perf() -> bytes:
    return bytes(dumps.PERFORMANCE_RECORD)


def _empty_patch() -> bytes:
    return bytes(dumps.PATCH_RECORD)


def _read_vlq(data: bytes, index: int) -> Tuple[int, int]:
    value = 0
    while index < len(data):
        byte = data[index]
        index += 1
        value = (value << 7) | (byte & 0x7F)
        if not (byte & 0x80):
            break
    return value, index


def read_midi_file(path: str) -> List[m.Packet]:
    """Extract Wavestation SysEx messages from a Standard MIDI File."""
    with open(path, "rb") as handle:
        raw = handle.read()
    if not raw.startswith(b"MThd"):
        raise UnsupportedFormat("not a Standard MIDI File (no MThd header)")
    if len(raw) < 14:
        raise UnsupportedFormat("truncated MIDI header")
    header_len = int.from_bytes(raw[4:8], "big")
    if header_len < 6:
        raise UnsupportedFormat("truncated MIDI header")
    ntrks = int.from_bytes(raw[10:12], "big")
    pos = 8 + header_len
    out: List[m.Packet] = []
    for _track in range(ntrks):
        if pos + 8 > len(raw) or raw[pos : pos + 4] != b"MTrk":
            raise UnsupportedFormat("missing MTrk chunk")
        trk_len = int.from_bytes(raw[pos + 4 : pos + 8], "big")
        trk_start = pos + 8
        trk_end = trk_start + trk_len
        if trk_end > len(raw):
            raise UnsupportedFormat("truncated MTrk chunk")
        track = raw[trk_start:trk_end]
        pos = trk_end
        _parse_track(track, out)
    return out


def _parse_track(track: bytes, out: List[m.Packet]) -> None:
    i = 0
    running_status = 0
    n = len(track)
    while i < n:
        _delta, i = _read_vlq(track, i)
        if i >= n:
            break
        status = track[i]
        i, running_status = _handle_event(track, i, status, running_status, out, n)


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


def _is_ignored(fn: int) -> bool:
    return fn in (
        m.Function.PARAMETER_CHANGE,
        0x60,
        m.Function.MULTI_SELECT,
        m.Function.MULTI_SELECT_SR,
        m.Function.WRITE_COMPLETED,
        m.Function.WRITE_ERROR,
        m.Function.DATA_LOAD_COMPLETED,
        m.Function.DATA_LOAD_ERROR,
    )


def _wire_byte(pkt: m.Packet) -> Optional[int]:
    if pkt.function in (m.Function.PERFORMANCE_DUMP, m.Function.SINGLE_PATCH_DUMP):
        if not pkt.payload:
            return None
        return pkt.payload[0]
    return pkt.bank


def _make_group(card: bool, wire: int, groups: List[dict[str, Any]]) -> dict[str, Any]:
    if card:
        card_count = sum(1 for x in groups if x.get("card"))
        scope = f"CARD/import{card_count}"
    else:
        scope = banks.by_sysex(wire).id
    return {
        "card": card,
        "wire": wire,
        "scope": scope,
        "perfs": [None] * banks.PERFORMANCES_PER_BANK,
        "patches": [None] * banks.PATCHES_PER_BANK,
        "waveseq": None,
        "has_perf": False,
        "has_patch": False,
        "has_ws": False,
        "funcs": set(),
    }


def _select_group(
    wire: int,
    fn: int,
    groups: List[dict[str, Any]],
    group_by_wire: dict[int, dict[str, Any]],
    current_card: Optional[dict[str, Any]],
) -> Tuple[dict[str, Any], Optional[dict[str, Any]]]:
    is_card = wire == banks.bank("CARD").sysex
    if is_card:
        g = current_card
        if g is None or fn in g["funcs"]:
            g = _make_group(True, wire, groups)
            groups.append(g)
            current_card = g
        return g, current_card
    g = group_by_wire.get(wire)
    if g is None:
        g = _make_group(False, wire, groups)
        groups.append(g)
        group_by_wire[wire] = g
    return g, current_card


def _merge_packet(
    pkt: m.Packet,
    groups: List[dict[str, Any]],
    group_by_wire: dict[int, dict[str, Any]],
    current_card: Optional[dict[str, Any]],
    multiset_banks: List[ImportedBank],
    basename: str,
    fmt: str,
) -> Optional[dict[str, Any]]:
    fn = pkt.function
    if _is_ignored(fn):
        return current_card
    if fn == m.Function.MULTISET_DUMP:
        data = pkt.data()
        ms_list = multi.parse(data)
        scope = "multi"
        label = f"{basename} [{scope}]"
        src = deps.Source(bank_id=scope, performances=[], patches=[], waveseq=None)
        multiset_banks.append(
            ImportedBank(
                label=label,
                scope=scope,
                format=fmt,
                source=src,
                multisets=tuple(ms_list),
            )
        )
        return current_card
    wire = _wire_byte(pkt)
    if wire is None:
        return current_card
    if wire not in {b.sysex for b in banks.BANKS}:
        return current_card

    g, current_card = _select_group(wire, fn, groups, group_by_wire, current_card)

    _apply_dump(g, pkt, fn)
    g["funcs"].add(fn)
    return current_card


def _apply_dump(g: dict[str, Any], pkt: m.Packet, fn: int) -> None:
    bulk = (
        m.Function.ALL_PERFORMANCE_DUMP,
        m.Function.ALL_PATCH_DUMP,
        m.Function.ALL_WAVESEQ_DUMP,
    )
    if fn in bulk:
        _apply_bulk(g, pkt, fn)
        return
    _apply_single(g, pkt, fn)


def _apply_bulk(g: dict[str, Any], pkt: m.Packet, fn: int) -> None:
    if fn == m.Function.ALL_PERFORMANCE_DUMP:
        dump = dumps.parse_bulk(pkt)
        for idx, rec_bytes in enumerate(dump.records):
            g["perfs"][idx] = rec_bytes
        g["has_perf"] = True
    elif fn == m.Function.ALL_PATCH_DUMP:
        dump = dumps.parse_bulk(pkt)
        for idx, rec_bytes in enumerate(dump.records):
            g["patches"][idx] = rec_bytes
        g["has_patch"] = True
    else:
        data = pkt.data()
        if len(data) >= rec.WAVESEQ_BANK_SIZE:
            g["waveseq"] = data[: rec.WAVESEQ_BANK_SIZE]
        else:
            g["waveseq"] = data if data else None
        g["has_ws"] = True


def _apply_single(g: dict[str, Any], pkt: m.Packet, fn: int) -> None:
    if fn == m.Function.PERFORMANCE_DUMP:
        rec_bytes = dumps.parse_performance(pkt)
        slot = pkt.payload[1]
        if 0 <= slot < banks.PERFORMANCES_PER_BANK:
            g["perfs"][slot] = rec_bytes
            g["has_perf"] = True
    elif fn == m.Function.SINGLE_PATCH_DUMP:
        rec_bytes = dumps.parse_patch(pkt)
        slot = pkt.payload[1]
        if 0 <= slot < banks.PATCHES_PER_BANK:
            g["patches"][slot] = rec_bytes
            g["has_patch"] = True


def _expand_all_data(packet: m.Packet) -> List[m.Packet]:
    try:
        data = m.from_nibbles(packet.payload)
    except ValueError as exc:
        raise UnsupportedFormat("all-data dump could not be decoded") from exc
    prefix = len(data) - 2 * (50 * 181 + 35 * 426 + 8784)
    if not (0 <= prefix <= 4096):
        raise UnsupportedFormat(
            "all-data dump does not match the 2-bank (RAM1/RAM2) layout: "
            f"prefix {prefix} is outside 0..4096; only the two-bank all-data "
            "structure is recognised"
        )
    perf_size = 50 * 181
    patch_size = 35 * 426
    ws_size = 8784
    off = prefix
    perf1 = data[off : off + perf_size]
    off += perf_size
    perf2 = data[off : off + perf_size]
    off += perf_size
    patch1 = data[off : off + patch_size]
    off += patch_size
    patch2 = data[off : off + patch_size]
    off += patch_size
    ws1 = data[off : off + ws_size]
    off += ws_size
    ws2 = data[off : off + ws_size]
    off += ws_size

    out: List[m.Packet] = []
    specs = (
        (m.Function.ALL_PERFORMANCE_DUMP, 0, perf1),
        (m.Function.ALL_PERFORMANCE_DUMP, 1, perf2),
        (m.Function.ALL_PATCH_DUMP, 0, patch1),
        (m.Function.ALL_PATCH_DUMP, 1, patch2),
        (m.Function.ALL_WAVESEQ_DUMP, 0, ws1),
        (m.Function.ALL_WAVESEQ_DUMP, 1, ws2),
    )
    for fn, wire, block in specs:
        parsed = m.parse(m.frame_bulk(fn, wire, block))
        if parsed is not None:
            out.append(parsed)
    return out


def _expand_packets(packets: List[m.Packet]) -> List[m.Packet]:
    out: List[m.Packet] = []
    for pkt in packets:
        if pkt.function == m.Function.ALL_DATA_DUMP:
            out.extend(_expand_all_data(pkt))
        else:
            out.append(pkt)
    return out


def _collect_groups(
    packets: List[m.Packet], basename: str, fmt: str
) -> Tuple[List[dict[str, Any]], List[ImportedBank]]:
    groups: List[dict[str, Any]] = []
    group_by_wire: dict[int, dict[str, Any]] = {}
    current_card: Optional[dict[str, Any]] = None
    multiset_banks: List[ImportedBank] = []
    for pkt in _expand_packets(packets):
        current_card = _merge_packet(
            pkt, groups, group_by_wire, current_card, multiset_banks, basename, fmt
        )
    return groups, multiset_banks


def _handle_event(
    track: bytes, i: int, status: int, running_status: int, out: List[m.Packet], n: int
) -> Tuple[int, int]:
    if status in (0xF0, 0xF7):
        i += 1
        length, i = _read_vlq(track, i)
        msg_data = track[i : i + length]
        i += length
        sysex = msg_data if msg_data.startswith(b"\xf0") else b"\xf0" + msg_data
        if not sysex.endswith(b"\xf7"):
            sysex = sysex + b"\xf7"
        packet = m.parse(sysex)
        if packet is not None:
            out.append(packet)
        return i, 0
    if status == 0xFF:
        i += 1
        if i >= n:
            return i, running_status
        i += 1
        length, i = _read_vlq(track, i)
        i += length
        return i, 0
    if status & 0x80:
        running_status = status
        i += 1
        if running_status & 0xF0 in (0xC0, 0xD0):
            i += 1
        else:
            i += 2
        return i, running_status
    if running_status and (running_status & 0xF0) not in (0xF0,):
        if running_status & 0xF0 in (0xC0, 0xD0):
            i += 1
        else:
            i += 2
        return i, running_status
    i += 1
    return i, running_status


def _assemble(packets: List[m.Packet], path: str, fmt: str) -> List[ImportedBank]:
    basename = os.path.basename(path)
    # ordered internal/card groups
    groups, multiset_banks = _collect_groups(packets, basename, fmt)
    # build group banks
    result: List[ImportedBank] = []
    for g in groups:
        if g["has_perf"]:
            perfs = [(r if r is not None else _empty_perf()) for r in g["perfs"]]
        else:
            perfs = []
        if g["has_patch"]:
            patches = [(r if r is not None else _empty_patch()) for r in g["patches"]]
        else:
            patches = []
        ws = g["waveseq"] if g["has_ws"] else None
        src = deps.Source(
            bank_id=g["scope"],
            performances=perfs,
            patches=patches,
            waveseq=ws,
        )
        label = f"{basename} [{g['scope']}]"
        result.append(
            ImportedBank(
                label=label,
                scope=g["scope"],
                format=fmt,
                source=src,
                multisets=(),
            )
        )

    # append multiset banks (they were collected; place after groups is acceptable)
    result.extend(multiset_banks)
    return result


def _read_sqs_packets(path: str) -> Tuple[List[m.Packet], str]:
    from kws import soundquest

    try:
        return soundquest.read_sqs(path), "sqs"
    except (soundquest.SoundQuestError, ValueError) as exc:
        raise UnsupportedFormat(str(exc)) from exc


def _load_packets(path: str, ext: str) -> Tuple[List[m.Packet], str]:
    if ext in (".syx", ".sysex"):
        return dumps.read_syx_file(path), "sysex"
    if ext in (".mid", ".midi"):
        return read_midi_file(path), "midi"
    if ext == ".sqs":
        return _read_sqs_packets(path)
    with open(path, "rb") as handle:
        raw = handle.read()
    if not raw:
        raise UnsupportedFormat("empty file")
    sniffed = _sniff_format(raw)
    if sniffed == "sqs":
        return _read_sqs_packets(path)
    if sniffed == "midi":
        return read_midi_file(path), "midi"
    if sniffed == "sysex":
        return dumps.read_syx_file(path), "sysex"
    raise UnsupportedFormat(f"unsupported format {sniffed}")


def read_file(path: str) -> List[ImportedBank]:
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, "rb") as handle:
            head = handle.read(4)
    except OSError:
        head = b""
    if ext == ".lib" or head == b"MROF":
        from kws import sounddiver

        try:
            lib_banks = sounddiver.read_banks(path)
        except (sounddiver.SoundDiverError, ValueError) as exc:
            raise UnsupportedFormat(str(exc)) from exc
        return [
            ImportedBank(
                label=f"{os.path.basename(path)} [{bank.scope}]",
                scope=bank.scope,
                format="lib",
                source=deps.Source(
                    bank_id=bank.scope,
                    performances=list(bank.performances),
                    patches=list(bank.patches),
                    waveseq=bank.waveseq,
                ),
                notes=bank.note,
            )
            for bank in lib_banks
        ]
    packets, fmt = _load_packets(path, ext)
    if not packets:
        raise UnsupportedFormat("no Wavestation messages found")
    return _assemble(packets, path, fmt)


def describe_file(path: str) -> str:
    """Return a human-readable summary of ``path`` as a string."""
    ext = os.path.splitext(path)[1].lower()
    sniffed = None
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
        sniffed = _sniff_format(raw)
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
    lines = []
    for ib in banks_list:
        lines.append(ib.label)
        if ib.notes:
            lines.append(f"  note: {ib.notes}")
        perfs = ib.source.performances
        for slot, perf in enumerate(perfs):
            if not perf:
                continue
            if not any(perf):
                continue
            name = m.decode_name(perf)
            if name.strip():
                lines.append(f"  {slot}: {name}")
    return "\n".join(lines)
