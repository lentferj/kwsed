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

"""The live multi catalog: snapshots, archive, and the mismatch rules."""

from __future__ import annotations

from typing import Any

from kws import banks, multi
from kws import multisets as mc


def _multiset(**parts: Any) -> Any:
    """One Multiset with the given channel bindings.

    ``parts`` maps "channel" to ``(bank byte, performance)``; unlisted
    channels are unused.
    """
    entries = []
    for channel in range(16):
        bank, performance = parts.get(str(channel), (0, 50))
        entries.append(
            multi.Part(
                channel=channel + 1,
                bank=bank,
                performance=performance,
                level=100,
                flag=0,
            )
        )
    return multi.Multiset(number=0, name="M", parts=tuple(entries))


def _record(multiset: Any) -> bytes:
    encoded = multi.encode([multiset])
    return encoded[: multi.MULTISET_SIZE]


def _snapshot(multiset: Any) -> mc.MultiSnapshot:
    return mc.build_snapshot(
        multiset,
        _record(multiset),
        card_scope_for=lambda channel: "CARD/39" if channel == 0 else "CARD/20",
        name_for=lambda bank, number, scope: (
            "Warm Pad" if scope == "CARD/39" else "Bass" if bank == 0 else "ROM Lead"
        ),
        card_digest_for=lambda scope: (
            "digest-39" if scope == "CARD/39" else "digest-20"
        ),
        recorded_at="2026-11-13T00:00:00+00:00",
    )


def test_the_snapshot_records_card_scope_name_and_digest() -> None:
    card = banks.bank("CARD").sysex
    ms = _multiset(**{"0": (card, 5), "1": (0, 2), "2": (3, 7)})
    snap = _snapshot(ms)
    assert snap.digest == mc.record_digest(_record(ms))
    assert snap.channels[0].card_scope == "CARD/39"
    assert snap.channels[0].card_digest == "digest-39"
    assert snap.channels[0].name == "Warm Pad"
    assert snap.channels[1].card_scope is None
    assert snap.channels[1].name == "Bass"
    assert 3 not in snap.channels, "unused channels are not recorded"


def test_a_different_card_is_a_mismatch() -> None:
    card = banks.bank("CARD").sysex
    snap = _snapshot(_multiset(**{"0": (card, 5)}))
    found = mc.check(
        snap,
        active_card_scope="CARD/20",
        active_card_digest="digest-20",
        name_at=lambda bank, number: None,
    )
    assert len(found) == 1
    assert found[0].kind == "card"
    assert "CARD/39" in found[0].text
    assert "CARD/20" in found[0].text


def test_the_same_card_content_under_another_bank_is_not_a_break() -> None:
    card = banks.bank("CARD").sysex
    snap = _snapshot(_multiset(**{"0": (card, 5)}))
    assert (
        mc.check(
            snap,
            active_card_scope="CARD/20",
            active_card_digest="digest-39",
            name_at=lambda bank, number: None,
        )
        == []
    )


def test_the_expected_card_is_not_a_mismatch() -> None:
    card = banks.bank("CARD").sysex
    snap = _snapshot(_multiset(**{"0": (card, 5)}))
    assert (
        mc.check(
            snap,
            active_card_scope="CARD/39",
            active_card_digest="digest-39",
            name_at=lambda bank, number: None,
        )
        == []
    )


def test_a_renamed_ram_slot_is_a_mismatch() -> None:
    snap = _snapshot(_multiset(**{"1": (0, 2)}))
    found = mc.check(
        snap,
        active_card_scope=None,
        active_card_digest="",
        name_at=lambda bank, number: "Other",
    )
    assert len(found) == 1
    assert found[0].kind == "ram"
    assert "Warm Pad" in found[0].text or "Bass" in found[0].text
    assert "Other" in found[0].text


def test_an_unread_ram_bank_is_not_evidence() -> None:
    snap = _snapshot(_multiset(**{"1": (0, 2)}))
    assert (
        mc.check(
            snap,
            active_card_scope=None,
            active_card_digest="",
            name_at=lambda bank, number: None,
        )
        == []
    )


def test_rom_slots_are_not_checked() -> None:
    snap = _snapshot(_multiset(**{"2": (5, 7)}))  # ROM 6 by its wire byte
    assert (
        mc.check(
            snap,
            active_card_scope=None,
            active_card_digest="",
            name_at=lambda bank, number: "Renamed",
        )
        == []
    )


def test_the_archive_round_trips(tmp_path: Any) -> None:
    snap = _snapshot(_multiset(**{"0": (banks.bank("CARD").sysex, 5), "1": (0, 2)}))
    path = str(tmp_path / "multisets.json")
    mc.save({0: snap}, path)
    loaded = mc.load(path)
    assert loaded == {0: snap}


def test_a_corrupt_archive_is_empty_not_fatal(tmp_path: Any) -> None:
    path = str(tmp_path / "multisets.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{not json")
    assert mc.load(path) == {}


def test_an_unrecorded_card_bank_is_a_visible_warning() -> None:
    """A Card channel recorded without a bank cannot be compared, but it
    must still say so rather than staying silent."""
    card = banks.bank("CARD").sysex
    ms = _multiset(**{"0": (card, 5)})
    snap = mc.build_snapshot(
        ms,
        _record(ms),
        card_scope_for=lambda channel: None,
        name_for=lambda bank, number, scope: "",
        card_digest_for=lambda scope: "",
    )
    found = mc.check(
        snap,
        active_card_scope="CARD/25",
        active_card_digest="",
        name_at=lambda bank, number: None,
    )
    assert len(found) == 1
    assert found[0].kind == "card"
    assert "no bank was recorded" in found[0].text
    assert "CARD/25" in found[0].text


def test_a_changed_channel_is_stale_not_a_card_warning() -> None:
    """A staged Multiset no longer points at the card; say that, not 'wrong
    card'."""
    card = banks.bank("CARD").sysex
    recorded = _multiset(**{"0": (card, 5)})
    snap = _snapshot(recorded)
    current = _multiset(**{"0": (banks.bank("RAM3").sysex, 0)})
    found = mc.check(
        snap,
        active_card_scope="CARD/25",
        active_card_digest="other",
        name_at=lambda bank, number: None,
        multiset=current,
    )
    assert len(found) == 1
    assert found[0].kind == "stale"
    assert "RAM 3 00" in found[0].text
    assert "re-record" in found[0].text
