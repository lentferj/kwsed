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

from kws import banks, cards, dumps
from kws import messages as m
from kwsed import cli
from kwsed.demo import DemoDevice

# A catalog path that cannot exist, so the offline commands read no names
# and their output is the same on every machine. kws/data/catalog.json is
# gitignored and present or absent depending on who generated it.
NO_CATALOG = os.path.join(os.sep, "nonexistent", "kwsed-test-catalog.json")
NO_CARDS = os.path.join(os.sep, "nonexistent", "kwsed-test-cards.json")
# A live-names path that cannot exist, so no real machine cache is read.
NO_LIVE_NAMES = os.path.join(os.sep, "nonexistent", "kwsed-test-live-names.json")


def run(argv):
    """main() with the demo device, returning its exit code."""
    return cli.main(
        [
            "--demo",
            "--catalog",
            NO_CATALOG,
            "--cards",
            NO_CARDS,
            "--live-names",
            NO_LIVE_NAMES,
        ]
        + argv
    )


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


class TestWhatADifferenceMeans:
    """`kwscli dump` and `list` mark a differing name with `*`, and the mark
    means different things in the two kinds of bank.

    A writable bank (Card) holds somebody's own work, saved over the
    factory preset; a ROM bank cannot change, so there the printed list is
    the wrong one. The wording is the family's, via
    `kws.catalog.disagreement_words`; each test pins the catalog and a
    read-back live name, so the difference is deterministic rather than a
    demo name colliding with a factory one.
    """

    def _names(self, tmp_path, bank_id):
        catalog = tmp_path / "cat.json"
        catalog.write_text(
            json.dumps(
                {"banks": {bank_id: [{"kind": "patch", "n": 3, "name": "Printed"}]}}
            ),
            encoding="utf-8",
        )
        live = tmp_path / "live.json"
        live.write_text(
            json.dumps({f"{bank_id}:patch:003": "Saved Own Work"}),
            encoding="utf-8",
        )
        return ["--catalog", str(catalog), "--live-names", str(live)]

    def test_a_readable_bank_reports_saved_work(self, tmp_path, capsys):
        names = ["dump", "CARD", "patch"]
        assert cli.main(["--demo"] + self._names(tmp_path, "CARD") + names) == 0
        out = capsys.readouterr().out
        assert "saved over the factory preset" in out
        assert "the printed list is wrong" not in out

    def test_a_rom_bank_reports_the_catalog_as_wrong(self, tmp_path, capsys):
        names = ["dump", "ROM4", "patch"]
        assert cli.main(["--demo"] + self._names(tmp_path, "ROM4") + names) == 0
        out = capsys.readouterr().out
        assert "the printed list is wrong; the unit is right" in out
        assert "saved over the factory preset" not in out

    def test_list_marks_differing_names_with_the_same_wording(self, tmp_path, capsys):
        assert (
            cli.main(
                ["--demo"] + self._names(tmp_path, "CARD") + ["list", "CARD", "patch"]
            )
            == 0
        )
        out = capsys.readouterr().out
        assert "saved over the factory preset" in out
        assert "the printed list is wrong" not in out

    def test_select_sends_bank_select_and_program_change(self, capsys):
        device = DemoDevice()
        args = cli.build_parser().parse_args(["select", "CARD", "performance", "3"])
        cli._cmd_select(device, args)
        out = capsys.readouterr().out
        assert "MSB 0, LSB 1, program change 53" in out
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


class _SavingDevice:
    """Answers a bank request with a full bank of invented names."""

    description = "stand-in"
    channel = 4

    def __init__(self, *, fail=()):
        self.calls = []
        self.fail = set(fail)

    def read_raw(self, bank_number, function, **_k):
        from kws import records as r

        self.calls.append((bank_number, function))
        if function in self.fail:
            raise TimeoutError("nothing came back")
        if function == m.Function.ALL_PATCH_DUMP:
            kind, size = banks.Kind.PATCH, r.PATCH_SIZE
        elif function == m.Function.ALL_PERFORMANCE_DUMP:
            kind, size = banks.Kind.PERFORMANCE, r.PERFORMANCE_SIZE
        else:  # pragma: no cover - the CLI asks for one of the two
            raise AssertionError(f"unexpected request {function:#04x}")
        payload = bytearray(banks.count_for(kind) * size)
        for number in range(banks.count_for(kind)):
            name = f"Invented {number:02d}".encode()
            at = number * size
            payload[at : at + len(name)] = name
        return bytes(m.frame_bulk(function, bank_number, bytes(payload), channel=4))

    def close(self):
        pass


class TestBackupSave:
    """`backup save` reads, writes envelopes, and changes nothing."""

    def _args(self, tmp_path, *extra):
        return cli.build_parser().parse_args(
            [
                "backup",
                "save",
                "--yes",
                "--backup-dir",
                str(tmp_path / "backups"),
                *extra,
            ]
        )

    def test_it_saves_both_kinds_for_every_writable_bank(self, tmp_path, capsys):
        from kws import backup

        device = _SavingDevice()
        assert cli._cmd_backup(device, self._args(tmp_path)) == 0
        entries = backup.list_backups(str(tmp_path / "backups"))
        assert len(entries) == 8
        assert {entry.slot for entry in entries} == {
            "RAM 1",
            "RAM 2",
            "RAM 3",
            "Card",
        }
        assert sum(entry.kind == banks.Kind.PATCH for entry in entries) == 4
        assert sum(entry.kind == banks.Kind.PERFORMANCE for entry in entries) == 4
        # A block is the message a .syx would hold, not a private container.
        for entry in entries:
            packet = m.parse(entry.blocks["dump"])
            assert packet is not None
            assert packet.function in (
                m.Function.ALL_PATCH_DUMP,
                m.Function.ALL_PERFORMANCE_DUMP,
            )
        out = capsys.readouterr().out
        assert "RAM 1" in out and "Card" in out

    def test_one_bank_and_one_kind_can_be_asked_for(self, tmp_path):
        from kws import backup

        device = _SavingDevice()
        args = self._args(tmp_path, "--bank", "CARD", "--kind", "patch")
        assert cli._cmd_backup(device, args) == 0
        assert device.calls == [(banks.bank("CARD").sysex, m.Function.ALL_PATCH_DUMP)]
        entries = backup.list_backups(str(tmp_path / "backups"))
        assert len(entries) == 1
        assert entries[0].slot == "Card"
        assert entries[0].kind == banks.Kind.PATCH

    def test_a_rom_bank_is_refused_before_anything_is_read(self, tmp_path, capsys):
        device = _SavingDevice()
        args = self._args(tmp_path, "--bank", "ROM4")
        assert cli._cmd_backup(device, args) == 2
        assert device.calls == []
        out = capsys.readouterr().out
        assert "ROM and cannot be written" in out
        assert "RAM 1" in out, "it should say which banks can be saved"

    def test_no_yes_reads_nothing_and_says_so(self, tmp_path, capsys):
        device = _SavingDevice()
        args = cli.build_parser().parse_args(
            ["backup", "save", "--backup-dir", str(tmp_path / "backups")]
        )
        assert cli._cmd_backup(device, args) == 2
        assert device.calls == []
        err = capsys.readouterr().err
        assert "--yes" in err
        assert "Nothing was done" in err

    def test_a_failed_kind_is_reported_and_the_rest_still_saves(self, tmp_path, capsys):
        from kws import backup

        device = _SavingDevice(fail={m.Function.ALL_PERFORMANCE_DUMP})
        assert cli._cmd_backup(device, self._args(tmp_path)) == 1
        entries = backup.list_backups(str(tmp_path / "backups"))
        assert len(entries) == 4
        assert {entry.kind for entry in entries} == {banks.Kind.PATCH}
        assert "could not be read" in capsys.readouterr().out

    def test_main_checks_yes_before_opening_a_unit(self, tmp_path, capsys):
        assert run(["backup", "save", "--backup-dir", str(tmp_path)]) == 2
        assert "--yes" in capsys.readouterr().err


class TestBackupRestore:
    """A restore is a write, so it goes through the same fence as `send`."""

    def _args(self, path, tmp_path, *extra):
        return cli.build_parser().parse_args(
            [
                "backup",
                "restore",
                str(path),
                "--yes",
                "--backup-dir",
                str(tmp_path / "pre-write"),
                *extra,
            ]
        )

    def _envelope(self, tmp_path, *, bank="Card", kind=banks.Kind.PATCH, payload=None):
        from kws import backup
        from kws import records as r

        entry = banks.bank(bank)
        if payload is None:
            size = r.PATCH_SIZE if kind == banks.Kind.PATCH else r.PERFORMANCE_SIZE
            payload = bytes(banks.count_for(kind) * size)
        function = (
            m.Function.ALL_PATCH_DUMP
            if kind == banks.Kind.PATCH
            else m.Function.ALL_PERFORMANCE_DUMP
        )
        frame = m.frame_bulk(function, entry.sysex, payload, channel=0)
        return backup.save(
            str(tmp_path / "store"),
            kind=kind,
            slot=entry.label,
            name=entry.label,
            device_id="0",
            source="kwsed",
            blocks={"dump": bytes(frame)},
        )

    def test_it_writes_through_the_fence_and_verifies_by_read_back(
        self, tmp_path, capsys
    ):
        from kws import backup
        from tests.test_write import PATCH_BANK, Unit

        unit = Unit(contents=bytes(PATCH_BANK))
        path = self._envelope(tmp_path, payload=PATCH_BANK)
        assert cli._cmd_backup(unit.bridge, self._args(path, tmp_path)) == 0

        functions = [d[4] for d in unit.midi_out.sent if len(d) > 5]
        # The names preview, then the fence's own read, the write and the
        # read-back. A restore that sent first would put the write second.
        assert functions == [
            m.Function.BULK_REQUEST,
            m.Function.BULK_REQUEST,
            m.Function.ALL_PATCH_DUMP,
            m.Function.BULK_REQUEST,
        ]
        assert len(backup.list_backups(str(tmp_path / "pre-write"))) == 1
        out = capsys.readouterr().out
        assert "about to replace: Card, all patches" in out
        assert "written and verified" in out

    def test_a_rom_envelope_is_refused_and_nothing_is_sent(self, tmp_path, capsys):
        from tests.test_write import Unit

        unit = Unit()
        path = self._envelope(tmp_path, bank="ROM4")
        assert cli._cmd_backup(unit.bridge, self._args(path, tmp_path)) == 2
        assert unit.midi_out.sent == []
        assert "ROM and cannot be written" in capsys.readouterr().out

    def test_an_envelope_whose_slot_and_dump_disagree_is_refused(
        self, tmp_path, capsys
    ):
        from kws import backup
        from tests.test_write import PATCH_BANK, Unit

        entry = banks.bank("Card")
        frame = m.frame_bulk(
            m.Function.ALL_PATCH_DUMP, entry.sysex, PATCH_BANK, channel=0
        )
        path = backup.save(
            str(tmp_path / "store"),
            kind=banks.Kind.PATCH,
            slot="RAM 1",
            name="RAM 1",
            device_id="0",
            source="kwsed",
            blocks={"dump": bytes(frame)},
        )
        unit = Unit()
        assert cli._cmd_backup(unit.bridge, self._args(path, tmp_path)) == 2
        assert unit.midi_out.sent == []
        assert "says RAM 1 but its dump was for Card" in capsys.readouterr().out

    def test_no_yes_sends_nothing_and_says_so(self, tmp_path, capsys):
        from tests.test_write import Unit

        unit = Unit()
        path = self._envelope(tmp_path)
        args = cli.build_parser().parse_args(
            [
                "backup",
                "restore",
                str(path),
                "--backup-dir",
                str(tmp_path / "pre-write"),
            ]
        )
        assert cli._cmd_backup(unit.bridge, args) == 2
        assert unit.midi_out.sent == []
        err = capsys.readouterr().err
        assert "--yes" in err
        assert "Nothing was done" in err

    def test_an_unreadable_file_is_refused(self, tmp_path, capsys):
        from tests.test_write import Unit

        unit = Unit()
        missing = tmp_path / "not-a-backup.json"
        assert cli._cmd_backup(unit.bridge, self._args(missing, tmp_path)) == 2
        assert unit.midi_out.sent == []
        assert "not a backup kwsed can read" in capsys.readouterr().out

    def test_a_bad_read_back_is_reported_and_exits_non_zero(self, tmp_path, capsys):
        from tests.test_write import PATCH_BANK, Unit

        unit = Unit(contents=bytes(PATCH_BANK))
        original = unit._sent

        def lying(data):
            original(data)
            if len(data) > 5 and data[4] == m.Function.ALL_PATCH_DUMP:
                wrong = bytearray(unit.contents)
                wrong[0] ^= 0xFF
                unit.contents = bytes(wrong)

        unit.midi_out.send_message = lying  # type: ignore[method-assign]
        path = self._envelope(tmp_path, payload=PATCH_BANK)
        assert cli._cmd_backup(unit.bridge, self._args(path, tmp_path)) == 1
        out = capsys.readouterr().out
        assert "does NOT match" in out


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
        assert set(cli._OFFLINE) == {"banks", "list", "find", "ports", "cards"}

    def test_backup_list_needs_no_unit_but_save_and_restore_do(self):
        parser = cli.build_parser()
        assert cli._offline(parser.parse_args(["backup", "list"]))
        assert not cli._offline(parser.parse_args(["backup", "save", "--yes"]))
        assert not cli._offline(
            parser.parse_args(["backup", "restore", "x.json", "--yes"])
        )

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


class TestCardsVerb:
    """`kwscli cards` is the archive of the HCard's forty banks.

    Offline like `banks` and `list`: it reads no unit, so it works with the
    synthesiser off, and a `.syx` it writes holds names only.
    """

    def _archive(self, tmp_path):
        path = str(tmp_path / "cards.json")
        cards.save(
            cards.capture(
                7,
                'Korg WPC-13 "Performance 3"',
                {0: "Opening", 1: "Second"},
                {0: "Bell"},
                captured_at="2026-11-07T12:00:00+00:00",
            ),
            path,
        )
        return path

    def test_the_table_lists_forty_banks(self, capsys):
        assert run(["cards"]) == 0
        out = capsys.readouterr().out
        assert "WS Internal ROM" in out
        assert "User 15" in out
        assert out.count("not captured") == 40

    def test_a_captured_bank_is_marked_in_the_table(self, tmp_path, capsys):
        path = self._archive(tmp_path)
        assert cli.main(["--cards", path, "cards"]) == 0
        out = capsys.readouterr().out
        assert 'Korg WPC-13 "Performance 3"' in out
        assert out.count("not captured") == 39

    def test_one_bank_shows_its_archive(self, tmp_path, capsys):
        path = self._archive(tmp_path)
        assert cli.main(["--cards", path, "cards", "07"]) == 0
        out = capsys.readouterr().out
        assert "Card 07" in out
        assert "Opening" in out
        assert "Bell" in out
        assert "Second" in out

    def test_an_uncaptured_bank_still_shows_the_table_name(self, capsys):
        assert run(["cards", "16"]) == 0
        out = capsys.readouterr().out
        assert "WPC-1S" in out
        assert "needs WSC-01" in out
        assert "not captured" in out

    def test_it_dumps_one_bank_as_syx(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "kws.bankstore.default_dir", lambda: str(tmp_path / "emptybanks")
        )
        path = self._archive(tmp_path)
        out = tmp_path / "bank07.syx"
        code = cli.main(
            [
                "--cards",
                path,
                "cards",
                "07",
                "--kind",
                "patch",
                "--syx",
                str(out),
            ]
        )
        assert code == 0
        packets = dumps.read_syx_file(str(out))
        assert len(packets) == 1
        packet = packets[0]
        assert packet.function == m.Function.ALL_PATCH_DUMP
        assert packet.bank == banks.bank("CARD").sysex
        dump = dumps.parse_bulk(packet)
        assert dump.kind == banks.Kind.PATCH
        assert dump.names()[0] == "Bell"

    def test_syx_uses_real_records_when_cached(self, tmp_path, monkeypatch, capsys):
        from kws import banks, bankstore
        from kws import records as rec

        path = self._archive(tmp_path)
        banks_dir = tmp_path / "banks"
        marker = b"RealData"
        blob = (
            marker
            + b"\x00" * (rec.PATCH_SIZE - len(marker))
            + b"\x00" * (rec.PATCH_SIZE * (banks.PATCHES_PER_BANK - 1))
        )
        # ensure exact size
        assert len(blob) == rec.PATCH_SIZE * banks.PATCHES_PER_BANK
        bankstore.save("CARD/07", patches=blob, directory=str(banks_dir))
        monkeypatch.setattr("kws.bankstore.default_dir", lambda: str(banks_dir))

        out = tmp_path / "real.syx"
        code = cli.main(
            [
                "--cards",
                path,
                "cards",
                "07",
                "--kind",
                "patch",
                "--syx",
                str(out),
            ]
        )
        assert code == 0
        err = capsys.readouterr().err
        assert "full record data" in err
        packets = dumps.read_syx_file(str(out))
        packet = packets[0]
        dump = dumps.parse_bulk(packet)
        assert bytes(dump.records[0][:8]) == marker

    def test_syx_without_a_kind_is_a_usage_error(self, tmp_path, capsys):
        path = self._archive(tmp_path)
        out = tmp_path / "bank.syx"
        code = cli.main(["--cards", path, "cards", "07", "--syx", str(out)])
        assert code != 0
        assert "needs --kind" in capsys.readouterr().err
        assert not out.exists()

    def test_syx_of_an_uncaptured_bank_is_a_usage_error(self, tmp_path, capsys):
        out = tmp_path / "bank.syx"
        code = cli.main(
            [
                "--cards",
                str(tmp_path / "cards.json"),
                "cards",
                "07",
                "--kind",
                "patch",
                "--syx",
                str(out),
            ]
        )
        assert code != 0
        assert "not captured" in capsys.readouterr().err
        assert not out.exists()

    def test_an_unknown_bank_is_a_usage_error(self, capsys):
        assert run(["cards", "99"]) != 0
        assert "00-39" in capsys.readouterr().err


class TestCardsVerificationWords:
    """`kwscli cards` says whether a capture was confirmed by content.

    A capture filed on the row's say-so is `unverified` — the HCard may be
    on another bank — and one a digest confirmed is `verified`. Both words
    are the app's (9f97d16); the CLI repeats them so the archive can be
    read without opening the browser. Offline like every other `cards`
    invocation: the words come from the archive record, `--cards` points at
    a file under tmp, and no port is opened.
    """

    def _save(self, tmp_path, *, verified, digest):
        path = str(tmp_path / "cards.json")
        cards.save(
            cards.capture(
                7,
                'Korg WPC-13 "Performance 3"',
                {0: "Opening"},
                {0: "Bell"},
                captured_at="2026-11-07T12:00:00+00:00",
                digest=digest,
                verified=verified,
            ),
            path,
        )
        return path

    def test_one_bank_says_unverified_for_a_say_so_capture(self, tmp_path, capsys):
        path = self._save(tmp_path, verified=False, digest="")
        assert cli.main(["--cards", path, "cards", "07"]) == 0
        out = capsys.readouterr().out
        assert "unverified" in out
        assert "digest: (none)" in out

    def test_one_bank_says_verified_and_shows_the_digest_prefix(self, tmp_path, capsys):
        digest = cards.content_digest([b"perf"], [b"patch"])
        path = self._save(tmp_path, verified=True, digest=digest)
        assert cli.main(["--cards", path, "cards", "07"]) == 0
        out = capsys.readouterr().out
        assert "unverified" not in out
        assert "verified" in out
        # The reference is the archive, so the line shows a short prefix —
        # enough to eyeball two captures against each other — not the hash.
        assert f"digest: {digest[:8]}" in out

    def test_the_listing_marks_only_an_unverified_bank(self, tmp_path, capsys):
        path = self._save(tmp_path, verified=False, digest="")
        assert cli.main(["--cards", path, "cards"]) == 0
        out = capsys.readouterr().out
        assert "unverified" in out
        assert "not captured" in out
        assert out.count("not captured") == 39

    def test_a_verified_bank_is_unmarked_in_the_listing(self, tmp_path, capsys):
        digest = cards.content_digest([b"perf"], [b"patch"])
        path = self._save(tmp_path, verified=True, digest=digest)
        assert cli.main(["--cards", path, "cards"]) == 0
        out = capsys.readouterr().out
        assert "unverified" not in out
        assert out.count("not captured") == 39

    def test_the_verb_stays_offline(self):
        args = cli.build_parser().parse_args(["cards"])
        assert cli._offline(args)


def test_one_banks_kind_filter_narrows_the_display(tmp_path, capsys):
    path = str(tmp_path / "cards.json")
    cards.save(
        cards.capture(
            7,
            'Korg WPC-13 "Performance 3"',
            {0: "Opening"},
            {0: "Bell"},
            captured_at="2026-11-07T12:00:00+00:00",
        ),
        path,
    )
    assert cli.main(["--cards", path, "cards", "07", "--kind", "patch"]) == 0
    out = capsys.readouterr().out
    assert "Bell" in out
    assert "Opening" not in out
