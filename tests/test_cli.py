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

"""kwscli, driven end to end against the demo device. Fixtures invented.

Every name here is invented per CLAUDE.md. The demo device answers the
reads this tool makes; the write paths are exercised through their guards,
which is where a mistake does damage, and through one complete round trip
against a stand-in that stores what it is sent.
"""

import argparse
import json
import os

import pytest

from kws import banks
from kws import messages as m
from kwsed import cli
from kwsed.demo import DemoDevice

# A catalog path that cannot exist, so the offline commands read no names
# and their output is the same on every machine. kws/data/catalog.json is
# gitignored and present or absent depending on who generated it.
NO_CATALOG = os.path.join(os.sep, "nonexistent", "kwsed-test-catalog.json")


def run(argv):
    """main() with the demo device, returning its exit code."""
    return cli.main(["--demo", "--catalog", NO_CATALOG] + argv)


class TestTable:
    """The plain-text table every offline command prints through."""

    def test_it_pads_columns_to_the_widest_cell(self):
        out = cli._table([["a", "longer"], ["bbb", "c"]], ["x", "y"])
        lines = out.splitlines()
        assert lines[0] == "  x |      y"
        assert lines[1] == "--- | ------"
        assert lines[2] == "  a | longer"
        assert lines[3] == "bbb |      c"

    def test_trailing_space_is_trimmed(self):
        out = cli._table([["a"]], ["header"])
        assert out.splitlines()[2] == "     a"

    def test_no_rows_prints_nothing(self):
        assert cli._table([], ["x", "y"]) == ""


class TestPlural:
    def test_the_awkward_one(self):
        assert cli._plural("patch") == "patches"
        assert cli._plural("performance") == "performances"
        assert cli._plural("waveseq") == "wave sequences"


class TestOfflineCommands:
    """These need no unit at all, and `main` never builds one for them."""

    def test_banks_lists_twelve_with_four_writable(self, capsys):
        assert run(["banks"]) == 0
        out = capsys.readouterr().out
        assert "RAM 1" in out and "Card" in out
        assert out.count("writable") == 4
        assert "ROM" in out

    def test_list_one_banks_slots(self, capsys):
        assert run(["list", "CARD", "patch"]) == 0
        out = capsys.readouterr().out
        assert "Card" in out and "patches" in out
        # 35 patches, numbered 00..34
        assert "34" in out and "35" not in out

    def test_list_rejects_an_unknown_bank(self):
        with pytest.raises(SystemExit):
            run(["list", "NOPE", "patch"])

    def test_find_with_no_catalog_reports_nothing(self, capsys):
        assert run(["find", "glass"]) == 0
        assert "nothing matching" in capsys.readouterr().err

    def test_find_searches_a_catalog_that_exists(self, tmp_path, capsys):
        path = tmp_path / "cat.json"
        path.write_text(
            json.dumps(
                {
                    "banks": {
                        "CARD": [{"kind": "patch", "n": 3, "name": "Invented Chime"}]
                    }
                }
            ),
            encoding="utf-8",
        )
        assert cli.main(["--catalog", str(path), "find", "chime"]) == 0
        assert "Invented Chime" in capsys.readouterr().out


class TestTheDemoDeviceCommands:
    def test_status_reports_the_unit(self, capsys):
        assert run(["status"]) == 0
        out = capsys.readouterr().out
        assert "(Korg)" in out
        assert "(Wavestation)" in out
        assert "global channel" in out

    def test_dump_names_the_bank_the_reply_came_from(self, capsys):
        assert run(["dump", "CARD", "patch"]) == 0
        out = capsys.readouterr().out
        assert "Card" in out and "patches" in out

    def test_dump_defaults_to_patches(self, capsys):
        assert run(["dump", "RAM1"]) == 0
        assert "patches" in capsys.readouterr().out

    def test_select_sends_and_says_it_is_a_best_effort(self, capsys):
        device = DemoDevice()
        args = cli.build_parser().parse_args(
            ["dump", "CARD"]
        )  # borrow the parser for a valid Namespace shape
        args = cli.build_parser().parse_args(["select", "CARD", "patch", "3"])
        cli._cmd_select(device, args)
        out = capsys.readouterr().out
        assert "best effort" in out
        assert device.selected_log[0].number == 3
        assert device.selected_log[0].bank.label == "Card"


class TestMulti:
    """`read_multi` is outside the demo device's subset, so a stand-in."""

    def _device(self):
        from kws import multi as multi_mod

        data = bytearray(
            multi_mod.NAME_BASE + multi_mod.MULTISETS * multi_mod.NAME_SIZE
        )
        for number in range(multi_mod.MULTISETS):
            base = number * multi_mod.MULTISET_SIZE
            for channel in range(multi_mod.CHANNELS):
                at = base + multi_mod.PART_BASE + channel * multi_mod.PART_SIZE
                data[at] = 0  # bank: RAM 1
                data[at + 1] = channel  # performance 0-15
                data[at + 2] = 127  # full level
            name = f"Invented{number}".encode()[: multi_mod.NAME_SIZE]
            at = multi_mod.NAME_BASE + number * multi_mod.NAME_SIZE
            data[at : at + len(name)] = name
        frame = m.frame(
            m.Function.MULTISET_DUMP, [0, 1] + list(m.to_nibbles(bytes(data)))
        )

        class Device:
            description = "stand-in"

            def read_multi(self):
                return frame

            def close(self):
                pass

        return Device()

    def test_it_prints_the_used_channels(self, capsys):
        args = cli.build_parser().parse_args(["multi"])
        assert cli._cmd_multi(self._device(), args) == 0
        out = capsys.readouterr().out
        assert "32 multisets" in out
        assert "ch  1" in out
        # bank byte 0 is RAM 1, so the label must not read as a raw number
        assert "RAM 1" in out

    def test_all_includes_empty_multisets(self, capsys):
        args = cli.build_parser().parse_args(["multi", "--all"])
        cli._cmd_multi(self._device(), args)
        out = capsys.readouterr().out
        assert "(unnamed)" in out or "00  " in out


class TestSendGuards:
    """`send` destroys a bank, so every refusal before the wire is a test.

    None of these reach a device: each is refused on the file or on the
    destination bank, before anything is read or sent.
    """

    def _args(self, tmp_path, *extra):
        return cli.build_parser().parse_args(
            ["send", str(tmp_path / "in.syx"), "--to", "CARD"] + list(extra)
        )

    def _write_dump(self, tmp_path, function, payload, bank=3):
        path = tmp_path / "in.syx"
        frame = m.frame_bulk(function, bank, payload)
        path.write_bytes(bytes(frame))
        return path

    def test_a_missing_file_is_refused(self, tmp_path, capsys):
        assert cli._cmd_send(None, self._args(tmp_path)) == 2
        assert "cannot be read" in capsys.readouterr().out

    def test_a_file_with_no_wavestation_message_is_refused(self, tmp_path, capsys):
        path = tmp_path / "in.syx"
        path.write_bytes(bytes([0xF0, 0x41, 0x10, 0x00, 0x10, 0x12, 0xF7]))
        assert cli._cmd_send(None, self._args(tmp_path)) == 2
        assert "no Wavestation message" in capsys.readouterr().out

    def test_a_single_patch_dump_is_not_something_send_writes(self, tmp_path, capsys):
        path = tmp_path / "in.syx"
        path.write_bytes(bytes(m.single_patch_dump(3, 1, bytes(426), channel=0)))
        assert cli._cmd_send(None, self._args(tmp_path)) == 2
        assert "does not send" in capsys.readouterr().out

    def test_a_short_bank_is_refused(self, tmp_path, capsys):
        from kws import records as r

        self._write_dump(
            tmp_path, m.Function.ALL_PATCH_DUMP, bytes(banks.PATCHES_PER_BANK * 10)
        )
        assert cli._cmd_send(None, self._args(tmp_path)) == 2
        assert "Refusing to send a short bank" in capsys.readouterr().out

    def test_a_rom_destination_is_refused(self, tmp_path, capsys):
        from kws import records as r

        self._write_dump(
            tmp_path,
            m.Function.ALL_PATCH_DUMP,
            bytes(banks.PATCHES_PER_BANK * r.PATCH_SIZE),
        )
        args = cli.build_parser().parse_args(
            ["send", str(tmp_path / "in.syx"), "--to", "ROM4"]
        )
        assert cli._cmd_send(None, args) == 2
        out = capsys.readouterr().out
        assert "ROM and cannot be written" in out
        assert "RAM 1" in out, "it should say which banks can be written"

    def test_the_first_of_several_dumps_is_the_one_sent(self, tmp_path, capsys):
        from kws import records as r

        path = tmp_path / "in.syx"
        path.write_bytes(
            bytes(
                m.frame_bulk(
                    m.Function.ALL_PATCH_DUMP,
                    3,
                    bytes(banks.PATCHES_PER_BANK * r.PATCH_SIZE),
                )
            )
            + bytes(
                m.frame_bulk(
                    m.Function.ALL_PERFORMANCE_DUMP,
                    3,
                    bytes(banks.PERFORMANCES_PER_BANK * r.PERFORMANCE_SIZE),
                )
            )
        )
        args = cli.build_parser().parse_args(["send", str(path), "--to", "CARD"])
        # The next refusal is the destination read, which this device cannot do.
        assert cli._cmd_send(_Unreadable(), args) == 1
        assert "Refusing to write" in capsys.readouterr().out


class _Unreadable:
    """A device whose every read fails, so `send` must stop before sending."""

    description = "unreadable"

    def read_raw(self, *_a, **_k):
        raise TimeoutError("nothing came back")

    def read_bank_names(self, *_a, **_k):
        raise TimeoutError("nothing came back")

    def write_bank(self, *_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("a write happened after an unreadable destination")

    def close(self):
        pass


class TestSendWithoutYes:
    """Without `--yes` nothing is sent, and the names are shown first."""

    def test_it_names_what_it_would_replace(self, tmp_path, capsys):
        from kws import records as r

        payload = bytearray(banks.PATCHES_PER_BANK * r.PATCH_SIZE)
        payload[0:7] = b"Invited"[:7]
        path = tmp_path / "in.syx"
        path.write_bytes(
            bytes(m.frame_bulk(m.Function.ALL_PATCH_DUMP, 3, bytes(payload)))
        )
        args = cli.build_parser().parse_args(["send", str(path), "--to", "CARD"])
        assert cli._cmd_send(_Reading(payload), args) == 0
        out = capsys.readouterr().out
        assert "about to replace" in out
        assert "Invited" in out, "a slot number means nothing; the names do"
        assert "Nothing has been sent" in out
        assert "--yes" in out


class _Reading:
    """Answers the destination read, and records any write attempt."""

    def __init__(self, contents):
        self.contents = bytes(contents)
        self.wrote = []

    description = "stand-in"

    def read_raw(self, bank_number, function, **_k):
        return bytes(m.frame_bulk(function, bank_number, self.contents))

    def read_bank_names(self, bank, kind=None, **_k):
        from kws import dumps

        packet = m.parse(
            bytes(m.frame_bulk(m.Function.ALL_PATCH_DUMP, 3, self.contents))
        )
        assert packet is not None
        dump = dumps.parse_bulk(packet)
        return dump.kind, dump.names()

    def write_bank(self, *a, **k):
        self.wrote.append((a, k))
        return {"backup": "/dev/null", "verified": True, "mismatch": None}

    def close(self):
        pass


class TestTheChannelArguments:
    def test_a_channel_outside_one_to_sixteen_is_refused(self):
        with pytest.raises(SystemExit):
            cli.main(["--demo", "--midi-channel", "0", "status"])
        with pytest.raises(SystemExit):
            cli.main(["--demo", "--midi-channel", "17", "status"])

    def test_the_two_channels_are_both_range_checked(self):
        # --channel is the one a program change goes out on; --device-channel
        # is the unit's own base. They are different numbers on most of these
        # instruments and the spec says they must not share a word, so both
        # get their own option and both are checked.
        for option in ("--channel", "--device-channel"):
            with pytest.raises(SystemExit):
                cli.main(["--demo", option, "17", "status"])

    def test_the_parser_defaults_are_none(self):
        # --midi-channel is now --device-channel, with the old spelling still
        # accepted; both land in the same dest, so there is one attribute.
        args = cli.build_parser().parse_args(["banks"])
        assert args.device_channel is None and args.channel is None
        old = cli.build_parser().parse_args(["--midi-channel", "7", "banks"])
        assert old.device_channel == 7


class TestOfflineNeverBuildsADevice:
    def test_the_offline_set_is_what_it_claims(self):
        assert set(cli._OFFLINE) == {"banks", "list", "find", "ports", "backup"}

    def test_every_offline_name_is_in_the_command_table(self):
        assert set(cli._COMMANDS) >= cli._OFFLINE

    def test_a_namespace_without_the_attribute_is_not_needed(self):
        """The offline handlers take args only; none touches _device."""
        for name in cli._OFFLINE:
            assert name in cli._COMMANDS


class TestParserShape:
    def test_dump_takes_an_optional_kind(self):
        parser = cli.build_parser()
        assert parser.parse_args(["dump", "CARD"]).kind == banks.Kind.PATCH

    def test_send_requires_a_destination(self):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["send", "x.syx"])

    def test_unknown_commands_are_refused(self):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["frobnicate"])


def test_the_argparse_type_is_the_one_the_handlers_expect():
    """A guard on the annotations added when mypy went strict."""
    args = cli.build_parser().parse_args(["list", "CARD", "patch"])
    assert isinstance(args, argparse.Namespace)
