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

"""The write gate for kwsed."""

from __future__ import annotations

__all__ = ["EditGate"]


class EditGate:
    def __init__(self, *, armed: bool = False) -> None:
        self.armed = armed

    def toggle(self) -> bool:
        self.armed = not self.armed
        return self.armed

    def describe(self) -> str:
        return "write gate ARMED" if self.armed else "write gate locked"

    def refuse(self, what: str) -> str:
        return (
            f"{what} not sent: the write gate is locked. Press w to arm it, "
            "or start with --allow-write."
        )
