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

"""Print the Wavestation's System Setup dump, to confirm the card-name offset.

**Read-only.** It sends one System Exclusive request (``0x0E``) and prints
the ``0x51`` reply as hex. It writes no slot, selects no sound, and changes
nothing on the unit.

The card-name field is the one wire fact docs/HCARD.md section 7 leaves
open. Select a known HCard bank on the card, run this, and compare the bytes
with the name the card's display shows. Then set
``CARD_NAME_OFFSET``/``CARD_NAME_LEN`` in :mod:`kws.bridge` and delete the
``# CONFIRM AGAINST HARDWARE`` note.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from vinsynlib import spec


# The probe is deliberately quiet on import so --help works on a host with
# no MIDI backend; the bridge imports rtmidi.
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="probe_card_name.py",
        description=(
            "read the Wavestation's System Setup dump and print the bytes "
            "that carry the card name. Read-only: nothing is written."
        ),
    )
    parser.add_argument("--port", default=None, help="MIDI output port to use")
    parser.add_argument(
        "--recv-port", default=None, help="MIDI input port, if it differs"
    )
    parser.add_argument(
        "--device-channel",
        type=int,
        default=None,
        metavar="1-16",
        help="the unit's global channel (1-16); autodetected when omitted",
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="sweep every MIDI port instead of using the remembered one",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="seconds to wait for the reply (default: the bridge's own)",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    from kws import bridge
    from kws import messages as m

    channel = (args.device_channel - 1) if args.device_channel else 0
    try:
        if args.port:
            device = bridge.WavestationBridge.open(
                args.port, recv_port=args.recv_port, channel=channel
            )
        else:
            device = bridge.WavestationBridge.connect(scan=args.scan, channel=channel)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return spec.EXIT_ERROR
    try:
        data = device.read_system_setup(timeout=args.timeout)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return spec.EXIT_ERROR
    finally:
        device.close()

    if data is None:
        print(
            "no System Setup dump came back. The unit may be off, on another "
            "port, on another global channel, or have System Exclusive "
            "reception disabled. Nothing was written.",
            file=sys.stderr,
        )
        return spec.EXIT_ERROR

    end = bridge.CARD_NAME_OFFSET + bridge.CARD_NAME_LEN
    field = data[bridge.CARD_NAME_OFFSET : end]
    print("System Setup dump 0x51, decoded bytes:")
    print(" ".join(f"{byte:02x}" for byte in data))
    print()
    print(f"length            {len(data)} bytes")
    print(
        f"card-name field   offset {bridge.CARD_NAME_OFFSET}, "
        f"{bridge.CARD_NAME_LEN} bytes, CONFIRM AGAINST HARDWARE"
    )
    print(f"  raw             {' '.join(f'{byte:02x}' for byte in field)}")
    print(f"  as text         {m.decode_name(field)!r}")
    print()
    print("Read-only: one request was sent and nothing was written.")
    return spec.EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
