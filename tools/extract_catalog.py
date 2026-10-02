#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# Reads, but does not redistribute, the user's own System Exclusive dumps.
# Its output is performance and patch names, which are Korg's or a sound
# designer's, and is gitignored.
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

"""Build kwsed's name catalog from ``.syx`` dumps.

Give it any number of System Exclusive files. Every bank dump found
contributes its names, filed under the bank and kind **the dump itself
declares** rather than anything inferred from the filename.

Where two files hold the same bank and kind, the **first wins** and the rest
are reported. A directory of soundsets holds several dumps of the same bank;
silently keeping whichever was read last produces a catalog matching no
actual instrument.

Usage::

    python3 tools/extract_catalog.py ~/path/to/*.syx
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kws import banks  # noqa: E402
from kws import dumps  # noqa: E402
from kws import messages as m  # noqa: E402

DEFAULT_OUTPUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "kws",
    "data",
    "catalog.json",
)


def build(
    paths: List[str],
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, str], List[str]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    seen: Dict[str, str] = {}
    notes: List[str] = []

    for path in paths:
        base = os.path.basename(path)
        try:
            packets = dumps.read_syx_file(path)
        except OSError as exc:
            notes.append(f"{base}: cannot read ({exc})")
            continue
        found = 0
        for packet in packets:
            if packet.function not in (
                m.Function.ALL_PERFORMANCE_DUMP,
                m.Function.ALL_PATCH_DUMP,
            ):
                continue
            try:
                bank, kind, names = dumps.names_from_dump(packet)
            except m.ProtocolError as exc:
                notes.append(f"{base}: {exc}")
                continue
            found += 1
            marker = f"{bank.id}:{kind}"
            if marker in seen:
                continue
            seen[marker] = base
            out.setdefault(bank.id, []).extend(
                {"kind": kind, "n": number, "name": name}
                for number, name in sorted(names.items())
            )
        if not found:
            notes.append(f"{base}: no Wavestation bank dump in it")
    for rows in out.values():
        rows.sort(key=lambda row: (row["kind"], row["n"]))
    return out, seen, notes


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("syx", nargs="+")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    missing = [p for p in args.syx if not os.path.exists(p)]
    if missing:
        raise SystemExit(f"error: no such file: {', '.join(missing)}")

    banks_out, seen, notes = build(args.syx)
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    if not banks_out:
        raise SystemExit(
            "error: none of those files held a Wavestation bank dump. kwsed "
            "works without a catalog, showing numbers only."
        )

    payload = {
        "source": "; ".join(f"{k}={v}" for k, v in sorted(seen.items())),
        "generated": datetime.date.today().isoformat(),
        "note": (
            "Generated locally by tools/extract_catalog.py from the "
            "user's own dumps. Not distributed with kwsed."
        ),
        "banks": banks_out,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)
        handle.write("\n")

    total = sum(len(rows) for rows in banks_out.values())
    print(f"wrote {args.output}: {total} names across {len(banks_out)} bank(s)")
    for bank_id in sorted(banks_out, key=lambda b: banks.bank(b).number):
        rows = banks_out[bank_id]
        kinds: Dict[str, int] = {}
        for row in rows:
            kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
        detail = ", ".join(
            f"{n} {k}{'' if n == 1 else 's'}" for k, n in sorted(kinds.items())
        )
        print(f"  {banks.bank(bank_id).label:<8} {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
