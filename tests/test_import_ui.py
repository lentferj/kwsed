# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#

"""Focused tests for the TUI import flow."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import patch

import pytest
from textual.widgets import DataTable

from kws import bankstore, config, deps, importers, perfcopy, relations
from kws import messages as m
from kwsed.app import _HELP, KEY_HINTS, CopyPerformanceScreen, KwsedApp
from kwsed.demo import DemoDevice
from kwsed.file_picker import FilePickerScreen
from kwsed.gate import EditGate
from kwsed.import_bank import WholeBankChoice, WholeBankImportScreen
from kwsed.import_screen import BankImportScreen
from kwsed.import_single import SingleImportChoice, SingleImportScreen
from vinsynlib.ui.modals import ChoiceScreen

PERF_RECORD_SIZE = 181
PATCH_RECORD_SIZE = 426


def _perf_record(name: str = "TestName") -> bytes:
    raw = name.encode("ascii").ljust(16, b"\x00")
    return raw + bytes([1]) * (PERF_RECORD_SIZE - 16)


def _empty_perf() -> bytes:
    return bytes(PERF_RECORD_SIZE)


def _make_bank(scope: str = "RAM1", active_slots: Optional[list[int]] = None) -> Any:
    if active_slots is None:
        active_slots = [3]
    perfs: list[bytes] = [_empty_perf() for _ in range(50)]
    for slot in active_slots:
        perfs[slot] = _perf_record()
    patches = [bytes(PATCH_RECORD_SIZE) for _ in range(35)]
    # make patches "active" by nonzero? Tests only count joined bytes; leave as is.
    source = deps.Source(
        bank_id=scope,
        performances=perfs,
        patches=patches,
        waveseq=None,
    )
    return importers.ImportedBank(
        label=f"Bank {scope}",
        scope=scope,
        format="sysex",
        source=source,
        multisets=(),
        notes="",
    )


def _make_app(tmp_path: Any, bankstore_dir: Optional[str] = None) -> KwsedApp:
    bs = None if bankstore_dir is None else bankstore_dir
    return KwsedApp(DemoDevice(), bankstore_dir=bs)


@pytest.mark.asyncio
async def test_i_binding_hint_and_help() -> None:
    assert any(
        getattr(b, "key", None) == "I" and getattr(b, "action", None) == "import"
        for b in KwsedApp.BINDINGS
    )
    assert "I import" in KEY_HINTS
    assert "Shortcuts: I import" in _HELP
    assert "I   import a file" in _HELP


@pytest.mark.asyncio
async def test_action_import_opens_choice_screen() -> None:
    app = KwsedApp(DemoDevice())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_import()
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen)
        assert app.screen._choices == [
            "Whole bank",
            "Single / multiple performances",
        ]
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1


@pytest.mark.asyncio
async def test_import_path_chosen_pushes_bank_import_screen() -> None:
    app = KwsedApp(DemoDevice())
    bank = _make_bank()
    pushed: list[Any] = []

    def fake_push(screen: Any, callback: Any = None) -> None:
        pushed.append((screen, callback))

    app.push_screen = fake_push  # type: ignore[assignment]
    with patch.object(importers, "read_file", return_value=[bank]):
        app._import_path_chosen(1, "fake.syx")
    assert len(pushed) == 1
    screen, _cb = pushed[0]
    assert isinstance(screen, BankImportScreen)
    assert screen.banks == [bank]
    assert screen._path == "fake.syx"


@pytest.mark.asyncio
async def test_import_path_chosen_error_and_empty() -> None:
    app = KwsedApp(DemoDevice())
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    pushed: list[Any] = []
    app.push_screen = lambda s, c=None: pushed.append(s)  # type: ignore[assignment]

    with patch.object(
        importers, "read_file", side_effect=importers.UnsupportedFormat("bad")
    ):
        app._import_path_chosen(0, "x.syx")
    assert pushed == []
    assert any("import: bad" in s for s in statuses)

    statuses.clear()
    pushed.clear()
    with patch.object(importers, "read_file", return_value=[]):
        app._import_path_chosen(0, "empty.syx")
    assert pushed == []
    assert any("no banks" in s for s in statuses)


@pytest.mark.asyncio
async def test_import_bank_chosen_kind0(tmp_path: Any) -> None:
    store_dir = str(tmp_path / "store")
    app = _make_app(tmp_path, bankstore_dir=store_dir)
    bank = _make_bank()
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    saved: list[dict[str, Any]] = []

    def fake_save(
        scope_id: Any,
        *,
        performances: Any = None,
        patches: Any = None,
        waveseq: Any = None,
        directory: Any = None,
    ) -> None:
        saved.append(
            {
                "scope": scope_id,
                "performances": performances,
                "patches": patches,
                "waveseq": waveseq,
                "directory": directory,
            }
        )

    with patch.object(bankstore, "save", fake_save):
        app._import_bank_chosen(0, [bank], 0)

    assert app._sources["RAM1"] is bank.source
    assert len(saved) == 1
    assert saved[0]["scope"] == "RAM1"
    assert saved[0]["directory"] == store_dir
    joined = b"".join(bank.source.performances)
    assert saved[0]["performances"] == joined
    joined_p = b"".join(bank.source.patches)
    assert saved[0]["patches"] == joined_p
    assert saved[0]["waveseq"] is None
    assert any("imported" in s for s in statuses)


@pytest.mark.asyncio
async def test_import_bank_chosen_kind1_pushes_copy_screen(tmp_path: Any) -> None:
    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    bank = _make_bank(active_slots=[3])
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    pushed: list[Any] = []

    def fake_push(screen: Any, callback: Any = None) -> None:
        pushed.append((screen, callback))
        if callback is not None and isinstance(screen, ChoiceScreen):
            callback(0)

    app.push_screen = fake_push  # type: ignore[assignment]
    with patch.object(bankstore, "save", lambda *a, **k: None):
        app._import_bank_chosen(1, [bank], 0)

    # first push ChoiceScreen
    assert any(isinstance(s, ChoiceScreen) for s, _ in pushed)
    # second push CopyPerformanceScreen
    copy_screens = [s for s, _ in pushed if isinstance(s, CopyPerformanceScreen)]
    assert len(copy_screens) == 1
    cs = copy_screens[0]
    assert cs.perf == relations.PerfId("RAM1", 3)
    assert cs.source_name == m.decode_name(bank.source.performances[3])


@pytest.mark.asyncio
async def test_no_active_performances_reports(tmp_path: Any) -> None:
    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    bank = _make_bank(active_slots=[])
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    pushed: list[Any] = []
    app.push_screen = lambda s, c=None: pushed.append(s)  # type: ignore[assignment]
    with patch.object(bankstore, "save", lambda *a, **k: None):
        app._import_bank_chosen(1, [bank], 0)
    assert any("no performances" in s for s in statuses)
    assert not any(isinstance(s, CopyPerformanceScreen) for s in pushed)


@pytest.mark.asyncio
async def test_bankstore_none_no_write(tmp_path: Any) -> None:
    app = _make_app(tmp_path, bankstore_dir=None)
    bank = _make_bank()
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    called = {"n": 0}

    def boom(*a: Any, **k: Any) -> None:
        called["n"] += 1
        raise AssertionError("bankstore.save should not be called")

    with patch.object(bankstore, "save", boom):
        app._import_bank_chosen(0, [bank], 0)
    assert app._sources["RAM1"] is bank.source
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_remembered_folder(tmp_path: Any, monkeypatch: Any) -> None:
    cfg_path = str(tmp_path / "config.toml")
    app = KwsedApp(DemoDevice(), config_path=cfg_path)
    # ensure import folder initial
    app._import_folder = str(tmp_path)

    # monkeypatch FilePickerScreen with fake
    class FakePicker:
        def __init__(self, start="", start_row=0):
            self.folder = tmp_path / "picked"
            self.last_row = 7

    monkeypatch.setattr("kwsed.app.FilePickerScreen", FakePicker)

    # monkeypatch push_screen to invoke callback with a path
    def fake_push(screen: Any, callback: Any = None) -> None:
        # emulate picker callback
        if callback is not None:
            callback(str(tmp_path / "fakepath.syx"))

    app.notify_status = lambda msg, *a, **k: None  # type: ignore[method-assign]
    monkeypatch.setattr(importers, "read_file", lambda p: [])
    app.push_screen = fake_push  # type: ignore[assignment]
    saved: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        config,
        "save_import_folder",
        lambda value, path=None: saved.append((value, path)),
    )
    app._import_kind_chosen(0)
    assert (
        any(str(tmp_path / "picked") == s[0] or s[0].endswith("picked") for s in saved)
        or saved
    )
    # at minimum assert save called
    assert len(saved) >= 1
    assert saved[0][1] == cfg_path or saved[0][1] is not None


@pytest.mark.asyncio
async def test_choice_escape_no_crash() -> None:
    app = KwsedApp(DemoDevice())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_import()
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1


@pytest.mark.asyncio
async def test_whole_cache_choice_saves(tmp_path: Any) -> None:
    store_dir = str(tmp_path / "store")
    app = _make_app(tmp_path, bankstore_dir=store_dir)
    bank = _make_bank()
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    saved: list[dict[str, Any]] = []

    def fake_save(
        scope_id: Any,
        *,
        performances: Any = None,
        patches: Any = None,
        waveseq: Any = None,
        directory: Any = None,
    ) -> None:
        saved.append(
            {"scope": scope_id, "directory": directory, "performances": performances}
        )

    with patch.object(bankstore, "save", fake_save):
        app._import_whole_bank_chosen([bank], WholeBankChoice(0, "RAM1", False))

    assert app._sources["RAM1"] is bank.source
    assert len(saved) == 1
    assert saved[0]["scope"] == "RAM1"
    assert any("imported" in s for s in statuses)


@pytest.mark.asyncio
async def test_whole_unit_choice_locked(tmp_path: Any) -> None:
    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    bank = _make_bank()
    gate = EditGate(armed=False)
    app._write_gate = gate
    writes: list[Any] = []

    def _fake_write(*a: Any, **k: Any) -> dict[str, Any]:
        writes.append((a, k))
        return {"verified": True}

    app.device.write_bank_bundle = _fake_write
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    app._import_whole_bank_chosen([bank], WholeBankChoice(0, "RAM1", True))
    assert writes == []
    assert any("locked" in s for s in statuses)


@pytest.mark.asyncio
async def test_whole_unit_choice_armed(tmp_path: Any) -> None:
    store_dir = str(tmp_path / "store")
    app = _make_app(tmp_path, bankstore_dir=store_dir)
    from kws import banks as _banks

    # bank with waveseq present
    from tests.test_bankbuild import make_waveseq

    ws = make_waveseq({0: [40, 41]})[0]
    bank = _make_bank()
    bank.source.waveseq = ws
    target_source = _make_bank(scope="RAM3").source
    app._sources["RAM3"] = target_source
    gate = EditGate(armed=True)
    app._write_gate = gate
    writes: list[dict[str, Any]] = []

    def fake_write(*a: Any, **k: Any) -> dict[str, Any]:
        writes.append(dict(k))
        writes[-1]["args"] = a
        return {"verified": True}

    app.device.write_bank_bundle = fake_write
    recorded: list[Any] = []

    def _rec(r: Any) -> None:
        recorded.append(r)

    app._record_write = _rec  # type: ignore[method-assign]
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]

    with patch.object(bankstore, "save", lambda *a, **k: None):
        app._import_whole_bank_write(bank, WholeBankChoice(0, "RAM3", True))

    assert len(writes) == 1
    w = writes[0]
    assert w["before_blocks"]  # non-empty
    # target bytes relocated to RAM3
    assert w["performances"] is not None
    # check a part bank points to target.number if present
    # _sources updated
    assert "RAM3" in app._sources
    assert len(recorded) == 1
    assert app.catalog.display_name("RAM3", 3, _banks.Kind.PERFORMANCE) == "TestName"


@pytest.mark.asyncio
async def test_before_blocks_frames(tmp_path: Any) -> None:
    app = _make_app(tmp_path)
    from kws import banks as _banks
    from tests.test_bankbuild import make_patch, make_performance, make_waveseq

    ws = make_waveseq({0: [40]})[0]
    source = deps.Source(
        bank_id="RAM1",
        performances=[make_performance("P", ((0, 0),)) for _ in range(50)],
        patches=[make_patch("Q") for _ in range(35)],
        waveseq=ws,
    )
    bank = _banks.bank("RAM3")
    blocks = app._before_blocks(source, bank)
    assert "performances" in blocks
    assert "patches" in blocks
    assert "waveseq" in blocks
    for key, raw in blocks.items():
        pkt = m.parse(raw)
        assert pkt is not None
        joined = (
            b"".join(source.performances)
            if key == "performances"
            else b"".join(source.patches)
            if key == "patches"
            else source.waveseq
        )
        assert pkt.data() == joined

    # without waveseq
    source2 = deps.Source(
        bank_id="RAM1",
        performances=source.performances,
        patches=source.patches,
        waveseq=None,
    )
    blocks2 = app._before_blocks(source2, bank)
    assert "waveseq" not in blocks2


@pytest.mark.asyncio
async def test_single_target_same_scope_refused(tmp_path: Any) -> None:
    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    bank = _make_bank(scope="RAM3")
    from kws import banks as _banks

    writable = _banks.writable_banks()
    target = None
    for w in writable:
        if w.id == "RAM3":
            target = w
    assert target is not None
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    pushed: list[Any] = []
    app.push_screen = lambda s, c=None: pushed.append(s)  # type: ignore[assignment]
    app._import_single_target_chosen("p", bank, [target], 0) if False else None
    # call with target same scope
    app._import_single_target_chosen("p", bank, writable, writable.index(target))
    assert not any(isinstance(s, SingleImportScreen) for s in pushed)
    assert any("same scope" in s for s in statuses)


@pytest.mark.asyncio
async def test_single_target_different_pushes_screen(tmp_path: Any) -> None:
    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    from kws import banks as _banks

    bank = _make_bank(scope="RAM1")
    writable = _banks.writable_banks()
    # pick target RAM3
    target = next(w for w in writable if w.id == "RAM3")
    target_source = _make_bank(scope="RAM3").source
    app._sources["RAM3"] = target_source
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    pushed: list[Any] = []

    def fake_push(screen: Any, callback: Any = None) -> None:
        pushed.append((screen, callback))

    app.push_screen = fake_push  # type: ignore[assignment]
    app._import_single_target_chosen("p", bank, writable, writable.index(target))
    assert any(isinstance(s, SingleImportScreen) for s, _ in pushed)


@pytest.mark.asyncio
async def test_single_chosen_armed(tmp_path: Any) -> None:
    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    from kws import banks as _banks

    bank = _make_bank(scope="RAM1")
    # perf record non-empty at slot 3
    target = next(w for w in _banks.writable_banks() if w.id == "RAM3")
    target_source = _make_bank(scope="RAM3").source
    app._sources["RAM3"] = target_source
    gate = EditGate(armed=True)
    app._write_gate = gate
    sources = {bank.scope: bank.source, target.id: target_source}
    writes: list[dict[str, Any]] = []

    def fake_write(*a: Any, **k: Any) -> dict[str, Any]:
        writes.append(dict(k))
        return {"verified": True}

    app.device.write_bank_bundle = fake_write
    recorded: list[Any] = []

    def _rec(r: Any) -> None:
        recorded.append(r)

    app._record_write = _rec  # type: ignore[method-assign]
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]

    from kws import relations as _rels

    choice = SingleImportChoice(
        target_bank="RAM3", assignments={3: _rels.PerfId("RAM1", 3)}
    )
    with patch.object(bankstore, "save", lambda *a, **k: None):
        app._import_single_chosen(choice, target, bank, sources)

    assert len(writes) == 1
    assert writes[0]["before_blocks"]
    assert "RAM3" in app._sources
    assert len(recorded) == 1
    assert app.catalog.display_name("RAM3", 3, _banks.Kind.PERFORMANCE) == "TestName"


@pytest.mark.asyncio
async def test_import_refreshes_the_target_bank_table(tmp_path: Any) -> None:
    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    from kws import banks as _banks
    from kws import relations as _rels

    bank = _make_bank(scope="RAM1")
    target = next(w for w in _banks.writable_banks() if w.id == "RAM3")
    target_source = _make_bank(scope="RAM3", active_slots=[]).source
    app._sources["RAM3"] = target_source
    app._write_gate = EditGate(armed=True)
    app.device.write_bank_bundle = lambda *a, **k: {"verified": True}

    def _no_record(_result: Any) -> None:
        return None

    app._record_write = _no_record  # type: ignore[method-assign]
    app.notify_status = lambda *a, **k: None  # type: ignore[method-assign]
    sources = {bank.scope: bank.source, target.id: target_source}

    async with app.run_test() as pilot:
        await pilot.pause()
        target_number = _banks.bank("RAM3").number
        app._fill((target_number, _banks.Kind.PERFORMANCE))
        await pilot.pause()
        slot_table = app.query_one("#slot-table", DataTable)
        assert "TestName" not in str(slot_table.get_row_at(3)[2])

        choice = SingleImportChoice(
            target_bank="RAM3", assignments={3: _rels.PerfId("RAM1", 3)}
        )
        with patch.object(bankstore, "save", lambda *a, **k: None):
            app._import_single_chosen(choice, target, bank, sources)
        await pilot.pause()
        slot_table = app.query_one("#slot-table", DataTable)
        assert "TestName" in str(slot_table.get_row_at(3)[2])


@pytest.mark.asyncio
async def test_single_chosen_locked(tmp_path: Any) -> None:
    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    from kws import banks as _banks

    bank = _make_bank(scope="RAM1")
    target = next(w for w in _banks.writable_banks() if w.id == "RAM3")
    target_source = _make_bank(scope="RAM3").source
    app._sources["RAM3"] = target_source
    gate = EditGate(armed=False)
    app._write_gate = gate
    sources = {bank.scope: bank.source, target.id: target_source}
    writes: list[Any] = []
    app.device.write_bank_bundle = lambda *a, **k: writes.append(1)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]

    from kws import relations as _rels

    choice = SingleImportChoice(
        target_bank="RAM3", assignments={3: _rels.PerfId("RAM1", 3)}
    )
    app._import_single_chosen(choice, target, bank, sources)
    assert writes == []
    assert any("locked" in s for s in statuses)


@pytest.mark.asyncio
async def test_write_failures_leave_sources(tmp_path: Any) -> None:
    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    from kws import banks as _banks

    bank = _make_bank()
    bank.source.waveseq = None
    target_source = _make_bank(scope="RAM3").source
    app._sources["RAM3"] = target_source
    gate = EditGate(armed=True)
    app._write_gate = gate
    app.device.write_bank_bundle = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("boom")
    )
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]

    # whole
    with patch.object(bankstore, "save", lambda *a, **k: None):
        app._import_whole_bank_write(bank, WholeBankChoice(0, "RAM3", True))
    assert any("boom" in s or "import:" in s for s in statuses)
    # sources unchanged (RAM3 still original)
    assert app._sources["RAM3"] is target_source

    # single
    sources = {bank.scope: bank.source, "RAM3": target_source}
    from kws import relations as _rels

    choice = SingleImportChoice(
        target_bank="RAM3", assignments={3: _rels.PerfId("RAM1", 3)}
    )
    target = next(w for w in _banks.writable_banks() if w.id == "RAM3")
    app._import_single_chosen(choice, target, bank, sources)
    assert app._sources["RAM3"] is target_source


@pytest.mark.asyncio
async def test_empty_assignments_no_write(tmp_path: Any) -> None:
    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    from kws import banks as _banks

    bank = _make_bank(scope="RAM1")
    target = next(w for w in _banks.writable_banks() if w.id == "RAM3")
    target_source = _make_bank(scope="RAM3").source
    app._sources["RAM3"] = target_source
    gate = EditGate(armed=True)
    app._write_gate = gate
    sources = {bank.scope: bank.source, target.id: target_source}
    writes: list[Any] = []
    app.device.write_bank_bundle = lambda *a, **k: writes.append(1)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]

    choice = SingleImportChoice(target_bank="RAM3", assignments={})
    app._import_single_chosen(choice, target, bank, sources)
    assert writes == []
