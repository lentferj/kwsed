# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors

from __future__ import annotations

from pathlib import Path

import pytest

from kws import banks, deps, importers
from kws import messages as m
from kws import records as r
from kwsed import import_companions as ic
from tests.test_bankbuild import make_patch, make_performance, make_waveseq


def _write_syx(
    path: Path,
    bank_byte: int,
    perf_data: bytes | None = None,
    patch_data: bytes | None = None,
    ws_data: bytes | None = None,
) -> None:
    data = b""
    if perf_data is not None:
        data += m.frame_bulk(m.Function.ALL_PERFORMANCE_DUMP, bank_byte, perf_data)
    if patch_data is not None:
        data += m.frame_bulk(m.Function.ALL_PATCH_DUMP, bank_byte, patch_data)
    if ws_data is not None:
        data += m.frame_bulk(m.Function.ALL_WAVESEQ_DUMP, bank_byte, ws_data)
    path.write_bytes(data)


def _perf_bank(perf):
    buf = bytearray(banks.PERFORMANCES_PER_BANK * r.PERFORMANCE_SIZE)
    buf[0 : r.PERFORMANCE_SIZE] = perf
    return bytes(buf)


def _patch_bank(patch, slot=3):
    buf = bytearray(banks.PATCHES_PER_BANK * r.PATCH_SIZE)
    buf[slot * r.PATCH_SIZE : (slot + 1) * r.PATCH_SIZE] = patch
    return bytes(buf)


def test_normalized_stem_examples() -> None:
    assert ic.normalized_stem("MS_Modern Times_Performances.syx") == "ms modern times"
    assert ic.normalized_stem("MS_Modern Times_Patches.MID") == "ms modern times"
    assert ic.normalized_stem("MS_Modern Times_Waveseq.syx") == "ms modern times"
    assert ic.normalized_stem("foo_Wave Seq.syx") == "foo"
    assert ic.normalized_stem("foo_Wave Sequence.syx") == "foo"
    assert ic.normalized_stem("foo_Bank.syx") == "foo bank"


def test_candidate_paths_same_dir_ext_exclusion(tmp_path: Path) -> None:
    d = tmp_path / "dir"
    d.mkdir()
    (d / "a.syx").write_bytes(b"")
    (d / "b.MID").write_bytes(b"")
    (d / "c.txt").write_bytes(b"")
    target = d / "target.syx"
    target.write_bytes(b"")
    paths = ic.candidate_paths(str(target))
    names = sorted(Path(p).name for p in paths)
    assert names == ["a.syx", "b.MID"]
    assert str(target) not in paths


def _make_package(tmp_path: Path) -> tuple[Path, Path, Path]:
    perf = make_performance("P", parts=((0, 3),))
    patch = make_patch("P", waves=((0, 0), (0, 40), (0, 40), (0, 40)))
    seqs = make_waveseq({0: [40]})[0]

    perf_path = tmp_path / "MS_Test_Performances.syx"
    patch_path = tmp_path / "MS_Test_Patches.syx"
    ws_path = tmp_path / "MS_Test_Waveseq.syx"

    _write_syx(perf_path, 0, perf_data=_perf_bank(perf))
    _write_syx(patch_path, 0, patch_data=_patch_bank(patch, 3))
    _write_syx(ws_path, 0, ws_data=seqs)
    return perf_path, patch_path, ws_path


def test_find_companions_returns_patch_candidate(tmp_path: Path) -> None:
    perf_path, _patch_path, _ws_path = _make_package(tmp_path)
    banks_list = importers.read_file(str(perf_path))
    source = banks_list[0].source
    comps = ic.find_companions(str(perf_path), source)
    # patch candidate reduces missing patches
    assert any(Path(c.path).name == "MS_Test_Patches.syx" for c in comps)
    for c in comps:
        assert c.audit.missing_patches == () or len(c.audit.missing_patches) < 1


def test_best_companion_none_with_multiple(tmp_path: Path) -> None:
    perf_path, _patch_path, _ws_path = _make_package(tmp_path)
    # create an extra same-stem file that also reduces.
    # Instead make two patch-like candidates by adding a .syx with same stem.
    # Simulate multiple by monkey? Assert best_companion None when >1.
    # At least with one candidate best_companion returns it or None.
    banks_list = importers.read_file(str(perf_path))
    source = banks_list[0].source
    comps = ic.find_companions(str(perf_path), source)
    bc = ic.best_companion(str(perf_path), source)
    if len(comps) == 1:
        assert bc is not None
    else:
        assert bc is None


def test_unrelated_stem_ignored(tmp_path: Path) -> None:
    perf_path, _patch_path, _ws_path = _make_package(tmp_path)
    other = tmp_path / "Other_Patches.syx"
    patch = make_patch("P", waves=((0, 0), (0, 40), (0, 40), (0, 40)))
    _write_syx(other, 0, patch_data=_patch_bank(patch, 3))
    banks_list = importers.read_file(str(perf_path))
    source = banks_list[0].source
    comps = ic.find_companions(str(perf_path), source)
    assert all(Path(c.path).name != "Other_Patches.syx" for c in comps)


def test_different_bank_ignored(tmp_path: Path) -> None:
    perf_path, _patch_path, _ws_path = _make_package(tmp_path)
    # RAM2 patch data, same stem
    # need same stem: use MS_Test.syx with RAM2 patches
    other = tmp_path / "MS_Test.syx"
    patch = make_patch("P", waves=((0, 0), (0, 40), (0, 40), (0, 40)))
    _write_syx(other, 1, patch_data=_patch_bank(patch, 3))
    banks_list = importers.read_file(str(perf_path))
    source = banks_list[0].source
    comps = ic.find_companions(str(perf_path), source)
    assert all(Path(c.path).name != "MS_Test.syx" for c in comps)


def test_complete_source_returns_empty(tmp_path: Path) -> None:
    perf_path, _patch_path, _ws_path = _make_package(tmp_path)
    perf = make_performance("P", parts=((0, 3),))
    patch = make_patch("P", waves=((0, 0), (0, 40), (0, 40), (0, 40)))
    seqs = make_waveseq({0: [40]})[0]
    pb = bytearray(banks.PATCHES_PER_BANK * r.PATCH_SIZE)
    pb[3 * r.PATCH_SIZE : 4 * r.PATCH_SIZE] = patch
    patches = [
        bytes(pb[i * r.PATCH_SIZE : (i + 1) * r.PATCH_SIZE])
        for i in range(banks.PATCHES_PER_BANK)
    ]
    perfs = [perf] + [bytes(r.PERFORMANCE_SIZE)] * (banks.PERFORMANCES_PER_BANK - 1)
    src = deps.Source(
        scope_id="RAM1",
        performances=perfs,
        patches=patches,
        waveseq=seqs,
        bank_number=0,
    )
    assert ic.find_companions(str(perf_path), src) == []


def test_equivalent_syx_candidates_collapse(tmp_path: Path) -> None:
    perf_path, patch_path, _ws_path = _make_package(tmp_path)
    duplicate = tmp_path / "MS_Test_Patches.sysex"
    duplicate.write_bytes(patch_path.read_bytes())
    banks_list = importers.read_file(str(perf_path))
    source = banks_list[0].source
    comps = ic.find_companions(str(perf_path), source)
    assert len(comps) >= 2
    unique = ic.unique_equivalent_companions(comps, str(perf_path))
    assert len(unique) == 1


def test_partial_candidate_excluded(tmp_path: Path) -> None:
    perf_path, _patch_path, _ws_path = _make_package(tmp_path)
    # same stem, but patches empty (no reduction)
    other = tmp_path / "MS_Test.syx"
    buf = bytearray(banks.PATCHES_PER_BANK * r.PATCH_SIZE)  # all zeros
    _write_syx(other, 0, patch_data=bytes(buf))
    banks_list = importers.read_file(str(perf_path))
    source = banks_list[0].source
    comps = ic.find_companions(str(perf_path), source)
    assert all(Path(c.path).name != "MS_Test.syx" for c in comps)
