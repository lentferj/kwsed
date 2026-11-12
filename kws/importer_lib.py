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

"""SoundDiver .LIB file reader for Wavestation banks."""

from __future__ import annotations

from typing import List

from kws import sounddiver
from kws.exceptions import UnsupportedFormat
from kws.types import ImportedBank

__all__ = [
    "read_lib_file",
    "looks_like_lib",
]


def read_lib_file(path: str) -> List[ImportedBank]:
    """Read Wavestation banks from a SoundDiver .LIB file."""
    try:
        lib_banks = sounddiver.read_banks(path)
    except (sounddiver.SoundDiverError, ValueError) as exc:
        raise UnsupportedFormat(str(exc)) from exc

    import os

    from kws import deps

    result: List[ImportedBank] = []
    for bank in lib_banks:
        result.append(
            ImportedBank(
                label=f"{os.path.basename(path)} [{bank.scope}]",
                scope=bank.scope,
                format="lib",
                source=deps.Source(
                    scope_id=bank.scope,
                    performances=list(bank.performances),
                    patches=list(bank.patches),
                    waveseq=bank.waveseq,
                ),
                notes=bank.note,
            )
        )
    return result


def looks_like_lib(data: bytes) -> bool:
    """Quick check if data looks like a SoundDiver .LIB file (MROF)."""
    return data.startswith(b"MROF")
