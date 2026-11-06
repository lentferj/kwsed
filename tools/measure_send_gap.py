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

"""Measure the two numbers kwsed's transport guesses at.

``kws/bridge.py`` sets ``SEND_GAP = 0.010``, inherited from the sibling
projects where it is also a guess, and ``kws/alsa.py`` paces every
bank-sized send at ``Sender.BYTES_PER_SECOND = 2800`` in 256-byte chunks --
a number reasoned out from the wire rather than measured on it. TODO.md
item 5 wanted both measured rather than assumed. This is the bench tool
that measures them.

**A bench tool: the unit half drives the real unit and must be run alone.**
Quit every other MIDI program first, and leave the front panel alone while
it runs. It opens a real port and sends the messages described below; it
will not open one, or its own loopback, without ``--yes``.

**It changes no memory, and makes no sound.** Every message that leaves
this tool for the unit is a dump request -- a read. The nanosyned original
has a SELECT_GAP half that sends Bank Select and Program Change; there is
no counterpart here, because kwsed's ``select()`` is the one message that
changes what the unit plays, and nothing in this tool sends one.

What is measured
----------------

``SEND_GAP``
    For each candidate gap, read one whole bank as a single bank-sized
    System Exclusive message -- a patch bank is nearly 30 kB on the wire --
    and compare the bytes against a reference read taken at
    ``--reference-gap`` (0.100 s), twice. The interesting failure is not
    only a timeout: kwsed drains the input before every request and checks
    which bank the reply describes, so a reply belonging to the *previous*
    request is discarded rather than filed -- which turns a too-small gap
    into a timeout rather than a wrong read, and is exactly why a bare
    success count is not the check. The siblings compare names because they
    read one slot at a time; kwsed reads the whole bank in one message, so
    the bytes are compared instead: everything the names are computed from,
    plus the frame around them.

    Bank-sized messages go *out* through :mod:`kws.alsa` on a write, paced
    by ``Sender.BYTES_PER_SECOND``; the second half measures that pace
    directly, which is why it is in this tool at all.

``BYTES_PER_SECOND``
    Push messages of increasing size through the real
    :class:`kws.alsa.Sender` -- 256-byte chunks, the EAGAIN retry, the
    whole path a bank write takes -- into a loopback port this tool creates
    itself, and report the largest that arrives byte-identical at each
    pace. **No unit, no wire, nothing stored**: the loopback is this
    machine's own ALSA sequencer, so what this half cannot show is an
    interface dropping what it cannot hold -- the 672 missing bytes of
    TODO.md item 5b need a real interface to happen. What it does show is
    whether the chunking carries a bank-sized message intact, and how fast
    the bytes actually went out.

Neither constant is edited here. The final block prints the values to
paste into ``kws/bridge.py`` and ``kws/alsa.py`` deliberately, and a
measured value supersedes the guess. Record the run in
``docs/RESOLUTION_NOTES.md``.

Usage::

    python3 tools/measure_send_gap.py                  # dry listing
    python3 tools/measure_send_gap.py --yes --scan
    python3 tools/measure_send_gap.py --yes --no-unit   # the loopback only
    python3 tools/measure_send_gap.py --yes --gaps 0.002,0.001 --no-transport

Exit status is 0 when the measurement completed, 1 when it could not be
taken at all -- no port, nothing answering, a reference read that was not
reproducible, or no ALSA sequencer for the loopback -- and 2 for a usage
error. A candidate gap failing is the *result*, not an error.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, List, Optional, Tuple, TypeVar

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kws import alsa, banks
from kws import config as cfg
from kws import messages as m
from vinsynlib import cli, spec

if TYPE_CHECKING:
    from kws import bridge

#: Candidate ``SEND_GAP``s in seconds, longest first: the current guess
#: first, then tighter, so the table says where it starts to fail. The same
#: five the hardware suite sweeps in ``tests/test_hardware.py``, which is
#: the sweep this tool is the bench half of.
DEFAULT_GAPS: Tuple[float, ...] = (0.010, 0.005, 0.002, 0.001, 0.0005)

#: The gap the reference read is taken at. Long enough that a failure there
#: is the unit's problem rather than the timing's.
DEFAULT_REFERENCE_GAP = 0.100

#: How many reference passes. One clean sweep is an absence of evidence on
#: a cable: a marginal link fails intermittently, and a one-shot read of a
#: reference is exactly what an intermittent failure hides behind.
REFERENCE_PASSES = 2

#: Sizes the loopback half tries, in bytes. A message rtmidi itself can
#: carry; one it cannot (RESOLUTION_NOTES section 11 measured rtmidi at 8 kB
#: and failing at 16 kB); and a framed patch bank, the largest message kwsed
#: ever puts on the wire.
DEFAULT_SIZES: Tuple[int, ...] = (1024, 8192, 29828)

#: Paces to try, in bytes a second: the one :class:`kws.alsa.Sender` ships
#: with, the DIN cable's own 3125 (31250 baud, ten bits to a byte), and
#: twice the wire -- the pace at which an interface with no flow control
#: starts dropping what it cannot hold.
DEFAULT_PACES: Tuple[int, ...] = (alsa.Sender.BYTES_PER_SECOND, 3125, 6250)

#: Failure lines printed per half. The rest are counted, not listed: a pace
#: that loses every size fails loudly enough in the table already.
SHOWN = 6

#: The loopback port this process creates. Carries the pid so two runs at
#: once cannot read each other's answers as their own.
LOOPBACK_NAME = f"kwsed-measure-send-gap-{os.getpid()}"

#: How long to wait for the loopback after the send finished. Everything
#: was already handed to the sequencer by then, so a reply that has not
#: arrived within the grace period is not coming.
RECEIVE_GRACE = 10.0


@dataclass
class GapResult:
    """One candidate gap, after all its repeats."""

    gap: float
    clean: int
    repeats: int
    timeouts: int
    differs: int
    mean_seconds: float
    notes: List[str] = field(default_factory=list)


@dataclass
class Trial:
    """One size pushed through the loopback at one pace."""

    size: int
    intact: bool
    send_seconds: float
    received: int

    @property
    def status(self) -> str:
        """What came back, in one word -- the table's fourth column.

        ``short`` is the word that matters: a message that arrived shorter
        than it went out is the signature of a receiver dropping what it
        cannot hold, which is the failure this half exists to see.
        """
        if self.intact:
            return "intact"
        if not self.received:
            return "no reply"
        if self.received != self.size:
            return "short"
        return "differs"


@dataclass(frozen=True)
class Sweep:
    """What one run sweeps, decided before anything is opened."""

    entry: banks.Bank
    kind: str
    gaps: Tuple[float, ...]
    sizes: Tuple[int, ...]
    paces: Tuple[int, ...]
    repeats: int
    reference_gap: float
    channel: int


@dataclass
class Findings:
    """What each half measured, for the paste-ready block at the end."""

    send_gap: Optional[float] = None
    send_why: str = "not measured"
    pace_why: str = "not measured"


def _payload(size: int) -> bytes:
    """A well-formed System Exclusive message of exactly ``size`` bytes.

    Data bytes stay in 0x40-0x7F so nothing inside can be read as a status
    byte and end the message early; the shape only has to survive the wire,
    not decode as anything.
    """
    body = bytes((index % 64) + 0x40 for index in range(size - 2))
    return bytes([0xF0]) + body + bytes([0xF7])


def _difference(sent: bytes, got: bytes) -> str:
    """Where a read stopped matching the reference, in one line."""
    if len(sent) != len(got):
        return f"{len(got)} bytes arrived, {len(sent)} were expected"
    for index, (a, b) in enumerate(zip(sent, got)):
        if a != b:
            return f"byte {index} differs: {a:#04x} expected, {b:#04x} arrived"
    return "the bytes match, which contradicts this line"


def _function_for(kind: str) -> int:
    """The dump function a bank read of this kind asks for."""
    if kind == banks.Kind.PATCH:
        return m.Function.ALL_PATCH_DUMP
    if kind == banks.Kind.PERFORMANCE:
        return m.Function.ALL_PERFORMANCE_DUMP
    if kind == "waveseq":
        return m.Function.ALL_WAVESEQ_DUMP
    raise LookupError(f"no kind {kind!r}")


# --- the unit half -----------------------------------------------------------


def take_reference(
    device: bridge.WavestationBridge,
    entry: banks.Bank,
    function: int,
    gap: float,
    passes: int,
) -> Optional[bytes]:
    """The bank read ``passes`` times at ``gap``, or None if that is not reproducible.

    A reference that is not reproducible makes every later comparison
    meaningless, so this stops rather than sweeping on top of it. The
    candidate gap is installed only for the reference's own duration and
    restored on the way out, exception or not.
    """
    from kws import bridge as b

    original = b.SEND_GAP
    b.SEND_GAP = gap
    reference: Optional[bytes] = None
    try:
        for index in range(passes):
            print(
                f"reference pass {index + 1}/{passes} at {gap * 1000:.0f} ms...",
                file=sys.stderr,
            )
            started = time.perf_counter()
            got = device.read_raw(entry.sysex, function)
            seconds = time.perf_counter() - started
            if reference is not None and got != reference:
                print(f"  {_difference(reference, got)}", file=sys.stderr)
                print(
                    "  that is at the *reference* gap, so the reference is "
                    "not trustworthy. Is the unit connected, is System "
                    "Exclusive reception on, and is nothing else talking "
                    "to it?",
                    file=sys.stderr,
                )
                return None
            reference = got
            print(f"  {len(got)} bytes in {seconds:.2f} s", file=sys.stderr)
    finally:
        b.SEND_GAP = original
    return reference


def _read_at_gap(
    device: bridge.WavestationBridge,
    entry: banks.Bank,
    function: int,
    reference: bytes,
    gap: float,
    repeats: int,
) -> GapResult:
    """Read the bank ``repeats`` times with ``gap`` as the send gap.

    The gap is the one :data:`kws.bridge.SEND_GAP` sleeps after every
    outgoing message, installed here for the duration of one candidate.
    A slot that does not answer is counted rather than raised, so the table
    can show *which* gaps fail and how, the way the sibling tools do.
    """
    clean = timeouts = differs = 0
    total = 0.0
    notes: List[str] = []
    for _ in range(repeats):
        started = time.perf_counter()
        try:
            got = device.read_raw(entry.sysex, function)
        except TimeoutError:
            timeouts += 1
            notes.append("no dump came back within the timeout")
            total += time.perf_counter() - started
            continue
        total += time.perf_counter() - started
        if got == reference:
            clean += 1
        else:
            differs += 1
            notes.append(_difference(reference, got))
    return GapResult(
        gap=gap,
        clean=clean,
        repeats=repeats,
        timeouts=timeouts,
        differs=differs,
        mean_seconds=total / repeats,
        notes=notes,
    )


def _print_row(result: GapResult) -> None:
    """One line of the gap table, and up to ``SHOWN`` notes under it."""
    score = f"{result.clean}/{result.repeats}"
    print(
        f"{result.gap * 1000:7.3f}ms  {score:>6}  {result.timeouts:>7}  "
        f"{result.differs:>6}  {result.mean_seconds:7.2f}"
    )
    for note in result.notes[:SHOWN]:
        print(f"      {note}")
    if len(result.notes) > SHOWN:
        print(f"      ... and {len(result.notes) - SHOWN} more")


def measure_send(
    device: bridge.WavestationBridge,
    entry: banks.Bank,
    function: int,
    reference: bytes,
    sweep: Sweep,
) -> List[GapResult]:
    """Sweep the candidate SEND_GAPs. Returns one result per gap."""
    from kws import bridge as b

    original = b.SEND_GAP
    results: List[GapResult] = []
    print("")
    print(f"{'gap ms':>9}  {'clean':>6}  {'timeout':>7}  {'differ':>6}  {'mean s':>7}")
    try:
        for gap in sweep.gaps:
            b.SEND_GAP = gap
            result = _read_at_gap(
                device, entry, function, reference, gap, sweep.repeats
            )
            _print_row(result)
            results.append(result)
    finally:
        b.SEND_GAP = original
    return results


def smallest_clean(results: List[GapResult]) -> Optional[float]:
    """The smallest gap whose every repeat matched the reference."""
    clean = [result.gap for result in results if result.clean == result.repeats]
    return min(clean) if clean else None


def report(results: List[GapResult], sweep: Sweep, send_gap: Optional[float]) -> None:
    """Turn the table into the finding and its caveats."""
    print("")
    clean = [result.gap for result in results if result.clean == result.repeats]
    if clean:
        print(
            "gaps that read the bank cleanly every time: "
            + ", ".join(f"{gap:g}" for gap in clean)
            + " s"
        )
    else:
        print("no candidate gap read the bank cleanly.")
    if send_gap is None:
        print("Extend --gaps upwards before trusting any value.")
    elif send_gap == min(sweep.gaps):
        print(f"Even the shortest gap tried ({send_gap:g} s) was clean, so the")
        print("edge is not in this list. Extend --gaps downwards to look")
        print("further (0.000 is as low as it goes).")
    else:
        print(
            f"smallest gap that read {sweep.entry.label} cleanly "
            f"{sweep.repeats} time(s): {send_gap:g} s"
        )


def unit_answers(device: bridge.WavestationBridge) -> bool:
    """One Identity Request, so a wrong port fails in half a second."""
    if device.identify(timeout=0.5) is None:
        print(
            "nothing answered an Identity Request on that port. The unit "
            "may be off, on another port, or not carrying the reply back "
            "to this computer.",
            file=sys.stderr,
        )
        return False
    return True


def open_device(
    args: argparse.Namespace, sweep: Sweep
) -> Optional[bridge.WavestationBridge]:
    """The port ``--port`` names, or the remembered/swept one.

    The bridge is imported here, not at the top of the file: ``rtmidi`` is
    imported with it, and a tool whose ``--help`` needs a MIDI backend is a
    tool that cannot say what it does on a machine that has none.
    """
    try:
        from kws import bridge as b

        b.install_clean_exit()
        if args.port:
            return b.WavestationBridge.open(
                args.port, recv_port=args.recv_port, channel=sweep.channel
            )
        # The remembered-port path, exactly as kwscli takes it: migrate a
        # legacy working-directory config first, then make sure the
        # per-tool directory exists for the pair that gets written back.
        cfg.migrate_legacy_config("kwsed")
        os.makedirs(cfg.config_dir("kwsed"), exist_ok=True)
        return b.WavestationBridge.connect(
            config_path=cfg.DEFAULT_PATH,
            channel=sweep.channel,
            scan=args.scan,
            on_try=lambda name: print(f"  probing {name}...", file=sys.stderr),
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def measure(args: argparse.Namespace, sweep: Sweep, findings: Findings) -> int:
    """Open the unit, take the reference, sweep, and report."""
    device = open_device(args, sweep)
    if device is None:
        findings.send_why = "no port was opened"
        return spec.EXIT_ERROR
    try:
        if not unit_answers(device):
            findings.send_why = "nothing answered an Identity Request"
            return spec.EXIT_ERROR
        function = _function_for(sweep.kind)
        print(
            f"reading {sweep.entry.label} {sweep.kind} for the reference...",
            file=sys.stderr,
        )
        try:
            reference = take_reference(
                device,
                sweep.entry,
                function,
                sweep.reference_gap,
                REFERENCE_PASSES,
            )
        except TimeoutError as exc:
            print(f"error: {exc}", file=sys.stderr)
            findings.send_why = "the reference read did not answer"
            return spec.EXIT_ERROR
        if reference is None:
            findings.send_why = "the reference read was not reproducible"
            print("No measurement was taken, so keep the current guess.")
            return spec.EXIT_ERROR
        results = measure_send(device, sweep.entry, function, reference, sweep)
        findings.send_gap = smallest_clean(results)
        if findings.send_gap is None:
            findings.send_why = "no candidate gap read the bank cleanly"
        report(results, sweep, findings.send_gap)
        return spec.EXIT_OK
    except (LookupError, TimeoutError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        findings.send_why = str(exc)
        return spec.EXIT_ERROR
    finally:
        device.close()


# --- the transport half ------------------------------------------------------


def _open_loopback() -> Tuple[Any, Tuple[int, int]]:
    """A virtual input port of this process, and where it sits.

    The address comes from a *second* client's port list: rtmidi hides a
    client's own ports from that client, so the port this process just
    created is only visible to a different one.
    """
    import rtmidi

    receiver = rtmidi.MidiIn()
    probe = rtmidi.MidiOut()
    try:
        receiver.ignore_types(sysex=False, timing=True, active_sense=True)
        receiver.open_virtual_port(LOOPBACK_NAME)
        names = [name for name in probe.get_ports() if LOOPBACK_NAME in name]
        address = alsa.port_address(names[0]) if names else None
        if address is None:
            raise RuntimeError(
                "the loopback port this tool created did not appear in the "
                "port list; without it the transport half cannot measure"
            )
    except Exception:
        receiver.close_port()
        receiver.delete()
        probe.delete()
        raise
    probe.delete()
    return receiver, address


def _close_loopback(receiver: Any) -> None:
    receiver.close_port()
    receiver.delete()


def _receive(receiver: Any) -> bytes:
    """The message the loopback assembled, or ``b""`` by the deadline.

    The deadline starts when the *send* finished: everything was already
    handed to the sequencer by then, so what has not arrived within the
    grace period is not coming.
    """
    deadline = time.perf_counter() + RECEIVE_GRACE
    while True:
        message = receiver.get_message()
        if message is not None:
            return bytes(message[0])
        if time.perf_counter() >= deadline:
            return b""
        time.sleep(0.002)


def _loopback_trial(
    receiver: Any, address: Tuple[int, int], size: int, pace: int
) -> Trial:
    """Send ``size`` bytes at ``pace`` through the real Sender, and say what came back.

    The same construction, pacing and teardown :meth:`kws.bridge._send`
    takes for a message over its rtmidi limit, pointed at this process's
    own port instead of the unit. The receiver is drained first: a message
    from a trial that timed out would otherwise be read as this one's
    answer, which is the mistake the reference comparison exists to catch.
    """
    while receiver.get_message() is not None:
        pass
    payload = _payload(size)
    sender = alsa.Sender(*address, name=b"kwsed-measure")
    try:
        sender.BYTES_PER_SECOND = pace
        started = time.perf_counter()
        sender.send(payload)
        send_seconds = time.perf_counter() - started
        got = _receive(receiver)
    finally:
        sender.close()
    return Trial(
        size=size,
        intact=got == payload,
        send_seconds=send_seconds,
        received=len(got),
    )


def _print_trial(pace: int, gap_ms: float, trial: Trial) -> None:
    """One line of the transport table."""
    print(
        f"{pace:>9}  {gap_ms:7.1f}ms  {trial.size:>7}  {trial.status:>8}  "
        f"{trial.send_seconds:7.2f}"
    )


def _sweep_pace(
    receiver: Any, address: Tuple[int, int], pace: int, sizes: Tuple[int, ...]
) -> Tuple[List[Trial], List[str]]:
    """Every size at one pace: the trials, and the ones that did not survive."""
    trials: List[Trial] = []
    failures: List[str] = []
    gap_ms = alsa.Sender.CHUNK * 1000.0 / pace
    for size in sizes:
        trial = _loopback_trial(receiver, address, size, pace)
        trials.append(trial)
        _print_trial(pace, gap_ms, trial)
        if not trial.intact:
            failures.append(f"{size} bytes at {pace} B/s: {trial.status}")
    return trials, failures


def measure_transport(sweep: Sweep, findings: Findings) -> int:
    """Sweep pace against size through the real Sender, into this process's port."""
    if not alsa.available():
        print(
            "no ALSA sequencer on this host; the transport half cannot run.",
            file=sys.stderr,
        )
        findings.pace_why = "no ALSA sequencer on this host"
        return spec.EXIT_ERROR
    print("")
    print("transport: kws.alsa.Sender into a loopback port this tool created")
    print("(no unit, no wire: what this half cannot show is an interface")
    print("dropping what it cannot hold -- see TODO.md item 5b)")
    print("")
    print(
        f"{'pace B/s':>9}  {'chunk gap':>9}  {'size':>7}  {'result':>8}  {'send s':>7}"
    )
    try:
        receiver, address = _open_loopback()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        findings.pace_why = f"the loopback could not be opened ({exc})"
        return spec.EXIT_ERROR
    failures: List[str] = []
    try:
        for pace in sweep.paces:
            trials, lost = _sweep_pace(receiver, address, pace, sweep.sizes)
            failures.extend(lost)
            intact = [trial.size for trial in trials if trial.intact]
            largest = max(intact) if intact else 0
            print(
                f"  at {pace} B/s: largest intact message {largest} bytes "
                f"(of {max(sweep.sizes)} tried)"
            )
    finally:
        _close_loopback(receiver)
    if failures:
        for failure in failures[:SHOWN]:
            print(f"  lost: {failure}")
        if len(failures) > SHOWN:
            print(f"  ... and {len(failures) - SHOWN} more")
        findings.pace_why = f"{len(failures)} message(s) did not survive the loopback"
    else:
        findings.pace_why = (
            "every size survived every pace here; only a real interface "
            "can drop what it cannot hold"
        )
    return spec.EXIT_OK


# --- the block both halves feed ----------------------------------------------


def _current_send_gap() -> Optional[float]:
    """``bridge.SEND_GAP`` as it stands, or None if the bridge will not import.

    The guard exists for one host: where libasound is missing, ``kws.bridge``
    cannot import at all -- and that is also the host where neither half
    ran, so the block still has to print without a traceback.
    """
    try:
        from kws import bridge as b
    except Exception:
        return None
    return b.SEND_GAP


def print_constants(findings: Findings) -> None:
    """The final block, ready to paste into ``kws/bridge.py`` and ``kws/alsa.py``."""
    print("")
    print("recommended constants, from what this run measured")
    print("-" * 60)
    current = _current_send_gap()
    if findings.send_gap is not None:
        print(f"SEND_GAP = {findings.send_gap:g}")
    elif current is None:
        print(f"SEND_GAP unchanged ({findings.send_why}; kws.bridge did not import)")
    else:
        print(f"SEND_GAP = {current:.3f}   # unchanged: {findings.send_why}")
    print(
        f"BYTES_PER_SECOND = {alsa.Sender.BYTES_PER_SECOND}   "
        f"# unchanged: {findings.pace_why}"
    )
    print("")
    print("A measured value supersedes the guess. This tool edits neither file:")
    print("make the change deliberately, and record the run in")
    print("docs/RESOLUTION_NOTES.md.")


# --- the command line --------------------------------------------------------


def describe_plan(args: argparse.Namespace, sweep: Sweep) -> None:
    """Print what will happen, before anything is opened."""
    print("MEASURE SEND_GAP / THE ALSA SEND PACE -- A BENCH TOOL")
    print("=" * 60)
    print("This tool drives the unit and must be run alone. Quit every other")
    print("MIDI program, and do not touch the front panel while it measures.")
    print("")
    print("It changes no memory and makes no sound: every message it sends to")
    print("the unit is a dump request -- a read. There is no SELECT half here,")
    print("unlike the nanosyned original, because kwsed's select() is the one")
    print("message that changes what the unit plays.")
    print("")
    if args.port:
        target = f"output {args.port!r}, input {args.recv_port or args.port!r}"
    elif args.scan:
        target = "sweep every MIDI port (--scan)"
    else:
        target = (
            f"the port remembered in {cfg.DEFAULT_PATH} "
            "(sweeping only if it does not answer)"
        )
    print(f"  port        : {target}")
    print(f"  channel     : {sweep.channel + 1} (MIDI channel)")
    print(f"  bank        : {sweep.entry.label}, {sweep.kind} kind")
    print(
        f"  reference   : {sweep.reference_gap * 1000:.0f} ms "
        f"x {REFERENCE_PASSES} passes"
    )
    print(f"  gaps        : {', '.join(f'{gap:g}' for gap in sweep.gaps)} s")
    print(f"  repeats     : {sweep.repeats} bank read(s) per gap")
    if args.no_transport:
        print("  transport   : skipped (--no-transport)")
    else:
        sizes = ", ".join(str(size) for size in sweep.sizes)
        paces = ", ".join(str(pace) for pace in sweep.paces)
        print(f"  transport   : {sizes} bytes at {paces} B/s (loopback)")
    if args.no_unit:
        print("  unit        : skipped (--no-unit)")
    print("")


def build_parser() -> argparse.ArgumentParser:
    """The family's options, then this tool's own."""
    parser = cli.make_parser(
        prog="measure_send_gap",
        description=(
            "Measure kwsed's SEND_GAP and its ALSA send pace. Bench tool; "
            "the unit half opens a real MIDI port."
        ),
        epilog=(
            "examples:\n"
            "  python3 tools/measure_send_gap.py        # dry listing\n"
            "  python3 tools/measure_send_gap.py --yes --scan\n"
            "  python3 tools/measure_send_gap.py --yes --no-unit\n"
            "  python3 tools/measure_send_gap.py --yes "
            "--gaps 0.002,0.001 --no-transport"
        ),
        distribution="kwsed",
    )
    # config=False, as in kwscli: the settings cache is read and written at
    # the browser's default per-tool path, and a --config flag this tool
    # would only ever pass through is one that teaches a flag can lie.
    cli.add_common_arguments(
        parser,
        port=True,
        scan=True,
        recv_port=True,
        channel=True,
        config=False,
        demo=False,
        yes=True,
    )
    cli.append_flag_help(
        parser, "yes", "without it nothing is opened, not even the loopback."
    )
    parser.add_argument(
        "--gaps",
        metavar="SECONDS,...",
        default=None,
        help="comma-separated candidate gaps, longest first "
        f"(default: {','.join(f'{gap:g}' for gap in DEFAULT_GAPS)})",
    )
    parser.add_argument(
        "--reference-gap",
        metavar="SECONDS",
        type=float,
        default=DEFAULT_REFERENCE_GAP,
        help="gap for the reference read; must exceed every --gaps value "
        f"(default: {DEFAULT_REFERENCE_GAP:g})",
    )
    parser.add_argument(
        "--repeats",
        metavar="N",
        type=int,
        default=1,
        help="bank reads per candidate gap; a marginal gap fails "
        "intermittently, so 3 is better evidence (default: 1)",
    )
    parser.add_argument(
        "--bank",
        default="CARD",
        help="which bank to read (default: CARD, the spare the hardware suite sweeps)",
    )
    parser.add_argument(
        "--kind",
        default=banks.Kind.PATCH,
        choices=[banks.Kind.PERFORMANCE, banks.Kind.PATCH, "waveseq"],
        help="which kind of bank dump to ask for (default: patch)",
    )
    parser.add_argument(
        "--sizes",
        metavar="BYTES,...",
        default=None,
        help="comma-separated message sizes for the loopback half "
        f"(default: {','.join(str(size) for size in DEFAULT_SIZES)})",
    )
    parser.add_argument(
        "--paces",
        metavar="BPS,...",
        default=None,
        help="comma-separated byte rates for the loopback half "
        f"(default: {','.join(str(pace) for pace in DEFAULT_PACES)})",
    )
    parser.add_argument(
        "--no-transport",
        action="store_true",
        help="skip the loopback half and measure only SEND_GAP",
    )
    parser.add_argument(
        "--no-unit",
        action="store_true",
        help="skip the unit half; measure only the local ALSA loopback",
    )
    return parser


_T = TypeVar("_T")


def _list_of(
    parser: argparse.ArgumentParser,
    text: str,
    kind: Callable[[str], _T],
    flag: str,
) -> List[_T]:
    """One comma-separated option, refused by name when it is not that list."""
    try:
        found = [kind(part) for part in text.split(",") if part.strip()]
    except ValueError:
        parser.error(f"{flag}: {text!r} is not a comma-separated list of numbers")
    if not found:
        parser.error(f"{flag} needs at least one number")
    return found


def gaps_of(parser: argparse.ArgumentParser, text: Optional[str]) -> List[float]:
    """``--gaps``, or the defaults, validated."""
    if text is None:
        return list(DEFAULT_GAPS)
    gaps = _list_of(parser, text, float, "--gaps")
    if any(gap < 0 for gap in gaps):
        parser.error("every --gaps value must be zero or more")
    return gaps


def sizes_of(parser: argparse.ArgumentParser, text: Optional[str]) -> List[int]:
    """``--sizes``, or the defaults, validated."""
    if text is None:
        return list(DEFAULT_SIZES)
    sizes = _list_of(parser, text, int, "--sizes")
    if any(size < 2 for size in sizes):
        parser.error("every --sizes value must be at least 2 (F0 ... F7)")
    return sizes


def paces_of(parser: argparse.ArgumentParser, text: Optional[str]) -> List[int]:
    """``--paces``, or the defaults, validated."""
    if text is None:
        return list(DEFAULT_PACES)
    paces = _list_of(parser, text, int, "--paces")
    if any(pace < 1 for pace in paces):
        parser.error("every --paces value must be at least 1")
    return paces


def settings(parser: argparse.ArgumentParser, args: argparse.Namespace) -> Sweep:
    """Validate the options, before anything is opened."""
    gaps = gaps_of(parser, args.gaps)
    if args.reference_gap <= max(gaps):
        parser.error("--reference-gap must be longer than every --gaps value")
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    if args.no_unit and args.no_transport:
        parser.error("nothing to measure: --no-unit and --no-transport together")
    try:
        entry = banks.bank(args.bank)
    except LookupError as exc:
        parser.error(f"--bank: {exc}")
    cli.validate_common(args)
    return Sweep(
        entry=entry,
        kind=args.kind,
        gaps=tuple(gaps),
        sizes=tuple(sizes_of(parser, args.sizes)),
        paces=tuple(paces_of(parser, args.paces)),
        repeats=args.repeats,
        reference_gap=args.reference_gap,
        channel=cli.channel_of(args, default=cfg.load_channel()) or 0,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    sweep = settings(parser, args)

    describe_plan(args, sweep)
    if not args.yes:
        print("dry run: nothing was opened and nothing was sent.")
        print("Re-run with --yes to drive the unit and the loopback.")
        return spec.EXIT_OK

    findings = Findings()
    codes: List[int] = []
    if args.no_unit:
        findings.send_why = "skipped with --no-unit"
    else:
        codes.append(measure(args, sweep, findings))
    if args.no_transport:
        findings.pace_why = "skipped with --no-transport"
    else:
        codes.append(measure_transport(sweep, findings))
    print_constants(findings)
    return spec.EXIT_ERROR if spec.EXIT_ERROR in codes else spec.EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
