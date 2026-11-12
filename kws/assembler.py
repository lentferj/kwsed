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

"""Shared bank assembly logic for converting SysEx packets to ImportedBank objects."""

from __future__ import annotations

import os
from typing import Any, List, Optional, Tuple

from kws import banks, deps, dumps, multi
from kws import messages as m
from kws import records as rec
from kws.types import ImportedBank

__all__ = [
    "assemble",
    "relabel_disk_banks",
]


def _empty_perf() -> bytes:
    return bytes(dumps.PERFORMANCE_RECORD)


def _empty_patch() -> bytes:
    return bytes(dumps.PATCH_RECORD)


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
        src = deps.Source(scope_id=scope, performances=[], patches=[], waveseq=None)
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
        from kws.exceptions import UnsupportedFormat

        raise UnsupportedFormat("all-data dump could not be decoded") from exc
    prefix = len(data) - 2 * (50 * 181 + 35 * 426 + 8784)
    if not (0 <= prefix <= 4096):
        from kws.exceptions import UnsupportedFormat

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


def assemble(packets: List[m.Packet], path: str, fmt: str) -> List[ImportedBank]:
    """Assemble packets into ImportedBank objects."""
    basename = os.path.basename(path)
    groups, multiset_banks = _collect_groups(packets, basename, fmt)
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
            scope_id=g["scope"],
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
    result.extend(multiset_banks)
    return result


def relabel_disk_banks(
    banks_list: List[ImportedBank], label: str, name: str
) -> List[ImportedBank]:
    """Relabel banks from disk/container members."""
    out: List[ImportedBank] = []
    for bank in banks_list:
        out.append(
            ImportedBank(
                label=f"{label} / {name} [{bank.scope}]",
                scope=bank.scope,
                format="img",
                source=bank.source,
                multisets=bank.multisets,
                notes=bank.notes,
            )
        )
    return out
