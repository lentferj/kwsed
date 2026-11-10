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

"""Container reader for SoundDiver ``.LIB`` files.

Only the IFF container and the ``LENT`` block layout are interpreted
here.  The Wavestation adaptation of the data block is not decoded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, NoReturn, cast

__all__ = ["LibEntry", "read_entries", "read_lib"]


@dataclass(frozen=True)
class LibEntry:
    index: int
    name: str
    location: str
    comment: str
    header: bytes
    data: bytes
    uses: bytes


def _decode_block_text(block: bytes) -> str:
    return block.decode("latin-1").replace("\x00", "")


def _apply_block(block_id: int, block_data: bytes, state: dict[str, object]) -> None:
    if block_id == 1:
        state["name"] = _decode_block_text(block_data)
    elif block_id == 2:
        state["location"] = _decode_block_text(block_data)
    elif block_id == 3:
        state["data"] = block_data
    elif block_id == 4:
        state["uses"] = block_data
    elif block_id == 6:
        state["comment"] = _decode_block_text(block_data)


def _parse_tnel(body: bytes, index: int) -> LibEntry:
    if len(body) < 12:
        raise ValueError("truncated TNEL body in .lib file")
    header = body[:12]
    rest = body[12:]

    state: dict[str, object] = {
        "name": "",
        "location": "",
        "comment": "",
        "data": b"",
        "uses": b"",
    }

    i = 0
    n = len(rest)
    while i < n:
        if i >= n:
            break
        block_id = rest[i]
        i += 1
        if block_id == 0:
            # End of data. The 1-2 bytes that may follow are not a length.
            break
        if i >= n:
            raise ValueError("truncated block header in .lib file")
        l1 = rest[i]
        i += 1
        if l1 & 0x80:
            if i >= n:
                raise ValueError("truncated block length in .lib file")
            l2 = rest[i]
            i += 1
            length = ((l1 & 0x7F) << 7) | l2
        else:
            length = l1
        if i + length > n:
            raise ValueError("block overflow in .lib file")
        block_data = rest[i : i + length]
        i += length
        _apply_block(block_id, block_data, state)

    return LibEntry(
        index=index,
        name=str(state["name"]),
        location=str(state["location"]),
        comment=str(state["comment"]),
        header=header,
        data=cast(bytes, state["data"]),
        uses=cast(bytes, state["uses"]),
    )


def read_entries(path: str) -> List[LibEntry]:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise ValueError(f"cannot read .lib file: {exc}") from exc

    if len(data) < 12:
        raise ValueError("not a valid .lib file: truncated header")
    if data[:4] != b"MROF":
        raise ValueError("not a valid .lib file: missing MROF container")

    size = int.from_bytes(data[4:8], "little")
    if size < 4 or 8 + size > len(data):
        raise ValueError("truncated MROF container in .lib file")
    if data[8:12] != b"BLSS":
        raise ValueError("not a valid .lib file: bad form type")

    bound = 8 + size
    entries: List[LibEntry] = []
    index = 0
    pos = 12
    while pos < bound:
        if pos + 8 > bound:
            raise ValueError("truncated chunk header in .lib file")
        chunk_id = data[pos : pos + 4]
        chunk_size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        content_start = pos + 8
        content_end = content_start + chunk_size
        if content_end > bound or content_end > len(data):
            raise ValueError("chunk overflow in .lib file")

        body = data[content_start:content_end]
        if chunk_id == b"TNEL":
            entries.append(_parse_tnel(body, index))
            index += 1

        pos = content_end

    return entries


def read_lib(path: str) -> NoReturn:
    try:
        read_entries(path)
    except (ValueError, OSError):
        pass
    from kws.importers import UnsupportedFormat

    raise UnsupportedFormat(
        "SoundDiver .LIB entries are read by name, but the Wavestation "
        "data block is not decoded; export the bank as .syx from "
        "SoundDiver, or provide the adaptation layout"
    )
