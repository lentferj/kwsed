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

"""``kwscli`` -- the same information as the TUI, in a pipe."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Callable, Dict, List, Optional

from kws import banks
from kws import catalog as cat

__all__ = ["main", "build_parser"]


def _table(rows: List[List[str]], headers: List[str]) -> str:
    if not rows:
        return ""
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    out = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip(),
           "  ".join("-" * widths[i] for i in range(len(headers)))]
    for row in rows:
        out.append("  ".join(cell.ljust(widths[i])
                             for i, cell in enumerate(row)).rstrip())
    return "\n".join(out)


def _catalog(args) -> cat.Catalog:
    return cat.load(getattr(args, "catalog", None))


def _cmd_banks(_device, args) -> None:
    catalog = _catalog(args)
    rows = []
    for entry in banks.BANKS:
        named = {}
        for kind in (banks.Kind.PERFORMANCE, banks.Kind.PATCH):
            named[kind] = sum(
                1 for n in range(banks.count_for(kind))
                if catalog.name(entry.id, kind, n))
        rows.append([
            str(entry.number), entry.label,
            f"{named[banks.Kind.PERFORMANCE]}/{banks.PERFORMANCES_PER_BANK}",
            f"{named[banks.Kind.PATCH]}/{banks.PATCHES_PER_BANK}",
            "writable" if entry.writable else "ROM",
        ])
    print(_table(rows, ["#", "bank", "performances", "patches", ""]))


def _cmd_list(_device, args) -> None:
    catalog = _catalog(args)
    entry = banks.bank(args.bank)
    rows = []
    for item in banks.slots(entry.number, args.kind):
        rows.append([f"{item.number:02d}",
                     catalog.display_name(entry.id, args.kind, item.number)])
    print(f"{entry.label} — {args.kind}s\n")
    print(_table(rows, ["#", "name"]))


def _cmd_find(_device, args) -> None:
    catalog = _catalog(args)
    hits = catalog.search(args.name)
    if not hits:
        print(f"nothing matching {args.name!r}", file=sys.stderr)
        return
    rows = [[bank_id, kind, f"{number:02d}", name]
            for bank_id, kind, number, name in hits]
    print(_table(rows, ["bank", "kind", "#", "name"]))


def _cmd_ports(_device, _args) -> None:
    from kws.bridge import MidiUnavailable, list_ports

    try:
        ins, outs = list_ports()
    except MidiUnavailable as exc:
        raise SystemExit(f"error: {exc}") from exc
    both = set(ins) & set(outs)
    for label, names in (("inputs", ins), ("outputs", outs)):
        print(f"{label}:")
        for name in names:
            print(f"  {name}" + ("   [bidirectional]" if name in both else ""))
    print("\nA Wavestation on a DIN cable is behind an interface whose name")
    print("says nothing about Korg. `kwscli status` asks every port.")


def _cmd_status(device, _args) -> None:
    fields = device.identify()
    if fields is None:
        raise SystemExit("error: nothing answered an Identity Request.")
    print(f"port            {device.description}")
    print(f"manufacturer    {fields['manufacturer']:#04x}"
          f"{'  (Korg)' if fields['is_korg'] else ''}")
    print(f"family          {fields['family']}"
          f"{'  (Wavestation)' if fields['is_wavestation'] else ''}")
    print(f"global channel  {fields['channel'] + 1}")


def _cmd_dump(device, args) -> None:
    """Read one bank off the unit. Read-only, **makes no sound**."""
    dump = device.read_bank(args.bank)
    catalog = _catalog(args)
    names = dump.names()
    print(f"{dump.bank.label} — {dump.kind}s "
          f"({len(names)} named of {dump.count})")
    print("\nWhich kind comes back is the unit's choice, not the request's:")
    print("one code asks for a bank and the unit answers with whatever it")
    print("is set to dump. kwsed reports what actually arrived.\n")
    rows = []
    for number in sorted(names):
        printed = catalog.name(dump.bank.id, dump.kind, number) or ""
        live = names[number]
        rows.append([f"{number:02d}", live, printed,
                     "*" if printed and printed != live else ""])
    print(_table(rows, ["#", "on the unit", "in the catalog", ""]))


def _cmd_select(device, args) -> None:
    """Select a sound. **This changes what the unit is playing.**"""
    item = banks.slot(args.bank, args.kind, args.number)
    device.select(item, channel=None if args.channel is None
                  else args.channel - 1)
    print(f"sent bank select {item.bank.number} and program change "
          f"{item.number} for {item}")
    print("Which of the twelve banks a Bank Select actually reaches is NOT")
    print("established (see TODO.md), so treat this as a best effort.")


def _cmd_multi(device, args) -> int:
    """Show the Multisets. One request; makes no sound."""
    from kws import messages as msg, multi as multi_mod
    from kws import banks as bk

    sets = multi_mod.parse(msg.parse(device.read_multi()).data())
    print(f"{len(sets)} multisets")
    for entry in sets:
        used = [p for p in entry.parts if p.used]
        if not used and not args.all:
            continue
        print(f"\n{entry.number:02d}  {entry.name or '(unnamed)'}"
              f"   {len(used)} channel(s)")
        for part in used:
            try:
                label = bk.by_sysex(part.bank).label
            except LookupError:
                label = f"bank {part.bank}"
            print(f"    ch {part.channel:2d}  {label:7s} perf "
                  f"{part.performance:02d}  level {part.level}")
    return 0


def _cmd_send(device, args) -> int:
    """Write a bank dump into the instrument. **This destroys a bank.**

    The gate here is the command line's equivalent of the TUI's
    arm-then-fire: without ``--yes`` it reads the destination, says what is
    about to be replaced **by name**, and stops. A slot number means nothing;
    the names it is about to overwrite mean everything.
    """
    from kws import backup, banks as bk, dumps, messages as m, records as rec

    try:
        size = os.path.getsize(args.file)
    except OSError as exc:
        print(f"{args.file} cannot be read ({exc})")
        return 2
    packets = [p for p in dumps.read_syx_file(args.file)]
    if not packets:
        print(f"{args.file} holds no Wavestation message kwsed recognises")
        return 2
    sendable = [p for p in packets if p.function in (
        m.Function.ALL_PATCH_DUMP, m.Function.ALL_PERFORMANCE_DUMP,
        m.Function.ALL_WAVESEQ_DUMP)]
    if not sendable:
        print(f"{args.file} is a {packets[0].function_name}, which kwsed "
              f"does not send")
        return 2
    if len(sendable) > 1:
        print(f"{args.file} holds {len(sendable)} bank dumps; only the "
              f"first ({sendable[0].function_name}) will be sent")
    packet = sendable[0]
    kinds = {
        m.Function.ALL_PATCH_DUMP: bk.Kind.PATCH,
        m.Function.ALL_PERFORMANCE_DUMP: bk.Kind.PERFORMANCE,
        m.Function.ALL_WAVESEQ_DUMP: "waveseq",
    }
    kind = kinds[packet.function]
    sizes = {bk.Kind.PATCH: bk.PATCHES_PER_BANK * rec.PATCH_SIZE,
             bk.Kind.PERFORMANCE: bk.PERFORMANCES_PER_BANK
             * rec.PERFORMANCE_SIZE,
             "waveseq": rec.WAVESEQ_BANK_SIZE}
    payload = packet.data()[:sizes[kind]]
    if len(payload) < sizes[kind]:
        print(f"{args.file} decodes to {len(payload)} bytes; a {kind} bank "
              f"is {sizes[kind]}. Refusing to send a short bank.")
        return 2

    destination = bk.bank(args.to)
    if not destination.writable:
        print(f"{destination.label} is ROM and cannot be written. The banks "
              f"that can are: "
              f"{', '.join(b.label for b in bk.writable_banks())}.")
        return 2

    plural = {"patch": "patches", "performance": "performances",
              "waveseq": "wave sequences"}[kind]
    print(f"about to replace: {destination.label}, all {plural}")
    print(f"          source: {args.file} ({size} bytes)")

    if kind == "waveseq":
        try:
            device.read_raw(destination.sysex, m.Function.ALL_WAVESEQ_DUMP)
        except Exception as exc:
            print(f"     it now holds: could not be read ({exc})")
            print("Refusing to write a bank that cannot first be read and "
                  "backed up.")
            return 1
        print("     it now holds: (wave sequence names are not read)")
    else:
        try:
            current = device.read_bank_names(destination.number)
            _, names = current
            shown = [f"{n:03d} {name}" for n, name in
                     sorted(names.items())[:6]]
            print(f"     it now holds: {', '.join(shown)}"
                  f"{' ...' if len(names) > 6 else ''}")
        except Exception as exc:
            print(f"     it now holds: could not be read ({exc})")
            print("Refusing to write a bank that cannot first be read and "
                  "backed up.")
            return 1

    if not args.yes:
        print("\nNothing has been sent. Add --yes to write, and be sure: "
              "this replaces the whole bank and no power cycle brings it "
              "back.")
        return 0

    where = args.backup_dir or backup.default_dir()
    result = device.write_bank(destination.number, kind, payload,
                               backup_dir=where)
    print(f"\nbacked up to: {result['backup']}")
    if result["verified"] is True:
        print("written and verified by reading it back")
        return 0
    print(f"written, but the read-back does NOT match: {result['mismatch']}")
    print("The backup above holds what was there before.")
    return 1


_COMMANDS: Dict[str, Callable] = {
    "banks": _cmd_banks, "list": _cmd_list, "find": _cmd_find,
    "ports": _cmd_ports, "status": _cmd_status, "dump": _cmd_dump,

    "select": _cmd_select,
    "send": _cmd_send,
    "multi": _cmd_multi,
}
_OFFLINE = {"banks", "list", "find", "ports"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kwscli", description="Browse a Korg Wavestation SR's sounds.")
    parser.add_argument("--port")
    parser.add_argument("--recv-port")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--midi-channel", type=int, default=None,
                        help="the unit's global channel, 1-16")
    parser.add_argument("--channel", type=int, default=None)
    parser.add_argument("--catalog", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    kinds = [banks.Kind.PERFORMANCE, banks.Kind.PATCH]
    sub.add_parser("banks", help="the bank table (no unit needed)")
    sp = sub.add_parser("list", help="one bank's slots (no unit needed)")
    sp.add_argument("bank")
    sp.add_argument("kind", choices=kinds)
    sp = sub.add_parser("find", help="search names (no unit needed)")
    sp.add_argument("name")
    sub.add_parser("ports", help="MIDI ports on this host")
    sub.add_parser("status", help="ask the unit who it is")
    sp = sub.add_parser("dump", help="read a bank off the unit (silent)")
    sp.add_argument("bank")
    sp = sub.add_parser("select", help="select a sound ON THE UNIT")
    sp.add_argument("bank")
    sp.add_argument("kind", choices=kinds)
    sp.add_argument("number", type=int)
    sp = sub.add_parser("multi", help="the 32 Multisets (silent)")
    sp.add_argument("--all", action="store_true",
                    help="include Multisets with nothing assigned")
    sp = sub.add_parser("send", help="WRITE a bank dump INTO the unit")
    sp.add_argument("file", help="a .syx holding one bank dump")
    sp.add_argument("--to", required=True,
                    help="destination bank: RAM1, RAM2, RAM3 or CARD")
    sp.add_argument("--yes", action="store_true",
                    help="actually write; without it nothing is sent")
    sp.add_argument("--backup-dir", default=None,
                    help="where to save the destination before overwriting")
    return parser


def _build_device(args):
    if args.demo:
        from kwsed.demo import DemoDevice

        return DemoDevice()
    from kws import bridge as b

    b.install_clean_exit()
    if args.port:
        return b.WavestationBridge.open(
            args.port, recv_port=args.recv_port,
            channel=(args.midi_channel - 1) if args.midi_channel else 0)
    return b.WavestationBridge.autodetect(
        on_try=lambda name: print(f"  probing {name}...", file=sys.stderr))


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    handler = _COMMANDS[args.command]
    for name in ("channel", "midi_channel"):
        value = getattr(args, name, None)
        if value is not None and not 1 <= value <= 16:
            sys.exit(f"error: --{name.replace('_', '-')} is 1-16")

    if args.command in _OFFLINE:
        try:
            code = handler(None, args)
        except (LookupError, ValueError) as exc:
            sys.exit(f"error: {exc}")
        return int(code or 0)
    try:
        device = _build_device(args)
    except Exception as exc:
        sys.exit(f"error: {exc}")
    try:
        # the handler's code is the process's: `send` reports a read-back
        # that did not match by exiting non-zero, and a script that ignores
        # that would treat a failed write as a success
        code = handler(device, args)
    except (LookupError, TimeoutError, ValueError, RuntimeError) as exc:
        sys.exit(f"error: {exc}")
    finally:
        device.close()
    return int(code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
