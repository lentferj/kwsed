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

**Write fence.** Every write method in this module (``write_patch``,
``write_performance``, ``write_bank``, ``write_bank_bundle``,
``write_multi``) is fenced: the destination is read in full and backed up
*before* anything is sent, and (when ``verify=True``) the write is read
back and compared. The options ``save_backup=False`` (``write_patch`` only)
and ``verify=False`` (all writers) **remove parts of that fence**.

* ``save_backup=False`` skips the pre-write read entirely -- no backup
  envelope is written and the write cannot be undone.
* ``verify=False`` skips the read-back comparison -- the bytes are sent
  but whether they arrived and were stored is unknown.

Without these, a failed write cannot be detected or undone. They exist for
staging (the audition workflow) where a human hears the result before
committing, and for bulk operations where the caller provides its own
before-blocks. Normal callers should leave both at their defaults.
"""

from __future__ import annotations

import contextlib
import re
import signal
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Protocol

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
    "DEFAULT_SINGLE_OBJECT_TIMEOUT",
    "CARD_NAME_OFFSET",
    "CARD_NAME_LEN",
    "MIDI_MODE_PARAMETER",
    "MIDI_MODE_OMNI",
    "MIDI_MODE_POLY",
    "MIDI_MODE_MULTI",
    "MIDI_MODE_MONO",
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
DEFAULT_SINGLE_OBJECT_TIMEOUT = 15.0

#: How long to wait for a bank dump. A patch bank is nearly 30 kB, which is
#: about eight seconds of 31250-baud MIDI before the unit's own delay.
DUMP_TIMEOUT = 20.0

#: The global parameter that selects OMNI/POLY/MULTI/MONO reception, and
#: its documented values (Reference Guide Appendix 4 section 5.12). The
#: value travels as ASCII, so MIDI mode 2 is the bytes ``2B 32`` ("+2").
MIDI_MODE_PARAMETER = 4
MIDI_MODE_OMNI = 0
MIDI_MODE_POLY = 1
MIDI_MODE_MULTI = 2
MIDI_MODE_MONO = 3

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

#: Rough wire bytes per dump, for the progress estimate. Every record byte
#: travels as two bytes (nibble encoded), so this is the payload size twice.
_WIRE_BYTES = {
    banks.Kind.PATCH: banks.PATCHES_PER_BANK * records.PATCH_SIZE * 2,
    banks.Kind.PERFORMANCE: banks.PERFORMANCES_PER_BANK * records.PERFORMANCE_SIZE * 2,
    "waveseq": records.WAVESEQ_BANK_SIZE * 2,
}


_FUNCTION_WIRE_BYTES = {
    m.Function.ALL_PATCH_DUMP: _WIRE_BYTES[banks.Kind.PATCH],
    m.Function.ALL_PERFORMANCE_DUMP: _WIRE_BYTES[banks.Kind.PERFORMANCE],
    m.Function.ALL_WAVESEQ_DUMP: _WIRE_BYTES["waveseq"],
}


_BUNDLE_LABELS = {
    "patches": "patches",
    "performances": "performances",
    "waveseq": "wave sequences",
}


def _bundle_label(kind: str) -> str:
    return _BUNDLE_LABELS.get(kind, kind)


def _wire_estimate(total: int) -> float:
    """How long ``total`` wire bytes take, plus a moment to process."""
    return total / alsa.Sender.BYTES_PER_SECOND + 0.8


#: The three kinds a copied performance drags along, with the dump each one
#: is written and read back as. A bundle names them in this order for the
#: send -- wave sequences first, performances last -- so a read-back of a
#: performance cannot precede the patches it points at.
_BUNDLE_FUNCTIONS = {
    "waveseq": m.Function.ALL_WAVESEQ_DUMP,
    "patches": m.Function.ALL_PATCH_DUMP,
    "performances": m.Function.ALL_PERFORMANCE_DUMP,
}

#: The decoded size of each kind's whole bank. Checked before anything is
#: read or sent: a frame built from a short payload would still be a legal
#: message, and the unit would store a bank with the tail missing.
_BUNDLE_SIZES = {
    "performances": banks.PERFORMANCES_PER_BANK * records.PERFORMANCE_SIZE,
    "patches": banks.PATCHES_PER_BANK * records.PATCH_SIZE,
    "waveseq": records.WAVESEQ_BANK_SIZE,
}

_HINTS = ("wavestation", "ws sr", "wssr", "korg")


class MidiInPort(Protocol):
    """Structural type for a MIDI input port.

    Only the methods actually used by :class:`WavestationBridge` are
    included. The real :class:`rtmidi.MidiIn` and test doubles implement
    this interface.
    """

    def get_message(self) -> Optional[Tuple[List[int], float]]:
        """Return the next pending message or ``None``.

        The message is a list of MIDI status/data bytes; the float is the
        timestamp (delta-time in seconds since the previous message).
        """
        ...

    def ignore_types(self, sysex: bool, timing: bool, active_sense: bool) -> None:
        """Configure which message types the port reports."""
        ...

    def close_port(self) -> None:
        """Close the underlying system port."""
        ...

    def delete(self) -> None:
        """Release the port object and any backend resources."""
        ...


class MidiOutPort(Protocol):
    """Structural type for a MIDI output port.

    Only the methods actually used by :class:`WavestationBridge` are
    included. The real :class:`rtmidi.MidiOut` and test doubles implement
    this interface.
    """

    def send_message(self, data: List[int]) -> None:
        """Send a MIDI message.

        ``data`` is a list of integers (status + data bytes).
        """
        ...

    def close_port(self) -> None:
        """Close the underlying system port."""
        ...

    def delete(self) -> None:
        """Release the port object and any backend resources."""
        ...


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
        midi_in: MidiInPort,
        midi_out: MidiOutPort,
        *,
        channel: int = 0,
        description: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        single_object_timeout: float = DEFAULT_SINGLE_OBJECT_TIMEOUT,
    ) -> None:
        self._in: MidiInPort = midi_in
        self._out: MidiOutPort = midi_out
        self.channel = channel
        self.description = description
        self.timeout = timeout
        self.single_object_timeout = single_object_timeout
        #: Which ports this conversation is on, by name. `description` said
        #: so only to a human reading it, and the connection policy has to
        #: be able to put the answer somewhere -- which it cannot do with a
        #: string it would have to parse back apart. None until `open` sets
        #: it, and None for the test doubles that build one directly.
        self.port: Optional[str] = None
        self.recv_port: Optional[str] = None
        #: ``progress(label, fraction)`` while a transfer runs; ``None`` as
        #: the fraction means it is over. Set by the TUI so a ten-second
        #: bank dump has something to show besides a blank status line.
        self.progress: Optional[Callable[[str, Optional[float]], None]] = None
        self._progress_label = ""
        self._progress_fraction = 0.0
        self._in.ignore_types(sysex=False, timing=True, active_sense=True)

    def _report(self, fraction: Optional[float]) -> None:
        """Tell the progress hook where a transfer is, if anyone listens.

        A UI callback that raises must not turn into a failed write, so its
        exceptions are swallowed: this is decoration, not protocol.
        """
        hook = self.progress
        if hook is None:
            return
        if fraction is not None and 0.0 < fraction < 1.0:
            # A read loop asks every few milliseconds; one-percent steps keep
            # that from becoming a message storm on the UI's queue. The
            # first announcement (0.0) always goes out.
            if (
                self._progress_fraction > 0.0
                and fraction - self._progress_fraction < 0.01
            ):
                return
            self._progress_fraction = fraction
        try:
            hook(self._progress_label, fraction)
        except Exception:
            pass

    @contextlib.contextmanager
    def _progress_scope(self, label: str, estimate: Optional[float] = None) -> "Any":
        """Announce a transfer, and always announce its end.

        ``estimate`` is the expected duration in seconds, used by the read
        loops to move the bar while they wait for a message that arrives
        whole and only at the end.
        """
        previous = self._progress_label
        self._progress_label = label
        self._progress_fraction = 0.0
        self._report(0.0)
        try:
            yield estimate
        finally:
            self._report(None)
            self._progress_label = previous
            self._progress_fraction = 0.0

    def _estimate_progress(self, started: float, estimate: Optional[float]) -> None:
        if not estimate:
            return
        elapsed = time.monotonic() - started
        self._report(min(0.95, elapsed / estimate))

    @classmethod
    def open(
        cls,
        port: str,
        *,
        recv_port: Optional[str] = None,
        channel: int = 0,
        timeout: float = DEFAULT_TIMEOUT,
        single_object_timeout: float = DEFAULT_SINGLE_OBJECT_TIMEOUT,
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
            single_object_timeout=single_object_timeout,
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

    #: Sends bigger than this get a progress ticker. Below it the message is
    #: over before a bar could move and the callers' hold covers the wait.
    PROGRESS_SEND_BYTES = 256

    def _send(self, data: bytes) -> None:
        data = bytes(data)
        stop: Optional[threading.Event] = None
        ticker: Optional[threading.Thread] = None
        if self.progress is not None and len(data) > self.PROGRESS_SEND_BYTES:
            # The transport may hand the whole message to the driver in one
            # call and never tell us how much has gone (rtmidi) or pace it
            # chunk by chunk (ALSA). A ticker against the wire-speed estimate
            # moves the bar either way; ALSA's exact counts still refine it.
            stop = threading.Event()
            ticker = threading.Thread(
                target=self._send_estimate,
                args=(len(data), stop),
                name="kwsed-progress",
                daemon=True,
            )
            ticker.start()
        try:
            self._send_now(data)
        finally:
            if stop is not None:
                stop.set()
            if ticker is not None:
                ticker.join(timeout=0.5)

    def _send_estimate(self, size: int, stop: threading.Event) -> None:
        """Move the bar while a message is being handed to the driver."""
        estimate = _wire_estimate(size)
        started = time.monotonic()
        while not stop.wait(0.1):
            elapsed = time.monotonic() - started
            self._report(min(0.94, elapsed / estimate))

    def _send_now(self, data: bytes) -> None:
        if len(data) > self.RTMIDI_LIMIT:
            address = (
                alsa.port_address(self.description.split(" -> ")[0])
                if alsa.available()
                else None
            )
            if address is not None:
                sender = alsa.Sender(*address, name=b"kwsed")
                try:
                    sender.send(
                        data,
                        progress=lambda sent, total: self._report(sent / total),
                    )
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

        single_object_timeout = cfg.load_single_object_timeout(config_path)

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
                    single_object_timeout=single_object_timeout,
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
            sweep=lambda try_hook: cls.autodetect(
                timeout=0.5,
                single_object_timeout=single_object_timeout,
                on_try=try_hook,
            ),
            remembered=remembered,
            scan=scan,
            remember=remember,
            on_try=on_try,
        )

    @classmethod
    def autodetect(
        cls,
        *,
        timeout: float = 0.5,
        single_object_timeout: float = DEFAULT_SINGLE_OBJECT_TIMEOUT,
        on_try: Optional[Callable[[str], None]] = None,
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
                bridge = cls.open(
                    port, timeout=timeout, single_object_timeout=single_object_timeout
                )
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
        kind_key = kind if kind is not None else banks.Kind.PATCH
        wire = _WIRE_BYTES.get(kind_key, _WIRE_BYTES[banks.Kind.PATCH])
        with self._progress_scope(
            f"receiving {entry.label} {banks.plural(kind_key)}",
            _wire_estimate(wire),
        ) as estimate:
            started = time.monotonic()
            self._drain()
            self._send(request)
            deadline = time.time() + (DUMP_TIMEOUT if timeout is None else timeout)
            while time.time() < deadline:
                message = self._in.get_message()
                if message is None:
                    self._estimate_progress(started, estimate)
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
            # The Card is the one bank the instrument only answers when the
            # hardware is there: with no Program Card in the slot the request is
            # simply not served, which looks identical to every other silence.
            # Lead with that cause rather than a list of cable faults.
            card_hint = ""
            if entry.id == "CARD":
                card_hint = (
                    " The Card bank only answers when a Program Card is in the "
                    "slot, so an empty slot (or an HCard that is not seated) is "
                    "the first thing to check."
                )
            raise TimeoutError(
                f"no dump came back for {entry.label}.{card_hint} The unit did "
                f"not refuse it either, so this otherwise means one of: powered "
                f"off, wrong port, wrong global channel (kwsed is using "
                f"{self.channel + 1}), System Exclusive reception disabled, or an "
                f"interface that cannot carry a message this long -- a patch bank "
                f"is nearly 30 kB."
            )

    def read_patch(
        self,
        bank: int | str,
        number: int,
        *,
        timeout: Optional[float] = None,
        label: Optional[str] = None,
    ) -> bytes:
        """One patch's 426 bytes. Read-only, and makes no sound.

        ``label`` names the transfer in the progress bar; the audition
        staging reads a slot to back it up, which is a different thing
        from reading it to play it and worth saying so.
        """
        entry = banks.bank(bank)
        label = label or f"reading {entry.label} patch {number:02d}"
        wire = records.PATCH_SIZE * 2
        with self._progress_scope(label, _wire_estimate(wire)) as estimate:
            started = time.monotonic()
            self._drain()
            self._send(m.single_patch_request(entry.sysex, number, self.channel))
            deadline = time.time() + (
                self.single_object_timeout if timeout is None else timeout
            )
            while time.time() < deadline:
                message = self._in.get_message()
                if message is None:
                    self._estimate_progress(started, estimate)
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
        wait_status: bool = True,
        before: Optional[bytes] = None,
        save_backup: bool = True,
    ) -> Dict[str, Any]:
        """Replace **one patch**. Destructive, and fenced like a bank write.

        Two messages, because one is not enough: a single patch dump lands
        in the edit buffer and names the destination, then a change of the
        SR-only EXECUTE_WRITE parameter commits it to that slot. That is
        Edisyn's write path, and it does not raise the unit's front-panel
        prompt. Korg's documented patch write command (0x11) does: seen
        live on a Wavestation SR (2026-11), it shows "SAVE PATCH -- ARE
        YOU SURE?" and waits for +1/YES, so a remote write stalls there and
        later reads come back addressed oddly while the prompt is up.

        Far smaller than a bank write: 426 bytes and one slot, rather than
        35 slots and nearly 30 kB on the wire. The fence is the same
        anyway, because the thing that makes a write safe is the backup and
        the read-back, not the size.

        ``save_backup`` is the audition's opt-out: the staging area is the
        user's scratch and capturing it costs a read per slot. With it off
        nothing is read before sending and no envelope is written, so the
        write cannot be undone -- the default keeps the fence intact.

        ``wait_status`` says whether to hold for the unit's WRITE_COMPLETED
        reply. The audition staging pairs this with ``verify=True`` and
        skips the wait: the read-back is the proof, and a unit that does
        not answer a parameter change would otherwise cost its timeout for
        every staged patch.

        **WARNING:** ``save_backup=False`` skips the pre-write read entirely,
        so no backup envelope is created and the write cannot be undone.
        ``verify=False`` skips the read-back comparison, so a failed write
        (cable disconnect, unit busy, memory protect, etc.) will not be
        detected. Without both, a failed write cannot be detected or undone.
        These options exist for the audition staging workflow where a human
        hears the result before committing; normal callers should leave both
        at their defaults.
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

        saved: Optional[str] = None
        if save_backup:
            if before is not None:
                if len(before) != records.PATCH_SIZE:
                    raise ValueError(
                        f"a patch is {records.PATCH_SIZE} bytes; got {len(before)}"
                    )
                before_bytes = bytes(before)
            else:
                try:
                    before_bytes = bytes(
                        self.read_patch(entry.sysex, number, timeout=timeout)
                    )
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
                        entry.sysex, number, before_bytes, channel=self.channel
                    )
                },
            )

        # Edisyn's write path (its performance writer pairs a dump with an
        # EXECUTE_WRITE change): the dump fills the edit buffer and names
        # the destination, and the SR-only parameter change performs the
        # write without the unit's front-panel confirmation. The documented
        # 0x11 command was seen live to stop at "SAVE PATCH -- ARE YOU
        # SURE?" and never write unattended.
        with self._progress_scope(f"writing {entry.label} patch {number:02d}"):
            self._drain()
            self._send(
                m.single_patch_dump(entry.sysex, number, data, channel=self.channel)
            )
            self._send(m.execute_write(channel=self.channel))

        result: Dict[str, Any] = {
            "bank": entry.label,
            "number": number,
            "backup": saved,
            "status": None,
            "verified": None,
            "mismatch": None,
        }
        if wait_status:
            deadline = time.time() + 2.0
            while time.time() < deadline:
                message = self._in.get_message()
                if message is None:
                    time.sleep(0.002)
                    continue
                packet = m.parse(message[0])
                if packet is None:
                    continue
                if packet.function in (
                    m.Function.WRITE_COMPLETED,
                    m.Function.WRITE_ERROR,
                ):
                    result["status"] = packet.function_name
                    break
        if not verify:
            return result
        after: Optional[bytes] = None
        for attempt in range(2):
            if attempt:
                # The unit can still be committing when the first read-back
                # is answered -- EXECUTE_WRITE is a front-panel save, not a
                # synchronous call. One retry tells a slow write from one
                # the unit never performed.
                time.sleep(0.25)
            try:
                after = self.read_patch(entry.sysex, number, timeout=timeout)
            except Exception as exc:
                result["verified"] = False
                result["mismatch"] = f"could not read it back ({exc})"
                return result
            if bytes(after) == bytes(data):
                break
        result["verified"] = after is not None and bytes(after) == bytes(data)
        if not result["verified"] and after is not None:
            result["mismatch"] = _first_difference(bytes(data), bytes(after))
        return result

    def read_performance(
        self, bank: int | str, number: int, *, timeout: Optional[float] = None
    ) -> bytes:
        """One performance's 181 bytes. Read-only for memory, but the reply
        lands in the performance edit buffer, so it clobbers whatever edit
        was buffered there -- the buffer is scratch, harmless, but say it."""
        entry = banks.bank(bank)
        self._drain()
        self._send(m.single_performance_request(entry.sysex, number, self.channel))
        deadline = time.time() + (
            self.single_object_timeout if timeout is None else timeout
        )
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
            if packet.function != m.Function.PERFORMANCE_DUMP:
                continue
            raw = bytes(message[0])
            if len(raw) < 7 or raw[5] != entry.sysex or raw[6] != number:
                continue
            return packet.data()[: records.PERFORMANCE_SIZE]
        raise TimeoutError(
            f"no performance came back for {entry.label} {number}. A reply for a "
            f"different slot does not count, so this can also mean the unit "
            f"is still answering an earlier request."
        )

    def send_performance_to_buffer(
        self,
        bank: int | str,
        number: int,
        data: bytes,
        *,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Fill the edit buffer with one performance. **Nothing is written.**

        A single performance dump lands in the unit's edit buffer: it is
        audible at once, bypasses memory protect, and survives nothing.
        The header's bank and number are where the buffer *would* go if
        the unit's own SAVE were executed; kwsed writes for real through
        :meth:`write_performance` instead, so the destination here only
        has to be a bank the unit knows.
        """
        entry = banks.bank(bank)
        if not 0 <= number < banks.PERFORMANCES_PER_BANK:
            raise ValueError(
                f"a bank holds {banks.PERFORMANCES_PER_BANK} performances, so "
                f"{number} is not one of them"
            )
        if len(data) != records.PERFORMANCE_SIZE:
            raise ValueError(
                f"a performance is {records.PERFORMANCE_SIZE} bytes; got {len(data)}"
            )

        with self._progress_scope(f"auditioning on {entry.label} {number:02d}"):
            self._drain()
            self._send(
                m.single_performance_dump(
                    entry.sysex, number, data, channel=self.channel
                )
            )
        return {"bank": entry.label, "number": number}

    def write_performance(
        self,
        bank: int | str,
        number: int,
        data: bytes,
        *,
        backup_dir: str,
        verify: bool = True,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Replace **one performance**. Destructive, and fenced like a patch write.

        Two messages, because one is not enough: a single performance dump
        lands in the edit buffer, and a separate write command commits it to
        the slot. The backup covers the slot it overwrites.

        **WARNING:** ``verify=False`` skips the read-back comparison, so a
        failed write (cable disconnect, unit busy, memory protect, etc.) will
        not be detected. Without verification, a failed write cannot be
        detected or undone. This option exists for workflows where the
        caller provides its own confirmation; normal callers should leave it
        at its default.
        """
        entry = banks.bank(bank)
        if not entry.writable:
            raise DeviceError(f"{entry.label} is ROM and cannot be written.")
        if not 0 <= number < banks.PERFORMANCES_PER_BANK:
            raise ValueError(
                f"a bank holds {banks.PERFORMANCES_PER_BANK} performances, so "
                f"{number} is not one of them"
            )
        if len(data) != records.PERFORMANCE_SIZE:
            raise ValueError(
                f"a performance is {records.PERFORMANCE_SIZE} bytes; got {len(data)}"
            )

        try:
            before = self.read_performance(entry.sysex, number, timeout=timeout)
        except Exception as exc:
            raise DeviceError(
                f"could not read {entry.label} performance {number} before "
                f"writing it, so nothing was sent ({exc})."
            ) from exc
        saved = backup.save(
            backup_dir,
            kind="performance",
            slot=_performance_backup_name(entry.label, number),
            name=_performance_backup_name(entry.label, number),
            device_id=str(self.channel),
            source="kwsed",
            blocks={
                "dump": m.single_performance_dump(
                    entry.sysex, number, before, channel=self.channel
                )
            },
        )

        # Edisyn's write, not the documented write command: the buffer's
        # destination was set by the dump above, and an EXECUTE_WRITE change
        # performs it without the unit's front-panel prompt. (The 0x11 patch
        # write command was seen live to stop at "SAVE PATCH -- ARE YOU
        # SURE?"; its 0x1A sibling is presumed to behave the same, and
        # Edisyn never uses either.)
        with self._progress_scope(f"writing {entry.label} performance {number:02d}"):
            self._drain()
            self._send(
                m.single_performance_dump(
                    entry.sysex, number, data, channel=self.channel
                )
            )
            self._send(m.execute_write(channel=self.channel))

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
            after = self.read_performance(entry.sysex, number, timeout=timeout)
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

        **WARNING:** ``verify=False`` skips the read-back comparison, so a
        failed write (cable disconnect, unit busy, memory protect, etc.) will
        not be detected. Without verification, a failed write cannot be
        detected or undone. This option exists for workflows where the
        caller provides its own confirmation; normal callers should leave it
        at its default.
        """
        raw = bytes(data)
        looks_like_frame = (
            len(raw) > 5
            and raw[0] == 0xF0
            and raw[-1] == 0xF7
            and raw[4] == m.Function.MULTISET_DUMP
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

        # The 0x62 structure is longer than the part kwsed models: the
        # unit's own dump decodes to 3777 bytes, of which the first 3264
        # are the Multisets and their names. The rest is preserved from the
        # pre-write read rather than zeroed.
        edited = raw
        merged = raw
        if looks_like_frame:
            sent = raw
        else:
            packet = m.parse(before)
            if packet is None or len(packet.payload) < 2:
                raise DeviceError(
                    "the Multi Mode Setup read back before writing was not a "
                    "Wavestation message, so nothing was sent. kwsed does not "
                    "overwrite a dump it could not read."
                )
            header = list(packet.payload[:2])
            full = packet.data()
            if len(full) > len(edited):
                merged = edited + full[len(edited) :]
            nibbles = list(m.to_nibbles(merged))
            # Korg's checksum covers the two header bytes and the nibble
            # data: the unit's own dump confirms it (header 00 01 makes the
            # header-inclusive sum differ from the nibbles-only one by one).
            check = m.checksum([*header, *nibbles])
            sent = m.frame(
                m.Function.MULTISET_DUMP,
                [*header, *nibbles, check],
                channel=self.channel,
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
        if looks_like_frame:
            result["verified"] = bytes(after) == bytes(sent)
            if not result["verified"]:
                result["mismatch"] = _first_difference(bytes(sent), bytes(after))
            return result
        after_packet = m.parse(after)
        if after_packet is None:
            result["verified"] = False
            result["mismatch"] = "the read-back was not a Wavestation message"
            return result
        got = after_packet.data()
        # Compare the part kwsed models (the Multisets and names). The
        # unmodelled tail is preserved from the pre-write read; if the unit
        # returns a different one, say so without failing the write for a
        # part this program does not claim to know.
        result["verified"] = got[: len(edited)] == edited
        if not result["verified"]:
            result["mismatch"] = _first_difference(edited, got[: len(edited)])
        elif got != merged:
            result["mismatch"] = (
                "the multisets were written; the unmodelled tail of the "
                "Multi Mode Setup differs"
            )
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

    def set_midi_mode(self, mode: int, *, channel: Optional[int] = None) -> None:
        """Select OMNI, POLY, MULTI or MONO reception. **Changes device state.**

        The MIDI mode is global parameter 4 on the Wavestation/SR
        (Reference Guide Appendix 4 section 5.12); it is changed with a
        Parameter Change message whose value is ASCII. Nothing is written
        to memory and there is no read-back, so the change is not verified.
        ``POLY`` is the single-Performance mode; ``MULTI`` routes all
        sixteen channels through the current Multiset.

        Sending the mode first is what makes a Program Change mean a
        Performance: in MULTI mode the same Program Change selects a
        Multiset instead.
        """
        value = int(mode)
        if value not in (
            MIDI_MODE_OMNI,
            MIDI_MODE_POLY,
            MIDI_MODE_MULTI,
            MIDI_MODE_MONO,
        ):
            raise ValueError(f"MIDI mode {value} is outside 0-3")
        target = self.channel if channel is None else channel
        if not 0 <= target <= 15:
            raise ValueError(f"channel {target} is outside 0-15")
        self._send(m.parameter_change(MIDI_MODE_PARAMETER, value, channel=target))

    def select(self, slot: banks.Slot, *, channel: Optional[int] = None) -> None:
        """Select a performance. **This changes the sound and the MIDI mode.**

        The unit is first put into ``POLY`` (single-Performance) mode,
        because in ``MULTI`` mode a Program Change selects a Multiset
        instead of a Performance. Then Bank Select MSB (CC0) = 0, Bank
        Select LSB (CC32) = the bank's MIDI bank, then a Program Change =
        program_base + slot.number. Per Player's Guide §4.8 and Reference
        Appendix 3 §1.3 the SR's twelve banks are six MIDI banks (CC32
        0-5), each holding two internal banks of 50, so Program Change
        0-49 selects the first and 50-99 the second. The SR has no patch
        mode: a Program Change always selects a Performance, never a
        patch, so a patch slot is refused. The SR's "Bank Select"
        MIDI-receive parameter must be ON for this to work; kwsed cannot
        read it.
        """
        if slot.kind == banks.Kind.PATCH:
            raise ValueError(
                "a patch is heard through a performance and cannot be "
                "selected on its own"
            )
        target = self.channel if channel is None else channel
        if not 0 <= target <= 15:
            raise ValueError(f"channel {target} is outside 0-15")
        bank = slot.bank
        program = bank.program_base + slot.number
        if program > 99:
            raise ValueError(
                f"program change {program} is outside 0-99 for {bank.label}"
            )
        self.set_midi_mode(MIDI_MODE_POLY, channel=target)
        status = 0xB0 | target
        self._out.send_message([status, 0x00, 0x00])
        self._out.send_message([status, 0x20, bank.midi_bank])
        self._out.send_message([0xC0 | target, program])
        time.sleep(SEND_GAP)

    def select_multi(self, number: int, *, channel: Optional[int] = None) -> None:
        """Make a Multiset current. **Changes the sound and the MIDI mode.**

        The unit is first put into ``MULTI`` mode, then Reference Guide
        Appendix 4, sections 2.1.20 and 2.1.21: ``0x5B`` for Multisets
        0-15, ``0x63`` for 16-31 (SR Expanded), the number sent as the
        byte the manual shows (16 -> ``0x10``). The unit changes its
        current multi setup on reception; there is no memory write and no
        read-back, so neither message is verified.
        """
        target = self.channel if channel is None else channel
        if not 0 <= target <= 15:
            raise ValueError(f"channel {target} is outside 0-15")
        self.set_midi_mode(MIDI_MODE_MULTI, channel=target)
        self._send(m.multi_select(number, channel=target))

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

        **WARNING:** ``verify=False`` skips the read-back comparison, so a
        failed write (cable disconnect, unit busy, memory protect, etc.) will
        not be detected. Without verification, a failed write cannot be
        detected or undone. This option exists for workflows where the
        caller provides its own confirmation; normal callers should leave it
        at its default.
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
        frame = m.frame_bulk(function, entry.sysex, data, channel=self.channel)
        with self._progress_scope(f"writing {entry.label} {banks.plural(kind)}"):
            self._drain()
            self._send(frame)

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
        """Replace several kinds in one bank as **one** undoable write.

        Copying a performance into another bank rewrites its performances,
        its patches and (when wave sequences are involved) its wave
        sequences. Three separate :meth:`write_bank` calls would produce
        three backups, three read-backs, and three chances to stop halfway
        through a bank that is then neither the old one nor the new one.

        So the fence is applied to the bundle as a whole:

        1. **A ROM bank is refused** before any traffic, as in
           :meth:`write_bank`.
        2. **Every payload is checked**, and every kind being written is
           read in full and backed up into **one** envelope. If any of those
           reads fails, nothing is sent -- a bundle that cannot be undone is
           not one this program performs.
        3. The dumps go out with wave sequences first and performances last,
           so the patches a performance names are already there when it
           arrives.
        4. **Every written kind is read back and compared**, and the result
           says whether all of them match rather than only the last.

        The order of the result's ``kinds`` is the send order, not the
        caller's argument order.

        **WARNING:** ``verify=False`` skips the read-back comparison for all
        kinds in the bundle, so a failed write (cable disconnect, unit busy,
        memory protect, etc.) will not be detected for any of them. Without
        verification, a failed write cannot be detected or undone. This
        option exists for workflows where the caller provides its own
        before-blocks and confirmation; normal callers should leave it at
        its default.
        """
        entry = banks.bank(bank)
        if not entry.writable:
            raise DeviceError(
                f"{entry.label} is ROM and cannot be written. The banks that "
                f"can are: "
                f"{', '.join(b.label for b in banks.writable_banks())}."
            )

        payloads: Dict[str, bytes] = {}
        for kind, data in (
            ("waveseq", waveseq),
            ("patches", patches),
            ("performances", performances),
        ):
            if data is None:
                continue
            expected = _BUNDLE_SIZES[kind]
            if len(data) != expected:
                raise ValueError(
                    f"{kind} must be {expected} bytes for a whole bank; got {len(data)}"
                )
            payloads[kind] = bytes(data)
        if not payloads:
            raise ValueError("nothing to write")

        # --- 1. back every destination up, or do not write any of them ------
        # For kinds supplied in before_blocks, use the provided raw framed
        # dump and do not read the destination. Parse it now, before anything
        # is sent, so a malformed frame fails fast.
        before: Dict[str, bytes] = {}
        predecoded: Dict[str, bytes] = {}
        for kind, function in _BUNDLE_FUNCTIONS.items():
            if kind not in payloads:
                continue
            supplied = None
            if before_blocks is not None and kind in before_blocks:
                supplied = before_blocks[kind]
            if supplied is not None:
                raw = bytes(supplied)
                packet = m.parse(raw)
                if packet is None:
                    raise ValueError(
                        f"{kind} before-block is not a valid Wavestation frame"
                    )
                try:
                    decoded = packet.data()
                except Exception as exc:
                    raise ValueError(
                        f"{kind} before-block could not be decoded: {exc}"
                    ) from exc
                if len(decoded) != _BUNDLE_SIZES[kind]:
                    raise ValueError(
                        f"{kind} before-block decodes to {len(decoded)} bytes; "
                        f"expected {_BUNDLE_SIZES[kind]}"
                    )
                before[kind] = raw
                predecoded[kind] = decoded
            else:
                try:
                    raw = self.read_raw(entry.sysex, function, timeout=timeout)
                except Exception as exc:
                    raise DeviceError(
                        f"could not read {entry.label} {kind} before writing it, "
                        f"so nothing was sent ({exc}). kwsed does not overwrite a "
                        f"bank it has not backed up."
                    ) from exc
                before[kind] = raw
                packet = m.parse(raw)
                if packet is None:
                    raise ValueError(
                        f"{entry.label} {kind} read back is not a valid frame"
                    )
                predecoded[kind] = packet.data()

        # --- unchanged kinds are skipped entirely ---------------------------
        unchanged: List[str] = [
            kind
            for kind in _BUNDLE_FUNCTIONS
            if kind in payloads and payloads[kind] == predecoded[kind]
        ]
        sent_kinds: List[str] = [
            kind
            for kind in _BUNDLE_FUNCTIONS
            if kind in payloads and kind not in unchanged
        ]

        saved = backup.save(
            backup_dir,
            kind="bank-bundle",
            slot=entry.label,
            name=entry.label,
            device_id=str(self.channel),
            source="kwsed",
            blocks=before,
        )

        # All unchanged: nothing to send or read back.
        if not sent_kinds:
            result: Dict[str, Any] = {
                "bank": entry.label,
                "kind": "bank-bundle",
                "kinds": [],
                "backup": saved,
                "verified": True,
                "mismatch": None,
                "unchanged": unchanged,
            }
            return result

        # --- 2. send, wave sequences first and performances last ------------
        self._drain()
        sent: Dict[str, bytes] = {}
        for kind, function in _BUNDLE_FUNCTIONS.items():
            if kind not in sent_kinds:
                continue
            frame = m.frame_bulk(
                function,
                entry.sysex,
                payloads[kind],
                channel=self.channel,
            )
            sent[kind] = frame
            with self._progress_scope(f"writing {entry.label} {_bundle_label(kind)}"):
                self._send(frame)

        result = {
            "bank": entry.label,
            "kind": "bank-bundle",
            "kinds": sent_kinds,
            "backup": saved,
            "verified": None,
            "mismatch": None,
            "unchanged": unchanged,
        }
        if not verify:
            return result

        # --- 3. read every sent kind back and compare -----------------------
        mismatch: Optional[str] = None
        for kind in sent_kinds:
            try:
                after = self.read_raw(
                    entry.sysex, _BUNDLE_FUNCTIONS[kind], timeout=timeout
                )
            except Exception as exc:
                mismatch = (
                    f"the write was sent but {entry.label} {kind} could not be "
                    f"read back to check it ({exc}). Whether it took is unknown."
                )
                break
            if bytes(after) != sent[kind]:
                mismatch = _first_difference(sent[kind], bytes(after))
                break
        result["verified"] = mismatch is None
        result["mismatch"] = mismatch
        return result

    def copy_bank(
        self,
        source: int | str,
        destination: int | str,
        *,
        backup_dir: str,
        verify: bool = True,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Copy one whole bank into another, kinds together and undoable.

        This is the unit's MIDI/Global BANK COPY, performed over documented
        requests instead of from the front panel, and fenced: the
        destination's previous contents are backed up into one envelope
        before anything is sent, and every written kind is read back and
        compared. The panel function offers neither, which is why this does
        not merely trigger it -- there is no documented SysEx request that
        would.

        All three kinds are copied. A partial copy (performances without the
        wave-sequence pool they name) silently changes how the destination
        sounds, so there is no kind selector; slot-preserving partial
        copying is :mod:`kws.perfcopy`'s job.

        The performance bank is **remapped** the way the panel function
        remaps it: every part that names the source bank itself is rewritten
        to name the destination, so the copy is self-contained rather than
        still playing from where it came from. Parts pointing at ROM, the
        Card or a third bank are left alone -- see
        :func:`kws.records.remap_performance_banks`.
        """
        src = banks.bank(source)
        dst = banks.bank(destination)
        if not src.writable:
            raise DeviceError(
                f"{src.label} is ROM and cannot be copied from. The banks "
                f"that can are: "
                f"{', '.join(b.label for b in banks.writable_banks())}."
            )
        if src.number == dst.number:
            raise DeviceError(f"copying {src.label} onto itself copies nothing")

        decoded: Dict[str, bytes] = {}
        for kind, function in _BUNDLE_FUNCTIONS.items():
            try:
                raw = self.read_raw(src.sysex, function, timeout=timeout)
            except Exception as exc:
                raise DeviceError(
                    f"could not read {src.label} {kind}, so nothing was sent "
                    f"({exc}). kwsed does not start a copy it cannot finish."
                ) from exc
            packet = m.parse(raw)
            if packet is None:
                raise DeviceError(f"{src.label} {kind} read back is not a valid frame")
            data = packet.data()
            expected = _BUNDLE_SIZES[kind]
            if len(data) != expected:
                raise DeviceError(
                    f"{src.label} {kind} decoded to {len(data)} bytes; a whole "
                    f"bank is {expected}"
                )
            decoded[kind] = bytes(data)

        decoded["performances"] = records.remap_performance_banks(
            decoded["performances"], src.number, dst.number
        )

        return self.write_bank_bundle(
            dst.number,
            performances=decoded["performances"],
            patches=decoded["patches"],
            waveseq=decoded["waveseq"],
            backup_dir=backup_dir,
            verify=verify,
            timeout=timeout,
        )

    def read_raw(
        self, bank_number: int, function: int, *, timeout: Optional[float] = None
    ) -> bytes:
        """One bank dump of a given kind, as the bytes that arrived."""
        wire = _FUNCTION_WIRE_BYTES.get(function, 8000)
        label = f"receiving bank {bank_number} {m.Function.name(function)}"
        with self._progress_scope(label, _wire_estimate(wire)) as estimate:
            started = time.monotonic()
            self._drain()
            self._send(m.bank_request(bank_number, function, self.channel))
            deadline = time.time() + (DUMP_TIMEOUT if timeout is None else timeout)
            while time.time() < deadline:
                message = self._in.get_message()
                if message is None:
                    self._estimate_progress(started, estimate)
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


def _performance_backup_name(bank_label: str, number: int) -> str:
    return f"{bank_label}-performance{number:02d}"
