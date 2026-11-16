# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# The Init record this module installs is the unit's own initialised state,
# embedded in kws/initbank.py and transcribed from System Exclusive bank
# dumps the instrument produced. No third-party code or data was used.
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

"""Replacing performance slots with the unit's Init record.

Remediation is the operation behind clearing a slot: a whole bank comes in,
and the chosen performance records are replaced with
:data:`kws.initbank.INIT_PERFORMANCE`. Nothing else in the bank is touched,
and the bank is copied rather than rebuilt, so a slot nobody chose keeps its
exact bytes.
"""

from __future__ import annotations

from typing import Iterable, Tuple

from kws import banks, initbank

__all__ = [
    "cleared_bank",
    "init_performances_in",
]

_RECORD = len(initbank.INIT_PERFORMANCE)
_BANK_SIZE = banks.PERFORMANCES_PER_BANK * _RECORD


def cleared_bank() -> Tuple[bytes, bytes, bytes]:
    """The whole cleared bank: performances, patches, wave sequences."""
    return initbank.cleared_bank()


def init_performances_in(current: bytes, slots: Iterable[int]) -> bytes:
    """A copy of ``current`` with the named performance slots set to Init.

    ``current`` must be a whole performance bank, 50 records of 181 bytes.
    Slots are 0-49, may be given in any order and may repeat; a repeated slot
    is one replacement. Every byte of every other slot is left exactly as it
    was, and an empty ``slots`` returns the input unchanged.
    """
    if len(current) != _BANK_SIZE:
        raise ValueError(
            f"a performance bank is {_BANK_SIZE} bytes "
            f"({banks.PERFORMANCES_PER_BANK} records of {_RECORD}); "
            f"got {len(current)}"
        )
    chosen = set(slots)
    for slot in chosen:
        if not 0 <= slot < banks.PERFORMANCES_PER_BANK:
            raise ValueError(
                f"performance slot {slot} is outside "
                f"0-{banks.PERFORMANCES_PER_BANK - 1}"
            )
    if not chosen:
        return current
    out = bytearray(current)
    for slot in chosen:
        start = slot * _RECORD
        out[start : start + _RECORD] = initbank.INIT_PERFORMANCE
    return bytes(out)
