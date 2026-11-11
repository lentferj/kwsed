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

"""Container reader for SoundQuest ``.sqs`` files.

Only the IFF container is interpreted here; the Ensoniq record
reconstruction from the reference implementation is deliberately not
ported, because the Wavestation payload is Korg SysEx rather than Ensoniq
records.  Bodies that carry Korg SysEx are unwrapped into individual
messages; bodies that do not are refused.
"""

from __future__ import annotations

from typing import List

from kws import messages as m

__all__ = ["SoundQuestError", "UnsupportedBody", "read_sqs"]


class SoundQuestError(ValueError):
    """Base class for SoundQuest container errors."""


class UnsupportedBody(SoundQuestError):
    """A body this reader cannot decode."""


_KORG_HEADER = b"\x42\x30\x28"


def _find_forms(data: bytes) -> List[int]:
    offsets: List[int] = []
    pos = 0
    while True:
        index = data.find(b"FORM", pos)
        if index < 0:
            break
        if index + 12 <= len(data) and data[index + 8 : index + 12] == b"BANK":
            offsets.append(index)
        pos = index + 4
    return offsets


def _extract_messages(body: bytes) -> List[bytes]:
    work = body
    if work.startswith(_KORG_HEADER):
        work = b"\xf0" + work
    if not work.startswith(b"\xf0\x42"):
        return []
    if not work.endswith(b"\xf7"):
        work = work + b"\xf7"
    messages: List[bytes] = []
    i = 0
    n = len(work)
    while i < n:
        if work[i] != 0xF0:
            i += 1
            continue
        j = work.find(0xF7, i)
        if j < 0:
            break
        messages.append(work[i : j + 1])
        i = j + 1
    return messages


def _handle_body(content: bytes, collected: List[m.Packet]) -> None:
    if not (content.startswith(b"\xf0\x42") or content.startswith(_KORG_HEADER)):
        # A native MidiQuest body may sit beside SysEx bodies. Skip it; the
        # caller refuses only when no SysEx body was found at all.
        return
    for msg_bytes in _extract_messages(content):
        packet = m.parse(msg_bytes)
        if packet is not None:
            collected.append(packet)


def _walk_form(
    data: bytes, form_offset: int, bound: int, collected: List[m.Packet]
) -> None:
    p = form_offset + 12
    while p < bound:
        # skip to a chunk header (id + size)
        while p < bound and p + 8 > bound:
            break
        if p + 8 > bound:
            break
        if not all(32 <= c < 127 for c in data[p : p + 4]):
            # not a chunk id; advance
            p += 1
            continue
        chunk_id = data[p : p + 4]
        chunk_size = int.from_bytes(data[p + 4 : p + 8], "big")
        content_start = p + 8
        content_end = content_start + chunk_size
        if content_end > bound or content_end > len(data):
            raise ValueError("truncated chunk body in .sqs file")
        content = data[content_start:content_end]
        if chunk_id == b"BODY":
            _handle_body(content, collected)
        padded = chunk_size + (chunk_size & 1)
        p = content_start + padded


def read_sqs(path: str) -> List[m.Packet]:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise ValueError(f"cannot read .sqs file: {exc}") from exc

    if not data:
        raise ValueError("empty .sqs file")
    if len(data) < 12 or data[:4] != b"LIST":
        raise ValueError("not a valid .sqs file: missing LIST container")
    list_size = int.from_bytes(data[4:8], "big")
    if list_size < 4 or 8 + list_size > len(data):
        raise ValueError("truncated LIST container in .sqs file")

    form_offsets = _find_forms(data)
    if not form_offsets:
        raise ValueError("no FORM ... BANK records found in .sqs file")

    collected: List[m.Packet] = []
    for fi, form_offset in enumerate(form_offsets):
        bound = form_offsets[fi + 1] if fi + 1 < len(form_offsets) else len(data)
        _walk_form(data, form_offset, bound, collected)

    if not collected:
        raise UnsupportedBody(
            "no Wavestation SysEx in .sqs file; MidiQuest-native bodies are "
            "not decoded yet"
        )
    return collected
