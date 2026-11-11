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

"""A stand-in Wavestation, so kwsed runs with no hardware and no MIDI ports.

It **builds real System Exclusive**: nibble-encodes its dumps with
:func:`kws.messages.to_nibbles` and lets the parser decode them again. A
dump this fake produces is one the encoder really encoded, which is the one
useful thing a fake can establish on its own.

What it cannot establish is that a Wavestation agrees. Names are invented,
per CLAUDE.md, from a pool obviously not Korg's.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Mapping, Optional, Tuple

from kws import backup
from kws import banks
from kws import dumps
from kws import messages as m
from kws import records

__all__ = ["DemoDevice", "demo_name"]

_ADJECTIVES = (
    "Velvet",
    "Rusted",
    "Glass",
    "Hollow",
    "Distant",
    "Folded",
    "Amber",
    "Quiet",
    "Drifting",
    "Woven",
    "Paper",
    "Tidal",
)
_NOUNS = (
    "Bell",
    "Pad",
    "Bass",
    "Lead",
    "Choir",
    "Pluck",
    "Sweep",
    "Stack",
    "Drone",
    "Keys",
    "Brass",
    "Wash",
)


def demo_name(bank_id: str, kind: str, number: int) -> str:
    seed = sum(ord(c) for c in bank_id + kind) * 31 + number * 17
    adjective = _ADJECTIVES[seed % len(_ADJECTIVES)]
    noun = _NOUNS[(seed // len(_ADJECTIVES)) % len(_NOUNS)]
    return f"{adjective} {noun}"[: m.NAME_LEN - 1]


#: The decoded size of each whole-bank payload a caller may write. The same
#: three the bridge checks, so the fake refuses what the unit would.
_WRITE_SIZES = {
    banks.Kind.PERFORMANCE: banks.PERFORMANCES_PER_BANK * records.PERFORMANCE_SIZE,
    banks.Kind.PATCH: banks.PATCHES_PER_BANK * records.PATCH_SIZE,
    "waveseq": records.WAVESEQ_BANK_SIZE,
}

#: A bundle names its kinds in the plural, as the bridge's result does; the
#: stored key stays the singular kind a plain write uses.
_BUNDLE_KINDS = {
    "performances": banks.Kind.PERFORMANCE,
    "patches": banks.Kind.PATCH,
    "waveseq": "waveseq",
}


#: The framed dump function for each writable kind, mirroring
#: ``kws.bridge``'s mapping so a backup envelope matches the bridge's.
_DUMP_FUNCTION = {
    banks.Kind.PATCH: m.Function.ALL_PATCH_DUMP,
    banks.Kind.PERFORMANCE: m.Function.ALL_PERFORMANCE_DUMP,
    "waveseq": m.Function.ALL_WAVESEQ_DUMP,
}


class DemoDevice:
    """Answers the subset of the protocol kwsed speaks."""

    def __init__(
        self,
        *,
        latency: float = 0.0,
        channel: int = 4,
        card_name: Optional[str] = None,
    ) -> None:
        self.description = "demo (no MIDI ports opened)"
        #: The name the fake card reports. Invented, like every demo name.
        self.card_name = card_name
        #: Not channel 0: a fake on the default would let a caller that
        #: never set the channel work by accident and fail on real hardware.
        self.channel = channel
        self.timeout = 1.0
        self.latency = latency
        self.selected: Optional[banks.Slot] = None
        self.selected_log: List[banks.Slot] = []
        self.selected_multi: Optional[int] = None
        self.selected_multi_log: List[int] = []
        #: The last Multi Mode Setup the fake was asked to store, and how
        #: many times: the editor's fence is exercised without a unit.
        self.multi_written: Optional[bytes] = None
        self.multi_write_count: int = 0
        #: What the fake was asked to write, per ``(bank id, kind)``. Stored
        #: decoded, so a later read returns exactly the bytes that went in --
        #: which is what a read-back and an undo have to see.
        self._written: Dict[Tuple[str, str], bytes] = {}
        self.bank_writes: List[Dict[str, Any]] = []
        self.bank_write_count: int = 0
        self.bundle_writes: List[Dict[str, Any]] = []
        self.bundle_write_count: int = 0
        self.received: List[bytes] = []
        self.identify_calls: int = 0
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def _tick(self) -> None:
        if self.latency:
            time.sleep(self.latency)

    def identify(self, *, timeout: float = 0.5) -> Dict[str, Any]:
        self.identify_calls += 1
        self._tick()
        return {
            "channel": self.channel,
            "manufacturer": m.KORG_ID,
            "family": (m.WAVESTATION_ID, 0),
            "is_korg": True,
            "is_wavestation": True,
        }

    def handle(self, data: bytes) -> Optional[bytes]:
        self.received.append(bytes(data))
        self._tick()
        packet = m.parse(data)
        if packet is None or packet.channel != self.channel:
            return None
        if packet.function == m.Function.WAVESEQ_BANK_REQUEST and packet.payload:
            return self.read_raw(packet.payload[0], m.Function.ALL_WAVESEQ_DUMP)
        kinds = {
            m.Function.BULK_REQUEST: banks.Kind.PATCH,
            m.Function.PERFORMANCE_BANK_REQUEST: banks.Kind.PERFORMANCE,
        }
        if packet.function not in kinds or not packet.payload:
            return m.frame(m.Function.DATA_LOAD_ERROR, channel=self.channel)
        return self._bank_dump(packet.payload[0], kinds[packet.function])

    def _bank_dump(self, number: int, kind: str) -> bytes:
        # The byte on the wire, not a display position: byte 3 is the
        # Card, and banks.bank() would resolve it to ROM 4.
        entry = banks.by_sysex(number)
        if kind == banks.Kind.PERFORMANCE:
            function, size = (m.Function.ALL_PERFORMANCE_DUMP, dumps.PERFORMANCE_RECORD)
        else:
            function, size = m.Function.ALL_PATCH_DUMP, dumps.PATCH_RECORD
        stored = self._written.get((entry.id, kind))
        if stored is not None:
            return m.frame_bulk(function, number, stored, channel=self.channel)
        raw = bytearray()
        for index in range(banks.count_for(kind)):
            record = bytearray(size)
            name = demo_name(entry.id, kind, index)
            record[: len(name)] = name.encode("ascii")
            # A byte with bit 7 set, so the nibble encoding is exercised.
            record[m.NAME_LEN] = 0x80 | (index & 0x7F)
            raw.extend(record)
        payload = [entry.sysex] + list(m.to_nibbles(bytes(raw)))
        return m.frame(function, payload, channel=self.channel)

    def read_raw(
        self,
        bank_number: int,
        function: int,
        *,
        timeout: Optional[float] = None,
    ) -> bytes:
        entry = banks.by_sysex(bank_number)
        if function in (m.Function.ALL_PERFORMANCE_DUMP, m.Function.ALL_PATCH_DUMP):
            kind = (
                banks.Kind.PERFORMANCE
                if function == m.Function.ALL_PERFORMANCE_DUMP
                else banks.Kind.PATCH
            )
            return self._bank_dump(bank_number, kind)
        if function == m.Function.ALL_WAVESEQ_DUMP:
            stored = self._written.get((entry.id, "waveseq"))
            if stored is not None:
                data = bytes(stored)
            else:
                pool = bytearray(records.WAVESEQ_BANK_SIZE)
                for i in range(records.SEQUENCES_PER_BANK):
                    name = f"Demo {i:02d}"
                    at = records.SEQNAME_BASE + i * records.SEQNAME_SIZE
                    encoded = name.encode("ascii")
                    pool[at : at + len(encoded)] = encoded
                data = bytes(pool)
            return bytes(
                m.frame_bulk(function, entry.sysex, data, channel=self.channel)
            )
        raise ValueError(f"unsupported function {function:#04x}")

    # --- the operations kwsed uses -------------------------------------------

    def read_bank(
        self,
        bank: int | str,
        kind: Optional[str] = None,
        *,
        timeout: Optional[float] = None,
    ) -> dumps.BulkDump:
        entry = banks.bank(bank)
        if kind == banks.Kind.PERFORMANCE:
            request = m.bank_request(
                entry.sysex, m.Function.ALL_PERFORMANCE_DUMP, self.channel
            )
        else:
            request = m.bulk_request(entry.sysex, self.channel)
        reply = self.handle(request)
        # handle() returns None when it declines the message -- a channel
        # mismatch, say -- which used to reach m.parse as a None and only
        # be caught by the isinstance test inside it.
        if reply is None:
            raise RuntimeError("demo device declined its own request")
        packet = m.parse(reply)
        if packet is None or packet.is_error:
            raise RuntimeError("demo device refused its own request")
        return dumps.parse_bulk(packet)

    def read_bank_names(
        self,
        bank: int | str,
        kind: Optional[str] = None,
        *,
        timeout: Optional[float] = None,
    ) -> Tuple[str, Dict[int, str]]:
        dump = self.read_bank(bank, kind)
        return dump.kind, dump.names()

    def read_card_name(self, *, timeout: Optional[float] = None) -> Optional[str]:
        """The fake card's name, or ``None`` when it has not got one."""
        self._tick()
        return self.card_name

    def select(self, slot: banks.Slot, *, channel: Optional[int] = None) -> None:
        self._tick()
        self.selected = slot
        self.selected_log.append(slot)

    def select_multi(self, number: int, *, channel: Optional[int] = None) -> None:
        self._tick()
        self.selected_multi = number
        self.selected_multi_log.append(number)

    def read_multi(self, *, timeout: Optional[float] = None) -> bytes:
        """A 0x62 dump: 32 invented Multisets, each assigning RAM 1."""
        from kws import multi as multi_mod

        self._tick()
        data = bytearray(
            multi_mod.NAME_BASE + multi_mod.MULTISETS * multi_mod.NAME_SIZE
        )
        for number in range(multi_mod.MULTISETS):
            base = number * multi_mod.MULTISET_SIZE
            for channel in range(multi_mod.CHANNELS):
                at = base + multi_mod.PART_BASE + channel * multi_mod.PART_SIZE
                data[at] = 0  # RAM 1's wire byte
                data[at + 1] = (number + channel) % banks.PERFORMANCES_PER_BANK
                data[at + 2] = 100
            name = f"Demo Set {number:02d}".encode()
            at = multi_mod.NAME_BASE + number * multi_mod.NAME_SIZE
            data[at : at + len(name)] = name
        # The real 0x62 structure is 3777 bytes: the 3264 modelled above,
        # then a 513-byte unmodelled block. The checksum covers the two
        # header bytes and the nibbles (the unit's own dump proves it).
        body = bytes(data) + b"\x69" * 512 + b"\x00"
        header = [0, 1]
        nibbles = list(m.to_nibbles(body))
        check = m.checksum([*header, *nibbles])
        return bytes(
            m.frame(
                m.Function.MULTISET_DUMP,
                [*header, *nibbles, check],
                channel=self.channel,
            )
        )

    def write_multi(
        self,
        data: Any,
        *,
        backup_dir: str,
        verify: bool = True,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Accept a Multi Mode Setup write; a fake has nothing to verify."""
        self._tick()
        before = (
            self.multi_written
            if self.multi_written is not None
            else bytes(len(bytes(data)))
        )
        self.multi_written = bytes(data)
        self.multi_write_count += 1
        path = backup.save(
            backup_dir,
            kind="multi",
            slot="multi",
            name="Multi Mode Setup",
            device_id=str(self.channel),
            source="kwsed",
            blocks={"dump": bytes(before)},
        )
        return {"backup": path, "verified": True, "mismatch": None}

    # --- writing, which is stored so a read-back and an undo can see it -----

    def _checked(
        self, bank: int | str, kind: str, data: Any
    ) -> Tuple[banks.Bank, bytes]:
        """Check one whole-bank payload, without storing it yet."""
        entry = banks.bank(bank)
        if not entry.writable:
            raise ValueError(
                f"{entry.label} is ROM and cannot be written. The banks that "
                f"can are: "
                f"{', '.join(b.label for b in banks.writable_banks())}."
            )
        if kind not in _WRITE_SIZES:
            raise ValueError(
                f"cannot write {kind!r}; have {', '.join(sorted(_WRITE_SIZES))}"
            )
        expected = _WRITE_SIZES[kind]
        payload = bytes(data)
        if len(payload) != expected:
            raise ValueError(
                f"{kind} must be {expected} bytes for a whole bank; got {len(payload)}"
            )
        return entry, payload

    def _accept(
        self, bank: int | str, kind: str, data: Any
    ) -> Tuple[banks.Bank, bytes]:
        """Check one whole-bank payload and remember it, decoded."""
        entry, payload = self._checked(bank, kind, data)
        self._written[(entry.id, kind)] = payload
        return entry, payload

    def write_bank(
        self,
        bank: int | str,
        kind: str,
        data: Any,
        *,
        backup_dir: str,
        verify: bool = True,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Accept a whole bank. The fake keeps what it was given.

        Nothing is sent and nothing is backed up -- this is the device a TUI
        screen or a test drives with no ports open. What it does provide is
        the same answer shape as the bridge and a later read that returns
        exactly the bytes written, so a read-back or an undo is exercised
        rather than short-circuited.
        """
        self._tick()
        entry, payload = self._checked(bank, kind, data)
        frame = self.read_raw(entry.sysex, _DUMP_FUNCTION[kind])
        entry, payload = self._accept(bank, kind, data)
        self.bank_write_count += 1
        self.bank_writes.append(
            {"bank": entry.label, "kind": kind, "bytes": len(payload)}
        )
        path = backup.save(
            backup_dir,
            kind=kind,
            slot=entry.label,
            name=entry.label,
            device_id=str(self.channel),
            source="kwsed",
            blocks={"dump": frame},
        )
        return {
            "bank": entry.label,
            "kind": kind,
            "backup": path,
            "verified": True,
            "mismatch": None,
        }

    def write_bank_bundle(
        self,
        bank: int | str,
        *,
        performances: Optional[bytes] = None,
        patches: Optional[bytes] = None,
        waveseq: Optional[bytes] = None,
        backup_dir: str,
        verify: bool = True,
        timeout: Optional[float] = None,
        before_blocks: Optional[Mapping[str, bytes]] = None,
    ) -> Dict[str, Any]:
        """Accept several kinds for one bank, stored in the bridge's order."""
        self._tick()
        provided = (
            ("waveseq", waveseq),
            ("patches", patches),
            ("performances", performances),
        )
        # A bundle names its kinds in the plural; the stored key is the
        # singular one so a later read finds it.
        checked: List[Tuple[str, str, bytes]] = []
        entry = banks.bank(bank)
        for bundle_kind, data in provided:
            if data is None:
                continue
            kind = _BUNDLE_KINDS[bundle_kind]
            entry, payload = self._checked(bank, kind, data)
            checked.append((bundle_kind, kind, payload))
        if not checked:
            raise ValueError("nothing to write")
        before: Dict[str, bytes] = {}
        for bundle_kind, kind, _payload in checked:
            before[bundle_kind] = self.read_raw(entry.sysex, _DUMP_FUNCTION[kind])
        for _bundle_kind, kind, payload in checked:
            self._written[(entry.id, kind)] = payload
        kinds = [bundle_kind for bundle_kind, _kind, _payload in checked]
        self.bundle_write_count += 1
        self.bundle_writes.append(
            {
                "bank": entry.label,
                "kinds": kinds,
                "bytes": sum(len(payload) for _bundle, _kind, payload in checked),
            }
        )
        path = backup.save(
            backup_dir,
            kind="bank-bundle",
            slot=entry.label,
            name=entry.label,
            device_id=str(self.channel),
            source="kwsed",
            blocks=before,
        )
        return {
            "bank": entry.label,
            "kind": "bank-bundle",
            "kinds": kinds,
            "backup": path,
            "verified": True,
            "mismatch": None,
        }
