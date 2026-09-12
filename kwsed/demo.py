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
from typing import Dict, List, Optional, Tuple

from kws import banks
from kws import dumps
from kws import messages as m

__all__ = ["DemoDevice", "demo_name"]

_ADJECTIVES = ("Velvet", "Rusted", "Glass", "Hollow", "Distant", "Folded",
               "Amber", "Quiet", "Drifting", "Woven", "Paper", "Tidal")
_NOUNS = ("Bell", "Pad", "Bass", "Lead", "Choir", "Pluck", "Sweep", "Stack",
          "Drone", "Keys", "Brass", "Wash")


def demo_name(bank_id: str, kind: str, number: int) -> str:
    seed = sum(ord(c) for c in bank_id + kind) * 31 + number * 17
    adjective = _ADJECTIVES[seed % len(_ADJECTIVES)]
    noun = _NOUNS[(seed // len(_ADJECTIVES)) % len(_NOUNS)]
    return f"{adjective} {noun}"[:m.NAME_LEN - 1]


class DemoDevice:
    """Answers the subset of the protocol kwsed speaks."""

    def __init__(self, *, latency: float = 0.0, channel: int = 4) -> None:
        self.description = "demo (no MIDI ports opened)"
        #: Not channel 0: a fake on the default would let a caller that
        #: never set the channel work by accident and fail on real hardware.
        self.channel = channel
        self.timeout = 1.0
        self.latency = latency
        self.selected: Optional[banks.Slot] = None
        self.selected_log: List[banks.Slot] = []
        self.received: List[bytes] = []
        #: Which kind the fake answers a bank request with. The real unit
        #: decides this itself, so kwsed must never assume -- and a fake
        #: that always answered "performance" would hide that.
        self.answer_kind = banks.Kind.PERFORMANCE
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def _tick(self) -> None:
        if self.latency:
            time.sleep(self.latency)

    def identify(self, *, timeout: float = 0.5) -> dict:
        self._tick()
        return {"channel": self.channel, "manufacturer": m.KORG_ID,
                "family": (m.WAVESTATION_ID, 0), "is_korg": True,
                "is_wavestation": True}

    def handle(self, data: bytes) -> Optional[bytes]:
        self.received.append(bytes(data))
        self._tick()
        packet = m.parse(data)
        if packet is None or packet.channel != self.channel:
            return None
        if packet.function != m.Function.BULK_REQUEST or not packet.payload:
            return m.frame(m.Function.DATA_FORMAT_ERROR, channel=self.channel)
        return self._bank_dump(packet.payload[0])

    def _bank_dump(self, number: int) -> bytes:
        entry = banks.bank(number)
        kind = self.answer_kind
        if kind == banks.Kind.PERFORMANCE:
            function, size = (m.Function.ALL_PERFORMANCE_DUMP,
                              dumps.PERFORMANCE_RECORD)
        else:
            function, size = m.Function.ALL_PATCH_DUMP, dumps.PATCH_RECORD
        raw = bytearray()
        for index in range(banks.count_for(kind)):
            record = bytearray(size)
            name = demo_name(entry.id, kind, index)
            record[:len(name)] = name.encode("ascii")
            # A byte with bit 7 set, so the nibble encoding is exercised.
            record[m.NAME_LEN] = 0x80 | (index & 0x7F)
            raw.extend(record)
        payload = [entry.number] + list(m.to_nibbles(bytes(raw)))
        return m.frame(function, payload, channel=self.channel)

    # --- the operations kwsed uses -------------------------------------------

    def read_bank(self, bank, *, timeout: Optional[float] = None):
        entry = banks.bank(bank)
        reply = self.handle(m.bulk_request(entry.number, self.channel))
        packet = m.parse(reply)
        if packet is None or packet.is_error:
            raise RuntimeError("demo device refused its own request")
        return dumps.parse_bulk(packet)

    def read_bank_names(self, bank, *, timeout: Optional[float] = None
                        ) -> Tuple[str, Dict[int, str]]:
        dump = self.read_bank(bank)
        return dump.kind, dump.names()

    def select(self, slot: banks.Slot, *, channel: Optional[int] = None
               ) -> None:
        self._tick()
        self.selected = slot
        self.selected_log.append(slot)
