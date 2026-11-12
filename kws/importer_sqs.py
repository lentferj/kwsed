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

"""SoundQuest/MidiQuest .sqs file reader for Wavestation SysEx messages."""

from __future__ import annotations

from typing import List

from kws import messages as m
from kws.exceptions import UnsupportedFormat

__all__ = [
    "read_sqs_file",
    "looks_like_sqs",
]


def read_sqs_file(path: str) -> List[m.Packet]:
    """Read Wavestation SysEx messages from a SoundQuest .sqs file."""
    from kws import soundquest

    try:
        return soundquest.read_sqs(path)
    except (soundquest.SoundQuestError, ValueError) as exc:
        raise UnsupportedFormat(str(exc)) from exc


def looks_like_sqs(data: bytes) -> bool:
    """Quick check if data looks like a SoundQuest .sqs file (LIST/FORM)."""
    head = data[:8]
    return head.startswith(b"LIST") or head.startswith(b"FORM")
