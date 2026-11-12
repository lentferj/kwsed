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

"""Property-based tests for key invariants using Hypothesis.

These tests verify mathematical properties and round-trip invariants that
should hold for all valid inputs.
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, settings

from kws import banks, deps
from kws import messages as m
from kws import records as r
from kws import relations as rel

# --- 1. Nibble encoding round-trip ---------------------------------------------


@given(data=st.binary(min_size=0, max_size=1024))
@settings(max_examples=100, deadline=None)
def test_nibble_encoding_round_trip(data: bytes) -> None:
    """to_nibbles(from_nibbles(data)) == data for arbitrary byte sequences."""
    nibbles = m.to_nibbles(data)
    decoded = m.from_nibbles(nibbles)
    assert decoded == data


@given(data=st.binary(min_size=0, max_size=512))
@settings(max_examples=100, deadline=None)
def test_nibble_encoding_inverse(data: bytes) -> None:
    """from_nibbles(to_nibbles(data)) == data."""
    nibbles = m.to_nibbles(data)
    decoded = m.from_nibbles(nibbles)
    assert decoded == data


@given(data=st.binary(min_size=0, max_size=512))
@settings(max_examples=50, deadline=None)
def test_to_nibbles_preserves_low_nibble_first(data: bytes) -> None:
    """Each byte becomes two nibbles: low nibble first, then high nibble."""
    nibbles = m.to_nibbles(data)
    assert len(nibbles) == 2 * len(data)
    for i, byte in enumerate(data):
        assert nibbles[2 * i] == (byte & 0x0F)
        assert nibbles[2 * i + 1] == ((byte >> 4) & 0x0F)


# --- 2. Record name round-trip -------------------------------------------------


@given(
    name=st.text(
        alphabet=st.characters(min_codepoint=32, max_codepoint=126),
        min_size=0,
        max_size=32,
    )
)
@settings(max_examples=100, deadline=None)
def test_record_name_round_trip(name: str) -> None:
    """encode_name + decode_name preserves name (truncation at 16 chars)."""
    encoded = m.encode_name(name)
    decoded = m.decode_name(encoded)
    expected = name[:16].rstrip()
    assert decoded == expected


@given(
    name=st.text(
        alphabet=st.characters(min_codepoint=0, max_codepoint=255),
        min_size=0,
        max_size=32,
    )
)
@settings(max_examples=100, deadline=None)
def test_record_name_handles_non_ascii(name: str) -> None:
    """Non-ASCII characters are replaced with spaces."""
    encoded = m.encode_name(name)
    decoded = m.decode_name(encoded)
    # decode_name only allows 32-126, others become space
    for ch in decoded:
        assert ch == " " or (32 <= ord(ch) <= 126)


# --- 3. Patch wave round-trip --------------------------------------------------


@given(
    bank=st.integers(min_value=0, max_value=11),
    wave=st.integers(min_value=0, max_value=0xFFFF),
    index=st.integers(min_value=0, max_value=3),
)
@settings(max_examples=100, deadline=None)
def test_patch_wave_round_trip(bank: int, wave: int, index: int) -> None:
    """set_patch_wave + patch_waves preserves wave references."""
    rec = bytearray(r.PATCH_SIZE)
    r.set_patch_wave(rec, index, bank=bank, wave=wave)
    waves = r.patch_waves(rec)
    assert waves[index].bank == bank
    # wave is masked to 16 bits in the word
    assert waves[index].wave == (wave & 0xFFFF)


@given(
    bank=st.integers(min_value=0, max_value=11),
    wave=st.integers(min_value=0, max_value=0xFFFF),
    index=st.integers(min_value=0, max_value=3),
)
@settings(max_examples=50, deadline=None)
def test_patch_wave_bank_exp_split(bank: int, wave: int, index: int) -> None:
    """Bank number split across bankNum and PATCH_BANK_EXP survives round-trip."""
    rec = bytearray(r.PATCH_SIZE)
    r.set_patch_wave(rec, index, bank=bank, wave=wave)
    waves = r.patch_waves(rec)
    assert waves[index].bank == bank


# --- 4. Performance part round-trip --------------------------------------------


@given(
    bank=st.integers(min_value=0, max_value=11),
    patch=st.integers(min_value=0, max_value=255),
    index=st.integers(min_value=0, max_value=7),
)
@settings(max_examples=100, deadline=None)
def test_performance_part_round_trip(bank: int, patch: int, index: int) -> None:
    """set_performance_part + performance_parts preserves part references."""
    rec = bytearray(r.PERFORMANCE_SIZE)
    r.set_performance_part(rec, index, bank=bank, patch=patch)
    parts = r.performance_parts(rec)
    assert parts[index].bank == bank
    assert parts[index].patch == (patch & 0xFF)


@given(
    bank=st.integers(min_value=0, max_value=11),
    patch=st.integers(min_value=0, max_value=255),
    index=st.integers(min_value=0, max_value=7),
)
@settings(max_examples=50, deadline=None)
def test_performance_part_bank_split_survives(
    bank: int, patch: int, index: int
) -> None:
    """Bank number split across bankNum and partMode survives round-trip."""
    rec = bytearray(r.PERFORMANCE_SIZE)
    r.set_performance_part(rec, index, bank=bank, patch=patch)
    parts = r.performance_parts(rec)
    assert parts[index].bank == bank


@given(
    index=st.integers(min_value=0, max_value=7),
    bank=st.integers(min_value=0, max_value=11),
    patch=st.integers(min_value=0, max_value=255),
    extra_mode_bits=st.integers(min_value=0, max_value=0x3F),
)
@settings(max_examples=50, deadline=None)
def test_performance_part_preserves_unrelated_mode_bits(
    index: int, bank: int, patch: int, extra_mode_bits: int
) -> None:
    """Rewriting a part leaves unrelated partMode settings alone."""
    rec = bytearray(r.PERFORMANCE_SIZE)
    at = r.PART_BASE + index * r.PART_SIZE + 4
    rec[at] = extra_mode_bits  # Set all non-bank bits
    r.set_performance_part(rec, index, bank=bank, patch=patch)
    # The bank bits are at positions 6-7, the rest should be preserved
    assert (rec[at] & 0x3F) == extra_mode_bits
    parts = r.performance_parts(rec)
    assert parts[index].bank == bank
    assert parts[index].patch == (patch & 0xFF)


# --- 5. Step link round-trip ---------------------------------------------------


@given(
    index=st.integers(min_value=0, max_value=500),
    forward=st.integers(min_value=0, max_value=0xFFFF),
    back=st.integers(min_value=0, max_value=0xFFFF),
    loop=st.integers(min_value=0, max_value=0xFFFF),
)
@settings(max_examples=100, deadline=None)
def test_step_links_round_trip(index: int, forward: int, back: int, loop: int) -> None:
    """set_step_links + step preserves link values."""
    bank = bytearray(r.WAVESEQ_BANK_SIZE)
    r.set_step_links(bank, index, forward=forward, back=back, loop=loop)
    step_info = r.step(bank, index)
    assert step_info["forward"] == (forward & 0xFFFF)
    assert step_info["back"] == (back & 0xFFFF)
    assert step_info["loop"] == (loop & 0xFFFF)


# --- 6. Dependency closure idempotence -----------------------------------------


def _make_source_with_waveseq(
    bank_id: str, sequences: dict[int, list[int]]
) -> deps.Source:
    """Create a source with a wave sequence bank."""
    bank = bytearray(r.WAVESEQ_BANK_SIZE)
    step = 1
    for seq, waves in sequences.items():
        head = r.SEQ_BASE + seq * r.SEQ_SIZE
        bank[head] = (step >> 8) & 0xFF
        bank[head + 1] = step & 0xFF
        for index, wave in enumerate(waves):
            at = r.STEP_BASE + step * r.STEP_SIZE
            bank[at + 7] = wave
            nxt = step + 1 if index + 1 < len(waves) else 0
            r.set_step_links(bank, step, forward=nxt, back=0, loop=0)
            step += 1
    return deps.Source(scope_id=bank_id, waveseq=bytes(bank))


@st.composite
def _sequences_and_seeds(draw):
    """Generate random wave sequences and seeds for quoted_closure testing."""
    seq_count = draw(st.integers(min_value=1, max_value=5))
    max_waves_per_seq = draw(st.integers(min_value=1, max_value=5))
    seed_count = draw(st.integers(min_value=1, max_value=5))

    sequences = {}
    for seq in range(seq_count):
        waves = []
        for i in range(max_waves_per_seq):
            # Wave can be ROM (>=32) or a sequence (<32)
            if seq_count > 1 and i % 3 == 0:
                waves.append(draw(st.integers(min_value=0, max_value=seq_count - 1)))
            else:
                waves.append(draw(st.integers(min_value=32, max_value=127)))
        sequences[seq] = waves

    seeds = []
    for _ in range(seed_count):
        seq = draw(st.integers(min_value=0, max_value=seq_count - 1))
        wave_bank = draw(st.integers(min_value=0, max_value=11))
        seeds.append(("A", wave_bank, seq))

    return sequences, seeds


@given(data=st.data())
@settings(max_examples=50, deadline=None)
def test_quoted_closure_idempotence(data: st.DataObject) -> None:
    """quoted_closure(quoted_closure(...)) == quoted_closure(...)."""
    sequences, seeds = data.draw(_sequences_and_seeds())

    source = _make_source_with_waveseq("A", sequences)
    sources = {"A": source}

    # First closure
    order1, chains1, unresolved1 = deps.quoted_closure(sources, seeds)
    # Second closure on the result
    order2, chains2, unresolved2 = deps.quoted_closure(sources, order1)

    assert order1 == order2
    assert set(chains1.keys()) == set(chains2.keys())
    assert unresolved1 == unresolved2


# --- 7. Walk sequence termination ----------------------------------------------


@given(
    chain_length=st.integers(min_value=0, max_value=20),
    has_loop=st.booleans(),
    has_quote=st.booleans(),
)
@settings(max_examples=50, deadline=None)
def test_walk_sequence_always_terminates(
    chain_length: int, has_loop: bool, has_quote: bool
) -> None:
    """walk_sequence always terminates on valid input (no infinite loops)."""
    bank = bytearray(r.WAVESEQ_BANK_SIZE)
    step = 1
    waves = []

    for i in range(chain_length):
        if has_quote and i == chain_length - 1 and chain_length > 0:
            waves.append(0)  # Quote sequence 0
        else:
            waves.append(40 + i)  # ROM wave

    # Build the chain
    bank[r.SEQ_BASE] = (step >> 8) & 0xFF
    bank[r.SEQ_BASE + 1] = step & 0xFF

    for i, wave in enumerate(waves):
        at = r.STEP_BASE + step * r.STEP_SIZE
        bank[at + 7] = wave
        if i + 1 < len(waves):
            nxt = step + 1
        elif has_loop and chain_length > 0:
            nxt = 1  # Loop back to first step
        else:
            nxt = 0
        r.set_step_links(bank, step, forward=nxt, back=0, loop=0)
        step += 1

    # This should terminate without hanging
    chain = deps.walk_sequence(bytes(bank), 0)
    assert len(chain.steps) <= r.STEPS_PER_BANK
    assert chain.cost == len(chain.steps)


@given(
    num_sequences=st.integers(min_value=1, max_value=5),
    max_chain_length=st.integers(min_value=1, max_value=10),
)
@settings(max_examples=50, deadline=None)
def test_walk_sequence_handles_circular_references(
    num_sequences: int, max_chain_length: int
) -> None:
    """Walk sequence handles circular references between sequences."""
    bank = bytearray(r.WAVESEQ_BANK_SIZE)
    step = 1

    for seq in range(num_sequences):
        head = r.SEQ_BASE + seq * r.SEQ_SIZE
        bank[head] = (step >> 8) & 0xFF
        bank[head + 1] = step & 0xFF

        # Each sequence quotes the next (circular)
        next_seq = (seq + 1) % num_sequences
        chain_len = (seq % max_chain_length) + 1

        for i in range(chain_len):
            at = r.STEP_BASE + step * r.STEP_SIZE
            if i == chain_len - 1:
                bank[at + 7] = next_seq  # Quote next sequence
            else:
                bank[at + 7] = 40 + i  # ROM wave
            nxt = step + 1 if i + 1 < chain_len else 0
            r.set_step_links(bank, step, forward=nxt, back=0, loop=0)
            step += 1

    # Walk all sequences - should terminate
    for seq in range(num_sequences):
        chain = deps.walk_sequence(bytes(bank), seq, scope_id="test")
        assert len(chain.steps) <= r.STEPS_PER_BANK


# --- 8. Structural digest stability --------------------------------------------


def _make_simple_patch(waves=((0, 40), (0, 40), (0, 40), (0, 40)), name="Test"):
    """Create a simple patch record."""
    rec = bytearray(r.PATCH_SIZE)
    rec[0 : len(name)] = name.encode()
    for i, (bank, wave) in enumerate(waves):
        r.set_patch_wave(rec, i, bank=bank, wave=wave)
    return bytes(rec)


def _make_simple_waveseq(chains: dict[int, list[int]]) -> bytes:
    """Create a simple wave sequence bank."""
    bank = bytearray(r.WAVESEQ_BANK_SIZE)
    step = 1
    for seq, waves in chains.items():
        head = r.SEQ_BASE + seq * r.SEQ_SIZE
        bank[head] = (step >> 8) & 0xFF
        bank[head + 1] = step & 0xFF
        for index, wave in enumerate(waves):
            at = r.STEP_BASE + step * r.STEP_SIZE
            bank[at + 7] = wave
            nxt = step + 1 if index + 1 < len(waves) else 0
            r.set_step_links(bank, step, forward=nxt, back=0, loop=0)
            step += 1
    return bytes(bank)


@given(
    wave_data=st.lists(
        st.integers(min_value=32, max_value=127), min_size=4, max_size=4
    ),
    name=st.text(
        alphabet=st.characters(min_codepoint=32, max_codepoint=126),
        min_size=0,
        max_size=16,
    ),
)
@settings(max_examples=50, deadline=None)
def test_structural_digest_stability_rom_waves(wave_data: list[int], name: str) -> None:
    """patch_structural_digest of same patch bytes is stable (ROM waves)."""
    patch = _make_simple_patch(waves=[(0, w) for w in wave_data], name=name)
    sources = {
        "RAM1": deps.Source(
            scope_id="RAM1", patches=[patch], waveseq=_make_simple_waveseq({})
        )
    }

    digest1 = rel.patch_structural_digest(patch, "RAM1", sources)
    digest2 = rel.patch_structural_digest(patch, "RAM1", sources)

    assert digest1 == digest2
    assert len(digest1) == 64  # SHA-256 hex


@given(
    seq_data=st.lists(
        st.lists(st.integers(min_value=32, max_value=127), min_size=1, max_size=5),
        min_size=1,
        max_size=4,
    ),
    name=st.text(
        alphabet=st.characters(min_codepoint=32, max_codepoint=126),
        min_size=0,
        max_size=16,
    ),
)
@settings(max_examples=50, deadline=None)
def test_structural_digest_stability_with_sequences(
    seq_data: list[list[int]], name: str
) -> None:
    """patch_structural_digest stable for patches using wave sequences."""
    # Create wave sequences
    chains = dict(enumerate(seq_data))
    waveseq = _make_simple_waveseq(chains)

    # Patch uses sequence 0 in wave bank 0
    patch = _make_simple_patch(waves=[(0, 0), (0, 40), (0, 40), (0, 40)], name=name)

    sources = {"RAM1": deps.Source(scope_id="RAM1", patches=[patch], waveseq=waveseq)}

    digest1 = rel.patch_structural_digest(patch, "RAM1", sources)
    digest2 = rel.patch_structural_digest(patch, "RAM1", sources)

    assert digest1 == digest2


@given(
    patch=st.binary(min_size=r.PATCH_SIZE, max_size=r.PATCH_SIZE),
)
@settings(max_examples=20, deadline=None)
def test_structural_digest_is_deterministic(patch: bytes) -> None:
    """Digest is deterministic for identical input bytes."""
    sources = {
        "RAM1": deps.Source(
            scope_id="RAM1", patches=[patch], waveseq=bytes(r.WAVESEQ_BANK_SIZE)
        )
    }

    digest1 = rel.patch_structural_digest(patch, "RAM1", sources)
    digest2 = rel.patch_structural_digest(patch, "RAM1", sources)

    assert digest1 == digest2


# --- 9. Bank number mapping consistency ----------------------------------------


@given(bank_index=st.integers(min_value=0, max_value=11))
@settings(max_examples=100, deadline=None)
def test_bank_by_number_by_sysex_consistency(bank_index: int) -> None:
    """bank.by_number(bank.by_sysex(...).number) ==
    bank.by_sysex(...) for valid banks."""
    bank_by_sysex = banks.by_sysex(bank_index)
    bank_by_number = banks.bank(bank_by_sysex.number)
    assert bank_by_number is bank_by_sysex


@given(bank_index=st.integers(min_value=0, max_value=11))
@settings(max_examples=100, deadline=None)
def test_bank_by_sysex_by_number_consistency(bank_index: int) -> None:
    """bank.by_sysex(bank.by_number(...).sysex) ==
    bank.by_number(...) for valid banks."""
    bank_by_number = banks.bank(bank_index)
    bank_by_sysex = banks.by_sysex(bank_by_number.sysex)
    assert bank_by_sysex is bank_by_number


@given(bank_index=st.integers(min_value=0, max_value=11))
@settings(max_examples=100, deadline=None)
def test_bank_id_consistency(bank_index: int) -> None:
    """Bank.id is consistent whether looked up by number or sysex."""
    bank1 = banks.bank(bank_index)
    bank2 = banks.by_sysex(bank1.sysex)
    assert bank1.id == bank2.id


@given(bank_index=st.integers(min_value=0, max_value=11))
@settings(max_examples=100, deadline=None)
def test_bank_writable_consistency(bank_index: int) -> None:
    """Bank.writable is consistent whether looked up by number or sysex."""
    bank1 = banks.bank(bank_index)
    bank2 = banks.by_sysex(bank1.sysex)
    assert bank1.writable == bank2.writable


@given(bank_index=st.integers(min_value=0, max_value=11))
@settings(max_examples=100, deadline=None)
def test_bank_midi_bank_consistency(bank_index: int) -> None:
    """Bank.midi_bank is consistent whether looked up by number or sysex."""
    bank1 = banks.bank(bank_index)
    bank2 = banks.by_sysex(bank1.sysex)
    assert bank1.midi_bank == bank2.midi_bank


@given(bank_index=st.integers(min_value=0, max_value=11))
@settings(max_examples=100, deadline=None)
def test_bank_program_base_consistency(bank_index: int) -> None:
    """Bank.program_base is consistent whether looked up by number or sysex."""
    bank1 = banks.bank(bank_index)
    bank2 = banks.by_sysex(bank1.sysex)
    assert bank1.program_base == bank2.program_base


# --- Additional property tests --------------------------------------------------


@given(data=st.binary(min_size=0, max_size=100))
@settings(max_examples=50, deadline=None)
def test_from_nibbles_drops_trailing_half_byte(data: bytes) -> None:
    """A trailing odd nibble is dropped."""
    # Add one extra nibble
    nibbles = m.to_nibbles(data) + bytes([0x01])
    decoded = m.from_nibbles(nibbles)
    assert decoded == data


@given(data=st.binary(min_size=1, max_size=50))
@settings(max_examples=50, deadline=None)
def test_from_nibbles_refuses_high_bit(data: bytes) -> None:
    """from_nibbles refuses bytes with bit 7 set."""
    nibbles = m.to_nibbles(data)
    # Corrupt one nibble to have high bit set
    if len(nibbles) > 1:
        bad_nibbles = bytearray(nibbles)
        bad_nibbles[0] = 0x80
        try:
            m.from_nibbles(bytes(bad_nibbles))
            raise AssertionError("Should have raised ProtocolError")
        except m.ProtocolError:
            pass


@given(
    bank=st.integers(min_value=0, max_value=11),
    patch=st.integers(min_value=0, max_value=34),
)
@settings(max_examples=50, deadline=None)
def test_slot_key_uniqueness(bank: int, patch: int) -> None:
    """Slot keys are unique for (bank, kind, number) triples."""
    slot1 = banks.slot(bank, banks.Kind.PATCH, patch)
    slot2 = banks.slot(bank, banks.Kind.PERFORMANCE, patch)
    assert slot1.key != slot2.key
    assert slot1 != slot2


@given(number=st.integers(min_value=0, max_value=31))
@settings(max_examples=50, deadline=None)
def test_multi_select_function_correct(number: int) -> None:
    """multi_select uses correct function code for number range."""
    frame = m.multi_select(number)
    if number <= 15:
        assert frame[4] == m.Function.MULTI_SELECT
    else:
        assert frame[4] == m.Function.MULTI_SELECT_SR


# --- Test for _active_patch_oscillators behavior (internal) --------------------


@given(
    name=st.sampled_from(["init", "init patch", "init patch.", "unnamed"]),
    num_waves=st.integers(min_value=0, max_value=4),
)
@settings(max_examples=50, deadline=None)
def test_active_patch_oscillators_init_name(name: str, num_waves: int) -> None:
    """Patches with 'init' names have no active oscillators."""
    rec = bytearray(r.PATCH_SIZE)
    rec[0 : len(name)] = name.encode()
    rec[r.PATCH_NUM_WAVES] = num_waves
    # All oscillators should be unused
    waves = r.patch_waves(rec)
    for w in waves:
        assert not w.used
        assert not w.is_sequence


# --- Test bank list completeness ------------------------------------------------


def test_all_twelve_banks_present() -> None:
    """All twelve banks are present with unique wire bytes."""
    assert len(banks.BANKS) == 12
    wire_bytes = [b.sysex for b in banks.BANKS]
    assert sorted(wire_bytes) == list(range(12))
    numbers = [b.number for b in banks.BANKS]
    assert sorted(numbers) == list(range(12))


def test_four_writable_banks() -> None:
    """Exactly four banks are writable."""
    writable = banks.writable_banks()
    assert len(writable) == 4
    labels = [b.label for b in writable]
    assert set(labels) == {"RAM 1", "RAM 2", "RAM 3", "Card"}


def test_bank_select_addressing() -> None:
    """Bank Select addressing matches documentation."""
    # RAM 1: midi_bank 0, program_base 0
    # RAM 2: midi_bank 0, program_base 50
    # RAM 3: midi_bank 2, program_base 0
    # ROM 4: midi_bank 2, program_base 50
    # ROM 5: midi_bank 3, program_base 0
    # ROM 6: midi_bank 3, program_base 50
    # ROM 7: midi_bank 4, program_base 0
    # ROM 8: midi_bank 4, program_base 50
    # ROM 9: midi_bank 5, program_base 0
    # ROM 10: midi_bank 5, program_base 50
    # ROM 11: midi_bank 1, program_base 0
    # Card: midi_bank 1, program_base 50
    expected = {
        "RAM 1": (0, 0),
        "RAM 2": (0, 50),
        "RAM 3": (2, 0),
        "ROM 4": (2, 50),
        "ROM 5": (3, 0),
        "ROM 6": (3, 50),
        "ROM 7": (4, 0),
        "ROM 8": (4, 50),
        "ROM 9": (5, 0),
        "ROM 10": (5, 50),
        "ROM 11": (1, 0),
        "Card": (1, 50),
    }
    for bank in banks.BANKS:
        assert (bank.midi_bank, bank.program_base) == expected[bank.label]


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-q", "--no-cov"])
