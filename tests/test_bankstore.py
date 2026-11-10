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

"""Bank store cache: save/load/merge/corrupt tolerance."""

import base64
import json
import os

import pytest

from kws import banks, bankstore, deps, favorites
from kws import records as r


def _perf_blob():
    return bytes(banks.PERFORMANCES_PER_BANK * r.PERFORMANCE_SIZE)


def _patch_blob():
    return bytes(banks.PATCHES_PER_BANK * r.PATCH_SIZE)


def _wave_blob():
    return bytes(r.WAVESEQ_BANK_SIZE)


class TestRoundTrip:
    def test_round_trip(self, tmp_path):
        d = str(tmp_path)
        for scope, filename in (
            ("RAM1", "RAM1.json"),
            ("CARD/07", "CARD__07.json"),
            ("CARD/c1", "CARD__c1.json"),
        ):
            perf = _perf_blob()
            patch = _patch_blob()
            wave = _wave_blob()
            bankstore.save(
                scope,
                performances=perf,
                patches=patch,
                waveseq=wave,
                directory=d,
            )
            assert filename in os.listdir(d)
            src = bankstore.load(scope, directory=d)
            assert src is not None
            assert src.bank_id == scope
            assert len(src.performances) == banks.PERFORMANCES_PER_BANK
            assert len(src.patches) == banks.PATCHES_PER_BANK
            assert src.waveseq == wave
            assert all(len(p) == r.PERFORMANCE_SIZE for p in src.performances)
            assert all(len(p) == r.PATCH_SIZE for p in src.patches)


class TestMerge:
    def test_partial_kinds_merge(self, tmp_path):
        d = str(tmp_path)
        scope = "RAM1"
        bankstore.save(scope, performances=_perf_blob(), directory=d)
        bankstore.save(scope, patches=_patch_blob(), directory=d)
        bankstore.save(scope, waveseq=_wave_blob(), directory=d)
        src = bankstore.load(scope, directory=d)
        assert src is not None
        assert len(src.performances) == banks.PERFORMANCES_PER_BANK
        assert len(src.patches) == banks.PATCHES_PER_BANK
        assert src.waveseq is not None

    def test_patches_alone_do_not_erase(self, tmp_path):
        d = str(tmp_path)
        scope = "RAM1"
        bankstore.save(
            scope,
            performances=_perf_blob(),
            waveseq=_wave_blob(),
            directory=d,
        )
        bankstore.save(scope, patches=_patch_blob(), directory=d)
        src = bankstore.load(scope, directory=d)
        assert src is not None
        assert len(src.performances) == banks.PERFORMANCES_PER_BANK
        assert src.waveseq is not None
        assert len(src.patches) == banks.PATCHES_PER_BANK


class TestWrongSize:
    def test_performances_one_byte_short(self, tmp_path):
        d = str(tmp_path)
        scope = "RAM1"
        path = os.path.join(d, "RAM1.json")
        with pytest.raises(ValueError):
            bankstore.save(
                scope,
                performances=bytes(
                    banks.PERFORMANCES_PER_BANK * r.PERFORMANCE_SIZE - 1
                ),
                directory=d,
            )
        assert not os.path.exists(path)

    def test_patches_one_byte_short(self, tmp_path):
        d = str(tmp_path)
        scope = "RAM1"
        path = os.path.join(d, "RAM1.json")
        with pytest.raises(ValueError):
            bankstore.save(
                scope,
                patches=bytes(banks.PATCHES_PER_BANK * r.PATCH_SIZE - 1),
                directory=d,
            )
        assert not os.path.exists(path)

    def test_waveseq_one_byte_short(self, tmp_path):
        d = str(tmp_path)
        scope = "RAM1"
        path = os.path.join(d, "RAM1.json")
        with pytest.raises(ValueError):
            bankstore.save(
                scope,
                waveseq=bytes(r.WAVESEQ_BANK_SIZE - 1),
                directory=d,
            )
        assert not os.path.exists(path)

    def test_wrong_size_does_not_overwrite_existing(self, tmp_path):
        d = str(tmp_path)
        scope = "RAM1"
        bankstore.save(scope, performances=_perf_blob(), directory=d)
        path = os.path.join(d, "RAM1.json")
        mtime_before = os.path.getmtime(path)
        try:
            bankstore.save(
                scope,
                patches=bytes(banks.PATCHES_PER_BANK * r.PATCH_SIZE - 1),
                directory=d,
            )
        except ValueError:
            pass
        assert os.path.getmtime(path) == mtime_before


class TestCorrupt:
    def test_not_json(self, tmp_path, capsys):
        d = str(tmp_path)
        scope = "RAM1"
        path = os.path.join(d, "RAM1.json")
        os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        assert bankstore.load(scope, directory=d) is None
        assert "ignoring" in capsys.readouterr().err

    def test_bad_base64(self, tmp_path, capsys):
        d = str(tmp_path)
        scope = "RAM1"
        path = os.path.join(d, "RAM1.json")
        os.makedirs(d, exist_ok=True)

        env = {
            "version": 1,
            "scope": scope,
            "performances": "!!!!",
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(env, f)
        assert bankstore.load(scope, directory=d) is None
        assert "ignoring" in capsys.readouterr().err

    def test_wrong_decoded_length(self, tmp_path, capsys):
        d = str(tmp_path)
        scope = "RAM1"
        path = os.path.join(d, "RAM1.json")
        os.makedirs(d, exist_ok=True)
        env = {
            "version": 1,
            "scope": scope,
            "performances": base64.b64encode(b"\x00" * 10).decode("ascii"),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(env, f)
        assert bankstore.load(scope, directory=d) is None
        assert "ignoring" in capsys.readouterr().err


class TestLoadAllAndHasComplete:
    def test_load_all_skips_corrupt(self, tmp_path):
        d = str(tmp_path)
        bankstore.save("RAM1", performances=_perf_blob(), directory=d)
        # corrupt file
        path = os.path.join(d, "BAD.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        result = bankstore.load_all(directory=d)
        assert "RAM1" in result
        assert len(result) == 1

    def test_has_complete_bank(self, tmp_path):
        d = str(tmp_path)
        scope = "RAM1"
        assert bankstore.has_complete_bank(scope, directory=d) is False
        bankstore.save(scope, performances=_perf_blob(), directory=d)
        assert bankstore.has_complete_bank(scope, directory=d) is False
        bankstore.save(scope, patches=_patch_blob(), directory=d)
        assert bankstore.has_complete_bank(scope, directory=d) is True

    def test_load_never_saved(self, tmp_path):
        d = str(tmp_path)
        assert bankstore.load("NOPE", directory=d) is None


class TestAtomic:
    def test_no_tmp_left(self, tmp_path):
        d = str(tmp_path)
        bankstore.save("RAM1", performances=_perf_blob(), directory=d)
        assert not any(p.endswith(".tmp") for p in os.listdir(d))


class TestDefaultDir:
    def test_default_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(favorites, "data_dir", lambda: str(tmp_path))
        assert bankstore.default_dir() == os.path.join(str(tmp_path), "banks")
