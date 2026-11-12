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

"""SysEx format reader for Wavestation files."""

from __future__ import annotations

from typing import List

from kws import messages as m

__all__ = [
    "read_syx_file",
    "read_syx_data",
    "looks_like_syx",
]


def read_syx_data(data: bytes) -> List[m.Packet]:
    """Every Wavestation message in a SysEx byte stream, in order.

    Foreign messages are skipped rather than raising: a file somebody
    assembled may carry other devices' data.
    """
    out: List[m.Packet] = []
    cursor = 0
    while True:
        start = data.find(b"\xf0", cursor)
        if start < 0:
            break
        end = data.find(b"\xf7", start)
        if end < 0:
            break
        packet = m.parse(data[start : end + 1])
        if packet is not None:
            out.append(packet)
        cursor = end + 1
    return out


def read_syx_file(path: str) -> List[m.Packet]:
    """Every Wavestation message in a ``.syx`` file, in order."""
    with open(path, "rb") as handle:
        raw = handle.read()
    return read_syx_data(raw)


def looks_like_syx(data: bytes) -> bool:
    """Quick check if data looks like SysEx (starts with F0)."""
    return data.startswith(b"\xf0")
