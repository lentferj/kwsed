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

"""Shared type definitions for kws modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from kws import deps, multi


@dataclass(frozen=True)
class ImportedBank:
    """One bank group read from a file."""

    label: str
    scope: str
    format: str
    source: deps.Source
    multisets: Tuple[multi.Multiset, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class LibBank:
    """The decodable patches, performances and wave sequences of one bank."""

    scope: str
    performances: List[bytes]
    patches: List[bytes]
    waveseq: Optional[bytes]
    note: str


@dataclass(frozen=True)
class LibEntry:
    index: int
    name: str
    location: str
    comment: str
    header: bytes
    data: bytes
    uses: bytes
