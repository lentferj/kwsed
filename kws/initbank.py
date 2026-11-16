# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# The embedded record bytes are the unit's own initialised state, read once
# from System Exclusive bank dumps the instrument produced and transcribed
# verbatim. No third-party code or data was used.
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

"""The unit's own Init bank, embedded so nothing reads it from a path.

A factory-initialised Wavestation SR fills its RAM banks with this state:
every performance slot holds one Init performance, every patch slot one Init
patch, and the wave-sequence bank is zeroed. kwsed builds blank banks from
that state, and carrying it as bytes literals keeps the app self-contained --
there is no reference dump on disk to find at runtime.

**Why literals rather than a resource file.** The bank's Init state is two
records (181 and 426 bytes) plus a zeroed pool; there is no 50- or 35-fold
data to package, so a ``kws/resources/`` file would add packaging
configuration, a loader and a importlib.resources indirection for less than
a kilobyte of payload. A literal needs none of that, cannot fail to be
found at runtime the way a data file next to an installed package can, and
is checked into review as source. What a literal loses -- the guard against
a silent edit of the bytes -- is pinned instead by digest tests
(``tests/test_initbank.py``), which assert the two records' SHA-256 digests
so a future edit cannot change the Init state without saying so.

The records were read once, during development, from SysEx bank dumps the
instrument itself produced, decoded with :func:`kws.importers.read_file`.
They are embedded verbatim below. Nothing here refers to where those dumps
live, and nothing at runtime opens a file.

``INIT_PERFORMANCE`` is one 181-byte record named ``Init``: part 0 points at
RAM 1 patch 0 and parts 1-7 are unused (patch 255). ``INIT_PATCH`` is one
426-byte record named ``Init``. A bank is 50 performances and 35 patches.
The wave-sequence bank is :data:`kws.records.WAVESEQ_BANK_SIZE` zero bytes,
which is an unused pool: no sequence headers, no steps and no names.
"""

from __future__ import annotations

from typing import Tuple

from kws import banks
from kws import records as rec

__all__ = [
    "INIT_PATCH",
    "INIT_PERFORMANCE",
    "cleared_bank",
    "init_patches",
    "init_performances",
    "init_waveseq",
]

#: One Init performance record, 181 bytes, verbatim from the unit.
INIT_PERFORMANCE: bytes = (
    b"\x49\x6e\x69\x74\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20"
    b"\x02\x82\x66\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x63\x32\x34\x00\x7f\x01\x7f\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\xff\x00\x32\x34\x00\x7f\x01\x7f"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\x00\x32\x34\x00\x7f"
    b"\x01\x7f\xe8\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\x00\x32\x34"
    b"\x00\x7f\x01\x7f\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\x00"
    b"\x32\x34\x00\x7f\x01\x7f\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\xff\x00\x32\x34\x00\x7f\x01\x7f\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\xff\x00\x32\x34\x00\x7f\x01\x7f\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\xff\x00\x32\x34\x00\x7f\x01\x7f\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00"
)

#: One Init patch record, 426 bytes, verbatim from the unit.
INIT_PATCH: bytes = (
    b"\x49\x6e\x69\x74\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20"
    b"\x00\x00\x00\x00\x00\x01\x00\x01\x00\x01\x00\x01\x00\x01\x00\x01"
    b"\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x7f\x7f\x7f\x7f\x7f\x7f\x7f\x7f\x7f\x7f\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x32"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x63\x63\x63\x00\x00\x00\x00\x00\x00"
    b"\x00\x63\x00\x63\xff\xff\xff\xff\xff\x02\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x63\x00\x00\x00"
    b"\x00\x00\x00\x63\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x32\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x63\x63\x63\x00\x00"
    b"\x00\x00\x00\x00\x00\x63\x00\x63\xff\xff\xff\xff\xff\x02\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x63\x00\x00\x00\x00\x00\x00\x63\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x32\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x63"
    b"\x63\x63\x00\x00\x00\x00\x00\x00\x00\x63\x00\x63\xff\xff\xff\xff"
    b"\xff\x02\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x63\x00\x00\x00\x00\x00\x00\x63\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x32\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x63\x63\x63\x00\x00\x00\x00\x00\x00\x00\x63\x00\x63"
    b"\xff\xff\xff\xff\xff\x02\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x63\x00\x00\x00\x00\x00\x00\x63"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
)


def init_performances() -> bytes:
    """The Init performance bank: 50 identical 181-byte records."""
    return INIT_PERFORMANCE * banks.PERFORMANCES_PER_BANK


def init_patches() -> bytes:
    """The Init patch bank: 35 identical 426-byte records."""
    return INIT_PATCH * banks.PATCHES_PER_BANK


def init_waveseq() -> bytes:
    """The Init wave-sequence bank: an all-zero 8784-byte pool."""
    return bytes(rec.WAVESEQ_BANK_SIZE)


def cleared_bank() -> Tuple[bytes, bytes, bytes]:
    """The whole cleared bank: performances, patches, wave sequences."""
    return (init_performances(), init_patches(), init_waveseq())
