# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# The port-enumeration helpers, the throttled send, the drain-before-request
# discipline and the clean-exit handling are ported from the sibling rxved,
# p2ked and x5ded projects, which port them from s3ked, eosed, k2kremote and
# mpc2emu:
#   Copyright (C) 2025-2026  mpc2emu contributors    - GPL-2.0-or-later
#   Copyright (C) 2026  k2kremote contributors       - GPL-2.0-or-later
#   Copyright (C) 2026  eosed contributors           - GPL-2.0-or-later
#   Copyright (C) 2026  s3ked contributors           - GPL-2.0-or-later
#   Copyright (C) 2026  rxved contributors           - GPL-2.0-or-later
#   Copyright (C) 2026  p2ked contributors           - GPL-2.0-or-later
#   Copyright (C) 2026  x5ded contributors           - GPL-2.0-or-later
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

"""MIDI transport for the Korg Wavestation SR.

**Partly verified against hardware on 2026-09-12.** Discovery, the Identity
Reply and bank dumps were exercised against a real WS SR. See DISCLAIMER.md.

Two things shape this module.

**A bank dump takes seconds, not milliseconds.** It is ~30 kB of System
Exclusive, and the unit takes a noticeable moment to begin. During
development a two-second wait made the request look like it had failed --
and then the dump arrived and was taken for the answer to the *next*
request. That is why :data:`DUMP_TIMEOUT` is generous and why the input is
drained before every request.

**The reply says which bank it is; the request is not to be trusted for
that.** The dump carries its own bank byte, and reading it from the reply
rather than assuming it matches what was asked is what stops a late answer
being filed under the wrong bank. The same mistake in a name catalog is
silent and permanent.
"""

from __future__ import annotations

import signal
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple

import rtmidi  # noqa: E402

from kws import alsa
from kws import backup
from kws import banks
from kws import dumps
from kws import messages as m

__all__ = [
    "SEND_GAP",
    "DEFAULT_TIMEOUT",
    "DUMP_TIMEOUT",
    "MidiUnavailable",
    "DeviceError",
    "DeviceRefused",
    "list_ports",
    "bidirectional_ports",
    "install_clean_exit",
    "WavestationBridge",
]

#: Gap after an outgoing message. **A guess**, inherited from the siblings.
SEND_GAP = 0.010

DEFAULT_TIMEOUT = 2.0

#: How long to wait for a bank dump. A patch bank is nearly 30 kB, which is
#: about eight seconds of 31250-baud MIDI before the unit's own delay.
DUMP_TIMEOUT = 20.0

_HINTS = ("wavestation", "ws sr", "wssr", "korg")


class MidiUnavailable(RuntimeError):
    """No MIDI backend on this host at all."""


class DeviceError(RuntimeError):
    """The device answered, but not with what was asked for."""


class DeviceRefused(DeviceError):
    """The device explicitly said no -- better news than a timeout."""

    def __init__(self, function: int) -> None:
        self.function = function
        super().__init__(
            f"the unit replied {m.Function.name(function)!r}. It is "
            f"connected and refused the request -- check the global channel "
            f"and that System Exclusive reception is enabled.")


def _client(kind: str):
    try:
        return rtmidi.MidiIn() if kind == "in" else rtmidi.MidiOut()
    except Exception as exc:  # pragma: no cover - depends on the host
        raise MidiUnavailable(
            f"no MIDI backend available ({exc}). On Linux this needs an ALSA "
            f"sequencer; in a container it must be passed through."
        ) from exc


def list_ports() -> Tuple[List[str], List[str]]:
    midi_in = _client("in")
    midi_out = _client("out")
    try:
        return (list(midi_in.get_ports()), list(midi_out.get_ports()))
    finally:
        midi_in.delete()
        midi_out.delete()


def bidirectional_ports() -> List[str]:
    ins, outs = list_ports()
    return [name for name in ins if name in outs]


def likely_korg_ports() -> List[str]:
    lowered = [(name, name.lower()) for name in bidirectional_ports()]
    return [name for name, low in lowered if any(h in low for h in _HINTS)]


def install_clean_exit(on_exit: Optional[Callable[[], None]] = None) -> None:
    def handler(signum, _frame):  # pragma: no cover - signal path
        if on_exit is not None:
            try:
                on_exit()
            except Exception:
                pass
        sys.exit(128 + signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # pragma: no cover - not main thread
            pass


class WavestationBridge:
    """One open conversation with one Wavestation."""

    def __init__(self, midi_in, midi_out, *, channel: int = 0,
                 description: str = "",
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self._in = midi_in
        self._out = midi_out
        self.channel = channel
        self.description = description
        self.timeout = timeout
        self._in.ignore_types(sysex=False, timing=True, active_sense=True)

    @classmethod
    def open(cls, port: str, *, recv_port: Optional[str] = None,
             channel: int = 0,
             timeout: float = DEFAULT_TIMEOUT) -> "WavestationBridge":
        ins, outs = list_ports()
        listen = recv_port or port
        if port not in outs:
            raise DeviceError(f"no output port named {port!r}; have {outs}")
        if listen not in ins:
            raise DeviceError(f"no input port named {listen!r}; have {ins}")
        midi_out = _client("out")
        midi_in = _client("in")
        midi_out.open_port(outs.index(port))
        midi_in.open_port(ins.index(listen))
        return cls(midi_in, midi_out, channel=channel,
                   description=f"{port} -> {listen}", timeout=timeout)

    def close(self) -> None:
        for client in (self._in, self._out):
            try:
                client.close_port()
                client.delete()
            except Exception:  # pragma: no cover - teardown
                pass

    # --- the wire ------------------------------------------------------------

    def _drain(self) -> None:
        while self._in.get_message() is not None:
            pass

    #: Above this, python-rtmidi cannot send at all on ALSA: it manages
    #: 8 kB and fails at 16 kB, and a patch bank is 29828 bytes. Messages
    #: this large go through :mod:`kws.alsa` instead.
    RTMIDI_LIMIT = 4096

    def _send(self, data: bytes) -> None:
        data = bytes(data)
        if len(data) > self.RTMIDI_LIMIT and alsa.available():
            address = alsa.port_address(self.description.split(" -> ")[0])
            if address is not None:
                sender = alsa.Sender(*address, name=b"kwsed")
                try:
                    sender.send(data)
                finally:
                    sender.close()
                time.sleep(SEND_GAP)
                return
        self._out.send_message(list(data))
        time.sleep(SEND_GAP)

    def identify(self, *, timeout: float = 0.5) -> Optional[dict]:
        """Ask who is there. One small message; makes no sound.

        The reply carries the unit's **global channel**, which every other
        request is answered on and which cannot otherwise be guessed.
        """
        self._drain()
        self._send(m.identity_request())
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = self._in.get_message()
            if message is None:
                time.sleep(0.002)
                continue
            fields = m.identity_reply_fields(message[0])
            if fields is not None:
                return fields
        return None

    @classmethod
    def autodetect(cls, *, timeout: float = 0.5,
                   on_try: Optional[Callable[[str], None]] = None
                   ) -> "WavestationBridge":
        """Find a Wavestation and adopt the channel it reports."""
        hinted = likely_korg_ports()
        candidates = hinted + [p for p in bidirectional_ports()
                               if p not in hinted]
        for port in candidates:
            if "Midi Through" in port:
                continue
            if on_try is not None:
                on_try(port)
            try:
                bridge = cls.open(port, timeout=timeout)
            except Exception:
                continue
            try:
                fields = bridge.identify(timeout=timeout)
            except Exception:
                fields = None
            if fields is not None and fields.get("is_wavestation"):
                bridge.channel = fields["channel"]
                return bridge
            bridge.close()
        raise DeviceError(
            "no Wavestation answered an Identity Request on any port. Check "
            "the cable, that the unit is on, and that its System Exclusive "
            "reception is enabled.")

    # --- operations ----------------------------------------------------------

    def read_bank(self, bank, *, timeout: Optional[float] = None
                  ) -> dumps.BulkDump:
        """Dump one bank. Read-only, and **makes no sound**.

        Which *kind* comes back -- performances or patches -- is the unit's
        choice, not the caller's: one request code asks for a bank and the
        unit answers with whatever it is currently set to dump. So the
        result carries its own kind and bank, read from the reply.

        An error reply raises immediately instead of waiting out the clock;
        a refusal is a different problem from silence and points somewhere
        different.
        """
        entry = banks.bank(bank)
        self._drain()
        self._send(m.bulk_request(entry.sysex, self.channel))
        deadline = time.time() + (
            DUMP_TIMEOUT if timeout is None else timeout)
        while time.time() < deadline:
            message = self._in.get_message()
            if message is None:
                time.sleep(0.003)
                continue
            packet = m.parse(message[0])
            if packet is None or packet.channel != self.channel:
                continue
            if packet.is_error:
                raise DeviceRefused(packet.function)
            if packet.function in (m.Function.ALL_PERFORMANCE_DUMP,
                                   m.Function.ALL_PATCH_DUMP):
                return dumps.parse_bulk(packet)
        raise TimeoutError(
            f"no dump came back for {entry.label}. The unit did not refuse "
            f"it either, so this means one of: powered off, wrong port, "
            f"wrong global channel (kwsed is using {self.channel + 1}), "
            f"System Exclusive reception disabled, or an interface that "
            f"cannot carry a message this long -- a patch bank is nearly "
            f"30 kB."
        )

    def read_multi(self, *, timeout: Optional[float] = None) -> bytes:
        """The Multisets, as the raw 0x62 message. Read-only, makes no sound.

        One request draws three replies -- 0x55, 0x5E and then 0x62 -- which
        Korg's appendix 4 says always travel together. Only the last carries
        all 32 Multisets with their 16 channels, so that is what comes back;
        the other two are read and discarded so they cannot be mistaken for
        the answer to the next request.

        The request code is **0x06**, documented. kwsed inferred 0x32 first,
        from the convention that a request sits 0x30 below its dump, and got
        silence -- see docs/RESOLUTION_NOTES.md section 8.
        """
        self._drain()
        self._send(_multi_request(self.channel))
        deadline = time.time() + (DUMP_TIMEOUT if timeout is None else timeout)
        while time.time() < deadline:
            message = self._in.get_message()
            if message is None:
                time.sleep(0.003)
                continue
            packet = m.parse(message[0])
            if packet is None or packet.channel != self.channel:
                continue
            if packet.is_error:
                raise DeviceRefused(packet.function)
            if packet.function == m.Function.MULTISET_DUMP:
                return bytes(message[0])
        raise TimeoutError(
            "no Multiset dump came back. The request is 0x06 and takes no "
            "argument; check the global channel and that System Exclusive "
            "reception is enabled.")

    def read_bank_names(self, bank, *, timeout: Optional[float] = None
                        ) -> Tuple[str, Dict[int, str]]:
        """``(kind, {number: name})`` for one bank. Silent."""
        dump = self.read_bank(bank, timeout=timeout)
        return dump.kind, dump.names()

    def select(self, slot: banks.Slot, *, channel: Optional[int] = None
               ) -> None:
        """Select a performance or patch. **This changes the sound.**

        Bank Select MSB/LSB then a Program Change. Which of the twelve banks
        a given Bank Select reaches is **not established** -- see TODO.md --
        so kwsed sends the bank's own number and says so rather than
        implying it knows.
        """
        target = self.channel if channel is None else channel
        if not 0 <= target <= 15:
            raise ValueError(f"channel {target} is outside 0-15")
        status = 0xB0 | target
        self._out.send_message([status, 0x00, 0x00])
        # Which number a Bank Select wants -- the display
        # position or the System Exclusive byte -- is NOT
        # established. The wire byte is sent because every
        # other place that names a bank uses it. See TODO 1.
        self._out.send_message([status, 0x20, slot.bank.sysex])
        self._out.send_message([0xC0 | target, slot.number])
        time.sleep(SEND_GAP)

    # --- writing, which is fenced ------------------------------------------

    #: Only these banks accept a write. Confirmed twice: kwsed's own bank
    #: table marks RAM 1-3 and the Card writable, and Edisyn's Wavestation
    #: support lists the same four and no others.
    def writable(self, bank) -> bool:
        return banks.bank(bank).writable

    def write_bank(self, bank, kind: str, data: bytes, *,
                   backup_dir: str,
                   verify: bool = True,
                   timeout: Optional[float] = None) -> dict:
        """Replace a whole bank. **This destroys what is there.**

        The only thing in kwsed that writes to the instrument, and it is
        fenced the way the sibling projects fence theirs:

        1. **A ROM bank is refused**, before anything is read or sent.
        2. **The destination is read in full and backed up first.** If that
           read fails, nothing is written -- a write that cannot be undone
           is not one this program performs.
        3. The dump is sent, nibble encoded and checksummed.
        4. **It is read back and compared**, and the result says so. A bulk
           dump is unacknowledged, so reporting the bytes sent as if they
           had arrived would be a guess dressed as a confirmation.

        Arming and confirming belong to the caller -- the TUI's modal and
        the command line's ``--yes`` -- because a fence that lives in the
        transport can be bypassed by the next caller who forgets it exists.
        """
        entry = banks.bank(bank)
        if not entry.writable:
            raise DeviceError(
                f"{entry.label} is ROM and cannot be written. The banks that "
                f"can are: "
                f"{', '.join(b.label for b in banks.writable_banks())}.")
        if kind not in (banks.Kind.PERFORMANCE, banks.Kind.PATCH,
                        "waveseq"):
            raise ValueError(f"cannot write {kind!r}")

        function = {
            banks.Kind.PATCH: m.Function.ALL_PATCH_DUMP,
            banks.Kind.PERFORMANCE: m.Function.ALL_PERFORMANCE_DUMP,
            "waveseq": m.Function.ALL_WAVESEQ_DUMP,
        }[kind]

        # --- 2. back the destination up, or do not write at all ------------
        try:
            before = self.read_raw(entry.sysex, function, timeout=timeout)
        except Exception as exc:
            raise DeviceError(
                f"could not read {entry.label} before writing it, so nothing "
                f"was sent ({exc}). kwsed does not overwrite a bank it has "
                f"not backed up.") from exc
        names = []
        try:
            packet = m.parse(before)
            if packet is not None and function != m.Function.ALL_WAVESEQ_DUMP:
                names = list(dumps.parse_bulk(packet).names().values())
        except Exception:
            names = []
        saved = backup.save(before, backup_dir, bank_label=entry.label,
                            kind=kind, names=names, channel=self.channel)

        # --- 3. send -------------------------------------------------------
        self._drain()
        self._send(m.frame_bulk(function, entry.sysex, data,
                                channel=self.channel))

        result = {"bank": entry.label, "kind": kind, "backup": saved,
                  "verified": None, "mismatch": None}
        if not verify:
            return result

        # --- 4. read back and compare --------------------------------------
        try:
            after = self.read_raw(entry.sysex, function, timeout=timeout)
        except Exception as exc:
            result["verified"] = False
            result["mismatch"] = (
                f"the write was sent but {entry.label} could not be read "
                f"back to check it ({exc}). Whether it took is unknown.")
            return result
        sent = m.frame_bulk(function, entry.sysex, data,
                            channel=self.channel)
        result["verified"] = bytes(after) == bytes(sent)
        if not result["verified"]:
            result["mismatch"] = _first_difference(bytes(sent), bytes(after))
        return result

    def read_raw(self, bank_number: int, function: int, *,
                 timeout: Optional[float] = None) -> bytes:
        """One bank dump of a given kind, as the bytes that arrived."""
        self._drain()
        self._send(m.bank_request(bank_number, function, self.channel))
        deadline = time.time() + (DUMP_TIMEOUT if timeout is None else timeout)
        while time.time() < deadline:
            message = self._in.get_message()
            if message is None:
                time.sleep(0.003)
                continue
            packet = m.parse(message[0])
            if packet is None or packet.channel != self.channel:
                continue
            if packet.is_error:
                raise DeviceRefused(packet.function)
            if packet.function == function:
                return bytes(message[0])
        raise TimeoutError(
            f"no {m.Function.name(function)} came back for bank "
            f"{bank_number}")


def _first_difference(sent: bytes, got: bytes) -> str:
    """Where a read-back stopped matching, in words rather than two dumps."""
    if len(sent) != len(got):
        return (f"the read-back is {len(got)} bytes and {len(sent)} were "
                f"sent")
    for index, (a, b) in enumerate(zip(sent, got)):
        if a != b:
            return (f"byte {index} differs: sent {a:#04x}, read back "
                    f"{b:#04x}")
    return "no difference found, which contradicts the comparison"


def _multi_request(channel: int) -> bytes:
    return m.frame(m.Function.MULTI_REQUEST, [], channel=channel)
