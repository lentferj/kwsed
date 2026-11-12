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

"""Standard MIDI file reader for Wavestation SysEx messages."""

from __future__ import annotations

from typing import List, Tuple

from kws import messages as m
from kws.exceptions import UnsupportedFormat

__all__ = [
    "read_midi_file",
    "read_midi_data",
    "looks_like_midi",
]


def _read_vlq(data: bytes, index: int) -> Tuple[int, int]:
    value = 0
    while index < len(data):
        byte = data[index]
        index += 1
        value = (value << 7) | (byte & 0x7F)
        if not (byte & 0x80):
            break
    return value, index


def read_midi_data(raw: bytes) -> List[m.Packet]:
    """Extract Wavestation SysEx messages from Standard MIDI File bytes."""
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


def read_midi_file(path: str) -> List[m.Packet]:
    """Extract Wavestation SysEx messages from a Standard MIDI File."""
    with open(path, "rb") as handle:
        raw = handle.read()
    return read_midi_data(raw)


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


def looks_like_midi(data: bytes) -> bool:
    """Quick check if data looks like a Standard MIDI File."""
    return data.startswith(b"MThd")
