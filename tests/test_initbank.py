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
# WARRANTY; without even the implied warranty of MERCHANTABILITY OR FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.

"""The embedded Init bank: sizes, identical records and the part layout.

The records are the unit's own factory-initialised state, carried as bytes
literals. These tests are synthetic: nothing here opens a file.
"""

import hashlib
import inspect

from kws import dumps, initbank, records


def _records(bank: bytes, size: int) -> tuple[bytes, ...]:
    """Split ``bank`` into its fixed-size records."""
    return tuple(bank[at : at + size] for at in range(0, len(bank), size))


def test_the_records_digests_are_pinned() -> None:
    """The two embedded records are exactly the bytes T0 transcribed.

    The literals were read once from the unit and are not to drift: a
    future edit that changes either record changes its digest, and this
    test refuses it. The prefixes are the digests reported by the T0
    extraction (2026-11-16) -- performance ``769d9575…``, patch
    ``79a5cec5…``.
    """
    assert hashlib.sha256(initbank.INIT_PERFORMANCE).hexdigest()[:8] == "769d9575", (
        "the Init performance record changed"
    )
    assert hashlib.sha256(initbank.INIT_PATCH).hexdigest()[:8] == "79a5cec5", (
        "the Init patch record changed"
    )


def test_record_and_bank_sizes() -> None:
    assert len(initbank.INIT_PERFORMANCE) == 181
    assert len(initbank.INIT_PATCH) == 426
    assert len(initbank.init_performances()) == 9050
    assert len(initbank.init_patches()) == 14910
    assert len(initbank.init_waveseq()) == 8784


def test_every_performance_is_the_same_init_record() -> None:
    records_in_bank = _records(initbank.init_performances(), 181)
    assert len(records_in_bank) == 50
    assert all(record == initbank.INIT_PERFORMANCE for record in records_in_bank)


def test_every_patch_is_the_same_init_record() -> None:
    patches_in_bank = _records(initbank.init_patches(), 426)
    assert len(patches_in_bank) == 35
    assert all(record == initbank.INIT_PATCH for record in patches_in_bank)


def test_both_records_are_named_init() -> None:
    assert dumps.record_name(initbank.INIT_PERFORMANCE) == "Init"
    assert dumps.record_name(initbank.INIT_PATCH) == "Init"


def test_part_zero_is_used_and_the_rest_are_unused() -> None:
    parts = records.performance_parts(initbank.INIT_PERFORMANCE)
    assert len(parts) == 8
    assert (parts[0].bank, parts[0].patch) == (0, 0)
    assert parts[0].used
    for part in parts[1:]:
        assert part.patch == records.UNUSED_PATCH
        assert not part.used


def test_wave_sequence_bank_is_zeroed() -> None:
    waveseq = initbank.init_waveseq()
    assert len(waveseq) == records.WAVESEQ_BANK_SIZE
    assert not any(waveseq)


def test_cleared_bank_holds_the_three_parts() -> None:
    performances, patches, waveseq = initbank.cleared_bank()
    assert len(performances) == 9050
    assert len(patches) == 14910
    assert len(waveseq) == 8784


def test_the_bank_is_built_in_not_read_from_a_path() -> None:
    """Every builder takes no argument, so there is no path to read."""
    builders = (
        initbank.init_performances,
        initbank.init_patches,
        initbank.init_waveseq,
        initbank.cleared_bank,
    )
    for builder in builders:
        assert inspect.signature(builder).parameters == {}
    assert isinstance(initbank.init_performances(), bytes)
    assert isinstance(initbank.init_patches(), bytes)
    assert isinstance(initbank.init_waveseq(), bytes)
    assert isinstance(initbank.cleared_bank(), tuple)


def test_public_names_are_exported() -> None:
    for name in (
        "INIT_PATCH",
        "INIT_PERFORMANCE",
        "cleared_bank",
        "init_patches",
        "init_performances",
        "init_waveseq",
    ):
        assert name in initbank.__all__
