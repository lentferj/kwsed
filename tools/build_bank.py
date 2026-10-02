#!/usr/bin/env python3
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
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.

"""Build a favourites bank from other banks' System Exclusive dumps.

Patch first: you name the patches, this works out what they drag along and
which performances become complete. See docs/BANK_BUILDER.md.

    python3 tools/build_bank.py \\
        --source A=dumps/cardA-*.syx --source B=dumps/cardB-*.syx \\
        --template dumps/empty-*.syx \\
        --patch A:3 --patch A:7 --patch B:12 \\
        --out favourites

Writes nothing to the instrument. The result is `.syx` files; loading them
is the instrument's own business, through its front panel.
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kws import bankbuild, banks, deps, messages as m, records as rec  # noqa: E402


def _load(spec):
    if "=" not in spec:
        raise SystemExit(f"--source wants ID=glob, got {spec!r}")
    bank_id, pattern = spec.split("=", 1)
    files = sorted(glob.glob(os.path.expanduser(pattern)))
    if not files:
        raise SystemExit(f"no files matched {pattern!r}")
    return bank_id, deps.load_syx(files, bank_id)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="ID=GLOB",
        help="a source bank's dumps; repeatable",
    )
    ap.add_argument(
        "--template",
        metavar="GLOB",
        default=None,
        help="dumps of an empty bank to build on (recommended: "
        "format a bank on the instrument and dump it)",
    )
    ap.add_argument(
        "--patch",
        action="append",
        default=[],
        metavar="ID:N",
        help="a patch to include, in priority order; repeatable",
    )
    ap.add_argument(
        "--bank",
        default="CARD",
        help="which bank the result is destined for (default CARD)",
    )
    ap.add_argument(
        "--out", default=None, help="write <out>-patch.syx etc.; omit for a dry run"
    )
    ap.add_argument(
        "--channel",
        type=int,
        default=1,
        help="global channel to frame the output for, 1-16",
    )
    args = ap.parse_args(argv)

    if not 1 <= args.channel <= 16:
        raise SystemExit("error: --channel is 1-16")

    sources = dict(_load(s) for s in args.source)
    if not sources:
        raise SystemExit("no --source given")

    chosen = []
    for spec in args.patch:
        if ":" not in spec:
            raise SystemExit(f"--patch wants ID:N, got {spec!r}")
        bank_id, number = spec.rsplit(":", 1)
        try:
            chosen.append((bank_id, int(number)))
        except ValueError:
            raise SystemExit(f"--patch wants ID:N, got {spec!r}")

    destination = banks.bank(args.bank)
    made = bankbuild.plan(sources, chosen)

    print(f"patches       {len(made.patch_map):3d} / {banks.PATCHES_PER_BANK}")
    print(f"wave seqs     {len(made.sequence_map):3d} / {rec.SEQUENCES_PER_BANK}")
    print(f"steps         {made.steps_used:3d} / {rec.STEPS_PER_BANK}")
    print(
        f"performances  {len(made.performances):3d} / "
        f"{banks.PERFORMANCES_PER_BANK}   (complete ones)"
    )
    for why in made.rejected:
        print("  left out:", why)
    for note in made.notes:
        print("  note:", note)

    if args.out is None:
        print("\ndry run: nothing written. Pass --out to build.")
        return 0

    if args.template:
        files = sorted(glob.glob(os.path.expanduser(args.template)))
        if not files:
            raise SystemExit(f"no files matched {args.template!r}")
        template = deps.load_syx(files, "TEMPLATE")
    else:
        print(
            "\nno --template: building on empty records. The instrument's "
            "own initialised records are safer -- format a bank and dump "
            "it."
        )
        template = deps.Source(bank_id="TEMPLATE")

    built = bankbuild.build(
        sources,
        made,
        template_patches=(
            b"".join(template.patches)
            if template.patches
            else bytes(banks.PATCHES_PER_BANK * rec.PATCH_SIZE)
        ),
        template_performances=(
            b"".join(template.performances)
            if template.performances
            else bytes(banks.PERFORMANCES_PER_BANK * rec.PERFORMANCE_SIZE)
        ),
        template_waveseq=(template.waveseq or bytes(rec.WAVESEQ_BANK_SIZE)),
        destination_bank=destination.sysex,
    )

    channel = args.channel - 1
    written = []
    for suffix, function, payload in (
        ("patch", m.Function.ALL_PATCH_DUMP, built.patches),
        ("perf", m.Function.ALL_PERFORMANCE_DUMP, built.performances),
        ("seq", m.Function.ALL_WAVESEQ_DUMP, built.waveseq),
    ):
        if payload is None:
            continue
        frame = m.frame_bulk(function, destination.sysex, payload, channel=channel)
        path = f"{args.out}-{suffix}.syx"
        with open(path, "wb") as handle:
            handle.write(bytes(frame))
        written.append((path, len(frame)))
    for path, size in written:
        print(f"wrote {path}  ({size} bytes)")
    print("\nNothing was sent to the instrument. Load these from its front panel.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
