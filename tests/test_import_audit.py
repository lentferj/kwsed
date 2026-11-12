# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.

from kws import banks, deps
from kws import import_audit as ia
from kws import records as r
from tests.test_bankbuild import make_patch, make_performance, make_waveseq


def _source(
    scope_id="RAM1", bank_number=None, performances=None, patches=None, waveseq=None
):
    return deps.Source(
        scope_id=scope_id,
        bank_number=bank_number,
        performances=list(performances or []),
        patches=list(patches or []),
        waveseq=waveseq,
    )


def test_complete_source():
    seqs = make_waveseq({0: [40]})[0]
    perf = make_performance("P", parts=((0, 0),))
    patch = make_patch("P", waves=((0, 0), (0, 40), (0, 40), (0, 40)))
    src = _source(performances=[perf], patches=[patch], waveseq=seqs)
    audit = ia.audit_source(src)
    assert audit.complete
    assert audit.missing_patches == ()
    assert audit.missing_waveseqs == ()
    assert audit.referenced_patches == (0,)
    assert audit.referenced_waveseqs == (0,)


def test_missing_patch():
    perf = make_performance("P", parts=((0, 3),))
    src = _source(performances=[perf], patches=[])
    audit = ia.audit_source(src)
    assert audit.referenced_patches == (3,)
    assert audit.missing_patches == (3,)


def test_wrong_external_bank():
    perf = make_performance("P", parts=((1, 0),))
    patch = make_patch("P")
    src = _source(performances=[perf], patches=[patch])
    audit = ia.audit_source(src)
    assert audit.referenced_patches == ()
    assert audit.missing_patches == ()
    assert audit.complete


def test_missing_waveseq():
    patch = make_patch("P", waves=((0, 0), (0, 40), (0, 40), (0, 40)))
    perf = make_performance("P", parts=((0, 0),))
    src_none = _source(performances=[perf], patches=[patch], waveseq=None)
    audit_none = ia.audit_source(src_none)
    assert audit_none.missing_waveseqs == (0,)
    assert audit_none.referenced_waveseqs == (0,)
    seqs = make_waveseq({0: [40]})[0]
    src_present = _source(performances=[perf], patches=[patch], waveseq=seqs)
    audit_present = ia.audit_source(src_present)
    assert audit_present.complete


def test_empty_init_patch_missing():
    perf = make_performance("P", parts=((0, 0),))
    empty_patch = bytes(r.PATCH_SIZE)
    src = _source(performances=[perf], patches=[empty_patch])
    audit = ia.audit_source(src)
    assert audit.missing_patches == (0,)
    assert audit.referenced_patches == (0,)


def test_bank_number_alias():
    seqs = make_waveseq({0: [40]})[0]
    perf = make_performance("P", parts=((0, 0),))
    patch = make_patch("P", waves=((0, 0), (0, 40), (0, 40), (0, 40)))
    src = _source(
        scope_id="RAM1#2",
        bank_number=0,
        performances=[perf],
        patches=[patch],
        waveseq=seqs,
    )
    assert ia.source_bank_number(src) == 0
    audit = ia.audit_source(src)
    assert audit.complete


def test_summary():
    perf = make_performance("P", parts=((0, 3), (0, 5)))
    # missing patch slots 3,5 with no patches
    src = _source(performances=[perf], patches=[])
    audit = ia.audit_source(src)
    # missing patches 3,5
    assert "missing 2 patch slot(s): 03, 05" in audit.summary()
    # waveseq missing: patch doesn't exist so no seq referenced; add a separate scenario
    patch2 = make_patch("P2", waves=((0, 2), (0, 40), (0, 40), (0, 40)))
    perf2 = make_performance("P2", parts=((0, 0),))
    src2 = _source(performances=[perf2], patches=[patch2], waveseq=None)
    audit2 = ia.audit_source(src2)
    assert "missing 1 wave sequence(s): 02" in audit2.summary()
    # both
    perf3 = make_performance("P3", parts=((0, 0), (0, 3)))
    src3 = _source(performances=[perf3], patches=[patch2], waveseq=None)
    audit3 = ia.audit_source(src3)
    s = audit3.summary()
    assert "missing 1 patch slot(s): 03" in s
    assert "missing 1 wave sequence(s): 02" in s
    assert ";" in s
    # complete
    src4 = _source(performances=[], patches=[])
    assert ia.audit_source(src4).summary() == "no missing local dependencies"


def test_all_empty_performances():
    perfs = [bytes(r.PERFORMANCE_SIZE) for _ in range(50)]
    src = _source(performances=perfs, patches=[])
    audit = ia.audit_source(src)
    assert audit.performance_count == 50
    assert audit.complete
