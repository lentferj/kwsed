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

"""Containment audit (plan D1). Every fixture name here is invented."""

import pytest

from kws import banks, containment, deps
from kws import records as r


def _performance(name, parts):
    """A performance record with ``parts`` as (bank number, patch slot)."""
    record = bytearray(r.PERFORMANCE_SIZE)
    record[0 : len(name)] = name.encode()
    for i in range(r.PARTS_PER_PERFORMANCE):
        r.set_performance_part(record, i, bank=0, patch=r.UNUSED_PATCH)
    for i, (bank, patch) in enumerate(parts):
        r.set_performance_part(record, i, bank=bank, patch=patch)
    return bytes(record)


def _patch(name, waves):
    """A patch record with ``waves`` as (bank number, wave number) per
    oscillator; oscillators beyond the list stay switched off."""
    record = bytearray(r.PATCH_SIZE)
    record[0 : len(name)] = name.encode()
    for i, (bank, wave) in enumerate(waves):
        r.set_patch_wave(record, i, bank=bank, wave=wave)
    return bytes(record)


def _source(scope_id="RAM1", bank_number=None, performances=()):
    return deps.Source(
        scope_id=scope_id,
        bank_number=bank_number,
        performances=list(performances),
    )


def test_fully_self_performance():
    src = _source(performances=[_performance("Audit Soloway", ((0, 3), (0, 7)))])
    audit = containment.audit_containment({"RAM1": src}, "RAM1")
    assert audit.source_bank_number == banks.bank("RAM1").number
    assert len(audit.performances) == 1
    pa = audit.performances[0]
    assert pa.slot == 0
    assert [(t.bank_number, t.is_rom, t.patch) for t in pa.parts] == [
        (0, False, 3),
        (0, False, 7),
    ]
    assert pa.self_only
    assert pa.contained
    assert pa.rom_parts == 0
    assert pa.external_banks == frozenset()
    assert audit.contained == 1
    assert audit.other_bank == 0
    assert audit.rom_assisted == 0
    assert audit.parts_per_bank == {}
    # the performance's own bank is a group in the patch records
    assert audit.patch_records == {banks.bank("RAM1").number: (3, 7)}


def test_self_plus_rom_is_contained_and_reported():
    src = _source(performances=[_performance("Audit Rommix", ((0, 2), (5, 9)))])
    audit = containment.audit_containment({"RAM1": src}, "RAM1")
    pa = audit.performances[0]
    # bank 5 is ROM 6, not writable
    assert not banks.bank(5).writable
    assert [(t.bank_number, t.is_rom, t.patch) for t in pa.parts] == [
        (0, False, 2),
        (5, True, 9),
    ]
    assert not pa.self_only
    assert pa.contained  # D1: self-or-ROM is contained
    assert pa.rom_parts == 1
    assert pa.external_banks == frozenset()
    assert audit.contained == 1
    assert audit.rom_assisted == 1
    assert audit.other_bank == 0
    assert audit.parts_per_bank == {}


def test_other_writable_bank_is_external():
    src = _source(performances=[_performance("Audit Crossway", ((0, 1), (1, 4)))])
    audit = containment.audit_containment({"RAM1": src}, "RAM1")
    pa = audit.performances[0]
    assert banks.bank(1).writable
    assert not pa.contained
    assert not pa.self_only
    assert pa.rom_parts == 0
    # the bank number appears in the audit, twice over
    assert pa.external_banks == frozenset({1})
    assert audit.other_bank == 1
    assert audit.contained == 0
    assert audit.parts_per_bank == {1: 1}
    assert set(audit.patch_records) == {0, 1}
    assert audit.patch_records[1] == (4,)


def test_card_slot_is_writable_not_rom():
    src = _source(performances=[_performance("Audit Cardway", ((11, 6),))])
    audit = containment.audit_containment({"RAM1": src}, "RAM1")
    pa = audit.performances[0]
    assert banks.bank(11).writable
    assert not pa.parts[0].is_rom
    assert pa.external_banks == frozenset({11})
    assert audit.other_bank == 1
    assert audit.rom_assisted == 0


def test_two_parts_on_the_same_external_patch_count_once():
    src = _source(
        performances=[_performance("Audit Twinned", ((1, 4), (1, 4), (2, 8)))]
    )
    audit = containment.audit_containment({"RAM1": src}, "RAM1")
    pa = audit.performances[0]
    assert pa.external_banks == frozenset({1, 2})
    assert audit.parts_per_bank == {1: 2, 2: 1}
    assert audit.patch_records[1] == (4,)
    assert audit.patch_records[2] == (8,)


def test_empty_performances_are_ignored():
    empty = bytearray(r.PERFORMANCE_SIZE)
    src = _source(
        performances=[
            bytes(empty),
            _performance("Audit Loner", ((0, 5),)),
            bytes(empty),
        ]
    )
    audit = containment.audit_containment({"RAM1": src}, "RAM1")
    assert [pa.slot for pa in audit.performances] == [1]
    assert audit.contained == 1
    assert audit.other_bank == 0


def test_init_named_slot_pointing_elsewhere_is_not_empty():
    # The factory Init shape (bank 0 / patch 0 on every part) is empty; an
    # init-named slot that points anywhere else is real work.
    src = _source(
        performances=[
            _performance("Init perf", ((0, 0),)),
            _performance("Init perf", ((0, 4),)),
        ]
    )
    audit = containment.audit_containment({"RAM1": src}, "RAM1")
    assert [pa.slot for pa in audit.performances] == [1]


def test_counts_and_per_bank_totals():
    performances = [
        _performance("Audit Alpha", ((0, 1),)),  # self
        _performance("Audit Bravo", ((0, 2), (5, 3))),  # self + ROM
        _performance("Audit Charlie", ((1, 4), (1, 6))),  # RAM 2, twice
        _performance("Audit Delta", ((1, 4), (2, 8))),  # RAM 2 and RAM 3
        _performance("Audit Echo", ((1, 4), (5, 0))),  # RAM 2 and ROM
    ]
    src = _source(performances=performances)
    audit = containment.audit_containment({"RAM1": src}, "RAM1")
    assert audit.contained == 2
    assert audit.rom_assisted == 1
    assert audit.other_bank == 3
    assert audit.parts_per_bank == {1: 4, 2: 1}
    assert audit.patch_records[0] == (1, 2)
    assert audit.patch_records[1] == (4, 6)
    assert audit.patch_records[2] == (8,)
    assert [pa.slot for pa in audit.performances] == [0, 1, 2, 3, 4]


def test_session_alias_resolves_through_bank_number():
    src = _source(
        scope_id="RAM1#2",
        bank_number=0,
        performances=[_performance("Audit Aliased", ((0, 9),))],
    )
    audit = containment.audit_containment({"RAM1#2": src}, "RAM1#2")
    assert audit.source_bank_number == 0
    assert audit.performances[0].self_only
    assert audit.contained == 1
    assert audit.patch_records == {0: (9,)}


def test_archived_card_slot_points_at_itself():
    # A performance from an archived card (CARD/07) whose part names the
    # Card slot (11) names that same archive, so it is self, not external.
    src = _source(
        scope_id="CARD/07",
        performances=[_performance("Audit Ownslot", ((11, 2),))],
    )
    audit = containment.audit_containment({"CARD/07": src}, "CARD/07")
    assert audit.source_bank_number == banks.bank("CARD").number
    pa = audit.performances[0]
    assert pa.self_only
    assert pa.contained
    assert pa.external_banks == frozenset()


def test_missing_source_raises():
    with pytest.raises(LookupError):
        containment.audit_containment({}, "RAM1")


def test_nothing_is_loaded_beyond_the_named_source():
    # Only the named source is read; the audit needs no other bank.
    src = _source(performances=[_performance("Audit Noloan", ((0, 3),))])
    audit = containment.audit_containment({"RAM1": src}, "RAM1")
    assert audit.contained == 1


# --- repoint_external_refs (plan D2, opt-in) ---------------------------------


def test_repoint_moves_used_parts_in_from_bank():
    # Bank 2 needs the high half of the split bank field (Bank_Exp 6), so a
    # round-trip through performance_parts proves both halves were written.
    assert banks.bank(2).writable
    perf = _performance("Repoint Farway", ((0, 3), (0, 7)))
    out = r.repoint_external_refs(perf + perf, 0, 2)
    assert len(out) == 2 * r.PERFORMANCE_SIZE
    for at in (0, r.PERFORMANCE_SIZE):
        parts = r.performance_parts(out[at : at + r.PERFORMANCE_SIZE])
        assert [(p.bank, p.patch) for p in parts[:2]] == [(2, 3), (2, 7)]


def test_repoint_leaves_to_bank_and_rom_parts_alone():
    # Bank 5 is one of the ROM banks; bank 2 is the destination itself.
    assert not banks.bank(5).writable
    perf = _performance("Repoint Mixed", ((2, 1), (5, 4), (1, 6)))
    out = r.repoint_external_refs(perf, 0, 2)
    parts = r.performance_parts(out)
    assert [(p.bank, p.patch) for p in parts[:3]] == [(2, 1), (5, 4), (1, 6)]


def test_repoint_leaves_unused_parts_alone():
    # _performance leaves the parts after the named ones switched off but
    # still naming bank 0, the bank being repointed.
    perf = _performance("Repoint Quiet", ((0, 3),))
    out = r.repoint_external_refs(perf, 0, 2)
    parts = r.performance_parts(out)
    assert (parts[0].bank, parts[0].patch) == (2, 3)
    for part in parts[1:]:
        assert (part.bank, part.patch) == (0, r.UNUSED_PATCH)


def test_repoint_preserves_size_and_leaves_input_alone():
    perf = _performance("Repoint Steady", ((0, 3),))
    other = _performance("Repoint Serene", ((1, 2),))
    bank = perf + other
    out = r.repoint_external_refs(bank, 0, 2)
    assert len(out) == len(bank)
    assert out[: len(perf)] != bank[: len(perf)]  # the changed record changed
    assert out[len(perf) :] == other  # an untouched record is byte-identical
    assert out[:16] == perf[:16]  # names survive
    assert bank == perf + other  # the input is not modified


def test_repoint_patch_waves_moves_sequences_only():
    # Oscillator waves: sequence 4 in bank 1, sequence 6 in bank 2, a PCM ROM
    # wave, and sequence 9 in bank 1 again. Only the bank-1 sequences move.
    patch = _patch("Repoint Waveseq", ((1, 4), (2, 6), (0, 40), (1, 9)))
    out = r.repoint_patch_wave_banks(patch + patch, 1, 3)
    assert len(out) == 2 * r.PATCH_SIZE
    for at in (0, r.PATCH_SIZE):
        waves = r.patch_waves(out[at : at + r.PATCH_SIZE])
        assert [(w.bank, w.wave) for w in waves] == [
            (3, 4),
            (2, 6),
            (0, 40),
            (3, 9),
        ]
        assert out[at : at + 16] == patch[:16]  # names survive
    assert patch + patch != out  # the input is not modified


def test_repoint_patch_waves_leaves_blank_patches_alone():
    blank = bytes(r.PATCH_SIZE)
    out = r.repoint_patch_wave_banks(blank + blank, 1, 3)
    assert out == blank + blank


@pytest.mark.parametrize(
    "repoint",
    [r.repoint_external_refs, r.repoint_patch_wave_banks],
)
def test_repoint_rejects_bad_banks(repoint):
    with pytest.raises(ValueError):
        repoint(b"\x00" * r.PERFORMANCE_SIZE, 12, 0)
    with pytest.raises(ValueError):
        repoint(b"\x00" * r.PERFORMANCE_SIZE, -1, 0)
    with pytest.raises(ValueError):
        repoint(b"\x00" * r.PERFORMANCE_SIZE, 0, 12)
    with pytest.raises(ValueError):
        repoint(b"\x00" * r.PERFORMANCE_SIZE, 3, 3)


def test_repoint_rejects_a_ragged_bank():
    with pytest.raises(ValueError):
        r.repoint_external_refs(b"\x00" * (r.PERFORMANCE_SIZE - 1), 0, 2)
    with pytest.raises(ValueError):
        r.repoint_patch_wave_banks(b"\x00" * (r.PATCH_SIZE - 1), 0, 2)
