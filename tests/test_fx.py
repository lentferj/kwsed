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
    FX_PCL_LIST,
    FX_PRESETS,
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


def test_pcl_bytes_and_no_overlap():
    for group in range(31):
        entries = FX_PCL_LIST[group]
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
        raw = bytes([ext - 47] + [0] * 7)
        block = bytearray(FX_SIZE)
        block[0] = 2  # extended marker
        block[1] = 2 | 0x80  # fx2 off, extended enable
        block[5:13] = raw
        block[13:21] = bytes(8)
        data = bytes(block)
        fb = FxBlock.from_bytes(data)
        assert fb.fx1.type == ext
        assert fb.to_bytes() == data


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


def test_invalid_type_byte_raises():
    block = bytearray(FX_SIZE)
    block[0] = 1  # invalid type bits
    block[1] = 2 | 0x80
    try:
        FxBlock.from_bytes(bytes(block))
    except ValueError as exc:
        assert "0" in str(exc) or "byte" in str(exc).lower()
    else:
        raise AssertionError("expected ValueError")


def test_fx_size():
    assert FX_SIZE == 21
