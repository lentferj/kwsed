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

"""Undo log binding for kwsed."""

from __future__ import annotations

from collections.abc import Callable

from vinsynlib.undo import Record, UndoLog

__all__ = ["Record", "UndoLog", "make_log"]


def make_log(
    apply_hook: Callable[[Record], bool | None] | None = None,
    gate_hook: Callable[[], str | None] | None = None,
    status_hook: Callable[[str], None] | None = None,
) -> UndoLog:
    """Thin constructor."""
    return UndoLog(apply_hook=apply_hook, gate_hook=gate_hook, status_hook=status_hook)
