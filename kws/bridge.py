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

import re
import signal
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import rtmidi

from kws import alsa
from kws import backup
from kws import banks
from kws import config as cfg
from kws import dumps
from kws import messages as m
from kws import records
from vinsynlib import midi

__all__ = [
    "SEND_GAP",
    "DEFAULT_TIMEOUT",
    "DUMP_TIMEOUT",
    "CARD_NAME_OFFSET",
    "CARD_NAME_LEN",
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

#: How long to wait for a **single** patch or performance. Measured, not
#: guessed: this unit answers a single patch request after about 9.9
#: seconds, every time and remarkably consistently, for 861 bytes that take
#: a third of a second on the wire. Whole-bank dumps come back far sooner.
#:
#: The old value here was DEFAULT_TIMEOUT, two seconds, which cut every one
#: of them off. The replies then arrived during the *next* request and were
#: taken for its answer -- which is how a read of Card 12 came back holding
#: the patch from Card 3, and why the behaviour looked erratic rather than
#: simply slow.
SINGLE_OBJECT_TIMEOUT = 15.0

#: How long to wait for a bank dump. A patch bank is nearly 30 kB, which is
#: about eight seconds of 31250-baud MIDI before the unit's own delay.
DUMP_TIMEOUT = 20.0

#: Where the card-name field sits in the **decoded** System Setup dump
#: (function ``0x51``), and how long it is.
#:
#: CONFIRM AGAINST HARDWARE: the offset is the one wire fact this feature
#: does not yet have. It is 0 until the read-only probe
#: (``tools/probe_card_name.py``) is compared against a real unit; until
#: then :meth:`WavestationBridge.read_card_name` accepts the first sixteen
#: bytes only when they look like a name, so the archive's identity
#: resolution treats anything implausible as unknown rather than filing it
#: under a wrong bank.
CARD_NAME_OFFSET = 0
CARD_NAME_LEN = 16

#: Which dump a bank request for a kind draws. The manual documents both;
#: kwsed sent only the patch one for its whole life and read the end of
#: that history as "the unit decides what to send".
_DUMP_FOR_KIND = {
    banks.Kind.PERFORMANCE: m.Function.ALL_PERFORMANCE_DUMP,
    banks.Kind.PATCH: m.Function.ALL_PATCH_DUMP,
}

_HINTS = ("wavestation", "ws sr", "wssr", "korg")


class MidiUnavailable(RuntimeError):
    """No MIDI backend on this host at all."""


class DeviceError(midi.DeviceError):
    """The device answered, but not with what was asked for.

    The library's class, so a probe that finds nothing is recognised by
    :func:`vinsynlib.midi.open_remembered_or_swept` and the sweep it falls
    back to happens -- with an announcement, rather than as a launch that
    quietly takes the slow path. It was a RuntimeError before and is one
    still, so every ``except`` clause that caught it does so unchanged.
    """


class DeviceRefused(DeviceError):
    """The device explicitly said no -- better news than a timeout."""

    def __init__(self, function: int) -> None:
        self.function = function
        super().__init__(
            f"the unit replied {m.Function.name(function)!r}. It is "
            f"connected and refused the request -- check the global channel "
            f"and that System Exclusive reception is enabled."
        )


def _client(kind: str) -> Any:
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
    def handler(signum: int, _frame: Any) -> None:  # pragma: no cover
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

    def __init__(
        self,
        midi_in: Any,
        midi_out: Any,
        *,
        channel: int = 0,
        description: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._in = midi_in
        self._out = midi_out
        self.channel = channel
        self.description = description
        self.timeout = timeout
        #: Which ports this conversation is on, by name. `description` said
        #: so only to a human reading it, and the connection policy has to
        #: be able to put the answer somewhere -- which it cannot do with a
        #: string it would have to parse back apart. None until `open` sets
        #: it, and None for the test doubles that build one directly.
        self.port: Optional[str] = None
        self.recv_port: Optional[str] = None
        self._in.ignore_types(sysex=False, timing=True, active_sense=True)

    @classmethod
    def open(
        cls,
        port: str,
        *,
        recv_port: Optional[str] = None,
        channel: int = 0,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> "WavestationBridge":
        ins, outs = list_ports()
        listen = recv_port or port
        if port not in outs:
            raise DeviceError(f"no output port named {port!r}; have {outs}")
        if listen not in ins:
            raise DeviceError(f"no input port named {listen!r}; have {ins}")
        midi_out = _client("out")
        midi_in = _client("in")
        try:
            # If the second open_port raises -- the port vanished between
            # the listing and here, or another program took it -- the first
            # client is already open and nothing would ever close it.
            midi_out.open_port(outs.index(port))
            midi_in.open_port(ins.index(listen))
        except Exception:
            for client in (midi_in, midi_out):
                try:
                    client.close_port()
                    client.delete()
                except Exception:
                    pass
            raise
        bridge = cls(
            midi_in,
            midi_out,
            channel=channel,
            description=f"{port} -> {listen}",
            timeout=timeout,
        )
        bridge.port = port
        bridge.recv_port = listen
        return bridge

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
        if len(data) > self.RTMIDI_LIMIT:
            address = (
                alsa.port_address(self.description.split(" -> ")[0])
                if alsa.available()
                else None
            )
            if address is not None:
                sender = alsa.Sender(*address, name=b"kwsed")
                try:
                    sender.send(data)
                finally:
                    sender.close()
                time.sleep(SEND_GAP)
                return
            # python-rtmidi drops a message this large silently on ALSA
            # (see TODO 5b), so refusing beats corrupting -- and refusing
            # whenever the ALSA path is unavailable, not only when
            # libasound loaded and the port name lacked an address.
            # alsa.available() is find_library("asound") having found
            # something, which fails on a host with no ldconfig while
            # rtmidi's own ALSA backend works perfectly; that host would
            # otherwise fall straight through to the silent drop this
            # check exists to prevent.
            #
            # Test doubles without a real MIDI port stay exempt, so the
            # write fence remains exercisable synthetically.
            if hasattr(self._out, "open_port"):
                raise DeviceError(
                    f"this message is {len(data)} bytes, which "
                    f"python-rtmidi cannot send on ALSA, and no ALSA "
                    f"sequencer address is available for "
                    f"{self.description!r}. Nothing was sent."
                )
        self._out.send_message(list(data))
        time.sleep(SEND_GAP)

    def identify(self, *, timeout: float = 0.5) -> Optional[Dict[str, Any]]:
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
    def connect(
        cls,
        *,
        config_path: str = cfg.DEFAULT_PATH,
        remembered: Optional[Tuple[str, str]] = None,
        scan: bool = False,
        channel: int = 0,
        on_try: Optional[Callable[[str], None]] = None,
    ) -> "WavestationBridge":
        """Open the unit, sweeping every port only when that is necessary.

        **The normal path is one Identity Request, to the port remembered in
        ``config_path``.** That is not only an optimisation.
        :meth:`autodetect` sweeps every bidirectional MIDI port on the
        machine, which on a studio box with thirty ports takes seconds,
        prints thirty lines, and pings every piece of hardware on the chain
        once per launch -- and once the answer is known there is nothing left
        to learn from asking again.

        So: use the remembered port if there is one, sweep if there is not,
        and sweep on request. ``scan=True`` is what ``--scan`` reaches,
        which is what is needed after the unit moves to another port -- USB
        re-enumeration renumbers ALSA clients out from under a saved name.

        A remembered port that does not answer falls back to the sweep, but
        **it says so first**. The guess is gone, and the announcement is
        what keeps the fallback from looking like the fast path having
        worked -- which is how a slow launch quietly becomes the normal one.
        What the sweep then finds is written back, so the launch after this
        one is fast again.

        The channel the unit reports wins over the one asked for, on the fast
        path as on the sweep: it is the fifth byte of every request, and the
        saved one can be stale.

        The policy itself is :func:`vinsynlib.midi.open_remembered_or_swept`,
        written and tested once for the whole family; this is the
        Wavestation's half of it, which is what an Identity Request on a
        named port means and where the answer is filed.

        ``remembered`` defaults to what ``config_path`` holds. It is a
        parameter so a caller can pass a pair it has already read, and so a
        test can say what the file says without writing one.
        """
        remembered = (
            remembered if remembered is not None else cfg.load_port_pair(config_path)
        )

        def probe(send: str, recv: str) -> "WavestationBridge":
            """One round trip to the named port. Raises if nothing answers."""
            try:
                bridge = cls.open(
                    send,
                    recv_port=recv,
                    channel=channel,
                    # 0.5 is autodetect's own discovery timeout, spelled
                    # as a literal there; the two probes must cost the
                    # # same or the fast path is not the fast path.
                    timeout=0.5,
                )
            except Exception as exc:
                raise DeviceError(str(exc)) from exc
            try:
                fields = bridge.identify(timeout=0.5)
            except Exception as exc:
                fields = None
                error: Optional[Exception] = exc
            else:
                error = None
            if fields is None or not fields.get("is_wavestation"):
                bridge.close()
                raise DeviceError(
                    f"no Wavestation answered an Identity Request on {send!r}"
                    + (f": {error}" if error is not None else "")
                )
            bridge.channel = fields["channel"]
            # The discovery timeout is short on purpose -- a port that is
            # not the instrument should cost as little as possible -- but it
            # must not become the session's. Every later request that does
            # not name its own would run at the probe's timeout rather than
            # the working one.
            bridge.timeout = DEFAULT_TIMEOUT
            return bridge

        def remember(bridge: "WavestationBridge") -> None:
            if bridge.port and bridge.recv_port:
                cfg.save_ports(bridge.port, bridge.recv_port, config_path)

        return midi.open_remembered_or_swept(
            probe=probe,
            sweep=lambda try_hook: cls.autodetect(timeout=0.5, on_try=try_hook),
            remembered=remembered,
            scan=scan,
            remember=remember,
            on_try=on_try,
        )

    @classmethod
    def autodetect(
        cls, *, timeout: float = 0.5, on_try: Optional[Callable[[str], None]] = None
    ) -> "WavestationBridge":
        """Find a Wavestation and adopt the channel it reports."""
        hinted = likely_korg_ports()
        candidates = hinted + [p for p in bidirectional_ports() if p not in hinted]
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
                # The discovery timeout is short on purpose -- a port
                # that is not the instrument should cost as little as
                # possible -- but it must not become the session's. Every
                # later request that does not name its own would run at
                # the probe's timeout rather than the working one.
                bridge.timeout = DEFAULT_TIMEOUT
                return bridge
            bridge.close()
        raise DeviceError(
            "no Wavestation answered an Identity Request on any port. Check "
            "the cable, that the unit is on, and that its System Exclusive "
            "reception is enabled."
        )

    # --- operations ----------------------------------------------------------

    def read_bank(
        self,
        bank: int | str,
        kind: Optional[str] = None,
        *,
        timeout: Optional[float] = None,
    ) -> dumps.BulkDump:
        """Dump one bank. Read-only, and **makes no sound**.

        Each kind has its own request -- ``0x1C`` for patches, ``0x1D``
        for performances -- so the caller names the kind and only that
        kind's dump is accepted, read from the reply like the bank. A
        late answer to an earlier request is otherwise indistinguishable
        from this one's, which is how wrong-bank reads happened.

        Without a kind, the patch request goes out and either dump is
        accepted; the result still carries the kind that actually
        arrived. That legacy form is for callers that report what came
        back rather than filing it under a pane.

        An error reply raises immediately instead of waiting out the clock;
        a refusal is a different problem from silence and points somewhere
        different.
        """
        entry = banks.bank(bank)
        # A tuple of any length: the kind-less form accepts either dump, the
        # other accepts exactly one, and the two branches assign different
        # shapes to the same name.
        wanted: Tuple[int, ...]
        if kind is None:
            wanted = (m.Function.ALL_PERFORMANCE_DUMP, m.Function.ALL_PATCH_DUMP)
            request = m.bulk_request(entry.sysex, self.channel)
        else:
            try:
                dump_function = _DUMP_FOR_KIND[kind]
            except KeyError:
                raise ValueError(
                    f"cannot read {kind!r}; have {', '.join(sorted(_DUMP_FOR_KIND))}"
                ) from None
            wanted = (dump_function,)
            request = m.bank_request(entry.sysex, dump_function, self.channel)
        self._drain()
        self._send(request)
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
            if packet.function not in wanted:
                continue
            # The bank the reply describes, not merely that it is a dump.
            # A late answer to a request that already timed out is
            # otherwise indistinguishable from this one's, and the caller
            # files these names under the bank it asked for -- which is how
            # a read came back holding another bank's contents. read_patch
            # guards the same way.
            if packet.bank is not None and packet.bank != entry.sysex:
                continue
            return dumps.parse_bulk(packet)
        raise TimeoutError(
            f"no dump came back for {entry.label}. The unit did not refuse "
            f"it either, so this means one of: powered off, wrong port, "
            f"wrong global channel (kwsed is using {self.channel + 1}), "
            f"System Exclusive reception disabled, or an interface that "
            f"cannot carry a message this long -- a patch bank is nearly "
            f"30 kB."
        )

    def read_patch(
        self, bank: int | str, number: int, *, timeout: Optional[float] = None
    ) -> bytes:
        """One patch's 426 bytes. Read-only, and makes no sound."""
        entry = banks.bank(bank)
        self._drain()
        self._send(m.single_patch_request(entry.sysex, number, self.channel))
        deadline = time.time() + (SINGLE_OBJECT_TIMEOUT if timeout is None else timeout)
        while time.time() < deadline:
            message = self._in.get_message()
            if message is None:
                time.sleep(0.002)
                continue
            packet = m.parse(message[0])
            if packet is None or packet.channel != self.channel:
                continue
            if packet.is_error:
                raise DeviceRefused(packet.function)
            if packet.function != m.Function.SINGLE_PATCH_DUMP:
                continue
            # Match the bank and number the reply describes, not merely
            # that it is a patch dump. A late answer to a request that has
            # already timed out is otherwise indistinguishable from the
            # answer to this one -- which is how a read of Card 12 came
            # back holding the patch from Card 3, and a read of RAM 1 20
            # came back holding one from the Card.
            raw = bytes(message[0])
            if len(raw) < 7 or raw[5] != entry.sysex or raw[6] != number:
                continue
            return packet.data()[: records.PATCH_SIZE]
        raise TimeoutError(
            f"no patch came back for {entry.label} {number}. A reply for a "
            f"different slot does not count, so this can also mean the unit "
            f"is still answering an earlier request."
        )

    def write_patch(
        self,
        bank: int | str,
        number: int,
        data: bytes,
        *,
        backup_dir: str,
        verify: bool = True,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Replace **one patch**. Destructive, and fenced like a bank write.

        Two messages, because one is not enough: a single patch dump lands
        in the edit buffer, and a separate write command commits it to the
        slot. Korg's appendix documents both. Edisyn sends the pair and
        records that it does not work -- its write command passes the
        function code and the bank where the bank and the number belong.

        Far smaller than a bank write: 426 bytes and one slot, rather than
        35 slots and 30 kB. The fence is the same anyway, because the thing
        that makes a write safe is the backup and the read-back, not the
        size.
        """
        entry = banks.bank(bank)
        if not entry.writable:
            raise DeviceError(f"{entry.label} is ROM and cannot be written.")
        if not 0 <= number < banks.PATCHES_PER_BANK:
            raise ValueError(
                f"a bank holds {banks.PATCHES_PER_BANK} patches, so "
                f"{number} is not one of them"
            )
        if len(data) != records.PATCH_SIZE:
            raise ValueError(f"a patch is {records.PATCH_SIZE} bytes; got {len(data)}")

        try:
            before = self.read_patch(entry.sysex, number, timeout=timeout)
        except Exception as exc:
            raise DeviceError(
                f"could not read {entry.label} patch {number} before "
                f"writing it, so nothing was sent ({exc})."
            ) from exc
        saved = backup.save(
            backup_dir,
            kind="patch",
            slot=_patch_backup_name(entry.label, number),
            name=_patch_backup_name(entry.label, number),
            device_id=str(self.channel),
            source="kwsed",
            blocks={
                "dump": m.single_patch_dump(
                    entry.sysex, number, before, channel=self.channel
                )
            },
        )

        self._drain()
        self._send(m.single_patch_dump(entry.sysex, number, data, channel=self.channel))
        self._send(m.patch_write_command(entry.sysex, number, self.channel))

        result: Dict[str, Any] = {
            "bank": entry.label,
            "number": number,
            "backup": saved,
            "status": None,
            "verified": None,
            "mismatch": None,
        }
        deadline = time.time() + 2.0
        while time.time() < deadline:
            message = self._in.get_message()
            if message is None:
                time.sleep(0.002)
                continue
            packet = m.parse(message[0])
            if packet is None:
                continue
            if packet.function in (m.Function.WRITE_COMPLETED, m.Function.WRITE_ERROR):
                result["status"] = packet.function_name
                break
        if not verify:
            return result
        try:
            after = self.read_patch(entry.sysex, number, timeout=timeout)
        except Exception as exc:
            result["verified"] = False
            result["mismatch"] = f"could not read it back ({exc})"
            return result
        result["verified"] = bytes(after) == bytes(data)
        if not result["verified"]:
            result["mismatch"] = _first_difference(bytes(data), bytes(after))
        return result

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
            "reception is enabled."
        )

    def write_multi(
        self,
        data: Any,
        *,
        backup_dir: str,
        verify: bool = True,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Replace the Multi Mode Setup (0x06). **Destructive.**

        Mirrors :meth:`write_bank`'s fence: the destination is read in full
        and backed up first, the dump is sent, and (when ``verify``) it is
        read back and compared. There is no ROM check because the Multisets
        are not a bank.
        """
        raw = bytes(data)
        if (
            len(raw) > 5
            and raw[0] == 0xF0
            and raw[-1] == 0xF7
            and raw[4] == m.Function.MULTISET_DUMP
        ):
            sent = raw
        else:
            sent = m.frame(
                m.Function.MULTISET_DUMP,
                [0, 1] + list(m.to_nibbles(raw)),
                channel=self.channel,
            )

        try:
            before = self.read_multi(timeout=timeout)
        except Exception as exc:
            raise DeviceError(
                "could not read the Multi Mode Setup before writing it, so "
                f"nothing was sent ({exc}). kwsed does not overwrite a dump "
                "it has not backed up."
            ) from exc
        saved = backup.save(
            backup_dir,
            kind="multi",
            slot="multi",
            name="Multi Mode Setup",
            device_id=str(self.channel),
            source="kwsed",
            blocks={"dump": before},
        )

        self._drain()
        self._send(sent)

        result: Dict[str, Any] = {
            "backup": saved,
            "verified": None,
            "mismatch": None,
        }
        if not verify:
            return result

        try:
            after = self.read_multi(timeout=timeout)
        except Exception as exc:
            result["verified"] = False
            result["mismatch"] = (
                "the write was sent but the Multi Mode Setup could not be "
                f"read back to check it ({exc}). Whether it took is unknown."
            )
            return result
        result["verified"] = bytes(after) == bytes(sent)
        if not result["verified"]:
            result["mismatch"] = _first_difference(bytes(sent), bytes(after))
        return result

    def read_bank_names(
        self,
        bank: int | str,
        kind: Optional[str] = None,
        *,
        timeout: Optional[float] = None,
    ) -> Tuple[str, Dict[int, str]]:
        """``(kind, {number: name})`` for one bank of one kind. Silent."""
        dump = self.read_bank(bank, kind, timeout=timeout)
        return dump.kind, dump.names()

    def read_system_setup(self, *, timeout: Optional[float] = None) -> Optional[bytes]:
        """The System Setup dump (``0x51``) as decoded bytes. Read-only.

        One small request, no selection and no sound. ``None`` rather than
        a timeout exception: the card name this dump carries is a hint used
        to identify which HCard bank is in front of the synth, and a unit
        that does not answer is "unknown", not a failure of the browser.
        An explicit refusal still raises :class:`DeviceRefused`, because
        that is a different answer from silence.
        """
        self._drain()
        self._send(m.frame(m.Function.SYSTEM_SETUP_REQUEST, [], channel=self.channel))
        deadline = time.time() + (DEFAULT_TIMEOUT if timeout is None else timeout)
        while time.time() < deadline:
            message = self._in.get_message()
            if message is None:
                time.sleep(0.002)
                continue
            packet = m.parse(message[0])
            if packet is None or packet.channel != self.channel:
                continue
            if packet.is_error:
                raise DeviceRefused(packet.function)
            if packet.function != m.Function.SYSTEM_SETUP_DUMP:
                continue
            return packet.data()
        return None

    def read_card_name(self, *, timeout: Optional[float] = None) -> Optional[str]:
        """The card name the synth reports, or ``None`` when it has none.

        The HCard rewrites this field whenever a bank is selected on the
        card (docs/HCARD.md section 2), so it is the only identity signal
        the Wavestation exposes for "which of the forty banks is active".

        ``None`` is not an error: a real card may hold no name, an empty
        field decodes to nothing, and a unit that has not answered leaves
        the question open. The caller resolves identity from the first
        performance and patch names in that case.
        """
        data = self.read_system_setup(timeout=timeout)
        if data is None:
            return None
        end = CARD_NAME_OFFSET + CARD_NAME_LEN
        if len(data) < end:
            return None
        name = m.decode_name(data[CARD_NAME_OFFSET:end]).strip()
        # CONFIRM AGAINST HARDWARE: while the offset is unconfirmed, trust
        # the field only if it reads as a name; probes belong in
        # tools/probe_card_name.py, and garbage here is "no usable name".
        if re.fullmatch(r"[ -~]{4,}", name) is None:
            return None
        return name

    def select(self, slot: banks.Slot, *, channel: Optional[int] = None) -> None:
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
    def writable(self, bank: int | str) -> bool:
        return banks.bank(bank).writable

    def write_bank(
        self,
        bank: int | str,
        kind: str,
        data: bytes,
        *,
        backup_dir: str,
        verify: bool = True,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
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
                f"{', '.join(b.label for b in banks.writable_banks())}."
            )
        if kind not in (banks.Kind.PERFORMANCE, banks.Kind.PATCH, "waveseq"):
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
                f"not backed up."
            ) from exc
        saved = backup.save(
            backup_dir,
            kind=kind,
            slot=entry.label,
            name=entry.label,
            device_id=str(self.channel),
            source="kwsed",
            blocks={"dump": before},
        )

        # --- 3. send -------------------------------------------------------
        self._drain()
        self._send(m.frame_bulk(function, entry.sysex, data, channel=self.channel))

        result: Dict[str, Any] = {
            "bank": entry.label,
            "kind": kind,
            "backup": saved,
            "verified": None,
            "mismatch": None,
        }
        if not verify:
            return result

        # --- 4. read back and compare --------------------------------------
        try:
            after = self.read_raw(entry.sysex, function, timeout=timeout)
        except Exception as exc:
            result["verified"] = False
            result["mismatch"] = (
                f"the write was sent but {entry.label} could not be read "
                f"back to check it ({exc}). Whether it took is unknown."
            )
            return result
        sent = m.frame_bulk(function, entry.sysex, data, channel=self.channel)
        result["verified"] = bytes(after) == bytes(sent)
        if not result["verified"]:
            result["mismatch"] = _first_difference(bytes(sent), bytes(after))
        return result

    def read_raw(
        self, bank_number: int, function: int, *, timeout: Optional[float] = None
    ) -> bytes:
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
            if packet.function != function:
                continue
            # Same guard as read_bank: the reply must describe the bank
            # that was asked for. This one feeds the write fence's backup
            # and its read-back comparison, so a dump from the wrong bank
            # here would back up the wrong slot and then "verify" a write
            # against it.
            if packet.bank is not None and packet.bank != bank_number:
                continue
            return bytes(message[0])
        raise TimeoutError(
            f"no {m.Function.name(function)} came back for bank {bank_number}"
        )


def _first_difference(sent: bytes, got: bytes) -> str:
    """Where a read-back stopped matching, in words rather than two dumps."""
    if len(sent) != len(got):
        return f"the read-back is {len(got)} bytes and {len(sent)} were sent"
    for index, (a, b) in enumerate(zip(sent, got)):
        if a != b:
            return f"byte {index} differs: sent {a:#04x}, read back {b:#04x}"
    return "no difference found, which contradicts the comparison"


def _multi_request(channel: int) -> bytes:
    return m.frame(m.Function.MULTI_REQUEST, [], channel=channel)


def _patch_backup_name(bank_label: str, number: int) -> str:
    return f"{bank_label}-patch{number:02d}"
