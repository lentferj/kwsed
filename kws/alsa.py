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

"""Sending a bank-sized System Exclusive message on Linux.

python-rtmidi cannot. It sends 8 kB happily and fails at 16 kB with "error
sending MIDI message to port", and a Wavestation patch bank is 29828 bytes,
so every bank write died before a byte left the machine. This is the way
round it, and it is all about limits that are not where they look:

**The kernel caps the sequencer client's output pool at 2000 cells.**
``snd_seq_set_client_pool_output`` accepts a larger number, returns success,
and clamps -- asking for 100000 leaves it at 2000. So "make the buffer
bigger" is not available, and any approach that needs one event to fit in
the pool is a dead end.

**A System Exclusive message need not be one event.** Once it reaches the
wire it is a byte stream, so it can be handed to the sequencer in many small
events whose payloads run together; the device at the far end sees one
continuous message. That is what makes this work at all. Verified
byte-for-byte: 29828 bytes sent through a virtual port and read back off its
raw side came out identical.

**EAGAIN is not an error here.** A 30 kB dump is about eight seconds of MIDI
at 31250 baud and the far end drains it no faster than the wire allows, so
the sequencer refuses more until it has room. It is offered again rather
than treated as a failure.

One trap worth naming, since it costs a segmentation fault rather than an
error: ``snd_seq_ev_ext_t`` is declared ``__attribute__((packed))`` in
ALSA's headers. Without ``_pack_ = 1`` ctypes aligns its pointer to offset 8
where ALSA reads it from 4, and the library dereferences whatever is there.
"""

import ctypes
import ctypes.util
import time

__all__ = ["Sender", "available", "port_address"]

_lib = None
try:
    _lib = ctypes.CDLL(ctypes.util.find_library("asound") or "libasound.so.2")
except OSError:  # pragma: no cover - depends on the host
    _lib = None

SND_SEQ_OPEN_OUTPUT = 1
SND_SEQ_NONBLOCK = 1
SND_SEQ_PORT_CAP_READ = 1 << 0
SND_SEQ_PORT_CAP_SUBS_READ = 1 << 5
SND_SEQ_PORT_TYPE_MIDI_GENERIC = 1 << 1
SND_SEQ_PORT_TYPE_APPLICATION = 1 << 20
SND_SEQ_EVENT_SYSEX = 130
SND_SEQ_TIME_STAMP_TICK = 0
SND_SEQ_EVENT_LENGTH_VARIABLE = 1 << 2


class _Addr(ctypes.Structure):
    _fields_ = [("client", ctypes.c_ubyte), ("port", ctypes.c_ubyte)]


class _Ext(ctypes.Structure):
    # snd_seq_ev_ext_t is declared __attribute__((packed)) in ALSA's
    # headers, so the pointer sits at offset 4 and the struct is 12 bytes.
    # Without _pack_ ctypes aligns the pointer to 8, ALSA reads it from the
    # wrong offset, and the result is a segmentation fault rather than an
    # error code.
    _pack_ = 1
    _fields_ = [("len", ctypes.c_uint), ("ptr", ctypes.c_void_p)]


class _Data(ctypes.Union):
    _fields_ = [("raw8", ctypes.c_ubyte * 12), ("ext", _Ext)]


class _Event(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_ubyte),
        ("flags", ctypes.c_ubyte),
        ("tag", ctypes.c_ubyte),
        ("queue", ctypes.c_ubyte),
        ("time", ctypes.c_ubyte * 8),
        ("source", _Addr),
        ("dest", _Addr),
        ("data", _Data),
    ]


if _lib is not None:
    _lib.snd_seq_open.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_int,
    ]
    _lib.snd_seq_set_client_name.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    _lib.snd_seq_create_simple_port.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_uint,
        ctypes.c_uint,
    ]
    _lib.snd_seq_connect_to.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
    ]
    _lib.snd_seq_set_client_pool_output.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    _lib.snd_seq_set_output_buffer_size.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    _lib.snd_seq_event_output.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Event)]
    _lib.snd_seq_event_output_direct.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_Event),
    ]
    _lib.snd_seq_drain_output.argtypes = [ctypes.c_void_p]
    _lib.snd_seq_close.argtypes = [ctypes.c_void_p]
    _lib.snd_strerror.argtypes = [ctypes.c_int]
    _lib.snd_strerror.restype = ctypes.c_char_p


def _check(code, what):
    if code < 0:
        detail = _lib.snd_strerror(code).decode() if _lib is not None else str(code)
        raise OSError(f"{what}: {detail}")
    return code


def _require_lib() -> None:
    if _lib is None:
        raise OSError(
            "no ALSA sequencer on this host; large dumps cannot be sent via kws.alsa"
        )


class Sender:
    def __init__(
        self,
        client: int,
        port: int,
        *,
        pool: int = 2000,
        buffer_size: int = 1 << 18,
        name: bytes = b"kwsed",
    ):
        _require_lib()
        self._seq = ctypes.c_void_p()
        _check(
            _lib.snd_seq_open(
                ctypes.byref(self._seq), b"default", SND_SEQ_OPEN_OUTPUT, 0
            ),
            "snd_seq_open",
        )
        _check(_lib.snd_seq_set_client_name(self._seq, name), "set_client_name")
        # Two different limits, and both bite. The kernel-side *pool* is
        # how many event cells the client may hold; the userspace *output
        # buffer* is how large a single event may be before
        # snd_seq_event_output refuses it with EINVAL. Raising only the pool
        # leaves a bank dump still rejected.
        _check(
            _lib.snd_seq_set_client_pool_output(self._seq, pool),
            "set_client_pool_output",
        )
        _check(
            _lib.snd_seq_set_output_buffer_size(self._seq, buffer_size),
            "set_output_buffer_size",
        )
        self._port = _check(
            _lib.snd_seq_create_simple_port(
                self._seq,
                b"out",
                SND_SEQ_PORT_CAP_READ | SND_SEQ_PORT_CAP_SUBS_READ,
                SND_SEQ_PORT_TYPE_MIDI_GENERIC | SND_SEQ_PORT_TYPE_APPLICATION,
            ),
            "create_simple_port",
        )
        _check(
            _lib.snd_seq_connect_to(self._seq, self._port, client, port), "connect_to"
        )

    #: Bytes per ALSA event. A System Exclusive message is just a byte
    #: stream once it reaches the wire, so it may be handed to the
    #: sequencer in several events whose payloads run together -- the
    #: receiving device sees one continuous message. That is what makes a
    #: 30 kB bank dump sendable at all: a single event of that size is
    #: refused with ENOMEM however large the client pool is set, because the
    #: kernel caps the pool at 2000 cells no matter what is asked for.
    CHUNK = 256

    #: A MIDI DIN cable carries 3125 bytes a second: 31250 baud, ten bits
    #: to a byte with the start and stop bits. Sending faster does not make
    #: a dump arrive sooner -- it makes the interface drop what it cannot
    #: hold, because MIDI has no flow control to push back with.
    #:
    #: This was not theoretical. The first bank written to real hardware
    #: went out in 0.00 seconds for 29828 bytes, and the instrument stored a
    #: stream with 672 bytes missing from the middle: every patch after the
    #: sixth was shifted earlier by 336 and the tail was lost. The read-back
    #: comparison caught it, which is the entire reason that comparison
    #: exists.
    #:
    #: **Deliberately below the wire rate, not at it.** Pacing at exactly
    #: 3125 took 9.53 seconds for 29828 bytes -- the theoretical minimum,
    #: which is another way of saying no margin at all. The sibling
    #: k2kremote records paying for exactly that mistake: a gap its own
    #: sweep said was safe locked the K2000 up in ordinary use, because a
    #: clean measurement is not the same as real traffic. Ten per cent back
    #: costs a second on a bank and is not worth arguing about.
    BYTES_PER_SECOND = 2800

    def send(self, data: bytes) -> None:
        """Send one message, paced to the speed of the wire."""
        data = bytes(data)
        for start in range(0, len(data), self.CHUNK):
            chunk = data[start : start + self.CHUNK]
            self._send_one(chunk)
            if start + self.CHUNK < len(data):
                time.sleep(len(chunk) / self.BYTES_PER_SECOND)

    def _send_one(self, data: bytes) -> None:
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        ev = _Event()
        ctypes.memset(ctypes.byref(ev), 0, ctypes.sizeof(ev))
        ev.type = SND_SEQ_EVENT_SYSEX
        ev.flags = SND_SEQ_TIME_STAMP_TICK | SND_SEQ_EVENT_LENGTH_VARIABLE
        ev.queue = 253  # SND_SEQ_QUEUE_DIRECT
        ev.source = _Addr(self._port, 0)
        ev.dest = _Addr(254, 0)  # SND_SEQ_ADDRESS_SUBSCRIBERS
        ev.data.ext.len = len(data)
        ev.data.ext.ptr = ctypes.cast(buf, ctypes.c_void_p)
        # Direct, not pooled. snd_seq_event_output buffers the event in
        # the client's kernel pool, and a bank dump does not fit however
        # large the pool is set -- it fails with ENOMEM at drain time,
        # which is a confusing place to hear about it. Direct dispatch
        # hands the event straight to the port and has no such limit.
        # EAGAIN means the destination cannot take it yet, not that
        # anything is wrong: a 30 kB dump is about eight seconds of MIDI at
        # 31250 baud, and the far end drains it no faster than the wire
        # allows. Wait and offer it again rather than failing the write.
        deadline = time.time() + 30.0
        while True:
            rc = _lib.snd_seq_event_output_direct(self._seq, ctypes.byref(ev))
            if rc >= 0:
                return
            if rc != -11 or time.time() > deadline:  # -EAGAIN
                _check(rc, "event_output_direct")
            time.sleep(0.002)

    def close(self):
        if self._seq:
            _lib.snd_seq_close(self._seq)
            self._seq = None


def port_address(name: str):
    """``(client, port)`` from an ALSA port name, or None.

    python-rtmidi's ALSA backend ends every port name with the sequencer
    address -- "U6MIDI Pro:U6MIDI Pro MIDI 1 44:0" -- which is the only
    thing tying a name the user typed to a client this module can open.
    """
    import re

    found = re.search(r"(\d+):(\d+)\s*$", name or "")
    if not found:
        return None
    return int(found.group(1)), int(found.group(2))


def available() -> bool:
    """Whether this host has an ALSA sequencer to talk to."""
    return _lib is not None
