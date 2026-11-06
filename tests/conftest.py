# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# kwsed is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation either version 2 of the License, or (at your option) any later
# version.

"""The opt-in hardware fixture, and the flags that turn it on.

Nothing here runs unless you ask for it. Only one session may drive a unit
at a time, so the suite that talks to a real Korg Wavestation SR is opt-in
twice over: the ``--hardware`` flag (or ``KWSED_HARDWARE=1``), and the
machine actually answering. ``make check`` and CI never pass the flag, so
``tests/test_hardware.py`` skips there by design rather than by accident.

A skip is a skip, not a pass. ``-rs`` names the reason, so "the unit was
off" and "the offsets are wrong" never look like the same green.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator

import pytest

from kws import bridge

HARDWARE_HELP = (
    "run the tests that need a real Korg Wavestation SR on a MIDI port. "
    "They open the MIDI ports, so only one session may use them at a time"
)


SCAN_HELP = (
    "sweep every MIDI port for the unit instead of using the remembered "
    "port, the way --scan does in the app."
)


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add the hardware opt-in, and the port sweep, to pytest."""
    group = parser.getgroup("hardware", "tests against a real Korg Wavestation SR")
    group.addoption(
        "--hardware",
        action="store_true",
        default=False,
        help=HARDWARE_HELP,
    )
    group.addoption(
        "--scan",
        action="store_true",
        default=False,
        help=SCAN_HELP,
    )


def _asked_for_hardware(config: pytest.Config) -> bool:
    """The flag, or the environment variable that says the same thing."""
    return bool(config.getoption("--hardware")) or bool(
        os.environ.get("KWSED_HARDWARE")
    )


@pytest.fixture(scope="session")
def device(request: pytest.FixtureRequest) -> Iterator[bridge.WavestationBridge]:
    """A live bridge to the attached unit, or a skip saying why not.

    Session-scoped on purpose: one connection, one port pair, held for the
    whole run rather than opened and closed per test. A failure to connect
    is a skip, never a failure; the reason lists every cause rather than
    picking one, because a request the unit cannot serve is answered with
    silence.
    """
    if not _asked_for_hardware(request.config):
        pytest.skip(
            "hardware tests need --hardware (or KWSED_HARDWARE=1); the "
            "suite that talks to a real Korg Wavestation SR is opt-in because "
            "only one session may drive the unit"
        )

    try:
        connection = bridge.WavestationBridge.connect(
            scan=bool(request.config.getoption("--scan"))
        )
    except (bridge.MidiUnavailable, bridge.DeviceError) as exc:
        pytest.skip(
            f"no Wavestation SR answered ({exc}). The unit may be powered off, "
            "on another port (USB renumbers them), busy with another "
            "session, or have System Exclusive reception off"
        )
    try:
        yield connection
    finally:
        with contextlib.suppress(Exception):
            connection.close()
