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

"""Shared exceptions for kws modules."""

from __future__ import annotations


class UnsupportedFormat(ValueError):
    """A file this reader cannot decode."""


class SoundDiverError(ValueError):
    """A ``.LIB`` object this module cannot turn into a bank record."""


class SoundQuestError(ValueError):
    """Base class for SoundQuest container errors."""


class UnsupportedBody(SoundQuestError):
    """A body this reader cannot decode."""


class Fat12Error(ValueError):
    """Raised for malformed or truncated FAT12 images."""
