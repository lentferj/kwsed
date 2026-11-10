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
import glob
import os
import sys
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from vinsynlib import cli, midi, render, spec
from kws import banks
from kws import cards as _cards
from kws import catalog as cat
from kws import livenames as _livenames
from kws import physical as _physical
from .buildinfo import build_epilog

__all__ = ["main", "build_parser"]


def _table(rows: List[List[str]], headers: List[str]) -> str:
    """Thin wrapper around :func:`vinsynlib.render.table`.

    Kept so callers and tests that still name it keep working; the family's
    renderer is the implementation.
    """
    return render.table(headers, rows)


def _catalog(args: argparse.Namespace) -> cat.Catalog:
    catalog = cat.load(getattr(args, "catalog", None))
    _livenames.apply_to(catalog, getattr(args, "live_names", None))
    return catalog


def _cmd_banks(_device: Any, args: argparse.Namespace) -> None:
    catalog = _catalog(args)
    rows = []
    for entry in banks.BANKS:
        named = {}
        for kind in (banks.Kind.PERFORMANCE, banks.Kind.PATCH):
            named[kind] = sum(
                1
                for n in range(banks.count_for(kind))
                if catalog.name(entry.id, n, kind)
            )
        rows.append(
            [
                str(entry.number),
                entry.label,
                f"{named[banks.Kind.PERFORMANCE]}/{banks.PERFORMANCES_PER_BANK}",
                f"{named[banks.Kind.PATCH]}/{banks.PATCHES_PER_BANK}",
                "writable" if entry.writable else "ROM",
            ]
        )
    print(render.table(["#", "bank", "performances", "patches", ""], rows))


def _cards_path(args: argparse.Namespace) -> str:
    return getattr(args, "cards", None) or _cards.default_path()


def _card_number(text: str) -> int:
    try:
        return int(text, 10)
    except ValueError:
        raise ValueError(
            f"{text!r} is not a bank number; the HCard has banks 00-39"
        ) from None


def _card_slot_active_scope(args: argparse.Namespace) -> Optional[str]:
    """The archived scope the one physical card slot currently holds.

    A hint, in the same order the app uses: an identified physical card
    wins, then the HCard bank last confirmed by a capture. ``None`` means
    nothing has said which bank is in the slot, so a card sub-scope cannot
    be read without mislabelling whatever is there.
    """
    card_id = _physical.active()
    if card_id is not None:
        return _physical.scope_id(card_id)
    number = _cards.load_active(getattr(args, "cards", None))
    if number is not None:
        return _cards.scope_id(number)
    return None


def _cards_status(bank: _cards.CapturedBank | None, entry: _cards.Card) -> str:
    return _cards.card_words(bank is not None, entry.needs_pcm)


def _cards_verification(bank: _cards.CapturedBank) -> str:
    """One word for whether the capture was confirmed by content.

    The words are the app's: ``verified`` says a digest matched the bank it
    was captured under; ``unverified`` says it was filed on the user's
    say-so and the HCard may actually be on another bank.
    """
    return "verified" if bank.verified else "unverified"


def _cards_digest(bank: _cards.CapturedBank) -> str:
    """A short digest prefix, or ``(none)`` for a capture without one."""
    if bank.digest:
        return f"digest: {bank.digest[:8]}"
    return "digest: (none)"


def _cmd_cards(_device: Any, args: argparse.Namespace) -> int:
    """The HCard's forty banks: the table, one archive, or one .syx.

    No unit is needed. The archive is what a capture filed; a ``.syx``
    written here holds the **names** from that archive and zeroed parameter
    data, because names are all the archive stores. Saying so plainly beats
    a file that looks like a whole bank and is not.
    """
    archive = _cards.load(_cards_path(args))
    if getattr(args, "bank", None) is None:
        rows = []
        for entry in _cards.cards():
            bank = archive.get(entry.number)
            status = _cards_status(bank, entry)
            # A capture filed on the row's say-so says so in the archive
            # column: "unverified" in place of "captured" (the PCM warning
            # after the semicolon is kept). Verified and uncaptured rows
            # stay word for word what they were.
            if bank is not None and bank.verified is False:
                status = status.replace("captured", "unverified", 1)
            rows.append(
                [
                    f"{entry.number:02d}",
                    _cards.bank_title(bank, entry) if bank is not None else entry.name,
                    status,
                    str(len(bank.performances)) if bank else "-",
                    str(len(bank.patches)) if bank else "-",
                ]
            )
        print(render.table(["#", "name", "archive", "performances", "patches"], rows))
        return spec.EXIT_OK

    try:
        number = _card_number(args.bank)
        entry = _cards.card(number)
    except (LookupError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return spec.EXIT_USAGE
    bank = archive.get(number)
    if getattr(args, "syx", None) is not None:
        return _dump_card_syx(args, entry, bank)

    if getattr(args, "label", None) is not None:
        if bank is None:
            print(
                f"error: Card {number:02d} is not captured, so it cannot be "
                f"labelled. Select it on the HCard and press C first.",
                file=sys.stderr,
            )
            return spec.EXIT_USAGE
        try:
            updated = _cards.set_label(number, args.label, _cards_path(args))
        except LookupError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return spec.EXIT_USAGE
        print(f"Card {number:02d} — {_cards.bank_title(updated, entry)}")
        return spec.EXIT_OK

    shown = _cards.bank_title(bank, entry) if bank is not None else entry.name
    print(f"Card {number:02d} — {shown}")
    print(f"{_cards_status(bank, entry)}")
    if bank is not None:
        print(f"{_cards_verification(bank)}  {_cards_digest(bank)}")
    print()
    rows = []
    wanted_kind = getattr(args, "kind", None)
    for kind in (banks.Kind.PERFORMANCE, banks.Kind.PATCH):
        if wanted_kind is not None and kind != wanted_kind:
            continue
        names = bank.names(kind) if bank is not None else {}
        for slot in range(banks.count_for(kind)):
            name = names.get(slot, "")
            rows.append(
                [
                    kind[:4],
                    f"{slot:02d}",
                    name if name else ("—" if bank is None else "(unnamed)"),
                ]
            )
    print(render.table(["kind", "#", "name"], rows))
    return spec.EXIT_OK


def _dump_card_syx(
    args: argparse.Namespace,
    entry: _cards.Card,
    bank: _cards.CapturedBank | None,
) -> int:
    """Write one captured bank as a ``.syx`` frame, names and zeros."""
    from kws import bankstore
    from kws import messages as msg, records as rec

    kind = getattr(args, "kind", None)
    if kind not in (banks.Kind.PERFORMANCE, banks.Kind.PATCH):
        print(
            "error: --syx needs --kind performance or --kind patch",
            file=sys.stderr,
        )
        return spec.EXIT_USAGE
    if bank is None:
        print(
            f"error: Card {entry.number:02d} is not captured, so there is "
            f"nothing to dump. Select it on the HCard and press C first.",
            file=sys.stderr,
        )
        return spec.EXIT_USAGE

    if kind == banks.Kind.PERFORMANCE:
        size, count = rec.PERFORMANCE_SIZE, banks.PERFORMANCES_PER_BANK
        function = msg.Function.ALL_PERFORMANCE_DUMP
    else:
        size, count = rec.PATCH_SIZE, banks.PATCHES_PER_BANK
        function = msg.Function.ALL_PATCH_DUMP

    real_records = None
    source = bankstore.load(_cards.scope_id(entry.number))
    if source is not None:
        if (
            kind == banks.Kind.PERFORMANCE
            and len(source.performances) == count
            and all(len(r) == size for r in source.performances)
        ):
            real_records = source.performances
        elif (
            kind == banks.Kind.PATCH
            and len(source.patches) == count
            and all(len(r) == size for r in source.patches)
        ):
            real_records = source.patches

    used_real = real_records is not None
    if real_records is not None:
        raw = b"".join(real_records)
    else:
        buf = bytearray(size * count)
        for slot, name in bank.names(kind).items():
            if not 0 <= slot < count:
                continue
            at = slot * size
            encoded = bytes(msg.encode_name(name))
            buf[at : at + len(encoded)] = encoded
        raw = bytes(buf)
    frame = msg.frame_bulk(function, banks.bank("CARD").sysex, raw, channel=0)

    destination = args.syx
    try:
        if destination == "-":
            sys.stdout.buffer.write(frame)
            sys.stdout.buffer.flush()
        else:
            with open(destination, "wb") as handle:
                handle.write(frame)
    except OSError as exc:
        print(f"error: {destination} cannot be written ({exc})", file=sys.stderr)
        return spec.EXIT_ERROR
    print(
        f"wrote Card {entry.number:02d} {_plural(kind)} as a {len(frame)}-byte "
        f"{msg.Function.name(function)}",
        file=sys.stderr,
    )
    if used_real:
        print(
            "full record data was written",
            file=sys.stderr,
        )
    else:
        print(
            "names only: the archive stores no parameter data, so every other "
            "byte in the frame is zero.",
            file=sys.stderr,
        )
    return spec.EXIT_OK


def _cmd_list(_device: Any, args: argparse.Namespace) -> None:
    catalog = _catalog(args)
    entry = banks.bank(args.bank)
    rows = []
    for item in banks.slots(entry.number, args.kind):
        mark = "*" if catalog.differs(entry.id, item.number, args.kind) else ""
        rows.append(
            [
                f"{item.number:02d}",
                catalog.display_name(entry.id, item.number, args.kind),
                mark,
            ]
        )
    print(f"{entry.label} — {_plural(args.kind)}\n")
    print(render.table(["#", "name", ""], rows))
    if any(row[2] for row in rows):
        print(f"\n`*` = {cat.disagreement_words(entry.writable)}.")


def _cmd_find(_device: Any, args: argparse.Namespace) -> None:
    catalog = _catalog(args)
    hits = catalog.search(args.name)
    if not hits:
        print(f"nothing matching {args.name!r}", file=sys.stderr)
        return
    rows = [
        [bank_id, kind, f"{number:02d}", name] for bank_id, kind, number, name in hits
    ]
    print(render.table(["bank", "kind", "#", "name"], rows))


def _cmd_ports(_device: Any, _args: argparse.Namespace) -> None:
    try:
        ins, outs = midi.list_ports()
    except midi.MidiUnavailable as exc:
        raise SystemExit(f"error: {exc}") from exc
    # The rendering is the family's, so all nine listings look alike; only the
    # trailing note is kwsed's, because only kwsed has a DIN cable to warn
    # about.
    print(
        midi.render_ports(
            ins,
            outs,
            note=(
                "A Wavestation on a DIN cable is behind an interface whose "
                "name\nsays nothing about Korg. `kwscli status` asks every "
                "port."
            ),
        )
    )


def _cmd_status(device: Any, _args: argparse.Namespace) -> None:
    fields = device.identify()
    if fields is None:
        raise SystemExit("error: nothing answered an Identity Request.")
    print(f"port            {device.description}")
    print(
        f"manufacturer    {fields['manufacturer']:#04x}"
        f"{'  (Korg)' if fields['is_korg'] else ''}"
    )
    print(
        f"family          {fields['family']}"
        f"{'  (Wavestation)' if fields['is_wavestation'] else ''}"
    )
    print(f"global channel  {fields['channel'] + 1}")


def _plural(kind: str) -> str:
    """ "patch" pluralises to "patches", not "patchs"."""
    if kind == "waveseq":
        return "wave sequences"
    return f"{kind}es" if kind.endswith("ch") else f"{kind}s"


def _cmd_dump(device: Any, args: argparse.Namespace) -> None:
    """Read one bank off the unit. Read-only, **makes no sound**.

    The kind is asked for rather than accepted: each has its own request
    code, ``0x1C`` for patches and ``0x1D`` for performances. It used to be
    the unit's choice -- kwsed had only the patch request and waited for
    whatever came back -- and the text here said so long after it stopped
    being true.
    """
    dump = device.read_bank(args.bank, args.kind)
    catalog = _catalog(args)
    names = dump.names()
    if not getattr(args, "demo", False):
        flat = {
            _livenames.key(dump.bank.id, number, dump.kind): name
            for number, name in names.items()
        }
        _livenames.save(flat, getattr(args, "live_names", None))
    print(
        f"{dump.bank.label} — {_plural(dump.kind)} ({len(names)} named of {dump.count})"
    )
    rows: list[list[str]] = []
    for number in sorted(names):
        printed = catalog.name(dump.bank.id, number, dump.kind) or ""
        live = names[number]
        mark = "*" if printed and printed != live else ""
        rows.append([f"{number:02d}", live, printed, mark])
    print(render.table(["#", "on the unit", "in the catalog", ""], rows))
    # The mark means different things in the two kinds of bank, and which
    # one is wrong is the difference only writability can carry: a writable
    # bank holds somebody's own work, a ROM one a stale printed list.
    if any(row[3] for row in rows):
        print(f"\n`*` = {cat.disagreement_words(dump.bank.writable)}.")


def _cmd_select(device: Any, args: argparse.Namespace) -> None:
    """Select a sound. **This changes what the unit is playing.**"""
    item = banks.slot(args.bank, args.kind, args.number)
    device.select(item, channel=None if args.channel is None else args.channel - 1)
    print(
        f"sent bank select MSB 0, LSB {item.bank.midi_bank}, program change "
        f"{item.bank.program_base + item.number} for {item}"
    )


def _cmd_multi(device: Any, args: argparse.Namespace) -> int:
    """Show the Multisets. One request; makes no sound."""
    from kws import messages as msg, multi as multi_mod
    from kws import banks as bk

    packet = msg.parse(device.read_multi())
    if packet is None:
        raise ValueError("the reply was not a Wavestation message")
    sets = multi_mod.parse(packet.data())
    print(f"{len(sets)} multisets")
    for entry in sets:
        used = [p for p in entry.parts if p.used]
        if not used and not args.all:
            continue
        print(
            f"\n{entry.number:02d}  {entry.name or '(unnamed)'}"
            f"   {len(used)} channel(s)"
        )
        for part in used:
            try:
                label = bk.by_sysex(part.bank).label
            except LookupError:
                label = f"bank {part.bank}"
            print(
                f"    ch {part.channel:2d}  {label:7s} perf "
                f"{part.performance:02d}  level {part.level}"
            )
    return spec.EXIT_OK


def _destination_names(device: Any, entry: Any, kind: str) -> str:
    """What a bank holds now, in names rather than a slot number.

    A slot number means nothing on the confirmation line; the names about
    to be replaced mean everything. Wave sequences have no names to read,
    so their read is still made -- it is the fence's proof that the
    destination can be read at all -- and the absence is said plainly.
    """
    from kws import messages as m

    if kind == "waveseq":
        device.read_raw(entry.sysex, m.Function.ALL_WAVESEQ_DUMP)
        return "(wave sequence names are not read)"
    _, names = device.read_bank_names(entry.number, kind)
    shown = [f"{number:03d} {name}" for number, name in sorted(names.items())[:6]]
    return f"{', '.join(shown)}{' ...' if len(names) > 6 else ''}"


def _cmd_send(device: Any, args: argparse.Namespace) -> int:
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
        return spec.EXIT_USAGE
    packets = [p for p in dumps.read_syx_file(args.file)]
    if not packets:
        print(f"{args.file} holds no Wavestation message kwsed recognises")
        return spec.EXIT_USAGE
    sendable = [
        p
        for p in packets
        if p.function
        in (
            m.Function.ALL_PATCH_DUMP,
            m.Function.ALL_PERFORMANCE_DUMP,
            m.Function.ALL_WAVESEQ_DUMP,
        )
    ]
    if not sendable:
        print(f"{args.file} is a {packets[0].function_name}, which kwsed does not send")
        return spec.EXIT_USAGE
    if len(sendable) > 1:
        print(
            f"{args.file} holds {len(sendable)} bank dumps; only the "
            f"first ({sendable[0].function_name}) will be sent"
        )
    packet = sendable[0]
    kinds = {
        m.Function.ALL_PATCH_DUMP: bk.Kind.PATCH,
        m.Function.ALL_PERFORMANCE_DUMP: bk.Kind.PERFORMANCE,
        m.Function.ALL_WAVESEQ_DUMP: "waveseq",
    }
    kind = kinds[packet.function]
    sizes = {
        bk.Kind.PATCH: bk.PATCHES_PER_BANK * rec.PATCH_SIZE,
        bk.Kind.PERFORMANCE: bk.PERFORMANCES_PER_BANK * rec.PERFORMANCE_SIZE,
        "waveseq": rec.WAVESEQ_BANK_SIZE,
    }
    payload = packet.data()[: sizes[kind]]
    if len(payload) < sizes[kind]:
        print(
            f"{args.file} decodes to {len(payload)} bytes; a {kind} bank "
            f"is {sizes[kind]}. Refusing to send a short bank."
        )
        return spec.EXIT_USAGE

    destination = bk.bank(args.to)
    if not destination.writable:
        print(
            f"{destination.label} is ROM and cannot be written. The banks "
            f"that can are: "
            f"{', '.join(b.label for b in bk.writable_banks())}."
        )
        return spec.EXIT_USAGE

    plural = _plural(kind)
    print(f"about to replace: {destination.label}, all {plural}")
    print(f"          source: {args.file} ({size} bytes)")

    try:
        # The kind matters: a kind-less read sends the patch request, so
        # sending a performance bank would print the destination's *patch*
        # names and the user would confirm the write against a list of the
        # wrong objects.
        held = _destination_names(device, destination, kind)
    except Exception as exc:
        print(f"     it now holds: could not be read ({exc})")
        print("Refusing to write a bank that cannot first be read and backed up.")
        return spec.EXIT_ERROR
    print(f"     it now holds: {held}")

    if not args.yes:
        print(
            "\nNothing has been sent. Add --yes to write, and be sure: "
            "this replaces the whole bank and no power cycle brings it "
            "back."
        )
        return spec.EXIT_OK

    where = args.backup_dir or backup.default_dir()
    result = device.write_bank(destination.number, kind, payload, backup_dir=where)
    print(f"\nbacked up to: {result['backup']}")
    if result["verified"] is True:
        print("written and verified by reading it back")
        return spec.EXIT_OK
    print(f"written, but the read-back does NOT match: {result['mismatch']}")
    print("The backup above holds what was there before.")
    return spec.EXIT_ERROR


def _read_dump(device: Any, bank_byte: int, function: int) -> bytes:
    """Ask the unit for one bank dump and return its decoded payload."""
    from kws import messages as m

    packet = m.parse(device.read_raw(bank_byte, function))
    if packet is None:
        raise ValueError(
            f"the reply to {m.Function.name(function)} was not a Wavestation message"
        )
    return packet.data()


def _read_records(
    device: Any, bank_byte: int, function: int, count: int, size: int
) -> List[bytes]:
    """One bank dump, split into whole records."""
    data = _read_dump(device, bank_byte, function)
    if len(data) < count * size:
        raise ValueError(
            f"the reply decodes to {len(data)} bytes; a whole bank is {count * size}"
        )
    return [data[index * size : (index + 1) * size] for index in range(count)]


def _read_scope(
    device: Any,
    scope: str,
    *,
    allow_card_slots: Optional[set[str]] = None,
) -> Any:
    """Read a whole scope off the unit as performances, patches and waveseq."""
    from kws import banks as bk, deps as deps_mod, messages as m
    from kws import records as rec

    if scope.startswith("CARD/") and scope not in (allow_card_slots or set()):
        raise ValueError(
            f"{scope} is not cached and kwscli cannot tell which card bank is "
            f"in the slot; pass --source {scope}=GLOB, or capture it first"
        )
    if scope.startswith("CARD/") or scope == "CARD":
        entry = bk.bank("CARD")
    else:
        entry = bk.bank(scope)
    performances = _read_records(
        device,
        entry.sysex,
        m.Function.ALL_PERFORMANCE_DUMP,
        bk.PERFORMANCES_PER_BANK,
        rec.PERFORMANCE_SIZE,
    )
    patches = _read_records(
        device,
        entry.sysex,
        m.Function.ALL_PATCH_DUMP,
        bk.PATCHES_PER_BANK,
        rec.PATCH_SIZE,
    )
    waveseq: Optional[bytes]
    try:
        raw = _read_dump(device, entry.sysex, m.Function.ALL_WAVESEQ_DUMP)
        waveseq = raw[: rec.WAVESEQ_BANK_SIZE]
    except Exception:
        # A bank can be complete without its wave sequences; the relation
        # graph reports the cost of that rather than refusing to show anything.
        waveseq = None
    return deps_mod.Source(
        bank_id=scope,
        performances=performances,
        patches=patches,
        waveseq=waveseq,
    )


def _source_specs(specs: Optional[Sequence[str]]) -> Dict[str, Any]:
    """Every ``ID=GLOB`` an offline caller named, loaded as a bank."""
    from kws import deps as deps_mod

    out: Dict[str, Any] = {}
    for text in specs or ():
        if "=" not in text:
            raise ValueError(f"--source wants ID=GLOB, got {text!r}")
        bank_id, pattern = text.split("=", 1)
        files = sorted(glob.glob(os.path.expanduser(pattern)))
        if not files:
            raise ValueError(f"no files matched {pattern!r}")
        out[bank_id] = deps_mod.load_syx(files, bank_id)
    return out


def _scope_source(
    device: Any,
    scope: str,
    directory: Optional[str],
    *,
    allow_card_slots: Optional[set[str]] = None,
) -> Optional[Any]:
    """A scope from the bank cache, or off the unit when one is attached."""
    from kws import bankstore

    where = directory or bankstore.default_dir()
    cached = bankstore.load(scope, where)
    if cached is not None:
        return cached
    if device is None:
        return None
    return _read_scope(device, scope, allow_card_slots=allow_card_slots)


def _cli_sources(
    device: Any,
    *,
    specs: Optional[Sequence[str]],
    bankstore_dir: Optional[str],
    need: Sequence[str],
    allow_card_slots: Optional[set[str]] = None,
) -> Dict[str, Any]:
    """The source banks a handler needs, from files, the cache or the unit."""
    sources = _source_specs(specs)
    for scope in need:
        if scope in sources:
            continue
        loaded = _scope_source(
            device,
            scope,
            bankstore_dir,
            allow_card_slots=allow_card_slots,
        )
        if loaded is None:
            raise ValueError(
                f"{scope} is not loaded: pass --source {scope}=GLOB, or fill "
                f"the bank cache with `kwscli backup save`"
            )
        sources[scope] = loaded
    return sources


def _extend_sources(
    sources: Dict[str, Any],
    device: Any,
    scopes: Sequence[str],
    directory: Optional[str],
    *,
    allow_card_slots: Optional[set[str]] = None,
) -> None:
    """Fill in dependency scopes when they can be found; leave the rest out."""
    for scope in scopes:
        if scope in sources:
            continue
        try:
            loaded = _scope_source(
                device, scope, directory, allow_card_slots=allow_card_slots
            )
        except Exception:
            loaded = None
        if loaded is not None:
            sources[scope] = loaded


def _part_scopes(sources: Dict[str, Any], chosen: Sequence[Any]) -> List[str]:
    """Every bank a set of performances' used parts points at."""
    from kws import records as rec, relations

    out: List[str] = []
    seen: set[str] = set()
    for perf in chosen:
        record = _performance_record(sources, perf.scope_id, perf.number)
        if record is None:
            continue
        for part in rec.performance_parts(record):
            if not part.used:
                continue
            scope = relations.resolve_part_scope(part.bank, perf.scope_id)
            if scope not in seen:
                seen.add(scope)
                out.append(scope)
    return out


def _sequence_scopes(sources: Dict[str, Any], chosen: Sequence[Any]) -> List[str]:
    """Every wave-sequence bank the chosen performances' patches quote."""
    from kws import records as rec, relations

    out: List[str] = []
    seen: set[str] = set()
    for perf in chosen:
        record = _performance_record(sources, perf.scope_id, perf.number)
        if record is None:
            continue
        for part in rec.performance_parts(record):
            if not part.used:
                continue
            patch_scope = relations.resolve_part_scope(part.bank, perf.scope_id)
            patch = _patch_record(sources, patch_scope, part.patch)
            if patch is None:
                continue
            for wave in rec.patch_waves(patch):
                if not wave.is_sequence:
                    continue
                scope = relations.resolve_part_scope(wave.bank, patch_scope)
                if scope not in seen:
                    seen.add(scope)
                    out.append(scope)
    return out


def _patch_wave_scopes(sources: Dict[str, Any], scope: str, number: int) -> List[str]:
    """Every wave-sequence bank one patch's oscillators quote."""
    from kws import records as rec, relations

    record = _patch_record(sources, scope, number)
    if record is None:
        return []
    out: List[str] = []
    seen: set[str] = set()
    for wave in rec.patch_waves(record):
        if not wave.is_sequence:
            continue
        seq_scope = relations.resolve_part_scope(wave.bank, scope)
        if seq_scope not in seen:
            seen.add(seq_scope)
            out.append(seq_scope)
    return out


def _performance_record(
    sources: Dict[str, Any], scope: str, number: int
) -> Optional[bytes]:
    source = sources.get(scope)
    if source is None or number >= len(source.performances):
        return None
    return bytes(source.performances[number])


def _patch_record(sources: Dict[str, Any], scope: str, number: int) -> Optional[bytes]:
    source = sources.get(scope)
    if source is None or number >= len(source.patches):
        return None
    return bytes(source.patches[number])


def _patch_name(sources: Dict[str, Any], scope: str, number: int) -> str:
    from kws import dumps

    record = _patch_record(sources, scope, number)
    return "(not loaded)" if record is None else dumps.record_name(record)


def _performance_label(sources: Dict[str, Any], perf: Any) -> str:
    from kws import dumps

    record = _performance_record(sources, perf.scope_id, perf.number)
    if record is None:
        return f"{perf.scope_id}:{perf.number}"
    return f'{perf.scope_id} {perf.number:02d} "{dumps.record_name(record)}"'


def _print_parts(sources: Dict[str, Any], record: bytes, scope: str) -> None:
    from kws import records as rec, relations

    for part in rec.performance_parts(record):
        if not part.used:
            continue
        patch_scope = relations.resolve_part_scope(part.bank, scope)
        name = _patch_name(sources, patch_scope, part.patch)
        print(f"  {part.index} -> {patch_scope}:{part.patch} {name}")


def _format_numbers(numbers: Sequence[int]) -> str:
    return ", ".join(str(number) for number in numbers) if numbers else "none"


def _format_shared(shared: Dict[int, List[int]]) -> str:
    if not shared:
        return "none"
    return ", ".join(
        f"{patch} (used by {', '.join(f'{number:02d}' for number in perfs)})"
        for patch, perfs in sorted(shared.items())
    )


def _deps_whole_bank(graph: Any, sources: Dict[str, Any], scope: str) -> int:
    from kws import dumps, perfcopy

    source = sources[scope]
    active = perfcopy.active_performances(source)
    if not active:
        print(f"{scope}: no active performances")
    for number in active:
        record = source.performances[number]
        print(f'{scope} {number:02d} "{dumps.record_name(record)}"')
        _print_parts(sources, record, scope)
    print(f"shared patches: {_format_shared(graph.shared_patches_in_bank(scope))}")
    print(
        "unreferenced patches: "
        f"{_format_numbers(graph.unreferenced_patches_in_bank(scope))}"
    )
    return spec.EXIT_OK


def _deps_one_performance(
    graph: Any, sources: Dict[str, Any], scope: str, number: int
) -> int:
    from kws import relations

    perf = relations.PerfId(scope, number)
    try:
        relation = graph.performance_relation(perf)
    except LookupError as exc:
        print(f"{exc}")
        return spec.EXIT_OK
    print(f'{scope} {number:02d} "{relation.name}"')
    _print_parts(sources, sources[scope].performances[number], scope)
    patches = ", ".join(
        f"{patch.scope_id}:{patch.number}" for patch in relation.unique_patches
    )
    print(f"unique patches: {patches or 'none'}")
    print(f"sequences: {len(relation.sequences)}")
    for seq_scope, wave_bank, sequence in relation.sequences:
        print(f"  {seq_scope} {sequence:02d} (wave bank {wave_bank})")
    print(f"step cost: {relation.step_cost}")
    for missing in relation.missing:
        print("  missing:", missing)

    shared = graph.shared_patches_in_bank(scope)
    sharing = [
        patch.number
        for patch in relation.unique_patches
        if patch.scope_id == scope and patch.number in shared
    ]
    if sharing:
        print("patches: shared with other performances")
        for patch_number in sharing:
            others = [
                f"{other:02d}" for other in shared[patch_number] if other != number
            ]
            print(
                f"  patch {patch_number} also used by "
                f"{', '.join(others) or 'no other performance'}"
            )
    else:
        print("patches: atomic")
    multisets = graph.multisets_using_performance(perf)
    print(
        "multisets: "
        + (", ".join(f"{number}.{channel}" for number, channel in multisets) or "none")
    )
    return spec.EXIT_OK


def _deps_one_patch(
    graph: Any, sources: Dict[str, Any], scope: str, number: int
) -> int:
    from kws import dumps, records as rec, relations

    record = _patch_record(sources, scope, number)
    if record is None:
        print(f"{scope}:{number} is not loaded")
        return spec.EXIT_OK
    print(f'{scope} {number:02d} "{dumps.record_name(record)}"')
    for wave in rec.patch_waves(record):
        if wave.is_sequence:
            seq_scope = relations.resolve_part_scope(wave.bank, scope)
            print(f"  Osc {wave.index + 1} -> wave sequence {wave.wave} in {seq_scope}")
        else:
            print(f"  Osc {wave.index + 1} -> wave {wave.wave}")
    used = graph.performances_using_patch(relations.PatchId(scope, number))
    if not used:
        print("used by: none")
    else:
        print(
            "used by: " + ", ".join(_performance_label(sources, perf) for perf in used)
        )
    return spec.EXIT_OK


def _loaded_card_scope(sources: Mapping[str, Any]) -> str:
    """The one archived card scope in `sources`, or the bare slot label."""
    cards = [scope for scope in sources if scope.startswith("CARD/")]
    return cards[0] if len(cards) == 1 else "CARD"


def _cmd_deps(device: Any, args: argparse.Namespace) -> int:
    """The Perf -> Patch -> WaveSeq graph, for a bank or one slot."""
    from kws import banks as bk, perfcopy, relations

    if (args.kind is None) != (args.number is None):
        print("error: a kind and a number go together", file=sys.stderr)
        return spec.EXIT_USAGE
    scope = args.bank
    active = _card_slot_active_scope(args)
    allowed = {active} if active is not None else None
    try:
        sources = _cli_sources(
            device,
            specs=args.source,
            bankstore_dir=args.bankstore,
            need=[scope],
            allow_card_slots=allowed,
        )
    except (LookupError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return spec.EXIT_USAGE

    chosen: List[Any] = []
    if args.kind is None:
        chosen = [
            relations.PerfId(scope, number)
            for number in perfcopy.active_performances(sources[scope])
        ]
    elif args.kind == bk.Kind.PERFORMANCE:
        chosen = [relations.PerfId(scope, args.number)]
    _extend_sources(
        sources,
        device,
        _part_scopes(sources, chosen),
        args.bankstore,
        allow_card_slots=allowed,
    )
    _extend_sources(
        sources,
        device,
        _sequence_scopes(sources, chosen),
        args.bankstore,
        allow_card_slots=allowed,
    )
    if args.kind == bk.Kind.PATCH and args.number is not None:
        _extend_sources(
            sources,
            device,
            _patch_wave_scopes(sources, scope, args.number),
            args.bankstore,
            allow_card_slots=allowed,
        )

    graph = relations.RelationGraph(
        sources, active_card_scope=_loaded_card_scope(sources)
    )
    if args.kind is None:
        return _deps_whole_bank(graph, sources, scope)
    if args.kind == bk.Kind.PERFORMANCE:
        return _deps_one_performance(graph, sources, scope, args.number)
    return _deps_one_patch(graph, sources, scope, args.number)


def _parse_perf_specs(texts: Sequence[str]) -> List[Any]:
    from kws import relations

    chosen: List[Any] = []
    for text in texts:
        if ":" not in text:
            raise ValueError(f"copy-perf wants SCOPE:NUMBER, got {text!r}")
        scope, number = text.rsplit(":", 1)
        try:
            chosen.append(relations.PerfId(scope, int(number)))
        except ValueError:
            raise ValueError(f"copy-perf wants SCOPE:NUMBER, got {text!r}") from None
    return chosen


def _print_atomic_remap(plan: Any) -> None:
    for alloc in plan.allocations:
        print(
            f"perf {alloc.target_perf_slot} <- {alloc.source_perf.scope_id}:"
            f"{alloc.source_perf.number} {alloc.name}"
        )
        for patch, slot in alloc.allocated_patches:
            print(f"  patch {slot} <- {patch.scope_id}:{patch.number}")
    for why in plan.rejected:
        print("  left out:", why)


def _print_atomic_usage(plan: Any) -> None:
    from kws import banks as bk, records as rec

    print(f"performances {len(plan.allocations)}/{bk.PERFORMANCES_PER_BANK}")
    print(f"patches      {plan.patch_count}/{bk.PATCHES_PER_BANK}")
    print(f"wave seqs    {len(plan.sequence_map)}/{rec.SEQUENCES_PER_BANK}")
    print(f"steps        {plan.steps_used}/{rec.STEPS_PER_BANK}")


def _write_atomic_files(
    prefix: str, destination: Any, channel: int, built: Any
) -> List[str]:
    """Write the three bank files a copy produces, and say where."""
    from kws import messages as m

    written: List[str] = []
    for suffix, function, payload in (
        ("patch", m.Function.ALL_PATCH_DUMP, built.patches),
        ("perf", m.Function.ALL_PERFORMANCE_DUMP, built.performances),
        ("seq", m.Function.ALL_WAVESEQ_DUMP, built.waveseq),
    ):
        if payload is None:
            continue
        frame = m.frame_bulk(function, destination.sysex, payload, channel=channel)
        path = f"{prefix}-{suffix}.syx"
        with open(path, "wb") as handle:
            handle.write(bytes(frame))
        print(f"wrote {path}  ({len(frame)} bytes)")
        written.append(path)
    print("Nothing was sent to the instrument.")
    return written


def _copy_perf_fence(
    device: Any, args: argparse.Namespace, destination: Any, built: Any
) -> int:
    """Name what the destination holds, then write only with ``--yes``."""
    from kws import backup

    kinds = (banks.Kind.PERFORMANCE, banks.Kind.PATCH, "waveseq")
    print(
        f"about to replace: {destination.label}, all "
        f"{_word_list([_plural(kind) for kind in kinds])}"
    )
    print("if any read below fails, nothing is sent")
    for kind in kinds:
        try:
            held = _destination_names(device, destination, kind)
        except Exception as exc:
            print(f"     it now holds: could not be read ({exc})")
            print("Refusing to write a bank that cannot first be read and backed up.")
            return spec.EXIT_ERROR
        print(f"     it now holds ({_plural(kind)}): {held}")

    if not args.yes:
        print(
            "\nNothing has been sent. Add --yes to write, and be sure: "
            "this replaces the whole bank and no power cycle brings it "
            "back."
        )
        return spec.EXIT_OK

    where = args.backup_dir or backup.default_dir()
    result = device.write_bank_bundle(
        destination.number,
        performances=built.performances,
        patches=built.patches,
        waveseq=built.waveseq,
        backup_dir=where,
    )
    print(f"\nbacked up to: {result['backup']}")
    if result["verified"] is True:
        print("written and verified by reading it back")
        return spec.EXIT_OK
    print(f"written, but the read-back does NOT match: {result['mismatch']}")
    print("The backup above holds what was there before.")
    return spec.EXIT_ERROR


def _real_library_scope(scope: str) -> bool:
    import re

    from kws import banks as bk

    try:
        bk.bank(scope)
        return True
    except LookupError:
        pass
    return bool(re.match(r"^CARD/\d{1,2}$", scope) or re.match(r"^CARD/c\d+$", scope))


def _cmd_import(_device: Any, args: argparse.Namespace) -> int:
    import re

    from kws import bankstore, importers

    if getattr(args, "list", False):
        try:
            print(importers.describe_file(args.file))
        except (importers.UnsupportedFormat, ValueError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return spec.EXIT_USAGE
        return spec.EXIT_OK

    try:
        banks_list = importers.read_file(args.file)
    except (importers.UnsupportedFormat, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return spec.EXIT_USAGE

    if getattr(args, "label", None):
        selected = None
        for bank in banks_list:
            if bank.label != args.label:
                continue
            if getattr(args, "scope", None) and bank.scope != args.scope:
                continue
            selected = bank
            break
        if selected is None:
            candidates = banks_list
            if getattr(args, "scope", None):
                candidates = [b for b in banks_list if b.scope == args.scope]
            print(
                "available labels: " + ", ".join(b.label for b in candidates),
                file=sys.stderr,
            )
            return spec.EXIT_USAGE
    elif getattr(args, "scope", None):
        selected = None
        for bank in banks_list:
            if bank.scope == args.scope:
                selected = bank
                break
        if selected is None:
            print(
                "available scopes: " + ", ".join(b.scope for b in banks_list),
                file=sys.stderr,
            )
            return spec.EXIT_USAGE
    elif len(banks_list) == 1:
        selected = banks_list[0]
    else:
        print(
            "available scopes: " + ", ".join(b.scope for b in banks_list),
            file=sys.stderr,
        )
        return spec.EXIT_USAGE

    source_scope = selected.scope
    if selected.notes:
        print(f"note: {selected.notes}")
    dest = getattr(args, "as", None)
    if dest is None:
        if _real_library_scope(source_scope):
            dest = source_scope
        else:
            print(
                "error: this bank needs --as (its scope is not a real library scope)",
                file=sys.stderr,
            )
            return spec.EXIT_USAGE

    # A bank's interior PartRef/WaveRef banks point at the bank it came
    # from; storing it under a different internal bank would leave those
    # references aimed at the old one. A card source may be filed under any
    # CARD/NN slot (all card banks share the same interior number), but an
    # internal source may only keep its own scope. Relocating performances
    # is copy-perf's job.
    dest_is_card = bool(
        re.match(r"^CARD/\d{1,2}$", dest) or re.match(r"^CARD/c\d+$", dest)
    )
    try:
        banks.bank(dest)
    except LookupError:
        if not dest_is_card:
            print(f"error: {dest!r} is not a valid destination scope", file=sys.stderr)
            return spec.EXIT_USAGE
    if source_scope.startswith("CARD/"):
        if not dest_is_card:
            print(
                "error: a card bank can only be filed under CARD/NN or CARD/cN",
                file=sys.stderr,
            )
            return spec.EXIT_USAGE
    elif dest != source_scope:
        print(
            "error: a bank keeps its own interior bank references; use "
            f"copy-perf to move performances into {dest}",
            file=sys.stderr,
        )
        return spec.EXIT_USAGE

    performances = None
    if selected.source.performances:
        performances = b"".join(selected.source.performances) or None
    patches = None
    if selected.source.patches:
        patches = b"".join(selected.source.patches) or None
    waveseq = selected.source.waveseq

    bankstore.save(
        dest,
        performances=performances,
        patches=patches,
        waveseq=waveseq,
        directory=args.bankstore,
    )

    cache = args.bankstore if args.bankstore is not None else bankstore.default_dir()
    print(
        f"{dest}: {len(selected.source.performances)} performances, "
        f"{len(selected.source.patches)} patches, "
        f"waveseq={'yes' if waveseq is not None else 'no'}"
    )
    print(f"cache: {cache}")
    return spec.EXIT_OK


def _cmd_copy_perf(device: Any, args: argparse.Namespace) -> int:
    """Copy performances and their private patches into one bank."""
    from kws import banks as bk, importers, perfcopy

    try:
        chosen = _parse_perf_specs(args.spec)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return spec.EXIT_USAGE
    destination = bk.bank(args.to)
    target_scope = destination.id

    # read --from-file banks first
    file_sources: Dict[str, Any] = {}
    for path in getattr(args, "from_file", None) or []:
        try:
            imported = importers.read_file(path)
        except (importers.UnsupportedFormat, ValueError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return spec.EXIT_USAGE
        for bank in imported:
            file_sources[bank.scope] = bank.source

    requested: List[str] = []
    for perf in chosen:
        if perf.scope_id not in requested:
            requested.append(perf.scope_id)
    active = _card_slot_active_scope(args)
    allowed = {active} if active is not None else None
    try:
        sources = _cli_sources(
            device,
            specs=args.source,
            bankstore_dir=args.bankstore,
            need=[s for s in requested if s not in file_sources],
            allow_card_slots=allowed,
        )
    except (LookupError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return spec.EXIT_USAGE
    sources.update(file_sources)

    if target_scope not in sources:
        try:
            target_source = _scope_source(device, target_scope, args.bankstore)
        except Exception:
            target_source = None
        if target_source is not None:
            sources[target_scope] = target_source
        elif args.mode == "append":
            print(
                f"note: {destination.label} could not be loaded, so append "
                f"starts from an empty bank"
            )
    _extend_sources(
        sources,
        device,
        _part_scopes(sources, chosen),
        args.bankstore,
        allow_card_slots=allowed,
    )
    _extend_sources(
        sources,
        device,
        _sequence_scopes(sources, chosen),
        args.bankstore,
        allow_card_slots=allowed,
    )

    built = perfcopy.copy_into_bank(
        sources,
        target_scope,
        chosen,
        mode=args.mode,
        destination_bank=destination,
    )
    if built.plan.rejected:
        print(
            "error: the copy does not fit the destination: "
            + "; ".join(built.plan.rejected),
            file=sys.stderr,
        )
        return spec.EXIT_ERROR
    _print_atomic_remap(built.plan)
    _print_atomic_usage(built.plan)

    if args.out is not None:
        _write_atomic_files(
            args.out,
            destination,
            (args.channel or 1) - 1,
            built,
        )
        return spec.EXIT_OK
    return _copy_perf_fence(device, args, destination, built)


def _cmd_backup_list(_args: argparse.Namespace) -> int:
    from kws import backup

    directory = getattr(_args, "backup_dir", None) or backup.default_dir()
    entries = sorted(
        backup.list_backups(directory), key=lambda e: e.saved, reverse=True
    )
    if not entries:
        print(f"no backups in {directory}")
        return spec.EXIT_OK
    rows = [
        [
            entry.kind or "?",
            entry.slot or "?",
            entry.name or "?",
            f"{entry.saved:.0f}" if entry.saved else "?",
        ]
        for entry in entries
    ]
    print(render.table(["kind", "slot", "name", "saved"], rows))
    return spec.EXIT_OK


def _function_for_kind(kind: str) -> int:
    from kws import messages as m

    functions = {
        banks.Kind.PERFORMANCE: m.Function.ALL_PERFORMANCE_DUMP,
        banks.Kind.PATCH: m.Function.ALL_PATCH_DUMP,
        "waveseq": m.Function.ALL_WAVESEQ_DUMP,
    }
    try:
        return functions[kind]
    except KeyError:
        raise ValueError(
            f"cannot back up {kind!r}; have {banks.Kind.PERFORMANCE}, "
            f"{banks.Kind.PATCH}"
        ) from None


def _bank_size(kind: str) -> int:
    from kws import records as rec

    if kind == banks.Kind.PATCH:
        return banks.PATCHES_PER_BANK * rec.PATCH_SIZE
    if kind == banks.Kind.PERFORMANCE:
        return banks.PERFORMANCES_PER_BANK * rec.PERFORMANCE_SIZE
    if kind == "waveseq":
        return rec.WAVESEQ_BANK_SIZE
    raise ValueError(f"no bank size for {kind!r}")


def _confirmed(args: argparse.Namespace, what: str, does: str) -> bool:
    """The command line's arm-then-fire for a destructive verb.

    The TUI has a screen for this; the command line has ``--yes``. A flag
    that is missing is a refusal, not a prompt: nothing is read, sent or
    written, and the message says how to proceed.
    """
    if getattr(args, "yes", False):
        return True
    print(
        f"error: refusing to run `{what}` without --yes: it {does}. Nothing was done.",
        file=sys.stderr,
    )
    return False


def _backup_gate(args: argparse.Namespace) -> bool:
    """True when a backup verb may run, or when it needs no confirmation."""
    which = getattr(args, "backup_command", None)
    if which == "save":
        return _confirmed(
            args,
            "backup save",
            "reads every selected bank and writes the backup envelopes",
        )
    if which == "restore":
        return _confirmed(
            args,
            "backup restore",
            "replaces a whole bank on the unit",
        )
    return True


def _cmd_backup_save(device: Any, args: argparse.Namespace) -> int:
    """Save one envelope per bank and kind. Read-only, **makes no sound**.

    ``--bank`` names one bank; without it every writable bank is saved. A
    bank holds performances and patches, which are different objects with
    different dump requests, so each bank yields one envelope per kind.
    The envelope is the family's; its ``dump`` block is the exact System
    Exclusive message a ``.syx`` file would hold, bank byte, checksum and
    all, so any tool that speaks the protocol can still restore it.
    """
    from kws import backup, dumps
    from kws import messages as m

    if not _backup_gate(args):
        return spec.EXIT_USAGE
    if args.bank is None:
        selected = banks.writable_banks()
    else:
        entry = banks.bank(args.bank)
        if not entry.writable:
            print(
                f"{entry.label} is ROM and cannot be written. The banks "
                f"that can are: "
                f"{', '.join(b.label for b in banks.writable_banks())}."
            )
            return spec.EXIT_USAGE
        selected = [entry]
    kinds = (
        [args.kind]
        if getattr(args, "kind", None)
        else [banks.Kind.PERFORMANCE, banks.Kind.PATCH]
    )
    directory = getattr(args, "backup_dir", None) or backup.default_dir()
    channel = getattr(device, "channel", None)
    device_id = "" if channel is None else str(channel)
    failures = 0
    for entry in selected:
        for kind in kinds:
            function = _function_for_kind(kind)
            try:
                raw = device.read_raw(entry.sysex, function)
            except Exception as exc:
                print(f"{entry.label} — {_plural(kind)}: could not be read ({exc})")
                failures += 1
                continue
            try:
                packet = m.parse(raw)
                if packet is None:
                    raise ValueError("the reply was not a Wavestation message")
                dump = dumps.parse_bulk(packet)
            except Exception as exc:
                print(f"{entry.label} — {_plural(kind)}: not a bank dump ({exc})")
                failures += 1
                continue
            if dump.bank.number != entry.number or dump.kind != kind:
                print(
                    f"{entry.label} — {_plural(kind)}: the reply was for "
                    f"{dump.bank.label} {_plural(dump.kind)}, so it was not saved"
                )
                failures += 1
                continue
            try:
                path = backup.save(
                    directory,
                    kind=kind,
                    slot=entry.label,
                    name=entry.label,
                    device_id=device_id,
                    source="kwsed",
                    blocks={"dump": bytes(raw)},
                )
            except backup.BackupError as exc:
                print(f"{entry.label} — {_plural(kind)}: could not be saved ({exc})")
                failures += 1
                continue
            print(f"{entry.label} — {_plural(kind)}: {path}")
    return spec.EXIT_ERROR if failures else spec.EXIT_OK


def _envelope_block(record: Any, kind: str) -> bytes:
    blocks = record.blocks
    if not isinstance(blocks, dict) or not blocks:
        raise ValueError("it holds no blocks")
    if "dump" in blocks:
        return bytes(blocks["dump"])
    if kind in blocks:
        return bytes(blocks[kind])
    if len(blocks) == 1:
        return bytes(next(iter(blocks.values())))
    raise ValueError(
        f"it holds blocks {', '.join(sorted(blocks))} and none is the whole bank"
    )


def _restore_plan(record: Any) -> "tuple[banks.Bank, str, bytes]":
    """``(destination, kind, payload)`` from an envelope, or ``ValueError``.

    The fence backups written by :meth:`WavestationBridge.write_bank` hold
    the message exactly as it arrived. A hand-saved envelope may hold the
    decoded records instead; both are accepted, and both are checked
    against the kind and bank the envelope claims before anything is sent.
    """
    from kws import messages as m

    kind = record.kind
    if kind not in (banks.Kind.PATCH, banks.Kind.PERFORMANCE):
        raise ValueError(
            f"it holds a {kind!r} backup; only whole-bank patch or "
            f"performance envelopes can be restored"
        )
    raw = _envelope_block(record, kind)
    packet = m.parse(raw) if raw.startswith(b"\xf0") else None
    frame_bank = None
    if packet is not None:
        expected = _function_for_kind(kind)
        if packet.function != expected:
            raise ValueError(
                f"its dump block is a {packet.function_name}, not a "
                f"{_plural(kind)} bank"
            )
        data = packet.data()
        if packet.bank is not None:
            frame_bank = banks.by_sysex(packet.bank)
    else:
        data = raw

    slot = (record.slot or "").strip()
    named = None
    if slot:
        try:
            named = banks.bank(slot)
        except LookupError:
            named = None
    if (
        named is not None
        and frame_bank is not None
        and named.number != frame_bank.number
    ):
        raise ValueError(
            f"its slot says {named.label} but its dump was for {frame_bank.label}"
        )
    destination = named or frame_bank
    if destination is None:
        raise ValueError(
            f"it does not name a bank (slot {record.slot!r}) and its dump "
            f"carries no bank byte"
        )

    size = _bank_size(kind)
    if len(data) < size:
        raise ValueError(
            f"its dump block decodes to {len(data)} bytes; a {kind} bank is {size}"
        )
    return destination, kind, bytes(data[:size])


_BUNDLE_BLOCK_KINDS: "tuple[tuple[str, str], ...]" = (
    ("performances", banks.Kind.PERFORMANCE),
    ("patches", banks.Kind.PATCH),
    ("waveseq", "waveseq"),
)


def _word_list(words: "list[str]") -> str:
    """Three kinds read ``a, b and c``; two read ``a and b``."""
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def _restore_bundle_plan(record: Any) -> "tuple[banks.Bank, Dict[str, bytes]]":
    """``(destination, {block name: decoded bytes})`` from a bundle envelope.

    The fence backups of :meth:`WavestationBridge.write_bank_bundle` hold
    one framed dump per kind, wave sequences through performances, exactly
    as they arrived. A hand-saved envelope may hold the decoded records
    instead; both are accepted, and every block is checked against the
    whole-bank size its kind claims -- and, when its frame carries one,
    against the bank it says it is for -- before anything is sent.
    """
    from kws import messages as m

    if record.kind != "bank-bundle":
        raise ValueError(f"it holds a {record.kind!r} backup, not a bank-bundle one")
    blocks = getattr(record, "blocks", None)
    if not isinstance(blocks, dict) or not blocks:
        raise ValueError("it holds no blocks")
    kinds = dict(_BUNDLE_BLOCK_KINDS)
    held = [name for name, _kind in _BUNDLE_BLOCK_KINDS if name in blocks]
    if not held:
        raise ValueError(
            f"it holds blocks {', '.join(sorted(blocks))} and no whole bank"
        )
    slot = (record.slot or "").strip()
    named = None
    if slot:
        try:
            named = banks.bank(slot)
        except LookupError:
            named = None
    destination: Optional[banks.Bank] = named
    payloads: Dict[str, bytes] = {}
    for name in held:
        kind = kinds[name]
        raw = bytes(blocks[name])
        packet = m.parse(raw) if raw.startswith(b"\xf0") else None
        data = raw
        if packet is not None:
            if packet.function != _function_for_kind(kind):
                raise ValueError(
                    f"its {name} block is a {packet.function_name}, not a "
                    f"{_plural(kind)} bank"
                )
            data = packet.data()
            if packet.bank is not None:
                packet_bank = banks.by_sysex(packet.bank)
                if destination is None:
                    destination = packet_bank
                elif destination.number != packet_bank.number:
                    raise ValueError(
                        f"its {name} dump was for {packet_bank.label} but "
                        f"the restore said {destination.label}"
                    )
        size = _bank_size(kind)
        if len(data) < size:
            raise ValueError(
                f"its {name} block decodes to {len(data)} bytes; a {kind} "
                f"bank is {size}"
            )
        payloads[name] = bytes(data[:size])
    if destination is None:
        raise ValueError(
            f"it does not name a bank (slot {record.slot!r}) and no dump "
            f"carries a bank byte"
        )
    return destination, payloads


def _cmd_backup_restore(device: Any, args: argparse.Namespace) -> int:
    """Load an envelope and write its bank back. **This destroys a bank.**

    The write goes through :meth:`WavestationBridge.write_bank` -- or, for
    a bank-bundle envelope, :meth:`WavestationBridge.write_bank_bundle` --
    not around it: the destination is read in full first, the pre-write
    snapshot is saved, the dump is sent at the ALSA pace and the result is
    read back and compared. The gate here only decides whether to ask it
    to.
    """
    from kws import backup

    if not _backup_gate(args):
        return spec.EXIT_USAGE
    try:
        record = backup.load(args.file)
    except (backup.BackupError, ValueError) as exc:
        print(f"{args.file} is not a backup kwsed can read ({exc})")
        return spec.EXIT_USAGE
    bundle = str(record.kind) == "bank-bundle"
    try:
        if bundle:
            destination, payloads = _restore_bundle_plan(record)
        else:
            destination, kind, payload = _restore_plan(record)
    except (LookupError, ValueError) as exc:
        print(f"{args.file} cannot be restored: {exc}")
        return spec.EXIT_USAGE
    if not destination.writable:
        print(
            f"{destination.label} is ROM and cannot be written. The banks "
            f"that can are: "
            f"{', '.join(b.label for b in banks.writable_banks())}."
        )
        return spec.EXIT_USAGE
    where = getattr(args, "backup_dir", None) or backup.default_dir()
    if bundle:
        return _restore_bundle_run(device, args, destination, payloads, where)

    plural = _plural(kind)
    print(f"about to replace: {destination.label}, all {plural}")
    print(f"          source: {args.file} ({len(payload)} bytes)")
    try:
        held = _destination_names(device, destination, kind)
    except Exception as exc:
        print(f"     it now holds: could not be read ({exc})")
        print("Refusing to write a bank that cannot first be read and backed up.")
        return spec.EXIT_ERROR
    print(f"     it now holds: {held}")

    result = device.write_bank(destination.number, kind, payload, backup_dir=where)
    print(f"\nbacked up to: {result['backup']}")
    if result["verified"] is True:
        print("written and verified by reading it back")
        return spec.EXIT_OK
    print(f"written, but the read-back does NOT match: {result['mismatch']}")
    print("The backup above holds what was there before.")
    return spec.EXIT_ERROR


def _restore_bundle_run(
    device: Any,
    args: argparse.Namespace,
    destination: banks.Bank,
    payloads: Dict[str, bytes],
    where: str,
) -> int:
    """Write every kind of a bank-bundle envelope back, as **one** write.

    The gate and the ROM check have already passed. Every kind is named
    and read before anything is sent: a bundle that cannot read what it
    is about to replace is not written, so a failed read below leaves the
    unit exactly as it was.
    """
    kinds = dict(_BUNDLE_BLOCK_KINDS)
    replaced = [
        _plural(kinds[name]) for name, _kind in _BUNDLE_BLOCK_KINDS if name in payloads
    ]
    print(f"about to replace: {destination.label}, all {_word_list(replaced)}")
    print(f"          source: {args.file}")
    print("if any read below fails, nothing is sent")
    for name, kind in _BUNDLE_BLOCK_KINDS:
        if name not in payloads:
            continue
        try:
            held = _destination_names(device, destination, kind)
        except Exception as exc:
            print(f"     it now holds: could not be read ({exc})")
            print("Refusing to write a bank that cannot first be read and backed up.")
            return spec.EXIT_ERROR
        print(f"     it now holds ({_plural(kind)}): {held}")
    result = device.write_bank_bundle(
        destination.number,
        performances=payloads.get("performances"),
        patches=payloads.get("patches"),
        waveseq=payloads.get("waveseq"),
        backup_dir=where,
    )
    print(f"\nbacked up to: {result['backup']}")
    if result["verified"] is True:
        print("written and verified by reading it back")
        return spec.EXIT_OK
    print(f"written, but the read-back does NOT match: {result['mismatch']}")
    print("The backup above holds what was there before.")
    return spec.EXIT_ERROR


def _cmd_backup(device: Any, args: argparse.Namespace) -> int:
    which = getattr(args, "backup_command", None)
    if which == "list":
        return _cmd_backup_list(args)
    if which == "save":
        return _cmd_backup_save(device, args)
    if which == "restore":
        return _cmd_backup_restore(device, args)
    raise ValueError(f"no backup command {which!r}")


_COMMANDS: Dict[str, Callable[[Any, argparse.Namespace], Any]] = {
    "banks": _cmd_banks,
    "list": _cmd_list,
    "find": _cmd_find,
    "ports": _cmd_ports,
    "status": _cmd_status,
    "dump": _cmd_dump,
    "select": _cmd_select,
    "send": _cmd_send,
    "multi": _cmd_multi,
    "backup": _cmd_backup,
    "cards": _cmd_cards,
    "deps": _cmd_deps,
    "copy-perf": _cmd_copy_perf,
    "import": _cmd_import,
}
_OFFLINE = {"banks", "list", "find", "ports", "cards", "import"}


def _has_source_for(args: argparse.Namespace, scope: str) -> bool:
    """True when ``--source`` names ``scope`` (its ID, not its glob)."""
    for text in getattr(args, "source", None) or ():
        if "=" in text and text.split("=", 1)[0] == scope:
            return True
    return False


def _deps_offline(args: argparse.Namespace) -> bool:
    """True when the bank `deps` must look at is already on disk."""
    from kws import bankstore

    scope = getattr(args, "bank", None)
    if not scope:
        return False
    if _has_source_for(args, scope):
        return True
    where = getattr(args, "bankstore", None) or bankstore.default_dir()
    return bankstore.load(scope, where) is not None


def _offline(args: argparse.Namespace) -> bool:
    """True when a command needs no unit. ``backup list`` is the one
    subcommand that qualifies: ``save`` and ``restore`` both open one.

    ``deps`` is offline when the bank it asks about is loaded from
    ``--source`` or the bank cache; ``copy-perf`` is offline when it writes
    files with ``--out``, because that path touches no unit at all.
    """
    if args.command == "backup":
        return getattr(args, "backup_command", None) == "list"
    if args.command == "deps":
        return _deps_offline(args)
    if args.command == "copy-perf":
        return getattr(args, "out", None) is not None
    return args.command in _OFFLINE


def build_parser() -> argparse.ArgumentParser:
    parser = cli.make_parser(
        prog="kwscli",
        description="Browse a Korg Wavestation SR's sounds.",
        distribution="kwsed",
        epilog=build_epilog(),
    )
    # config=False and favorites=False: nothing in kwscli *offers* either. A
    # flag that is accepted and then ignored teaches the user that a flag
    # can lie, which docs/UX-SPEC.md section 2 says is worse than no flag.
    # The settings file is still read and written, at the same default the
    # browser uses (./config.toml) -- see _build_device.
    cli.add_common_arguments(
        parser,
        port=True,
        scan=True,
        recv_port=True,
        channel=True,
        device_channel=True,
        catalog=True,
        config=False,
        favorites=False,
    )
    parser.add_argument(
        "--live-names",
        "--live",
        dest="live_names",
        default=None,
        help=(
            "cache of names read off the unit "
            f"(default: {_livenames.default_path()}; safe to delete)"
        ),
    )
    parser.add_argument(
        "--cards",
        dest="cards",
        default=None,
        help=(f"HCard bank archive (default: {_cards.default_path()}; safe to delete)"),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    kinds = [banks.Kind.PERFORMANCE, banks.Kind.PATCH]
    sub.add_parser("banks", help="the bank table (no unit needed)")
    sp = sub.add_parser(
        "cards", help="the HCard's forty banks and their archive (no unit needed)"
    )
    sp.add_argument(
        "bank",
        nargs="?",
        default=None,
        help="show one bank, 00-39 (default: the whole table)",
    )
    sp.add_argument(
        "--kind",
        choices=kinds,
        default=None,
        help="which kind --syx should hold (default: neither)",
    )
    sp.add_argument(
        "--syx",
        default=None,
        metavar="FILE",
        help="write one archived bank as a .syx (names only; '-' for stdout)",
    )
    sp.add_argument(
        "--label",
        default=None,
        metavar="TEXT",
        help="set a local label on a captured bank (offline)",
    )
    sp = sub.add_parser(
        "deps",
        help="the Perf -> Patch -> WaveSeq graph for a bank or slot "
        "(no unit when cached)",
    )
    sp.add_argument(
        "bank",
        help="scope: RAM1, ROM6, CARD/07 or CARD/c1",
    )
    sp.add_argument(
        "kind",
        nargs="?",
        default=None,
        choices=kinds,
        help="show one performance or patch instead of the whole bank",
    )
    sp.add_argument(
        "number",
        nargs="?",
        type=int,
        default=None,
        help="the slot to show, with a kind",
    )
    sp.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="ID=GLOB",
        help="a source bank's dumps; repeatable",
    )
    sp.add_argument(
        "--bankstore",
        default=None,
        metavar="DIR",
        help="bank cache directory (default: the per-platform data dir)",
    )
    sp = sub.add_parser(
        "copy-perf",
        help="copy performances with their private patches into one bank",
    )
    sp.add_argument(
        "spec",
        nargs="+",
        metavar="SCOPE:NUMBER",
        help="a performance to copy, in priority order",
    )
    sp.add_argument(
        "--to",
        required=True,
        choices=["RAM1", "RAM2", "RAM3", "CARD"],
        help="destination bank",
    )
    sp.add_argument(
        "--mode",
        choices=["append", "clean"],
        default="append",
        help="append to the destination's active performances, or replace it",
    )
    sp.add_argument(
        "--out",
        default=None,
        metavar="PREFIX",
        help="write <PREFIX>-perf.syx etc.; omit to write to the unit",
    )
    sp.add_argument(
        "--from-file",
        action="append",
        default=[],
        metavar="FILE",
        help=(
            "a bank file (SysEx, MIDI, .sqs, .LIB, .img, .zip or .bnk) "
            "to copy from; repeatable"
        ),
    )
    sp.add_argument(
        "--yes", action="store_true", help="actually write; without it nothing is sent"
    )
    sp.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="ID=GLOB",
        help="a source bank's dumps; repeatable",
    )
    sp.add_argument(
        "--bankstore",
        default=None,
        metavar="DIR",
        help="bank cache directory (default: the per-platform data dir)",
    )
    sp.add_argument(
        "--backup-dir",
        default=None,
        help="where to save the destination before overwriting",
    )
    sp = sub.add_parser("import", help="read a bank from a file into the bank cache")
    sp.add_argument("file")
    sp.add_argument("--list", action="store_true", help="list the file's banks")
    sp.add_argument(
        "--scope",
        default=None,
        metavar="SCOPE",
        help="choose the first bank with this scope",
    )
    sp.add_argument(
        "--label",
        default=None,
        metavar="LABEL",
        help="exact bank label from --list",
    )
    sp.add_argument(
        "--as",
        dest="as",
        default=None,
        metavar="DEST",
        help="cache scope (default: the source's own internal scope)",
    )
    sp.add_argument(
        "--bankstore",
        default=None,
        metavar="DIR",
        help="bank cache directory (default: the per-platform data dir)",
    )
    sp = sub.add_parser("list", help="one bank's slots (no unit needed)")
    sp.add_argument("bank")
    sp.add_argument("kind", choices=kinds)
    sp = sub.add_parser("find", help="search names (no unit needed)")
    sp.add_argument("name")
    sub.add_parser("ports", help="MIDI ports on this host")
    sub.add_parser("status", help="ask the unit who it is")
    sp = sub.add_parser("dump", help="read a bank off the unit (silent)")
    sp.add_argument("bank")
    sp.add_argument(
        "kind",
        nargs="?",
        default=banks.Kind.PATCH,
        choices=kinds,
        help="which kind to ask for (default: patch)",
    )
    sp = sub.add_parser("select", help="select a sound ON THE UNIT")
    sp.add_argument("bank")
    sp.add_argument("kind", choices=kinds)
    sp.add_argument("number", type=int)
    sp = sub.add_parser("multi", help="the 32 Multisets (silent)")
    sp.add_argument(
        "--all", action="store_true", help="include Multisets with nothing assigned"
    )
    sp = sub.add_parser("send", help="WRITE a bank dump INTO the unit")
    sp.add_argument("file", help="a .syx holding one bank dump")
    sp.add_argument(
        "--to", required=True, help="destination bank: RAM1, RAM2, RAM3 or CARD"
    )
    sp.add_argument(
        "--yes", action="store_true", help="actually write; without it nothing is sent"
    )
    sp.add_argument(
        "--backup-dir",
        default=None,
        help="where to save the destination before overwriting",
    )
    sp = sub.add_parser("backup", help="list, save or restore bank backups")
    backup_sub = sp.add_subparsers(dest="backup_command", required=True)
    sp_list = backup_sub.add_parser("list", help="list envelopes in the backup dir")
    sp_list.add_argument(
        "--backup-dir",
        default=None,
        help="backup directory to list (default: the per-platform data dir)",
    )
    sp_save = backup_sub.add_parser(
        "save", help="read banks off the unit into backup envelopes (silent)"
    )
    sp_save.add_argument(
        "--bank",
        default=None,
        help=(
            "one bank to save: RAM1, RAM2, RAM3 or CARD "
            "(default: all four writable banks)"
        ),
    )
    sp_save.add_argument(
        "--kind",
        default=None,
        choices=kinds,
        help="save only one kind (default: performances and patches)",
    )
    sp_save.add_argument(
        "--yes", action="store_true", help="confirm; without it nothing is read"
    )
    sp_save.add_argument(
        "--backup-dir",
        default=None,
        help="where to write the envelopes (default: the per-platform data dir)",
    )
    sp_restore = backup_sub.add_parser(
        "restore", help="WRITE a saved bank back into the unit"
    )
    sp_restore.add_argument("file", help="an envelope written by `backup save`")
    sp_restore.add_argument(
        "--yes", action="store_true", help="actually write; without it nothing is sent"
    )
    sp_restore.add_argument(
        "--backup-dir",
        default=None,
        help="where to save the destination before overwriting",
    )
    return parser


def _build_device(args: argparse.Namespace) -> Any:
    if args.demo:
        from kwsed.demo import DemoDevice

        return DemoDevice()
    from kws import bridge as b
    from kws import config as cfg

    b.install_clean_exit()
    if args.port:
        return b.WavestationBridge.open(
            args.port,
            recv_port=args.recv_port,
            # --midi-channel is now --device-channel; the old spelling still
            # parses. One-based on every command line, zero-based on the wire.
            channel=(args.device_channel - 1) if args.device_channel else 0,
        )
    # No --config flag on this parser, so the settings file is the browser's
    # default per-tool path. Migrate a legacy working-directory file first.
    cfg.migrate_legacy_config("kwsed")
    # Heal a settings file an older build wrote UI state into: the unquoted
    # dotted key is a table, and the settings writer refuses such a file.
    cfg.migrate_ui_state(cfg.DEFAULT_PATH)
    import os

    os.makedirs(cfg.config_dir("kwsed"), exist_ok=True)
    # One Identity Request to the remembered port; the sweep only when there
    # is nothing remembered, when the remembered port no longer answers, or
    # on --scan.
    return b.WavestationBridge.connect(
        config_path=cfg.DEFAULT_PATH,
        # --midi-channel is now --device-channel; the old spelling still
        # parses. One-based on every command line, zero-based on the wire.
        channel=(args.device_channel - 1) if args.device_channel else 0,
        scan=args.scan,
        on_try=lambda name: print(f"  probing {name}...", file=sys.stderr),
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    handler = _COMMANDS[args.command]
    # Both channels, both named by the family now: --channel is the one a
    # program change goes out on, --device-channel the unit's own base.
    cli.validate_common(args)

    if _offline(args):
        try:
            code = handler(None, args)
        except (LookupError, ValueError) as exc:
            sys.exit(f"error: {exc}")
        return int(code or 0)
    # A backup verb's --yes is checked before a port is opened, so a missing
    # confirmation costs a message rather than a MIDI session.
    if not _backup_gate(args):
        return spec.EXIT_USAGE
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
