# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# kwsed is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version.

"""The build line: which kwsed and which vinsynlib are running."""

from __future__ import annotations

from vinsynlib import spec
from vinsynlib.build import build_of

__all__ = ["build_epilog", "build_line"]


def build_line() -> str:
    parts: list[str] = []
    for name, origin in (("kwsed", __file__), ("vinsynlib", None)):
        build = build_of(name, origin=origin)
        where = build.version or "an unknown version"
        if build.commit:
            tagged = ""
            if build.tag:
                tagged = f", {build.tag.removeprefix('refs/tags/')}"
            where += f" ({build.commit[:7]}{tagged})"
        parts.append(f"{name} {where}")
    return "built from " + " and ".join(parts)


def build_epilog() -> str:
    return (
        f"{build_line()}\n\n"
        "exit codes:\n"
        f"  {spec.EXIT_OK}  it worked\n"
        f"  {spec.EXIT_ERROR}  it did not work (error: ... on stderr)\n"
        f"  {spec.EXIT_USAGE}  the command line itself was wrong"
    )
