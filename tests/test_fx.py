from __future__ import annotations

import random
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from kws.fx import (
    EFFECT_TYPES,
    FX_SIZE,
    Effect,
    EffectParam,
    FxBlock,
)
from kws.fx_tables import (
    FX_GROUP,
    FX_INDICES,
    FX_PARAM_LABELS,
    FX_PCL_LIST,
    FX_PRESETS,
    FxPclEntry,
)


def test_type_and_group_counts():
    assert len(EFFECT_TYPES) == 56
    assert len(FX_GROUP) == 56
    assert len(FX_INDICES) == 31
    assert len(FX_PCL_LIST) == 31
    assert len(FX_PRESETS) == 56


def test_every_name_non_empty():
    for entry in FX_GROUP:
        assert entry.name.strip() != ""
    for et in EFFECT_TYPES:
        assert et.name.strip() != ""


def test_every_group_referenced():
    groups = {et.group for et in EFFECT_TYPES}
    assert groups == set(range(31))


def _assert_byte_bits_disjoint(group: int, entries: tuple[FxPclEntry, ...]) -> None:
    for entry in entries:
        assert 0 <= entry.bytenum <= 7
    by_byte: dict[int, list[Any]] = {}
    for entry in entries:
        by_byte.setdefault(entry.bytenum, []).append(entry)
    for byte, es in by_byte.items():
        for i, a in enumerate(es):
            mag_a = abs(a.bitlen)
            mask_a = ((1 << mag_a) - 1) << a.bit
            for b in es[i + 1 :]:
                mag_b = abs(b.bitlen)
                mask_b = ((1 << mag_b) - 1) << b.bit
                assert mask_a & mask_b == 0, (group, byte)


def _assert_parambit_ranges_disjoint(
    group: int, entries: tuple[FxPclEntry, ...]
) -> None:
    # Fields of one logical parameter must occupy disjoint parambit
    # ranges; otherwise decode ORs storage bits into the wrong bit
    # positions of the value.
    by_param: dict[int, list[Any]] = {}
    for entry in entries:
        by_param.setdefault(entry.paramnum, []).append(entry)
    for paramnum, pes in by_param.items():
        for i, a in enumerate(pes):
            a_lo, a_hi = a.parambit, a.parambit + abs(a.bitlen)
            for b in pes[i + 1 :]:
                b_lo, b_hi = b.parambit, b.parambit + abs(b.bitlen)
                assert max(a_lo, b_lo) >= min(a_hi, b_hi), (
                    group,
                    paramnum,
                )


def test_pcl_bytes_and_no_overlap():
    for group in range(31):
        entries = FX_PCL_LIST[group]
        _assert_byte_bits_disjoint(group, entries)
        _assert_parambit_ranges_disjoint(group, entries)


def test_rows_non_empty_and_unique_keys():
    for group in range(31):
        type_num = next(t.number for t in EFFECT_TYPES if t.group == group)
        eff = Effect(type=type_num, raw=bytes(8))
        rows = eff.rows()
        if group == 0:
            assert rows == ()
        else:
            assert len(rows) > 0
        keys = [r.key for r in rows]
        assert len(keys) == len(set(keys))


def _valid_block(b: bytes) -> bool:
    for i in (0, 1):
        bits = b[i] & 0x3F
        if bits < 2 or bits > 49:
            return False
        if bits == 2:
            raw = b[5:13] if i == 0 else b[13:21]
            ext = raw[0] & 0x0F
            if ext != 0 and not (1 <= ext <= 8):
                return False
    return True


@given(st.binary(min_size=FX_SIZE, max_size=FX_SIZE).filter(_valid_block))
def test_roundtrip_random(block: bytes) -> None:
    fb = FxBlock.from_bytes(block)
    assert fb.to_bytes() == block


def test_header_roundtrip():
    raw1 = bytes([1, 2, 3, 4, 5, 6, 7, 8])
    raw2 = bytes([8, 7, 6, 5, 4, 3, 2, 1])
    fb = FxBlock(
        series=True,
        fx1=Effect(type=5, raw=raw1),
        fx2=Effect(type=10, raw=raw2),
        mix=(3, 7),
        mod_source=(2, 9),
        mod_amount=(-15, 12),
    )
    out = fb.to_bytes()
    rt = FxBlock.from_bytes(out)
    assert rt.series is True
    assert rt.fx1.type == 5
    assert rt.fx2.type == 10
    assert rt.fx1.raw == raw1
    assert rt.fx2.raw == raw2
    assert rt.mix == (3, 7)
    assert rt.mod_source == (2, 9)
    assert rt.mod_amount == (-15, 12)
    assert rt.to_bytes() == out


def test_extended_types_roundtrip():
    for ext in range(48, 56):
        # High bits of parameter byte 0 are set to ensure they survive.
        raw = bytes([(ext - 47) | 0x80] + [0] * 7)
        block = bytearray(FX_SIZE)
        block[0] = 2  # extended marker
        block[1] = 2 | 0x80  # fx2 off, extended enable
        block[5:13] = raw
        block[13:21] = bytes(8)
        data = bytes(block)
        fb = FxBlock.from_bytes(data)
        assert fb.fx1.type == ext
        assert fb.to_bytes() == data
        assert fb.fx1.raw[0] & 0x80


def test_with_value_noop():
    for group in range(1, 31):
        type_num = next(t.number for t in EFFECT_TYPES if t.group == group)
        eff = Effect(type=type_num, raw=bytes(8))
        for row in eff.rows():
            try:
                val = eff.display(row)
            except ValueError:
                continue
            new = eff.with_value(row, val)
            assert new.raw == eff.raw, (group, row.key)


def test_performance_slice_roundtrip():
    block = FxBlock(
        series=False,
        fx1=Effect(type=0, raw=bytes(8)),
        fx2=Effect(type=0, raw=bytes(8)),
        mix=(0, 0),
        mod_source=(0, 0),
        mod_amount=(0, 0),
    ).to_bytes()
    record = bytes([0] * 16) + block + bytes([0] * (181 - 16 - 21))
    fb = FxBlock.from_bytes(record[16:37])
    new_block = fb.to_bytes()
    new_record = record[:16] + new_block + record[37:]
    assert new_record[:16] == record[:16]
    assert new_record[37:] == record[37:]
    assert len(new_record) == len(record)


@given(st.binary(min_size=FX_SIZE, max_size=FX_SIZE))
def test_roundtrip_arbitrary_bytes(block: bytes) -> None:
    assert FxBlock.from_bytes(block).to_bytes() == block


def test_fx_size():
    assert FX_SIZE == 21


class TestPclFieldAssembly:
    """One logical parameter can span several storage fields.

    Edisyn's PCL list has a high-bit entry and a sign entry per parameter;
    a model that made one row per entry would show "Pre Delay" twice and
    setting one row would not set the other's bits.
    """

    @staticmethod
    def _rows() -> Any:
        return Effect(type=1, raw=bytes(8)).rows()  # group 1, REVERB EQ

    def test_one_row_per_logical_parameter(self) -> None:
        rows = self._rows()
        keys = [row.key for row in rows]
        assert len(keys) == len(set(keys))
        delays = [row for row in rows if row.label == "Pre Delay"]
        assert len(delays) == 1
        assert len(delays[0].fields) == 2, "low 8 bits and the high bit"

    def test_a_high_bit_field_round_trips(self) -> None:
        row = next(row for row in self._rows() if row.label == "Pre Delay")
        effect = Effect(type=1, raw=bytes(8)).with_value(row, 300)
        assert effect.display(row) == 300, "a 9-bit value, not 8"

    def test_a_sign_field_round_trips(self) -> None:
        row = next(row for row in self._rows() if row.label == "Mix Mod Amount")
        effect = Effect(type=1, raw=bytes(8)).with_value(row, -7)
        assert effect.display(row) == -7
        assert effect.with_value(row, 7).display(row) == 7

    def test_setting_one_row_leaves_the_others(self) -> None:
        base = Effect(type=1, raw=bytes(8))
        rows = base.rows()
        target = next(row for row in rows if row.label == "Pre Delay")
        changed = base.with_value(target, 300)
        for row in rows:
            if row.key != target.key:
                assert changed.display(row) == base.display(row)


def _reverb_rows(effect: Effect) -> tuple[EffectParam, EffectParam]:
    rows = effect.rows()
    pre_delay = next(row for row in rows if row.label == "Reverb Pre-Delay")
    decay = next(row for row in rows if row.label == "Reverb Decay Time")
    return pre_delay, decay


def test_hall_room_predelay_high_bits_decode() -> None:
    # Groups 19/20 (types 39/40): the pre-delay high field sits at
    # parambit 8 above the 8-bit low field in byte 5.
    for type_num in (39, 40):
        effect = Effect(type=type_num, raw=bytes([0, 0, 0x04, 0, 0, 0x2C, 0, 0]))
        pre_delay, decay = _reverb_rows(effect)
        # NOTE: Effect.display range-checks against the 0..150/250 UI
        # label, so the raw 10-bit decode (44 + (1 << 8) == 300) is read
        # via _assemble; display would raise for this out-of-range value.
        assert effect._assemble(pre_delay) == 300
        assert effect.display(decay) == 0


def test_hall_room_predelay_high_bits_encode() -> None:
    # The pre-delay high field must land in byte 2 bits 2-3, leaving the
    # Delay Time bits 0-1 of the same byte untouched.
    for type_num in (39, 40):
        base = Effect(type=type_num, raw=bytes(8))
        pre_delay, _ = _reverb_rows(base)
        updated = base.with_value(pre_delay, 101)
        assert updated.raw[5] == 101
        assert updated.raw[2] == 0x00
        assert updated.display(pre_delay) == 101
        other = Effect(type=type_num, raw=bytes([0, 0, 0x03, 0, 0, 0, 0, 0]))
        other_pre, _ = _reverb_rows(other)
        assert other.with_value(other_pre, 101).raw[2] == 0x03


def test_empty_roundtrips_and_types_zero():
    from kws.fx import FxBlock

    fb = FxBlock.empty()
    assert fb.fx1.type == 0
    assert fb.fx2.type == 0
    assert FxBlock.from_bytes(fb.to_bytes()) == fb
    assert FxBlock.from_bytes(fb.to_bytes()).fx1.type == 0
    assert FxBlock.from_bytes(fb.to_bytes()).fx2.type == 0


def test_with_type_normal_to_extended_clears_stale_nibble():
    from kws.fx import FxBlock

    eff = Effect(type=1, raw=bytes([0x0C] + [0] * 7), stored_type_bits=3)
    b = FxBlock(
        series=False,
        fx1=eff.with_type(48),
        fx2=Effect(type=0, raw=bytes(8), stored_type_bits=2),
        mix=(0, 0),
        mod_source=(0, 0),
        mod_amount=(0, 0),
    ).to_bytes()
    assert FxBlock.from_bytes(b).fx1.type == 48


def test_with_type_extended_to_normal_clears_ext_nibble():
    from kws.fx import FxBlock

    eff = Effect(type=48, raw=bytes([0x81] + [0] * 7))
    switched = eff.with_type(1)
    assert switched.raw[0] & 0x0F == 0
    b = FxBlock(
        series=False,
        fx1=switched,
        fx2=Effect(type=0, raw=bytes(8), stored_type_bits=2),
        mix=(0, 0),
        mod_source=(0, 0),
        mod_amount=(0, 0),
    ).to_bytes()
    assert b[5] & 0x0F == 0


def test_standard_mode_fx1_nonzero_nibble_is_off():
    data = bytes(bytearray([2, 2, 0, 0, 0, 0x83, 0, 0, 0, 0, 0, 0, 0] + [0] * 8))
    fb = FxBlock.from_bytes(data)
    assert fb.extended_enable is False
    assert fb.fx1.type == 0
    assert fb.to_bytes() == data


def test_standard_mode_fx2_nonzero_nibble_is_off():
    data = bytes(bytearray([2, 2, 0, 0, 0] + [0] * 8 + [0x85, 0, 0, 0, 0, 0, 0, 0]))
    fb = FxBlock.from_bytes(data)
    assert fb.extended_enable is False
    assert fb.fx2.type == 0
    assert fb.to_bytes() == data


def test_extended_mode_resolves_both_processors():
    for nibble in (1, 5, 8):
        raw1 = bytes([nibble] + [0] * 7)
        raw2 = bytes([nibble] + [0] * 7)
        block = bytearray(FX_SIZE)
        block[0] = 2
        block[1] = 2 | 0x80
        block[5:13] = raw1
        block[13:21] = raw2
        data = bytes(block)
        fb = FxBlock.from_bytes(data)
        assert fb.extended_enable is True
        assert fb.fx1.type == 47 + nibble
        assert fb.fx2.type == 47 + nibble
        assert fb.to_bytes() == data


def test_footswitch_labels_present():
    expected = {
        (8, 7): ("Footswitch", 0, 1, None),
        (9, 9): ("Footswitch", 0, 1, None),
        (10, 7): ("Footswitch", 0, 1, None),
        (12, 9): ("Footswitch", 0, 1, None),
        (13, 9): ("Footswitch", 0, 1, None),
        (14, 6): ("Footswitch", 0, 1, None),
    }
    for key, want in expected.items():
        assert FX_PARAM_LABELS.get(key) == want, key


def test_footswitch_does_not_disturb_neighbours():
    assert FX_PARAM_LABELS[(8, 6)] == ("Delay Right", 0, 500, None)
    assert FX_PARAM_LABELS[(8, 8)] == ("EQ Low", -12, 12, None)
    assert FX_PARAM_LABELS[(9, 8)] == ("Delay Right", 0, 500, None)
    assert FX_PARAM_LABELS[(10, 6)] == ("Output Mix", -10, 10, None)
    assert FX_PARAM_LABELS[(10, 8)] == ("EQ Low", -12, 12, None)
    assert FX_PARAM_LABELS[(12, 8)] == ("Output Level", -15, 15, None)
    assert FX_PARAM_LABELS[(12, 10)] == ("EQ Low", -12, 12, None)
    assert FX_PARAM_LABELS[(13, 8)] == ("Feedback", -100, 100, None)
    assert FX_PARAM_LABELS[(14, 5)] == ("Acceleration", 1, 15, None)
