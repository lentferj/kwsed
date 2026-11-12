# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# kwsed is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 2 of the License, or (at your option)
# any later version.
#
# kwsed is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for
# more details.

"""How a launch finds the unit: the remembered port, and the sweep.

Ported in the same shape as the sibling x5ded's and rxved's
tests/test_connect.py. These exist because `WavestationBridge.autodetect`
swept every bidirectional MIDI port on the machine on every single launch,
against a settings file that had the answer-shaped gap where a remembered
port should have been -- kwsed's config shim exported `save_last_port` and
nothing ever called it.

What is tested here is the *wiring*, not the policy: the policy itself is
`vinsynlib.midi.open_remembered_or_swept`, and its own tests live with it.
These prove that kwsed hands it the right probe, the right remembered pair,
and the right answer to --scan.

No hardware and no MIDI ports here: `open` and `autodetect` are stubbed
with what they would have found.
"""

from __future__ import annotations

import pytest

from kws import bridge as b
from kws import config as cfg

WAVESTATION_PORT = "Midi Out: Korg Wavestation SR 1 20:0"
OTHER_PORT = "Midi Out: Scarlett 18i8 USB MIDI 1 24:0"
SPLIT_IN = "Midi In: Something Else 1 30:0"

#: The identity reply a Wavestation sends, which is what `identify` reads
#: back. Invented here in the same way as the rest of the suite's fixtures.
REPLY = {
    "channel": 4,
    "manufacturer": 0x42,
    "model": 0x30,
    "is_korg": True,
    "is_wavestation": True,
}
FOREIGN_REPLY = {"channel": 4, "manufacturer": 0x41, "is_korg": False}


class _FakeBridge:
    """Stands in for an opened bridge. Closes are counted, not needed."""

    def __init__(self, port, recv, *, answers):
        self.port = port
        self.recv_port = recv
        #: What identify() returns: REPLY for a Wavestation, None for a port
        #: that stays silent, FOREIGN_REPLY for someone else's box. None and
        #: a foreign reply are the same case to this code -- not the unit.
        self.answers = answers
        self.channel = 0
        self.timeout = b.DEFAULT_TIMEOUT
        self.closed = False

    def identify(self, *, timeout=None):
        if self.answers is None:
            return None
        return self.answers

    def close(self):
        self.closed = True


class _Rig:
    """Records what was opened, what answered, and whether a sweep happened.

    `answering` maps a port name to what `identify` returns there.
    `ports` is what the host actually has, which is not the same thing -- a
    remembered name can still be a port with nothing behind it, and that is
    the case --scan exists for.
    """

    def __init__(self, answering, *, ports=None):
        self.answering = answering
        self.ports = ports if ports is not None else list(answering)
        self.opened = []
        self.tried = []
        self.swept = 0
        self.made = []

    def install(self, monkeypatch):
        rig = self

        def fake_open(
            cls,
            /,
            port,
            *,
            recv_port=None,
            channel=0,
            **rest,
        ):
            # `rest` is `timeout=...`, `single_object_timeout=...`, a discovery timeout,
            # deliberately dropped: the test that cares asserts on bridge.timeout after
            # the call rather than on what went in. Asserting the shape of it
            # is how a stub stops quietly accepting a call that changed.
            assert set(rest) <= {"timeout", "single_object_timeout"}, (
                f"unexpected kwargs: {rest}"
            )
            rig.opened.append(port)
            made = _FakeBridge(port, recv_port or port, answers=rig.answering.get(port))
            made.channel = channel
            rig.made.append(made)
            return made

        def fake_autodetect(
            cls, *, timeout=0.5, single_object_timeout=None, on_try=None
        ):
            # Mirrors the real one's shape -- hinted ports first -- so the
            # ordering is exercised rather than stubbed away.
            rig.swept += 1
            for name in rig.ports:
                if "Midi Through" in name:
                    continue
                if on_try is not None:
                    on_try(name)
                rig.tried.append(name)
                reply = rig.answering.get(name)
                if reply:
                    made = fake_open(cls, name)
                    made.channel = reply["channel"]
                    return made
            raise b.DeviceError("nothing answered")

        monkeypatch.setattr(b, "list_ports", lambda: (list(rig.ports), list(rig.ports)))
        monkeypatch.setattr(b, "likely_korg_ports", lambda: [])
        monkeypatch.setattr(b.WavestationBridge, "open", classmethod(fake_open))
        monkeypatch.setattr(
            b.WavestationBridge, "autodetect", classmethod(fake_autodetect)
        )
        return rig

    def unclosed(self):
        """Bridges that were opened and never closed."""
        return [x.port for x in self.made if not x.closed]


def _remembered(tmp_path, send=WAVESTATION_PORT, recv=""):
    """A settings file holding one remembered port pair."""
    path = str(tmp_path / "config.toml")
    cfg.save_ports(send, recv or send, path)
    return path


# --- the normal path: trust the config ---------------------------------------


class TestTheRememberedPort:
    def test_it_is_used_without_sweeping(self, monkeypatch, tmp_path):
        """One Identity Request to the saved port, and nothing else.

        The whole point. A sweep pings every MIDI device on the machine once
        per launch and takes seconds on a box with thirty ports.
        """
        path = _remembered(tmp_path)
        rig = _Rig({WAVESTATION_PORT: REPLY, OTHER_PORT: None}).install(monkeypatch)

        b.WavestationBridge.connect(config_path=path)

        assert rig.swept == 0, "swept every port when the answer was on file"
        assert rig.opened == [WAVESTATION_PORT]

    def test_the_remembered_pair_is_both_halves(self, monkeypatch, tmp_path):
        """The input port is remembered too, so a unit on a separate
        interface does not fall back to probing the output name."""
        path = str(tmp_path / "config.toml")
        cfg.save_ports(OTHER_PORT, SPLIT_IN, path)
        _Rig({OTHER_PORT: REPLY}).install(monkeypatch)

        got = b.WavestationBridge.connect(config_path=path)

        assert got.recv_port == SPLIT_IN

    def test_it_adopts_the_channel_the_unit_reports(self, monkeypatch, tmp_path):
        """Not the one asked for, and not the one in the file: it is the
        fifth byte of every request and the saved one can be stale."""
        path = _remembered(tmp_path)
        cfg.save_channel(0, path)
        _Rig({WAVESTATION_PORT: REPLY}).install(monkeypatch)

        got = b.WavestationBridge.connect(config_path=path)

        assert got.channel == 4

    def test_the_session_timeout_is_not_the_probe_timeout(self, monkeypatch, tmp_path):
        """A short discovery timeout must not become the working one.

        Otherwise every later request that does not name its own runs at the
        probe's value, and a slow but working connection looks like a timeout
        on every call.
        """
        path = _remembered(tmp_path)
        _Rig({WAVESTATION_PORT: REPLY}).install(monkeypatch)

        got = b.WavestationBridge.connect(config_path=path)

        assert got.timeout == b.DEFAULT_TIMEOUT
        assert got.timeout != 0.5

    def test_it_does_not_rewrite_the_config(self, monkeypatch, tmp_path):
        """The file is this path's *input*.

        Rewriting it unchanged would make a plain launch mutate the thing it
        was asked to trust, and any later change to what is stored would be
        invisible in a diff.
        """
        path = _remembered(tmp_path)
        _Rig({WAVESTATION_PORT: REPLY}).install(monkeypatch)

        b.WavestationBridge.connect(config_path=path)

        assert cfg.load_last_port(path) == WAVESTATION_PORT

    def test_someone_elses_box_is_not_the_unit(self, monkeypatch, tmp_path):
        """An Identity Reply from another manufacturer is a reply, and not an
        answer. Using it would send a program change to hardware the user
        never chose."""
        path = _remembered(tmp_path)
        rig = _Rig({WAVESTATION_PORT: FOREIGN_REPLY, OTHER_PORT: None}).install(
            monkeypatch
        )

        got = b.WavestationBridge.connect(config_path=path)

        assert rig.swept == 1, "a foreign reply was taken for the unit"
        assert got.port == OTHER_PORT or rig.made[0].closed

    def test_a_dead_remembered_port_leaves_nothing_open(self, monkeypatch, tmp_path):
        path = _remembered(tmp_path)
        rig = _Rig({WAVESTATION_PORT: None, OTHER_PORT: REPLY}).install(monkeypatch)

        b.WavestationBridge.connect(config_path=path)

        # The remembered one was closed on the way to falling back; the one
        # the sweep found is the caller's to close.
        assert rig.made[0].port == WAVESTATION_PORT
        assert rig.made[0].closed is True


# --- the fallback: sweep ----------------------------------------------------


class TestTheSweep:
    def test_it_runs_when_nothing_is_remembered(self, monkeypatch, tmp_path):
        path = str(tmp_path / "config.toml")  # never written: a first run
        rig = _Rig({OTHER_PORT: REPLY}).install(monkeypatch)

        b.WavestationBridge.connect(config_path=path)

        assert rig.swept == 1
        assert OTHER_PORT in rig.tried

    def test_it_runs_when_asked_for(self, monkeypatch, tmp_path):
        """--scan: what is needed after the unit moves to a renumbered port.

        The saved name is still a port on this host -- it is what the unit
        was on -- and nothing answers there now, because the unit is
        somewhere else. `ports` says it exists; `answering` says what is
        behind it.
        """
        path = _remembered(tmp_path)
        rig = _Rig({OTHER_PORT: REPLY}, ports=[WAVESTATION_PORT, OTHER_PORT]).install(
            monkeypatch
        )

        got = b.WavestationBridge.connect(config_path=path, scan=True)

        assert rig.swept == 1
        assert got.port == OTHER_PORT, "opened the saved port anyway"

    def test_scan_does_not_probe_the_remembered_port_first(self, monkeypatch, tmp_path):
        """--scan asks for the sweep, not for a check of the file and then
        the sweep."""
        path = _remembered(tmp_path)
        rig = _Rig({OTHER_PORT: REPLY}, ports=[WAVESTATION_PORT, OTHER_PORT]).install(
            monkeypatch
        )

        b.WavestationBridge.connect(config_path=path, scan=True)

        assert rig.opened == [OTHER_PORT], "the saved port was probed first"

    def test_a_dead_remembered_port_falls_back_and_says_so(self, monkeypatch, tmp_path):
        """Not silently. A fallback that says nothing is indistinguishable
        from the fast path having worked, which is how a slow launch quietly
        becomes the normal one."""
        path = _remembered(tmp_path)
        rig = _Rig({WAVESTATION_PORT: None, OTHER_PORT: REPLY}).install(monkeypatch)
        said: list[str] = []

        got = b.WavestationBridge.connect(config_path=path, on_try=said.append)

        assert rig.swept == 1
        assert got.port == OTHER_PORT
        assert said[0] == f"{WAVESTATION_PORT} (remembered, did not answer)"

    def test_the_fast_path_says_nothing(self, monkeypatch, tmp_path):
        """One Identity Request, and silence about it."""
        path = _remembered(tmp_path)
        _Rig({WAVESTATION_PORT: REPLY}).install(monkeypatch)
        said: list[str] = []

        b.WavestationBridge.connect(config_path=path, on_try=said.append)

        assert said == []

    def test_it_remembers_what_it_found(self, monkeypatch, tmp_path):
        """The end the whole thing is for: one sweep, then never again.

        kwsed's own App never wrote the port at all -- the shim exported
        `save_last_port` and nothing called it -- so this is the first line
        in kwsed that has ever put a port in the settings file.
        """
        path = str(tmp_path / "config.toml")
        _Rig({WAVESTATION_PORT: REPLY}, ports=[OTHER_PORT, WAVESTATION_PORT]).install(
            monkeypatch
        )

        b.WavestationBridge.connect(config_path=path)

        assert cfg.load_last_port(path) == WAVESTATION_PORT

    def test_the_next_launch_does_not_sweep(self, monkeypatch, tmp_path):
        path = str(tmp_path / "config.toml")
        first = _Rig({WAVESTATION_PORT: REPLY}, ports=[OTHER_PORT, WAVESTATION_PORT])
        first.install(monkeypatch)
        b.WavestationBridge.connect(config_path=path)
        assert first.swept == 1

        second = _Rig({WAVESTATION_PORT: REPLY}, ports=[OTHER_PORT, WAVESTATION_PORT])
        second.install(monkeypatch)
        b.WavestationBridge.connect(config_path=path)

        assert second.swept == 0
        assert second.opened == [WAVESTATION_PORT]

    def test_nothing_answered_is_the_sweeps_own_error(self, monkeypatch, tmp_path):
        """The user needs the sweep's sentence, not the stale port's."""
        path = _remembered(tmp_path)
        _Rig({WAVESTATION_PORT: None, OTHER_PORT: None}).install(monkeypatch)

        with pytest.raises(b.DeviceError, match="nothing answered"):
            b.WavestationBridge.connect(config_path=path)

    def test_on_try_is_optional(self, monkeypatch, tmp_path):
        """`--demo` and library callers pass none, and that must not crash."""
        path = str(tmp_path / "config.toml")
        _Rig({OTHER_PORT: REPLY}).install(monkeypatch)

        b.WavestationBridge.connect(config_path=path)

    def test_the_sweep_still_names_each_port_it_tries(self, monkeypatch, tmp_path):
        """The family's existing reporting, unchanged: one line per port
        tried, built from the on_try the caller supplies as
        "  probing <port>..."."""
        path = str(tmp_path / "config.toml")
        _Rig({OTHER_PORT: REPLY}, ports=[WAVESTATION_PORT, OTHER_PORT]).install(
            monkeypatch
        )
        said: list[str] = []

        b.WavestationBridge.connect(config_path=path, on_try=said.append)

        assert said, "the sweep said nothing about what it was trying"
        assert said[0] == WAVESTATION_PORT, "said so out of order"
        assert said[-1] == OTHER_PORT


# --- the remembered-pair reader ---------------------------------------------


class TestTheRememberedPair:
    def test_nothing_remembered_is_nothing(self, tmp_path):
        path = str(tmp_path / "config.toml")
        assert cfg.load_port_pair(path) is None

    def test_one_port_is_a_whole_pair(self, tmp_path):
        """A Wavestation's input is the port it outputs to unless the file
        says otherwise, so `recv_port` absent is not half a pair."""
        path = str(tmp_path / "config.toml")
        cfg.save_last_port(WAVESTATION_PORT, path)
        assert cfg.load_port_pair(path) == (WAVESTATION_PORT, WAVESTATION_PORT)

    def test_a_separate_input_port_is_kept(self, tmp_path):
        path = str(tmp_path / "config.toml")
        cfg.save_ports(WAVESTATION_PORT, SPLIT_IN, path)
        assert cfg.load_port_pair(path) == (WAVESTATION_PORT, SPLIT_IN)
