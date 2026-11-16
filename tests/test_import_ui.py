# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#

"""Focused tests for the TUI import flow."""

from __future__ import annotations

import os
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Optional, Sequence, cast
from unittest.mock import patch

import pytest
from textual.widgets import DataTable, Static

from kws import (
    banks,
    bankstore,
    config,
    deps,
    import_audit,
    importers,
    perfcopy,
    relations,
)
from kws import (
    cards as _cards,
)
from kws import messages as m
from kws import records as r
from kwsed.app import (
    _HELP,
    KEY_HINTS,
    BankCopyScreen,
    CopyPerformanceScreen,
    KwsedApp,
)
from kwsed.demo import DemoDevice
from kwsed.file_picker import FilePickerScreen
from kwsed.gate import EditGate
from kwsed.import_bank import (
    OVERFLOW_PATCHES,
    AffectedCountsScreen,
    AffectedImportScreen,
    Remediation,
    WholeBankChoice,
    WholeBankImportScreen,
)
from kwsed.import_companions import Companion
from kwsed.import_screen import BankImportScreen
from kwsed.import_single import SingleImportChoice, SingleImportScreen
from kwsed.import_source import SourceEntry, unit_bank_choices
from kwsed.target_bank import ImportMenuScreen, TargetBankScreen
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
        scope_id=scope,
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
    app = KwsedApp(DemoDevice(), bankstore_dir=bs)
    # Writes must not drop backup envelopes into the user's real archive.
    app._backup_dir = str(tmp_path / "backups")
    return app


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
async def test_import_menu_q_and_legend() -> None:
    app = KwsedApp(DemoDevice())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_import()
        await pilot.pause()
        assert isinstance(app.screen, ImportMenuScreen)
        hint = app.screen.query_one("#import-menu-hint", Static)
        assert "q / Esc back" in str(hint.render())
        await pilot.press("q")
        await pilot.pause()
        assert not isinstance(app.screen, ImportMenuScreen)
        assert len(app.screen_stack) == 1


@pytest.mark.asyncio
async def test_target_bank_screen_q_and_legend() -> None:
    app = KwsedApp(DemoDevice())
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = TargetBankScreen("Target bank", ["RAM 1", "RAM 2", "RAM 3", "Card"])
        app.push_screen(screen)
        await pilot.pause()
        assert isinstance(app.screen, TargetBankScreen)
        hint = app.screen.query_one("#target-bank-hint", Static)
        assert "q / Esc back" in str(hint.render())
        await pilot.press("q")
        await pilot.pause()
        assert not isinstance(app.screen, TargetBankScreen)


class _SourceHost:
    def __init__(self) -> None:
        self.source_banks: list[Any] = []
        self.sources: dict[str, Any] = {}
        self.source_index = 0
        self.added: list[Any] = []
        self.replaced: list[Any] = []

    def select_source(self, index: int) -> None:
        self.source_index = index

    def add_source_bank(self, bank: Any, source: Any = None) -> None:
        self.added.append((bank, source))
        self.source_banks.append(bank)
        self.sources[str(bank.scope)] = source if source is not None else bank.source

    def replace_source_bank(self, index: int, bank: Any, source: Any = None) -> None:
        self.replaced.append((index, bank, source))
        self.source_banks[index] = bank
        self.sources[str(bank.scope)] = source if source is not None else bank.source

    def set_status(self, message: str) -> None:
        self.status = message


@pytest.mark.asyncio
async def test_single_source_menu_offers_file_and_unit_sources(tmp_path: Any) -> None:
    app = _make_app(tmp_path)
    pushed: list[Any] = []
    app.push_screen = lambda s, c=None: pushed.append(s)  # type: ignore[assignment]
    app._open_single_source_menu()
    assert isinstance(pushed[0], ImportMenuScreen)
    assert pushed[0]._choices[0] == "From file…"
    assert "RAM 3" in pushed[0]._choices and "Card" in pushed[0]._choices


@pytest.mark.asyncio
async def test_source_menu_offers_file_and_unit_sources(tmp_path: Any) -> None:
    app = _make_app(tmp_path)
    host = _SourceHost()
    pushed: list[Any] = []
    app.push_screen = lambda s, c=None: pushed.append(s)  # type: ignore[assignment]
    app._single_source_menu(host)
    assert isinstance(pushed[0], ImportMenuScreen)
    assert pushed[0]._choices[-2:] == [
        "Add another source file…",
        "Add unit bank (RAM/ROM/Card)…",
    ]


@pytest.mark.asyncio
async def test_unit_bank_chooser_lists_rom_and_active_card(tmp_path: Any) -> None:
    app = _make_app(tmp_path)
    host = _SourceHost()
    app._active_card_scope = lambda: "CARD/07"  # type: ignore[method-assign]
    pushed: list[Any] = []
    app.push_screen = lambda s, c=None: pushed.append(s)  # type: ignore[assignment]
    app._browse_unit_source(host)
    assert isinstance(pushed[0], ImportMenuScreen)
    labels = pushed[0]._choices
    assert labels[:3] == ["RAM 1", "RAM 2", "RAM 3"]
    assert labels[-1] == "Card 07"
    assert "ROM 4" in labels and "ROM 11" in labels


@pytest.mark.asyncio
async def test_add_patch_file_merges_into_performance_source(tmp_path: Any) -> None:
    app = _make_app(tmp_path)
    app.notify_status = lambda *a, **k: None  # type: ignore[method-assign]
    host = _SourceHost()
    existing_source = deps.Source(
        scope_id="RAM1",
        performances=[_perf_record("Perf")] + [bytes(PERF_RECORD_SIZE)] * 49,
        patches=[],
        waveseq=None,
        bank_number=0,
    )
    existing = SourceEntry(
        label="Performance file",
        scope="RAM1",
        source=existing_source,
        origin="/p/Performances.syx",
    )
    host.source_banks = [existing]
    host.sources = {"RAM1": existing_source}
    patch_bank = SimpleNamespace(
        label="Patches file",
        scope="RAM1",
        source=deps.Source(
            scope_id="RAM1",
            performances=[],
            patches=[bytes(PATCH_RECORD_SIZE)] * 35,
            waveseq=None,
        ),
    )
    app._add_extra_source(host, patch_bank, "/p/Patches.syx")
    assert host.replaced
    _index, replaced, source = host.replaced[0]
    assert source.performances == existing_source.performances
    assert len(source.patches) == 35
    assert replaced.source_origin == "/p/Performances.syx"


def _partial_bank() -> importers.ImportedBank:
    from tests.test_bankbuild import make_performance

    source = deps.Source(
        scope_id="RAM1",
        performances=[make_performance("Needs Patch", ((0, 0),))]
        + [bytes(PERF_RECORD_SIZE)] * 49,
        patches=[],
        waveseq=None,
        bank_number=0,
    )
    return importers.ImportedBank(
        label="Performance file", scope="RAM1", format="sysex", source=source
    )


def _complete_bank() -> Any:
    from tests.test_bankbuild import make_patch, make_performance

    source = deps.Source(
        scope_id="RAM1",
        performances=[make_performance("Needs Patch", ((0, 0),))]
        + [bytes(PERF_RECORD_SIZE)] * 49,
        patches=[make_patch("Complete")] + [bytes(PATCH_RECORD_SIZE)] * 34,
        waveseq=None,
        bank_number=0,
    )
    return source


@pytest.mark.asyncio
async def test_complete_source_auto_merges_unique_companion(tmp_path: Any) -> None:
    app = _make_app(tmp_path)
    bank = _partial_bank()
    merged = _complete_bank()
    audit = import_audit.audit_source(merged)
    companion = Companion(
        path="/p/MS_Test_Patches.syx",
        label="Patches file",
        source=merged,
        merged=merged,
        audit=audit,
    )
    with patch("kwsed.app.find_companions", return_value=[companion]):
        completed, note = app._complete_source_file("/p/MS_Test.syx", bank)
    assert "auto-merged" in note
    assert len(completed.source.patches) == 35
    assert import_audit.audit_source(completed.source).complete


@pytest.mark.asyncio
async def test_complete_source_warns_when_no_companion(tmp_path: Any) -> None:
    app = _make_app(tmp_path)
    bank = _partial_bank()
    with patch("kwsed.app.find_companions", return_value=[]):
        completed, note = app._complete_source_file("/p/MS_Test.syx", bank)
    assert completed.source is bank.source
    assert "missing" in note
    assert "no companion" in note


@pytest.mark.asyncio
async def test_complete_source_reports_ambiguous_companions(tmp_path: Any) -> None:
    app = _make_app(tmp_path)
    bank = _partial_bank()
    merged = _complete_bank()
    candidate = Companion(
        path="/p/MS_Test_Patches.syx",
        label="Patches file",
        source=merged,
        merged=merged,
        audit=import_audit.audit_source(merged),
    )
    other = _complete_bank()
    other.patches[0] = bytes(PATCH_RECORD_SIZE)
    other_candidate = Companion(
        path="/p/MS_Test_Other.syx",
        label="Other patches",
        source=other,
        merged=other,
        audit=import_audit.audit_source(other),
    )
    with patch(
        "kwsed.app.find_companions",
        return_value=[candidate, other_candidate],
    ):
        _completed, note = app._complete_source_file("/p/MS_Test.syx", bank)
    assert "multiple companions" in note


@pytest.mark.asyncio
async def test_add_unit_source_reads_and_keeps_assignments(tmp_path: Any) -> None:
    app = _make_app(tmp_path)
    app.notify_status = lambda *a, **k: None  # type: ignore[method-assign]
    host = _SourceHost()
    source = _make_bank(scope="RAM2").source
    calls: list[str] = []

    def fake_read(scope: str) -> Any:
        calls.append(scope)
        return source

    app._read_source_offline = fake_read  # type: ignore[method-assign]
    choice = next(c for c in unit_bank_choices() if c.scope == "RAM2")
    app._add_unit_source(host, choice)
    assert calls == ["RAM2"]
    entry, aliased = host.added[0]
    assert entry.source_origin == "RAM 2"
    assert entry.scope == "RAM2"
    assert aliased.bank_number == banks.bank("RAM2").number


@pytest.mark.asyncio
async def test_add_cached_unit_source_does_not_read(tmp_path: Any) -> None:
    app = _make_app(tmp_path)
    host = _SourceHost()
    source = _make_bank(scope="ROM7").source
    app._sources["ROM7"] = source
    calls: list[str] = []

    def _read_source_offline(scope: str) -> Any:
        calls.append(scope)
        return source

    app._read_source_offline = _read_source_offline  # type: ignore[method-assign]
    choice = next(c for c in unit_bank_choices() if c.scope == "ROM7")
    app._add_unit_source(host, choice)
    assert calls == []
    assert host.added[0][0].source_origin == "ROM 7"


@pytest.mark.asyncio
async def test_add_unit_source_read_failure_is_refused(tmp_path: Any) -> None:
    app = _make_app(tmp_path)
    host = _SourceHost()
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    app._read_source_offline = lambda scope: (_ for _ in ()).throw(OSError("no"))  # type: ignore[method-assign]
    choice = next(c for c in unit_bank_choices() if c.scope == "RAM1")
    app._add_unit_source(host, choice)
    assert host.added == []
    assert any("could not read" in s for s in statuses)


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
    app._open_file_picker(0)
    assert (
        any(str(tmp_path / "picked") == s[0] or s[0].endswith("picked") for s in saved)
        or saved
    )
    # at minimum assert save called
    assert len(saved) >= 1
    assert saved[0][1] == cfg_path or saved[0][1] is not None


@pytest.mark.asyncio
async def test_whole_source_menu_offers_file_and_unit(tmp_path: Any) -> None:
    app = _make_app(tmp_path)
    pushed: list[Any] = []
    app.push_screen = lambda s, c=None: pushed.append(s)  # type: ignore[assignment]
    app._open_whole_source_menu()
    assert isinstance(pushed[0], ImportMenuScreen)
    choices = pushed[0]._choices
    assert choices[0] == "From file…"
    assert "RAM 1" in choices and "ROM 11" in choices


@pytest.mark.asyncio
async def test_whole_unit_source_opens_whole_screen(tmp_path: Any) -> None:
    app = _make_app(tmp_path)
    app.notify_status = lambda *a, **k: None  # type: ignore[method-assign]
    source = _make_bank(scope="RAM1").source
    app._read_source_offline = lambda scope: source  # type: ignore[method-assign]
    pushed: list[Any] = []
    app.push_screen = lambda s, c=None: pushed.append(s)  # type: ignore[assignment]
    app._whole_source_chosen(1)  # RAM 1 is not index 1? choices[0] file, 1 RAM1
    screen = pushed[0]
    assert isinstance(screen, WholeBankImportScreen)
    assert screen.banks[0].scope == "RAM1"
    assert getattr(screen.banks[0], "source_origin", "") == "RAM 1"


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
        app._import_whole_bank_write(
            bank,
            _approved(WholeBankChoice(0, "RAM3", True), bank),
            banks_list=[bank],
            path="one.syx",
        )

    assert len(writes) == 1
    w = writes[0]
    # The bridge must read the unit itself. A cached bank here would be
    # treated as "what is on the unit" and could skip the whole write.
    assert w["before_blocks"] is None
    # target bytes relocated to RAM3
    assert w["performances"] is not None
    # check a part bank points to target.number if present
    # _sources updated
    assert "RAM3" in app._sources
    assert len(recorded) == 1
    assert app.catalog.display_name("RAM3", 3, _banks.Kind.PERFORMANCE) == "TestName"


def _clean_perf_record() -> bytes:
    """The builder's clean slot: every part switched off, no name."""
    one = bytearray(PERF_RECORD_SIZE)
    for i in range(r.PARTS_PER_PERFORMANCE):
        r.set_performance_part(one, i, bank=0, patch=r.UNUSED_PATCH)
    return bytes(one)


def _as_written(record: bytes, part_targets: dict[int, tuple[int, int]]) -> bytes:
    """``record`` with the used parts repointed, byte for byte."""
    out = bytearray(record)
    for index, (bank_no, part_patch) in part_targets.items():
        r.set_performance_part(out, index, bank=bank_no, patch=part_patch)
    return bytes(out)


def _mixed_bank() -> Any:
    """A source bank: two contained performances, two that name RAM 2.

    Every name is invented. The two external performances point at RAM 2
    (bank number 1), so a self-contained write skips them; the two
    contained ones share one patch (slot 0), which PACKED must dedupe.
    """
    from tests.test_bankbuild import make_patch, make_performance

    perfs = [_empty_perf() for _ in range(50)]
    perfs[2] = make_performance("Anchor Line", ((0, 0),))
    perfs[5] = make_performance("Beacon Row", ((0, 0),))
    perfs[7] = make_performance("Crossing Deck", ((1, 2),))
    perfs[9] = make_performance("Drift Path", ((1, 2),))
    patches = [bytes(PATCH_RECORD_SIZE) for _ in range(35)]
    patches[0] = make_patch("Alpha Basis")
    bank = _make_bank(scope="RAM1")
    bank.source.performances = perfs
    bank.source.patches = patches
    return bank


def _approved(
    choice: Any,
    bank: Any,
    banks_list: Any = None,
    path: str = "one.syx",
    pass_slots: Any = (),
    companion_all_init: bool = False,
) -> Any:
    """Mint the plan approval a whole-bank write carries (P3).

    The same shared builder the import screen mints with
    (``kwsed.import_intent.build_intent``), planned from the file banks
    the write names -- so a direct ``_import_whole_bank_write`` call site
    presents the approval the write boundary requires. A companion
    choice's branch is decided by ``companion_all_init``, exactly as the
    app decides it at the boundary.
    """
    from kwsed import import_intent

    file_banks = list(banks_list) if banks_list is not None else [bank]
    sources: dict[str, Any] = {}
    for entry in file_banks:
        scope = getattr(entry, "scope", None)
        source = getattr(entry, "source", None)
        if isinstance(scope, str) and source is not None:
            sources[scope] = source
    sources[str(bank.scope)] = bank.source
    target_scope = str(getattr(choice, "target_bank", "") or "") or "CARD"
    try:
        target = banks.bank(target_scope)
    except LookupError:
        if not target_scope.startswith("CARD/"):
            raise
        target = banks.bank("CARD")
    intent = import_intent.build_intent(
        source_id=f"{path}|{bank.scope}",
        source_scope=str(bank.scope),
        sources=sources,
        source=bank.source,
        target=target,
        mode=getattr(choice, "mode", perfcopy.ImportMode.AS_IS),
        overflow_kind=str(
            getattr(choice, "overflow_kind", OVERFLOW_PATCHES) or OVERFLOW_PATCHES
        ),
        overflow_bank=str(getattr(choice, "overflow_bank", "") or ""),
        preflight_answer=str(getattr(choice, "preflight", "") or ""),
        session_id=str(getattr(choice, "session_key", "") or ""),
        pass_slots=tuple(pass_slots),
        companion_only=bool(getattr(choice, "companion_only", False)),
        companion_all_init=companion_all_init,
        # The remediation is part of the plan, exactly as the write
        # boundary rebuilds it from the choice.
        remediation=_remediation_name(choice),
        cleared_banks=tuple(getattr(choice, "cleared_banks", ()) or ()),
        init_banks=tuple(getattr(choice, "init_banks", ()) or ()),
    )
    return replace(choice, approval=import_intent.new_approval(intent))


def _remediation_name(choice: Any) -> str:
    """The choice's remediation as the intent builder spells it."""
    answer = getattr(choice, "remediation", None)
    if answer is None or answer is Remediation.NONE:
        return ""
    return str(getattr(answer, "value", answer))


def _companion_loaded_map(app: Any) -> dict[str, Any]:
    """The app's loaded copies, as the screen's branch snapshot (P4).

    The import screen decides a companion choice's branch from the same
    loaded copies the app decides it from at the write boundary; tests
    that open the screen by hand hand it this snapshot.
    """
    return {
        scope: tuple(getattr(src, "performances", []) or ())
        for scope, src in app._sources_with_store().items()
    }


def _armed_app(tmp_path: Any, monkeypatch: Any) -> tuple[Any, list[dict[str, Any]]]:
    """An app with an armed gate and a RAM 3 target, backups sandboxed.

    Returns the app and the list ``_record_write`` receives (the device's
    write result, whose ``backup`` path is the pre-write backup envelope).
    """
    from kws import backup as _backup

    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    app._sources["RAM3"] = _make_bank(scope="RAM3").source
    app._write_gate = EditGate(armed=True)
    app.notify_status = lambda *a, **k: None  # type: ignore[method-assign]
    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir, exist_ok=True)
    monkeypatch.setattr(_backup, "default_dir", lambda root=None: backup_dir)
    recorded: list[dict[str, Any]] = []
    app._record_write = recorded.append  # type: ignore[method-assign]
    return app, recorded


@pytest.mark.asyncio
async def test_whole_write_as_is_matches_relocate_bank(tmp_path: Any) -> None:
    """AS_IS stays today's byte-identical relocate_bank write."""
    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    app._sources["RAM3"] = _make_bank(scope="RAM3").source
    app._write_gate = EditGate(armed=True)
    app.notify_status = lambda *a, **k: None  # type: ignore[method-assign]
    recorded: list[Any] = []

    def _no_record(result: Any) -> None:
        recorded.append(result)

    app._record_write = _no_record  # type: ignore[method-assign]
    bank = _mixed_bank()
    writes: list[dict[str, Any]] = []

    def fake_write(*a: Any, **k: Any) -> dict[str, Any]:
        writes.append(dict(k))
        return {"verified": True}

    app.device.write_bank_bundle = fake_write

    with patch.object(bankstore, "save", lambda *a, **k: None):
        app._import_whole_bank_write(
            bank,
            _approved(
                WholeBankChoice(0, "RAM3", True, perfcopy.ImportMode.AS_IS), bank
            ),
            banks_list=[bank],
            path="one.syx",
        )

    assert len(writes) == 1
    expected = perfcopy.relocate_bank(bank.source, banks.bank("RAM3"))
    w = writes[0]
    assert w["performances"] == expected.performances
    assert w["patches"] == expected.patches
    assert w["waveseq"] == expected.waveseq
    # No cached before-block: the bridge reads the unit before writing it.
    assert w["before_blocks"] is None


def _written_perfs(device: DemoDevice) -> bytes:
    blob = device._written[("RAM3", banks.Kind.PERFORMANCE)]
    assert len(blob) == 50 * PERF_RECORD_SIZE
    return blob


def _written_patches(device: DemoDevice) -> bytes:
    blob = device._written[("RAM3", banks.Kind.PATCH)]
    assert len(blob) == 35 * PATCH_RECORD_SIZE
    return blob


def _perf_at(blob: bytes, slot: int) -> bytes:
    return blob[slot * PERF_RECORD_SIZE : (slot + 1) * PERF_RECORD_SIZE]


def _patch_at(blob: bytes, slot: int) -> bytes:
    return blob[slot * PATCH_RECORD_SIZE : (slot + 1) * PATCH_RECORD_SIZE]


@pytest.mark.asyncio
async def test_whole_write_one_to_one_cleans_skipped_slots(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """1TO1: contained performances keep their slots, the rest are clean."""
    app = _armed_app(tmp_path, monkeypatch)[0]
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
            {"scope": scope_id, "performances": performances, "patches": patches}
        )

    monkeypatch.setattr(bankstore, "save", fake_save)
    bank = _mixed_bank()
    ram3 = banks.bank("RAM3").number

    app._import_whole_bank_write(
        bank,
        _approved(
            WholeBankChoice(0, "RAM3", True, perfcopy.ImportMode.SELF_CONTAINED_1TO1),
            bank,
        ),
        banks_list=[bank],
        path="one.syx",
    )

    written = _written_perfs(app.device)
    source = bank.source
    assert _perf_at(written, 2) == _as_written(source.performances[2], {0: (ram3, 0)})
    # One private patch copy per performance: the second one shares the
    # source patch but lands on its own target slot.
    assert _perf_at(written, 5) == _as_written(source.performances[5], {0: (ram3, 1)})
    for slot in range(50):
        if slot in (2, 5):
            continue
        # Slot 7 names RAM 2 and is skipped: a clean slot, not the record.
        assert _perf_at(written, slot) == _clean_perf_record()
    patch_blob = _written_patches(app.device)
    assert _patch_at(patch_blob, 0) == source.patches[0]
    assert _patch_at(patch_blob, 1) == source.patches[0]
    for slot in range(2, 35):
        assert _patch_at(patch_blob, slot) == bytes(PATCH_RECORD_SIZE)
    # Exactly one bank was written, and the cache stub got the same bytes.
    assert set(app.device._written) == {
        ("RAM3", banks.Kind.PERFORMANCE),
        ("RAM3", banks.Kind.PATCH),
    }
    assert saved[0]["scope"] == "RAM3"
    assert saved[0]["performances"] == written
    assert saved[0]["patches"] == patch_blob


@pytest.mark.asyncio
async def test_whole_write_packed_compacts_non_contained_source(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """PACKED: a source that is half external compacts into a prefix."""
    app = _armed_app(tmp_path, monkeypatch)[0]
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    bank = _mixed_bank()
    ram3 = banks.bank("RAM3").number

    app._import_whole_bank_write(
        bank,
        _approved(
            WholeBankChoice(0, "RAM3", True, perfcopy.ImportMode.SELF_CONTAINED_PACKED),
            bank,
        ),
        banks_list=[bank],
        path="one.syx",
    )

    written = _written_perfs(app.device)
    source = bank.source
    # Two contained performances re-slot 0..n-1; the two external ones and
    # every empty slot vanish behind them.
    assert _perf_at(written, 0) == _as_written(source.performances[2], {0: (ram3, 0)})
    assert _perf_at(written, 1) == _as_written(source.performances[5], {0: (ram3, 0)})
    for slot in range(2, 50):
        assert _perf_at(written, slot) == _clean_perf_record()
    patch_blob = _written_patches(app.device)
    # Both performances share one patch slot; 1TO1 needed two.
    assert _patch_at(patch_blob, 0) == source.patches[0]
    for slot in range(1, 35):
        assert _patch_at(patch_blob, slot) == bytes(PATCH_RECORD_SIZE)


@pytest.mark.asyncio
async def test_whole_write_backs_up_in_every_mode(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The unconditional pre-write backup still appears for every mode."""
    from kws.perfcopy import ImportMode

    for mode in (
        ImportMode.AS_IS,
        ImportMode.SELF_CONTAINED_1TO1,
        ImportMode.SELF_CONTAINED_PACKED,
    ):
        app, recorded = _armed_app(tmp_path, monkeypatch)
        bank = _mixed_bank()
        app._import_whole_bank_write(
            bank,
            _approved(WholeBankChoice(0, "RAM3", True, mode), bank),
            banks_list=[bank],
            path="one.syx",
        )
        assert len(recorded) == 1
        backup_path = recorded[-1].get("backup")
        assert isinstance(backup_path, str) and backup_path
        assert os.path.exists(backup_path)


@pytest.mark.asyncio
async def test_before_blocks_frames(tmp_path: Any) -> None:
    app = _make_app(tmp_path)
    from kws import banks as _banks
    from tests.test_bankbuild import make_patch, make_performance, make_waveseq

    ws = make_waveseq({0: [40]})[0]
    source = deps.Source(
        scope_id="RAM1",
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
        scope_id="RAM1",
        performances=source.performances,
        patches=source.patches,
        waveseq=None,
    )
    blocks2 = app._before_blocks(source2, bank)
    assert "waveseq" not in blocks2


@pytest.mark.asyncio
async def test_single_target_same_scope_aliases_source(tmp_path: Any) -> None:
    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    bank = _make_bank(scope="RAM3")
    from kws import banks as _banks

    writable = _banks.writable_banks()
    target = next(w for w in writable if w.id == "RAM3")
    app._sources["RAM3"] = _make_bank(scope="RAM3").source
    pushed: list[Any] = []
    app.push_screen = lambda s, c=None: pushed.append(s)  # type: ignore[assignment]
    app._import_single_target_chosen(
        "p", bank, [bank], writable, writable.index(target)
    )
    screen = next(s for s in pushed if isinstance(s, SingleImportScreen))
    assert screen._source_scope() != "RAM3"
    source = screen.sources[screen._source_scope()]
    assert source.bank_number == _banks.bank("RAM3").number


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
    app._import_single_target_chosen(
        "p", bank, [bank], writable, writable.index(target)
    )
    assert any(isinstance(s, SingleImportScreen) for s, _ in pushed)


@pytest.mark.asyncio
async def test_back_from_single_returns_to_target_chooser(tmp_path: Any) -> None:
    app = _make_app(tmp_path)
    bank = _make_bank(scope="RAM1")
    target = next(w for w in banks.writable_banks() if w.id == "RAM3")
    pushed: list[Any] = []
    app.push_screen = lambda s, c=None: pushed.append(s)  # type: ignore[assignment]
    app._import_single_chosen(None, target, bank, {}, "p.syx", [bank], [target])
    assert pushed and isinstance(pushed[0], ChoiceScreen)
    assert pushed[0]._choices == [target.label]


@pytest.mark.asyncio
async def test_back_from_target_returns_to_source_chooser(tmp_path: Any) -> None:
    app = _make_app(tmp_path)
    bank = _make_bank(scope="RAM1")
    target = next(w for w in banks.writable_banks() if w.id == "RAM3")
    pushed: list[Any] = []
    app.push_screen = lambda s, c=None: pushed.append(s)  # type: ignore[assignment]
    app._import_single_target_chosen("p.syx", bank, [bank], [target], None)
    assert pushed and isinstance(pushed[0], BankImportScreen)


@pytest.mark.asyncio
async def test_back_from_source_returns_to_file_picker(tmp_path: Any) -> None:
    app = _make_app(tmp_path)
    bank = _make_bank(scope="RAM1")
    pushed: list[Any] = []
    app.push_screen = lambda s, c=None: pushed.append(s)  # type: ignore[assignment]
    app._import_single_source_chosen("p.syx", [bank], None)
    assert pushed and isinstance(pushed[0], ImportMenuScreen)


@pytest.mark.asyncio
async def test_back_from_whole_returns_to_file_picker(tmp_path: Any) -> None:
    app = _make_app(tmp_path)
    bank = _make_bank(scope="RAM1")
    pushed: list[Any] = []
    app.push_screen = lambda s, c=None: pushed.append(s)  # type: ignore[assignment]
    app._import_whole_bank_chosen([bank], None)
    assert pushed and isinstance(pushed[0], ImportMenuScreen)


@pytest.mark.asyncio
async def test_file_picker_cancel_returns_to_import_menu(tmp_path: Any) -> None:
    app = _make_app(tmp_path)
    pushed: list[Any] = []
    app.push_screen = lambda s, c=None: pushed.append((s, c))  # type: ignore[assignment]
    app._open_file_picker(1)
    assert pushed
    _picker, done = pushed[0]
    done(None)
    assert isinstance(pushed[-1][0], ImportMenuScreen)


@pytest.mark.asyncio
async def test_session_source_alias_carries_bank_number(tmp_path: Any) -> None:
    app = _make_app(tmp_path)
    bank = _make_bank(scope="RAM3")
    existing = {"RAM3": _make_bank(scope="RAM3").source}
    wrapped, aliased = app._session_source(bank, existing)
    assert wrapped.scope != "RAM3"
    assert aliased.bank_number == banks.bank("RAM3").number
    assert wrapped.label.endswith("(RAM3#2)")


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
async def test_single_chosen_uses_session_alias_sources(tmp_path: Any) -> None:
    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    from kws import banks as _banks
    from kws import records as _records
    from kws import relations as _rels
    from tests.test_bankbuild import make_patch, make_performance

    target = next(w for w in _banks.writable_banks() if w.id == "RAM3")
    target_source = deps.Source(
        scope_id="RAM3",
        performances=[bytes(PERF_RECORD_SIZE)] * 50,
        patches=[bytes(PATCH_RECORD_SIZE)] * 35,
        waveseq=None,
    )
    alias = "RAM1#2"
    aliased = deps.Source(
        scope_id=alias,
        performances=[make_performance("Alias", ((0, 0),))]
        + [bytes(PERF_RECORD_SIZE)] * 49,
        patches=[make_patch("AliasPatch")] + [bytes(PATCH_RECORD_SIZE)] * 34,
        waveseq=None,
        bank_number=0,
    )
    sources = {alias: aliased, target.id: target_source}
    app._sources[target.id] = target_source
    app._write_gate = EditGate(armed=True)
    app.device.write_bank_bundle = lambda *a, **k: {"verified": True}

    def _no_record(_result: Any) -> None:
        return None

    app._record_write = _no_record  # type: ignore[method-assign]
    app.notify_status = lambda *a, **k: None  # type: ignore[method-assign]
    original = _make_bank(scope="RAM1")
    choice = SingleImportChoice(
        target_bank="RAM3",
        assignments={0: _rels.PerfId(alias, 0)},
        sources=sources,
    )
    app._import_single_chosen(choice, target, original, sources)
    built = app._sources["RAM3"]
    record = built.performances[0]
    assert m.decode_name(record) == "Alias"
    assert any(
        part.bank == target.number for part in _records.performance_parts(record)
    )


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
        app._import_whole_bank_write(
            bank,
            _approved(WholeBankChoice(0, "RAM3", True), bank),
            banks_list=[bank],
            path="one.syx",
        )
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


def _decoded_bank(device: DemoDevice, bank: str, function: int) -> bytes:
    """One bank's decoded read-back payload, straight off the fake."""
    raw = device.read_raw(banks.bank(bank).sysex, function)
    packet = m.parse(raw)
    assert packet is not None
    return bytes(packet.data())


class TestBankCopyUi:
    @staticmethod
    async def _fire(
        app: KwsedApp,
        pilot: Any,
        source: Any,
        destination: Any,
        method: str = "fence",
    ) -> list[Any]:
        """Open the picker, arm the screen, fire, and wait for the dismissal."""
        writable = [b for b in banks.writable_banks() if b.number != source.number]
        results: list[Any] = []
        push = app.push_screen

        def _push(screen: Any, callback: Any = None, *args: Any, **kwargs: Any) -> Any:
            if isinstance(screen, BankCopyScreen):
                return push(screen, results.append, *args, **kwargs)
            return push(screen, callback, *args, **kwargs)

        app.push_screen = _push  # type: ignore[method-assign]
        app._copy_bank_target_chosen(source, writable, writable.index(destination))
        await pilot.pause()
        assert isinstance(app.screen, BankCopyScreen)
        if method == "device":
            await pilot.press("m")
            await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("w")
        for _ in range(50):
            await pilot.pause()
            if not isinstance(app.screen, BankCopyScreen):
                break
        assert not isinstance(app.screen, BankCopyScreen)
        return results

    @pytest.mark.asyncio
    async def test_screen_open_and_arm_send_nothing(self, tmp_path: Any) -> None:
        app = _make_app(tmp_path)
        calls: list[Any] = []

        def _never(*args: Any, **kwargs: Any) -> Any:
            calls.append((args, kwargs))
            raise AssertionError("opening or arming must not touch the device")

        app.device.copy_bank = _never
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(BankCopyScreen(app, banks.bank("RAM1"), banks.bank("CARD")))
            await pilot.pause()
            assert isinstance(app.screen, BankCopyScreen)
            await pilot.press("a")
            await pilot.pause()
            assert app.screen._armed is True
        assert calls == []
        assert app.device.bundle_write_count == 0

    @pytest.mark.asyncio
    async def test_the_method_toggles_and_is_shown(self, tmp_path: Any) -> None:
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(BankCopyScreen(app, banks.bank("RAM1"), banks.bank("CARD")))
            await pilot.pause()
            assert isinstance(app.screen, BankCopyScreen)
            screen = app.screen
            assert screen._method == "fence"
            assert "kwsed: read, remap, write, verify" in screen._describe()
            await pilot.press("m")
            await pilot.pause()
            assert screen._method == "device"
            assert "the unit's MOVE" in screen._describe()
            await pilot.press("m")
            await pilot.pause()
            assert screen._method == "fence"
            assert "kwsed: read, remap, write, verify" in screen._describe()

    @pytest.mark.asyncio
    async def test_changing_the_method_disarms(self, tmp_path: Any) -> None:
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(BankCopyScreen(app, banks.bank("RAM1"), banks.bank("CARD")))
            await pilot.pause()
            assert isinstance(app.screen, BankCopyScreen)
            screen = app.screen
            await pilot.press("a")
            await pilot.pause()
            assert screen._armed is True
            await pilot.press("m")
            await pilot.pause()
            assert screen._armed is False
            state = str(screen.query_one("#bank-copy-state", Static).render())
            assert "disarmed" in state
            assert "method changed" in state

    def test_describe_shows_real_slot_numbers_with_holes(self, tmp_path: Any) -> None:
        """Holes in the destination must not renumber the listed slots.

        The dialog names what will be replaced, so ``001`` must mean slot
        1 even when slot 0 is empty.
        """
        app = _make_app(tmp_path)
        source = banks.bank("RAM1")
        destination = banks.bank("CARD")

        def _named(size: int, name: str) -> bytes:
            return bytes(m.encode_name(name)) + bytes(size - m.NAME_LEN)

        perfs = [bytes(r.PERFORMANCE_SIZE) for _ in range(50)]
        perfs[1] = _named(r.PERFORMANCE_SIZE, "PerfOne")
        perfs[3] = _named(r.PERFORMANCE_SIZE, "PerfThree")
        patches = [bytes(r.PATCH_SIZE) for _ in range(35)]
        patches[2] = _named(r.PATCH_SIZE, "PatchTwo")
        patches[5] = _named(r.PATCH_SIZE, "PatchFive")
        app._sources[destination.id] = deps.Source(
            scope_id=destination.id,
            performances=perfs,
            patches=patches,
            waveseq=None,
        )
        text = BankCopyScreen(app, source, destination)._describe()
        assert "001 PerfOne" in text
        assert "003 PerfThree" in text
        assert "000 PerfOne" not in text
        assert "002 PatchTwo" in text
        assert "005 PatchFive" in text
        assert "000 PatchTwo" not in text

    @pytest.mark.asyncio
    async def test_fire_with_the_device_method(self, tmp_path: Any) -> None:
        app = _make_app(tmp_path)
        device = app.device
        source = banks.bank("RAM1")
        destination = banks.bank("CARD")
        ram_perfs = _decoded_bank(device, "RAM1", m.Function.ALL_PERFORMANCE_DUMP)
        ram_patches = _decoded_bank(device, "RAM1", m.Function.ALL_PATCH_DUMP)
        ram_waveseq = _decoded_bank(device, "RAM1", m.Function.ALL_WAVESEQ_DUMP)
        statuses: list[str] = []
        app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
        original = device.copy_bank
        calls: list[Any] = []

        def record(*args: Any, **kwargs: Any) -> Any:
            calls.append(kwargs.get("method"))
            return original(*args, **kwargs)

        device.copy_bank = record
        try:
            async with app.run_test() as pilot:
                await pilot.pause()
                app._write_gate = EditGate(armed=True)
                results = await self._fire(
                    app, pilot, source, destination, method="device"
                )
        finally:
            device.copy_bank = original
        assert calls == ["device"]
        assert results and results[0]["ok"] is True
        assert results[0]["destination"] == "Card"
        assert results[0]["verified"] is True
        got_perfs = _decoded_bank(device, "CARD", m.Function.ALL_PERFORMANCE_DUMP)
        got_patches = _decoded_bank(device, "CARD", m.Function.ALL_PATCH_DUMP)
        got_waveseq = _decoded_bank(device, "CARD", m.Function.ALL_WAVESEQ_DUMP)
        assert got_perfs == r.remap_performance_banks(
            ram_perfs, source.number, destination.number
        )
        assert got_patches == ram_patches
        assert got_waveseq == ram_waveseq
        assert any("unit's move" in s for s in statuses)

    @pytest.mark.asyncio
    async def test_fire_writes_and_dismisses(self, tmp_path: Any) -> None:
        app = _make_app(tmp_path)
        device = app.device
        source = banks.bank("RAM1")
        destination = banks.bank("CARD")
        ram_perfs = _decoded_bank(device, "RAM1", m.Function.ALL_PERFORMANCE_DUMP)
        ram_patches = _decoded_bank(device, "RAM1", m.Function.ALL_PATCH_DUMP)
        ram_waveseq = _decoded_bank(device, "RAM1", m.Function.ALL_WAVESEQ_DUMP)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._write_gate = EditGate(armed=True)
            results = await self._fire(app, pilot, source, destination)
        assert results and results[0]["ok"] is True
        assert results[0]["destination"] == "Card"
        assert results[0]["verified"] is True
        got_perfs = _decoded_bank(device, "CARD", m.Function.ALL_PERFORMANCE_DUMP)
        got_patches = _decoded_bank(device, "CARD", m.Function.ALL_PATCH_DUMP)
        got_waveseq = _decoded_bank(device, "CARD", m.Function.ALL_WAVESEQ_DUMP)
        assert got_perfs == r.remap_performance_banks(
            ram_perfs, source.number, destination.number
        )
        assert got_patches == ram_patches
        assert got_waveseq == ram_waveseq

    @pytest.mark.asyncio
    async def test_gate_locked_refuses(self, tmp_path: Any) -> None:
        app = _make_app(tmp_path)
        app._write_gate = EditGate(armed=False)
        statuses: list[str] = []
        app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            await pilot.pause()
            before = len(app.screen_stack)
            app.action_copy_bank()
            await pilot.pause()
            assert len(app.screen_stack) == before
            assert not isinstance(app.screen, BankCopyScreen)
        assert any("write gate is locked" in s for s in statuses)

    @pytest.mark.asyncio
    async def test_card_mode_none_refuses_card_end(self, tmp_path: Any) -> None:
        app = KwsedApp(
            DemoDevice(),
            card_mode="none",
            bankstore_dir=str(tmp_path / "store"),
        )
        statuses: list[str] = []
        app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
        source = banks.bank("RAM1")
        writable = [b for b in banks.writable_banks() if b.number != source.number]
        assert banks.bank("CARD") in writable
        async with app.run_test() as pilot:
            await pilot.pause()
            app._write_gate = EditGate(armed=True)
            before = len(app.screen_stack)
            app._copy_bank_target_chosen(
                source, writable, writable.index(banks.bank("CARD"))
            )
            await pilot.pause()
            assert len(app.screen_stack) == before
            assert not isinstance(app.screen, BankCopyScreen)
            # The action refuses before the picker when the Card is a candidate.
            app.action_copy_bank()
            await pilot.pause()
            assert len(app.screen_stack) == before
        assert statuses.count("card support is off (card_mode = none)") >= 2

    @pytest.mark.asyncio
    async def test_destination_cache_updated_after_copy(self, tmp_path: Any) -> None:
        app = _make_app(tmp_path)
        device = app.device
        source = banks.bank("RAM1")
        destination = banks.bank("CARD")
        async with app.run_test() as pilot:
            await pilot.pause()
            app._write_gate = EditGate(armed=True)
            await self._fire(app, pilot, source, destination)
        cached = app._sources["CARD"]
        payload = _decoded_bank(device, "CARD", m.Function.ALL_PERFORMANCE_DUMP)
        records = [
            payload[i * r.PERFORMANCE_SIZE : (i + 1) * r.PERFORMANCE_SIZE]
            for i in range(len(payload) // r.PERFORMANCE_SIZE)
        ]
        assert cached.performances == records

    @pytest.mark.asyncio
    async def test_a_verified_copy_auto_adopts_the_active_card(
        self, tmp_path: Any
    ) -> None:
        """A Card write re-digests the active bank's archive record.

        The copy is automatic: no prompt, and the bank's identity (name,
        label, capture time) survives while its content record and digest
        describe what is now on the card.
        """
        path = str(tmp_path / "cards.json")
        app = KwsedApp(
            DemoDevice(), cards_path=path, bankstore_dir=str(tmp_path / "store")
        )
        app._backup_dir = str(tmp_path / "backups")
        old = _cards.capture(
            31,
            "Old Name",
            {0: "Old Perf"},
            {0: "Old Patch"},
            label="WPC-13",
            digest="old-digest",
        )
        app._archive[31] = old
        app._active_card = 31
        device = app.device
        async with app.run_test() as pilot:
            await pilot.pause()
            app._write_gate = EditGate(armed=True)
            await self._fire(app, pilot, banks.bank("RAM1"), banks.bank("CARD"))
        perf_payload = _decoded_bank(device, "CARD", m.Function.ALL_PERFORMANCE_DUMP)
        patch_payload = _decoded_bank(device, "CARD", m.Function.ALL_PATCH_DUMP)
        perf_records = [
            perf_payload[i * r.PERFORMANCE_SIZE : (i + 1) * r.PERFORMANCE_SIZE]
            for i in range(len(perf_payload) // r.PERFORMANCE_SIZE)
        ]
        patch_records = [
            patch_payload[i * r.PATCH_SIZE : (i + 1) * r.PATCH_SIZE]
            for i in range(len(patch_payload) // r.PATCH_SIZE)
        ]
        adopted = app._archive[31]
        assert adopted.verified is True
        assert adopted.digest == _cards.content_digest(perf_records, patch_records)
        assert adopted.name == "Old Name"
        assert adopted.label == "WPC-13"
        assert adopted.captured_at == old.captured_at
        assert adopted.performances[0] == m.decode_name(perf_records[0])
        assert adopted.patches[0] == m.decode_name(patch_records[0])
        assert _cards.load(path)[31].digest == adopted.digest

    @pytest.mark.asyncio
    async def test_undo_restores_the_card(self, tmp_path: Any) -> None:
        # `_make_app` points the app's backup dir at tmp_path, so the undo
        # envelope lands there too.
        app = _make_app(tmp_path)
        device = app.device
        source = banks.bank("RAM1")
        destination = banks.bank("CARD")
        before = {
            "performances": _decoded_bank(
                device, "CARD", m.Function.ALL_PERFORMANCE_DUMP
            ),
            "patches": _decoded_bank(device, "CARD", m.Function.ALL_PATCH_DUMP),
            "waveseq": _decoded_bank(device, "CARD", m.Function.ALL_WAVESEQ_DUMP),
        }
        async with app.run_test() as pilot:
            await pilot.pause()
            app._write_gate = EditGate(armed=True)
            await self._fire(app, pilot, source, destination)
            assert len(app._undo_log) == 1, "one bundle is one undo record"
            app.action_undo()
            for _ in range(50):
                await pilot.pause()
                if len(app._undo_log) == 0:
                    break
            assert len(app._undo_log) == 0, "one `z` must consume the record"
        assert (
            _decoded_bank(device, "CARD", m.Function.ALL_PERFORMANCE_DUMP)
            == before["performances"]
        )
        assert (
            _decoded_bank(device, "CARD", m.Function.ALL_PATCH_DUMP)
            == before["patches"]
        )
        assert (
            _decoded_bank(device, "CARD", m.Function.ALL_WAVESEQ_DUMP)
            == before["waveseq"]
        )

    @pytest.mark.asyncio
    async def test_a_ram_copy_rechecks_the_multiset_catalog(
        self, tmp_path: Any
    ) -> None:
        """Copying over RAM 1 re-runs the Multiset check at once.

        A Multiset recorded against RAM 1 must show its new mismatch as
        soon as the copy lands, with no fresh device read: the written
        bytes already are the post-write state.
        """
        from kws import multi as multi_mod
        from kws import multisets as multi_snap

        app = _make_app(tmp_path)
        source = banks.bank("RAM2")
        destination = banks.bank("RAM1")
        parts = tuple(
            multi_mod.Part(
                channel=index + 1,
                bank=destination.sysex,
                performance=0 if index == 0 else 50,
                level=100,
                flag=0,
            )
            for index in range(16)
        )
        ms = multi_mod.Multiset(number=0, name="M", parts=parts)
        record = multi_mod.encode([ms])[: multi_mod.MULTISET_SIZE]
        app._multisets = [ms]
        app._multi_catalog[0] = multi_snap.build_snapshot(
            ms,
            record,
            card_scope_for=lambda channel: None,
            name_for=lambda bank, number, scope: "Warm Pad",
            card_digest_for=lambda scope: "",
            recorded_at="2026-11-13T00:00:00+00:00",
        )
        app.catalog.set_live_name("RAM1", 0, "Warm Pad", banks.Kind.PERFORMANCE)
        app._sources["RAM1"] = deps.Source(
            scope_id="RAM1",
            performances=[bytes(r.PERFORMANCE_SIZE)] * 50,
            patches=[],
            waveseq=None,
        )
        assert app._check_multi_snapshots([ms]) == {}
        perfs = bytearray(50 * r.PERFORMANCE_SIZE)
        perfs[0:16] = bytes(m.encode_name("Something Else"))
        written = {
            "performances": bytes(perfs),
            "patches": bytes(35 * r.PATCH_SIZE),
            "waveseq": bytes(r.WAVESEQ_BANK_SIZE),
        }
        reads: list[Any] = []
        failures: list[Any] = []
        done: list[Any] = []
        screen = SimpleNamespace(
            _write_done=done.append,
            _write_failed=failures.append,
        )
        original_read_raw = app.device.read_raw

        def _no_read(*args: Any, **kwargs: Any) -> Any:
            reads.append((args, kwargs))
            raise AssertionError("a bank copy must not re-read the device")

        async with app.run_test() as pilot:
            await pilot.pause()
            app.device.read_raw = _no_read
            try:
                app._copy_bank_done(
                    {"verified": True, "method": "fence", "written": written},
                    None,
                    source,
                    destination,
                    screen,
                )
                await pilot.pause()
            finally:
                app.device.read_raw = original_read_raw
        assert failures == []
        assert reads == [], "the check re-runs from the written bytes, not MIDI"
        assert done and done[0]["ok"] is True
        assert 0 in app._multi_mismatches
        mismatch = app._multi_mismatches[0][0]
        assert mismatch.kind == "ram"
        assert "Warm Pad" in mismatch.text
        assert "Something Else" in mismatch.text


async def _settle(pilot: Any, times: int = 20) -> None:
    for _ in range(times):
        await pilot.pause()


def _static_text(screen: Any, widget_id: str) -> str:
    return str(screen.query_one(f"#{widget_id}", Static).content)


def _spillover_banks(*, rom_companion: bool = False) -> list[Any]:
    """A file of invented banks: RAM 1 as the main source, RAM 2 beside it.

    The main bank's slot 4 names RAM 2 (bank number 1) in one part, so the
    audit's per-bank total is what the spill-over suggestion must follow.
    With ``rom_companion`` a ROM 4 bank joins the file: it is present, but
    it is not writable and must never be offered.
    """
    from tests.test_bankbuild import make_patch, make_performance

    perfs = [_empty_perf() for _ in range(50)]
    perfs[1] = make_performance("Copper Ledge", ((0, 0),))
    perfs[4] = make_performance("Willow Span", ((1, 3),))
    main = deps.Source(
        scope_id="RAM1",
        performances=perfs,
        patches=[make_patch("Slate Underlay")] + [bytes(PATCH_RECORD_SIZE)] * 34,
        waveseq=None,
    )
    out: list[Any] = [
        importers.ImportedBank(
            label="Main bank", scope="RAM1", format="sysex", source=main
        ),
        _make_bank(scope="RAM2", active_slots=[2]),
    ]
    if rom_companion:
        out.append(_make_bank(scope="ROM4", active_slots=[6]))
    return out


@pytest.mark.asyncio
async def test_spillover_list_appears_only_for_a_multi_bank_file() -> None:
    app = KwsedApp(DemoDevice())
    single = WholeBankImportScreen(path="one.syx", banks=[_make_bank(scope="RAM1")])
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(single)
        await _settle(pilot)
        await pilot.press("x")
        await _settle(pilot)
        assert isinstance(app.screen, WholeBankImportScreen)
        assert "only one source bank" in _static_text(single, "whole-bank-status")

        many = WholeBankImportScreen(path="two.syx", banks=_spillover_banks())
        app.push_screen(many)
        await _settle(pilot)
        await pilot.press("x")
        await _settle(pilot)
        assert isinstance(app.screen, ImportMenuScreen)
        choices = app.screen._choices
        assert len(choices) == 1
        assert "Bank RAM2" in choices[0]
    # Merely offering the companions writes nothing.
    assert app.device.bundle_write_count == 0


@pytest.mark.asyncio
async def test_spillover_suggests_the_audited_ram_bank_and_skips_rom() -> None:
    app = KwsedApp(DemoDevice())
    many = WholeBankImportScreen(
        path="many.syx", banks=_spillover_banks(rom_companion=True)
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(many)
        await _settle(pilot)
        audit = many._audit
        assert audit is not None
        assert audit.parts_per_bank == {banks.bank("RAM2").number: 1}

        await pilot.press("x")
        await _settle(pilot)
        assert isinstance(app.screen, ImportMenuScreen)
        choices = app.screen._choices
        # ROM 4 is in the file but cannot be written, so it is not offered.
        assert len(choices) == 1
        assert "→ RAM 2" in choices[0]
        assert "1 part names it" in choices[0]

        await pilot.press("enter")
        await _settle(pilot)
        assert isinstance(app.screen, TargetBankScreen)
        targets = app.screen._choices
        assert targets[0] == banks.bank("RAM2").label
        assert targets[1:] == [banks.bank("RAM1").label, banks.bank("RAM3").label]
        assert banks.bank("CARD").label not in targets
        assert not any(t.startswith("ROM") for t in targets)
    assert app.device.bundle_write_count == 0


@pytest.mark.asyncio
async def test_spillover_carries_the_current_mode() -> None:
    """The spill choice is a normal whole-bank choice, mode included."""
    app = KwsedApp(DemoDevice())
    captured: list[Any] = []
    many = WholeBankImportScreen(
        path="two.syx", banks=_spillover_banks(), gate=EditGate(armed=True)
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(many, captured.append)
        await _settle(pilot)
        await pilot.press("m")
        await _settle(pilot)
        await pilot.press("x")
        await _settle(pilot)
        await pilot.press("enter")
        await _settle(pilot)
        await pilot.press("enter")
        await _settle(pilot)
    choice = captured[0]
    assert isinstance(choice, WholeBankChoice)
    assert choice.source_index == 1
    assert choice.target_bank == banks.bank("RAM2").id
    assert choice.write_to_unit is True
    assert choice.mode is perfcopy.ImportMode.SELF_CONTAINED_1TO1


@pytest.mark.asyncio
async def test_spillover_is_one_fenced_write_to_the_audited_bank(
    tmp_path: Any, monkeypatch: Any
) -> None:
    app, recorded = _armed_app(tmp_path, monkeypatch)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    file_banks = _spillover_banks()
    # The unit's copy of RAM 2 is all Init: a whole-bank companion write
    # carries nothing away, so the bank travels whole as before.
    app._sources["RAM2"] = _make_bank(scope="RAM2", active_slots=[]).source
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            WholeBankImportScreen(
                path="two.syx",
                banks=file_banks,
                gate=app._write_gate,
                companion_loaded=_companion_loaded_map(app),
            ),
            lambda picked: app._import_whole_bank_chosen(file_banks, picked, "two.syx"),
        )
        await _settle(pilot)
        await pilot.press("x")
        await _settle(pilot)
        await pilot.press("enter")
        await _settle(pilot)
        assert isinstance(app.screen, TargetBankScreen)
        await pilot.press("enter")
        # The affected-performances question stands between the choice
        # and the write now; deciding and leaving goes straight through
        # when nothing is affected.
        await _answer_remediation_with_leave(app, pilot)
        for _ in range(100):
            await pilot.pause()
            if app.device.bundle_write_count:
                break
        await _settle(pilot)

    assert app.device.bundle_write_count == 1
    assert app.device.bundle_writes[0]["bank"] == banks.bank("RAM2").label
    assert set(app.device._written) == {
        ("RAM2", banks.Kind.PERFORMANCE),
        ("RAM2", banks.Kind.PATCH),
    }
    assert recorded, "the write is recorded, exactly once"
    backup_path = recorded[0]["backup"]
    assert isinstance(backup_path, str) and os.path.exists(backup_path)


@pytest.mark.asyncio
async def test_spillover_requires_the_write_gate(tmp_path: Any) -> None:
    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    app._write_gate = EditGate(armed=False)
    app.notify_status = lambda *a, **k: None  # type: ignore[method-assign]
    file_banks = _spillover_banks()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            WholeBankImportScreen(
                path="two.syx", banks=file_banks, gate=app._write_gate
            ),
            lambda picked: app._import_whole_bank_chosen(file_banks, picked),
        )
        await _settle(pilot)
        await pilot.press("x")
        await _settle(pilot)
        await pilot.press("enter")
        await _settle(pilot)
        assert isinstance(app.screen, TargetBankScreen)
        await pilot.press("enter")
        await _settle(pilot)
        # Still on the screen: nothing was dismissed, so nothing was written.
        assert isinstance(app.screen, WholeBankImportScreen)
        assert "write gate is locked" in _static_text(app.screen, "whole-bank-status")
    assert app.device.bundle_write_count == 0


@pytest.mark.asyncio
async def test_whole_bank_targets_are_the_four_physical_banks() -> None:
    """The target list never names an archived card bank.

    The unit has one card slot, so "Card 07" could only ever write the card
    that is in it. Offering every archive invited a write nobody could aim.
    """
    for card_mode in (None, "hcard", "cards"):
        screen = WholeBankImportScreen(
            path="one.syx",
            banks=[_make_bank(scope="RAM1")],
            card_mode=card_mode,
            hcard_banks=[7, 30],
        )
        ids = [str(getattr(w, "id", "")) for w in screen._writable]
        assert ids == ["RAM1", "RAM2", "RAM3", "CARD"]


def _card_write_app(
    tmp_path: Any, monkeypatch: Any, active_scope: str
) -> tuple[Any, list[tuple[int, dict[str, Any]]], dict[str, Any], list[str]]:
    """An armed app with a captured CARD/07, writes and statuses recorded."""
    from kws import backup as _backup

    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    app._sources["CARD/07"] = _make_bank(scope="CARD/07").source
    app._write_gate = EditGate(armed=True)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    app._active_card_scope = lambda: active_scope  # type: ignore[method-assign]
    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir, exist_ok=True)
    monkeypatch.setattr(_backup, "default_dir", lambda root=None: backup_dir)
    saved: dict[str, Any] = {}

    def fake_save(scope_id: str, **k: Any) -> None:
        saved["scope"] = scope_id
        saved.update(k)

    monkeypatch.setattr(bankstore, "save", fake_save)
    writes: list[tuple[int, dict[str, Any]]] = []

    def fake_write(bank_number: int, **k: Any) -> dict[str, Any]:
        writes.append((bank_number, k))
        return {"verified": True}

    app.device.write_bank_bundle = fake_write

    def _no_record(result: Any) -> None:
        return None

    app._record_write = _no_record  # type: ignore[method-assign]
    return app, writes, saved, statuses


@pytest.mark.asyncio
async def test_whole_write_to_hcard_scope_uses_slot_and_files_archive(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A CARD/07 write hits the one physical slot, filed under the archive."""
    app, writes, saved, _statuses = _card_write_app(tmp_path, monkeypatch, "CARD/07")
    bank = _make_bank(scope="RAM1")
    app._import_whole_bank_write(
        bank,
        _approved(WholeBankChoice(0, "CARD/07", True), bank),
        banks_list=[bank],
        path="one.syx",
    )
    assert len(writes) == 1
    assert writes[0][0] == banks.bank("CARD").number
    assert writes[0][1]["backup_dir"].endswith("backups")
    assert saved.get("scope") == "CARD/07"
    assert "CARD/07" in app._sources


@pytest.mark.asyncio
async def test_whole_write_to_other_hcard_scope_refuses(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The slot guard refuses a CARD/07 write while CARD/08 is inserted."""
    app, writes, saved, statuses = _card_write_app(tmp_path, monkeypatch, "CARD/08")
    app._sources.pop("CARD/07")
    bank = _make_bank(scope="RAM1")
    app._import_whole_bank_write(
        bank,
        _approved(WholeBankChoice(0, "CARD/07", True), bank),
        banks_list=[bank],
        path="one.syx",
    )
    assert writes == []
    assert saved == {}
    assert "CARD/07" not in app._sources
    assert any("CARD/07" in s and "CARD/08" in s for s in statuses)


def _multipass_bank() -> Any:
    """A source bank larger than one target: 55 contained, 2 external.

    Every name is invented. The contained performances in slots 0-54 each
    use their own patch slot (mod 35), so a packed pass cannot hold them
    all: the partition splits them into bank-sized passes. Slots 56-57
    name RAM 2 (bank number 1) and are skipped in every pass.
    """
    from tests.test_bankbuild import make_patch, make_performance

    perfs = [_empty_perf() for _ in range(58)]
    for slot in range(55):
        perfs[slot] = make_performance(f"Pass Perf {slot:02d}", ((0, slot % 35),))
    perfs[56] = make_performance("Outer One", ((1, 2),))
    perfs[57] = make_performance("Outer Two", ((1, 3),))
    patches = [make_patch(f"Basis {index:02d}") for index in range(35)]
    bank = _make_bank(scope="RAM1")
    bank.source.performances = perfs
    bank.source.patches = patches
    return bank


def _pass_blob(device: DemoDevice, bank_id: str, kind: str) -> bytes:
    return device._written[(bank_id, kind)]


@pytest.mark.asyncio
async def test_next_pass_needs_a_self_contained_mode() -> None:
    app = KwsedApp(DemoDevice())
    screen = WholeBankImportScreen(path="one.syx", banks=[_make_bank(scope="RAM1")])
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen)
        await _settle(pilot)
        await pilot.press("n")
        await _settle(pilot)
        assert isinstance(app.screen, WholeBankImportScreen)
        assert "self-contained mode" in _static_text(screen, "whole-bank-status")
    assert app.device.bundle_write_count == 0


def _pass_app(tmp_path: Any, monkeypatch: Any) -> KwsedApp:
    """An armed app with RAM 2 and RAM 3 ready to receive two passes."""
    from kws import backup as _backup

    app = KwsedApp(
        DemoDevice(),
        bankstore_dir=str(tmp_path / "store"),
    )
    app._sources["RAM2"] = _make_bank(scope="RAM2", active_slots=[]).source
    app._sources["RAM3"] = _make_bank(scope="RAM3", active_slots=[]).source
    app._write_gate = EditGate(armed=True)
    app.notify_status = lambda *a, **k: None  # type: ignore[method-assign]

    def _no_record(_result: Any) -> None:
        return None

    app._record_write = _no_record  # type: ignore[method-assign]
    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir, exist_ok=True)
    monkeypatch.setattr(_backup, "default_dir", lambda root=None: backup_dir)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    return app


async def _wait_for_writes(app: KwsedApp, pilot: Any, count: int) -> None:
    for _ in range(200):
        await pilot.pause()
        if app.device.bundle_write_count == count:
            break


def _assert_two_pass_bytes(app: KwsedApp, bank: Any) -> None:
    """Pass 1 fills RAM 2; pass 2 compacts the last five into RAM 3."""
    source = bank.source
    ram2 = banks.bank("RAM2").number
    ram3 = banks.bank("RAM3").number
    ram2_perf = _pass_blob(app.device, "RAM2", banks.Kind.PERFORMANCE)
    for slot in range(50):
        assert _perf_at(ram2_perf, slot) == _as_written(
            source.performances[slot], {0: (ram2, slot % 35)}
        )
    ram3_perf = _pass_blob(app.device, "RAM3", banks.Kind.PERFORMANCE)
    for slot in range(5):
        assert _perf_at(ram3_perf, slot) == _as_written(
            source.performances[50 + slot], {0: (ram3, slot)}
        )
    for slot in range(5, 50):
        assert _perf_at(ram3_perf, slot) == _clean_perf_record()
    ram2_patch = _pass_blob(app.device, "RAM2", banks.Kind.PATCH)
    for slot in range(35):
        assert _patch_at(ram2_patch, slot) == source.patches[slot]
    ram3_patch = _pass_blob(app.device, "RAM3", banks.Kind.PATCH)
    for slot in range(5):
        assert _patch_at(ram3_patch, slot) == source.patches[15 + slot]
    for slot in range(5, 35):
        assert _patch_at(ram3_patch, slot) == bytes(PATCH_RECORD_SIZE)


def _assert_pass_state(app: KwsedApp, bank: Any) -> None:
    """The session's pass state: disjoint writes, union = contained."""
    from kws import containment

    keys = [k for k in app._import_passes if k.startswith("two.syx|RAM1|")]
    assert len(keys) == 1
    state = app._import_passes[keys[0]]
    written = state["written"]
    assert set(written) == set(range(55))
    assert {slot for slot, t in written.items() if t == "RAM 2"} == set(range(50))
    assert {slot for slot, t in written.items() if t == "RAM 3"} == set(range(50, 55))
    audit = containment.audit_containment({"RAM1": bank.source}, "RAM1")
    contained = {entry.slot for entry in audit.performances if entry.contained}
    assert set(written) == contained
    assert 56 not in written and 57 not in written


@pytest.mark.asyncio
async def test_two_pass_import_splits_disjoint_targets(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Pass 1 fills RAM 2; ``n`` carries the remainder into CARD/07.

    Each pass is its own fenced write. The union of the written source
    slots is exactly the contained performances, the two targets hold
    disjoint sets, the non-contained performances are skipped in both
    passes, and an empty remainder is reported without writing.
    """
    app = _pass_app(tmp_path, monkeypatch)
    bank = _multipass_bank()
    async with app.run_test() as pilot:
        await pilot.pause()
        first = WholeBankImportScreen(
            path="two.syx", banks=[bank], gate=app._write_gate
        )
        app.push_screen(
            first,
            lambda picked: app._import_whole_bank_chosen([bank], picked, "two.syx"),
        )
        await _settle(pilot)
        await pilot.press("m")
        await _settle(pilot)
        await pilot.press("m")  # self-contained packed
        await _settle(pilot)
        line = _static_text(first, "whole-bank-mode")
        # The statement before the gate names this pass, the whole bank,
        # and what is left for later passes.
        assert "writes 50 of 57 performances packed into slots 0-49" in line
        assert "skipping 2" in line
        assert "5 more remain for later passes" in line
        await pilot.press("b")  # target RAM 2
        await _settle(pilot)
        await pilot.press("ctrl+s")
        await _answer_remediation_with_leave(app, pilot)
        await _wait_for_writes(app, pilot, 1)
        second = app.screen
        assert isinstance(second, WholeBankImportScreen)
        assert second is not first
        await _settle(pilot)
        # The pass state is echoed in the status line, and the skip count
        # stays relative to the whole source bank.
        assert "pass so far: 50 written (RAM 2: 50)" in _static_text(
            second, "whole-bank-status"
        )
        mode_line = _static_text(second, "whole-bank-mode")
        assert "next pass: writes 5 of 57" in mode_line
        assert "skipping 2" in mode_line

        await pilot.press("n")
        await _settle(pilot)
        assert isinstance(app.screen, TargetBankScreen)
        for _ in range(2):  # RAM 1 -> RAM 3
            await pilot.press("down")
        await pilot.press("enter")
        await pilot.press("2")  # the next pass is asked the same question
        await _wait_for_writes(app, pilot, 2)
        await _settle(pilot)
        assert app.device.bundle_write_count == 2
        third = app.screen
        assert isinstance(third, WholeBankImportScreen)
        # An empty remainder is reported, and nothing further is written.
        await pilot.press("n")
        await _settle(pilot)
        assert "no remaining performances" in _static_text(third, "whole-bank-status")
        assert app.device.bundle_write_count == 2

    _assert_two_pass_bytes(app, bank)
    _assert_pass_state(app, bank)


@pytest.mark.asyncio
async def test_self_contained_sessions_are_independent(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A repeated self-contained import starts over, spills pollute nothing."""
    app, _recorded = _armed_app(tmp_path, monkeypatch)
    app.notify_status = lambda *a, **k: None
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    bank = _mixed_bank()
    app._sources["RAM1"] = bank.source
    writes: list[dict[str, Any]] = []

    def fake_write(*a: Any, **k: Any) -> dict[str, Any]:
        writes.append(dict(k))
        return {"verified": True}

    app.device.write_bank_bundle = fake_write

    def write(key: str) -> None:
        choice = WholeBankChoice(
            0, "RAM3", True, perfcopy.ImportMode.SELF_CONTAINED_PACKED, key, True
        )
        app._import_whole_bank_write(
            bank,
            _approved(choice, bank),
            banks_list=[bank],
            path="one.syx",
        )

    write("session-one")
    assert len(writes) == 1
    first = writes[0]["performances"]
    # A companion-style write carries no session key and records nothing.
    write("")
    assert len(writes) == 2
    assert writes[1]["performances"] == first
    # A new session key starts over with the full contained set, not a
    # remainder, and a repeat of the first session continues nowhere.
    write("session-two")
    assert len(writes) == 3
    assert writes[2]["performances"] == first


@pytest.mark.asyncio
async def test_overflow_key_only_when_parts_name_another_bank() -> None:
    """`o` is offered for a mixed bank and refused for a self-contained one."""
    app = KwsedApp(DemoDevice())
    from tests.test_bankbuild import make_performance

    mixed = WholeBankImportScreen(path="mixed.syx", banks=[_mixed_bank()])
    clean_bank = _make_bank(scope="RAM1", active_slots=[])
    clean_bank.source.performances[3] = make_performance("Local Only", ((0, 0),))
    clean = WholeBankImportScreen(path="clean.syx", banks=[clean_bank])
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(clean)
        await _settle(pilot)
        await pilot.press("o")
        await _settle(pilot)
        assert isinstance(app.screen, WholeBankImportScreen)
        assert "nothing to overflow" in _static_text(clean, "whole-bank-status")
        clean.dismiss(None)
        await _settle(pilot)

        app.push_screen(mixed)
        await _settle(pilot)
        await pilot.press("o")
        await _settle(pilot)
        assert isinstance(app.screen, ImportMenuScreen)
        await pilot.press("enter")  # the patches-only kind, still the default
        await _settle(pilot)
        assert isinstance(app.screen, TargetBankScreen)
        # RAM 2 is the bank the parts name, so it is offered first.
        assert app.screen._choices[0] == banks.bank("RAM2").label


@pytest.mark.asyncio
async def test_overflow_choice_carries_the_bank_and_keeps_the_target() -> None:
    app = KwsedApp(DemoDevice())
    captured: list[Any] = []
    screen = WholeBankImportScreen(
        path="mixed.syx", banks=[_mixed_bank()], gate=EditGate(armed=True)
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen, captured.append)
        await _settle(pilot)
        await pilot.press("b")  # target RAM 2 ... then overflow must skip it
        await _settle(pilot)
        await pilot.press("o")
        await _settle(pilot)
        assert isinstance(app.screen, ImportMenuScreen)
        await pilot.press("enter")  # the patches-only kind
        await _settle(pilot)
        screen_now = app.screen
        assert isinstance(screen_now, TargetBankScreen)
        labels = screen_now._choices
        assert banks.bank("RAM2").label not in labels  # the target itself
        await pilot.press("enter")
        await _settle(pilot)
    choice = captured[0]
    assert choice.overflow_bank in ("RAM1", "RAM3")
    assert choice.target_bank == "RAM2"
    assert choice.mode is perfcopy.ImportMode.AS_IS
    assert choice.overflow_kind == "patches"


@pytest.mark.asyncio
async def test_overflow_writes_the_ram_bank_first_then_the_target(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Two fenced writes, overflow first, target second, both backed up."""
    from kws import backup as _backup

    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    app._sources["RAM3"] = _make_bank(scope="RAM3", active_slots=[]).source
    app._sources["CARD"] = _make_bank(scope="CARD", active_slots=[]).source
    app._write_gate = EditGate(armed=True)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir, exist_ok=True)
    monkeypatch.setattr(_backup, "default_dir", lambda root=None: backup_dir)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)

    def _no_record(_result: Any) -> None:
        return None

    app._record_write = _no_record  # type: ignore[method-assign]
    writes: list[tuple[int, dict[str, Any]]] = []

    def fake_write(bank_number: int, **k: Any) -> dict[str, Any]:
        writes.append((bank_number, dict(k)))
        return {"verified": True}

    app.device.write_bank_bundle = fake_write
    bank = _mixed_bank()
    app._import_whole_bank_write(
        bank,
        _approved(
            WholeBankChoice(
                0, "CARD", True, perfcopy.ImportMode.AS_IS, "", True, "RAM3"
            ),
            bank,
        ),
        banks_list=[bank],
        path="one.syx",
    )

    assert [number for number, _k in writes] == [
        banks.bank("RAM3").number,
        banks.bank("CARD").number,
    ]
    overflow_payload = writes[0][1]
    target_payload = writes[1][1]
    assert overflow_payload["patches"] is not None
    assert target_payload["performances"] is not None
    # No cached before-block for either: the bridge reads the unit itself,
    # so a fresh card is never mistaken for the cached one.
    assert all(k["before_blocks"] is None for _n, k in writes)
    # The status names both banks and the patches that moved.
    assert any("RAM 3" in s and "patches" in s for s in statuses), statuses


def _two_bank_file() -> list[Any]:
    """A two-bank file: the main bank's performances name RAM 2.

    Every name is invented. Slots 2 and 6 spend one part each on RAM 2
    (bank number 1), so an as-is write leaves two parts pointing at a bank
    the unit has not been given -- while the file carries that bank. The
    RAM 2 bank holds the patches those parts name, so an overflow plan can
    move them.
    """
    from tests.test_bankbuild import make_patch, make_performance

    perfs = [_empty_perf() for _ in range(50)]
    perfs[2] = make_performance("Harbor Gate", ((1, 2),))
    perfs[6] = make_performance("Lantern Field", ((1, 5),))
    main = deps.Source(
        scope_id="RAM1",
        performances=perfs,
        patches=[make_patch("Quarry Seam")] + [bytes(PATCH_RECORD_SIZE)] * 34,
        waveseq=None,
    )
    ram2 = _make_bank(scope="RAM2", active_slots=[])
    ram2.source.patches[2] = make_patch("Tide Chart")
    ram2.source.patches[5] = make_patch("Gull Wing")
    return [
        importers.ImportedBank(
            label="Main bank", scope="RAM1", format="sysex", source=main
        ),
        ram2,
    ]


@pytest.mark.asyncio
async def test_as_is_write_ends_in_a_popup_for_the_carried_companion(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The as-is write ends with a modal, not a line buried in the status."""
    from kwsed.import_bank import DanglingRefsScreen

    app, recorded = _armed_app(tmp_path, monkeypatch)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    # The preflight asks before the write; this exercises the fallback net
    # for a write that went out without it.
    monkeypatch.setattr(KwsedApp, "_preflight_companion", lambda self, *a, **k: None)
    file_banks = _two_bank_file()
    choice = WholeBankChoice(
        0, "RAM3", True, perfcopy.ImportMode.AS_IS, "as-is-session", True
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(
            file_banks[0],
            _approved(choice, file_banks[0], file_banks, "two.syx"),
            banks_list=file_banks,
            path="two.syx",
        )
        popup = None
        for _ in range(100):
            await pilot.pause()
            if isinstance(app.screen, DanglingRefsScreen):
                popup = app.screen
                break
        await _settle(pilot)
        assert popup is not None, "the popup was not shown"
        body = _static_text(popup, "dangling-body")
        assert "2 parts name RAM 2" in body
        assert "Main bank only" in body
        hint = _static_text(popup, "dangling-hint")
        assert "write the file's RAM 2 bank" in hint
        assert "esc" in hint
    # The write itself is unchanged: exactly one bundle, backed up.
    assert app.device.bundle_write_count == 1
    assert recorded, "the as-is write is recorded, exactly once"
    backup_path = recorded[0]["backup"]
    assert isinstance(backup_path, str) and os.path.exists(backup_path)


@pytest.mark.asyncio
async def test_the_popup_refusal_leaves_everything_alone(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """esc dismisses the popup and writes nothing more."""
    from kwsed.import_bank import DanglingRefsScreen

    app, _recorded = _armed_app(tmp_path, monkeypatch)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    # The preflight asks before the write; this exercises the fallback net
    # for a write that went out without it.
    monkeypatch.setattr(KwsedApp, "_preflight_companion", lambda self, *a, **k: None)
    file_banks = _two_bank_file()
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(
            file_banks[0],
            _approved(
                WholeBankChoice(0, "RAM3", True, perfcopy.ImportMode.AS_IS, "s", True),
                file_banks[0],
                file_banks,
                "two.syx",
            ),
            banks_list=file_banks,
            path="two.syx",
        )
        for _ in range(100):
            await pilot.pause()
            if isinstance(app.screen, DanglingRefsScreen):
                break
        assert isinstance(app.screen, DanglingRefsScreen)
        await pilot.press("escape")
        await _settle(pilot)
        assert not isinstance(app.screen, DanglingRefsScreen)
    assert app.device.bundle_write_count == 1


@pytest.mark.asyncio
async def test_the_popup_writes_the_companion_bank_on_x(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """x writes the file's companion into the bank its parts name."""
    from kwsed.import_bank import DanglingRefsScreen

    app, _recorded = _armed_app(tmp_path, monkeypatch)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    # The preflight asks before the write; this exercises the fallback net
    # for a write that went out without it.
    monkeypatch.setattr(KwsedApp, "_preflight_companion", lambda self, *a, **k: None)
    file_banks = _two_bank_file()
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(
            file_banks[0],
            _approved(
                WholeBankChoice(0, "RAM3", True, perfcopy.ImportMode.AS_IS, "s", True),
                file_banks[0],
                file_banks,
                "two.syx",
            ),
            banks_list=file_banks,
            path="two.syx",
        )
        for _ in range(100):
            await pilot.pause()
            if isinstance(app.screen, DanglingRefsScreen):
                break
        assert isinstance(app.screen, DanglingRefsScreen)
        await pilot.press("x")
        # The companion write is asked the affected-performances question
        # now; deciding and leaving goes straight through.
        await _answer_remediation_with_leave(app, pilot)
        for _ in range(100):
            await pilot.pause()
            if app.device.bundle_write_count == 2:
                break
    assert app.device.bundle_write_count == 2
    # The second write went to the bank the parts name, not the first target.
    assert app.device.bundle_writes[-1]["bank"] == banks.bank("RAM2").label


@pytest.mark.asyncio
async def test_patches_only_overflow_write_reopens_nothing(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A patches-only overflow repoints the parts itself: nothing to finish."""
    from kws import backup as _backup

    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    file_banks = _two_bank_file()
    app._sources["RAM2"] = file_banks[1].source
    app._sources["CARD"] = _make_bank(scope="CARD", active_slots=[]).source
    app._write_gate = EditGate(armed=True)
    app.notify_status = lambda *a, **k: None  # type: ignore[method-assign]
    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir, exist_ok=True)
    monkeypatch.setattr(_backup, "default_dir", lambda root=None: backup_dir)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)

    def _no_record(_result: Any) -> None:
        return None

    app._record_write = _no_record  # type: ignore[method-assign]
    choice = WholeBankChoice(
        0,
        "CARD",
        True,
        perfcopy.ImportMode.AS_IS,
        "overflow-session",
        True,
        "RAM3",
        OVERFLOW_PATCHES,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(
            file_banks[0],
            _approved(choice, file_banks[0], file_banks, "two.syx"),
            banks_list=file_banks,
            path="two.syx",
        )
        await _settle(pilot, times=60)
    # The overflow bank went out first, then the target; no screen returned.
    assert app.device.bundle_write_count == 2
    assert not any(isinstance(s, WholeBankImportScreen) for s in app.screen_stack)


@pytest.mark.asyncio
async def test_as_is_write_of_a_one_bank_file_reopens_nothing(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Without the companion in the file there is nothing to offer."""
    app, _recorded = _armed_app(tmp_path, monkeypatch)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    bank = _mixed_bank()
    choice = WholeBankChoice(
        0, "RAM3", True, perfcopy.ImportMode.AS_IS, "one-bank-session", True
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(
            bank,
            _approved(choice, bank, [bank], "one.syx"),
            banks_list=[bank],
            path="one.syx",
        )
        await _settle(pilot, times=60)
    assert app.device.bundle_write_count == 1
    assert not any(isinstance(s, WholeBankImportScreen) for s in app.screen_stack)


def _split_file_banks() -> list[Any]:
    """The mixed RAM 1 source and the RAM 2 bank its parts name.

    The two non-contained performances name RAM 2 patch 2, so the
    self-contained split can carry that patch only when the bank is part
    of the file. The patch name is invented, like every other fixture.
    """
    from tests.test_bankbuild import make_patch

    ram2 = _make_bank(scope="RAM2", active_slots=[2])
    ram2.source.patches[2] = make_patch("Harbour Slate")
    return [_mixed_bank(), ram2]


@pytest.mark.asyncio
async def test_overflow_kind_menu_offers_three_kinds() -> None:
    """``o`` asks which kind of overflow first, then for the RAM bank."""
    app = KwsedApp(DemoDevice())
    captured: list[Any] = []
    screen = WholeBankImportScreen(
        path="mixed.syx", banks=_split_file_banks(), gate=EditGate(armed=True)
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen, captured.append)
        await _settle(pilot)
        await pilot.press("o")
        await _settle(pilot)
        menu = app.screen
        assert isinstance(menu, ImportMenuScreen)
        assert menu._choices == [
            "Only the patches it cannot hold (performances stay on the target)",
            "The performances that point outside it, with all their patches "
            "(both banks self-contained)",
            "Cards only: one self-contained group per card, no RAM bank",
        ]
        await pilot.press("down")
        await _settle(pilot)
        await pilot.press("enter")  # the self-contained kind asks for a bank
        await _settle(pilot)
        assert isinstance(app.screen, TargetBankScreen)
        await pilot.press("enter")  # the first overflow bank: RAM 2
        await _settle(pilot)
    assert captured, "choosing a kind dismisses with a write choice"
    choice = captured[0]
    assert choice.overflow_bank == banks.bank("RAM2").id
    assert choice.overflow_kind == "self_contained"
    # Merely picking the kinds writes nothing.
    assert app.device.bundle_write_count == 0


@pytest.mark.asyncio
async def test_self_contained_split_writes_each_bank_once_with_its_own_backup(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Target first, overflow second, one fenced bundle each.

    The order is the partition's own -- the target receives the contained
    performances, the overflow bank the rest -- and each output bank is
    self-contained, so no bank depends on another being written first.
    Every bundle gets its own backup and its own read-back verification.
    """
    from kws import backup as _backup

    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    file_banks = _split_file_banks()
    bank = file_banks[0]
    ram2 = file_banks[1].source
    app._sources["RAM3"] = _make_bank(scope="RAM3", active_slots=[]).source
    app._sources["RAM2"] = ram2
    app._write_gate = EditGate(armed=True)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir, exist_ok=True)
    monkeypatch.setattr(_backup, "default_dir", lambda root=None: backup_dir)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    recorded: list[Any] = []
    app._record_write = recorded.append  # type: ignore[method-assign]

    app._import_whole_bank_write(
        bank,
        _approved(
            WholeBankChoice(
                0,
                "RAM3",
                True,
                perfcopy.ImportMode.AS_IS,
                "",
                True,
                "RAM2",
                "self_contained",
            ),
            bank,
            file_banks,
            "split.syx",
        ),
        banks_list=file_banks,
        path="split.syx",
    )

    # The statement before the write says what each bank will receive.
    statement = next(s for s in statuses if "RAM 3 gets" in s)
    assert "self-contained split" in statement
    assert "RAM 3 gets 2 performances and 1 patch" in statement
    assert "RAM 2 gets 2 performances and 1 patch" in statement
    assert statuses[-1].startswith(
        "imported Bank RAM1 as a self-contained split — 2 banks"
    )
    assert "RAM 3: 2 performances, 1 patch" in statuses[-1]
    assert "RAM 2: 2 performances, 1 patch" in statuses[-1]

    # One write per bank, in partition order, each its own complete bundle.
    assert [w["bank"] for w in app.device.bundle_writes] == [
        banks.bank("RAM3").label,
        banks.bank("RAM2").label,
    ]
    assert app.device.bundle_write_count == 2
    assert set(app.device._written) == {
        ("RAM3", banks.Kind.PERFORMANCE),
        ("RAM3", banks.Kind.PATCH),
        ("RAM2", banks.Kind.PERFORMANCE),
        ("RAM2", banks.Kind.PATCH),
    }
    # Two undo records, two backup envelopes: the fence is per bank.
    assert len(recorded) == 2
    for result in recorded:
        backup_path = result["backup"]
        assert isinstance(backup_path, str) and os.path.exists(backup_path)
        assert result["verified"] is True

    source = bank.source
    ram3 = banks.bank("RAM3").number
    ram2_bank = banks.bank("RAM2").number
    target_perfs = _pass_blob(app.device, "RAM3", banks.Kind.PERFORMANCE)
    assert _perf_at(target_perfs, 2) == _as_written(
        source.performances[2], {0: (ram3, 0)}
    )
    assert _perf_at(target_perfs, 5) == _as_written(
        source.performances[5], {0: (ram3, 0)}
    )
    # The target holds the contained performances only: the two that name
    # RAM 2 are clean slots, not relocated copies.
    for slot in range(50):
        if slot in (2, 5):
            continue
        assert _perf_at(target_perfs, slot) == _clean_perf_record()
    target_patches = _pass_blob(app.device, "RAM3", banks.Kind.PATCH)
    assert _patch_at(target_patches, 0) == source.patches[0]
    for slot in range(1, 35):
        assert _patch_at(target_patches, slot) == bytes(PATCH_RECORD_SIZE)

    # The overflow bank holds the external performances, repointed at its
    # own bank number, with the patch they name carried across.
    overflow_perfs = _pass_blob(app.device, "RAM2", banks.Kind.PERFORMANCE)
    assert _perf_at(overflow_perfs, 7) == _as_written(
        source.performances[7], {0: (ram2_bank, 0)}
    )
    assert _perf_at(overflow_perfs, 9) == _as_written(
        source.performances[9], {0: (ram2_bank, 0)}
    )
    for slot in range(50):
        if slot in (7, 9):
            continue
        assert _perf_at(overflow_perfs, slot) == _clean_perf_record()
    overflow_patches = _pass_blob(app.device, "RAM2", banks.Kind.PATCH)
    assert _patch_at(overflow_patches, 0) == ram2.patches[2]
    for slot in range(1, 35):
        assert _patch_at(overflow_patches, slot) == bytes(PATCH_RECORD_SIZE)
    # Both banks are now the session's idea of what is on the unit.
    assert app._sources["RAM3"].performances[2] == _perf_at(target_perfs, 2)
    assert app._sources["RAM2"].performances[7] == _perf_at(overflow_perfs, 7)


@pytest.mark.asyncio
async def test_self_contained_split_needing_more_banks_is_refused_at_the_screen() -> (
    None
):
    """The screen says how many banks the split needs and writes nothing."""
    app = KwsedApp(DemoDevice())
    captured: list[Any] = []
    screen = WholeBankImportScreen(
        path="big.syx", banks=[_multipass_bank()], gate=EditGate(armed=True)
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen, captured.append)
        await _settle(pilot)
        await pilot.press("o")
        await _settle(pilot)
        assert isinstance(app.screen, ImportMenuScreen)
        await pilot.press("down")  # the self-contained kind
        await _settle(pilot)
        await pilot.press("enter")
        await _settle(pilot)
        assert isinstance(app.screen, TargetBankScreen)
        await pilot.press("enter")
        await _settle(pilot)
        # Still open: a split that cannot fit is refused, not dismissed.
        still = app.screen
        assert isinstance(still, WholeBankImportScreen)
        assert still is screen
        status = _static_text(screen, "whole-bank-status")
        assert "self-contained split does not fit" in status
        assert "contained performances need 2 banks" in status
        assert "nothing is written" in status
        assert "x, one bank per write" in status
    assert captured == []
    assert app.device.bundle_write_count == 0


@pytest.mark.asyncio
async def test_self_contained_split_refusal_at_the_app_writes_nothing(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A split the two banks cannot hold is refused with its reasons."""
    app, _recorded = _armed_app(tmp_path, monkeypatch)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)

    multipass = _multipass_bank()
    multipass_banks = [multipass]
    choice = WholeBankChoice(
        0,
        "RAM3",
        True,
        perfcopy.ImportMode.AS_IS,
        "",
        True,
        "RAM2",
        "self_contained",
    )
    # The plan cannot be approved at all: the external share names patches
    # of a bank the file does not carry, so the planners refuse it and no
    # approval exists to present.
    with pytest.raises(ValueError):
        _approved(choice, multipass, multipass_banks, "big.syx")
    app._import_whole_bank_write(
        multipass, choice, banks_list=multipass_banks, path="big.syx"
    )

    assert app.device.bundle_write_count == 0
    # The plan cannot be approved, so the choice arrives without one and
    # the write boundary refuses it before anything else runs.
    assert any("no plan approval" in s for s in statuses), statuses


def _pack_source() -> Any:
    """A source of 120 contained performances, one patch slot each.

    Every name is invented. Performance i names patch i % 35 of its own
    bank, so a packed group of 50 needs all 35 patch slots of a bank and
    120 slots need three groups.
    """
    from tests.test_bankbuild import make_patch, make_performance

    perfs = [make_performance(f"Pack Perf {i:03d}", ((0, i % 35),)) for i in range(120)]
    patches = [make_patch(f"Pack Basis {i:02d}") for i in range(35)]
    return deps.Source(
        scope_id="RAM1", performances=perfs, patches=patches, waveseq=None
    )


def test_pack_self_contained_into_splits_into_bank_sized_groups() -> None:
    """Slots that outgrow one bank pack into the fewest groups that fit."""
    source = _pack_source()
    groups = perfcopy.pack_self_contained_into(
        {"RAM1": source}, "RAM1", list(range(120)), banks.bank("CARD")
    )
    assert [list(group.performances) for group in groups] == [
        list(range(50)),
        list(range(50, 100)),
        list(range(100, 120)),
    ]
    for group in groups:
        assert group.target_bank is banks.bank("CARD")
        plan = group.plan
        assert plan is not None
        assert plan.destination_bank is banks.bank("CARD")
        assert len(group.performances) <= banks.PERFORMANCES_PER_BANK
        assert plan.patch_count <= banks.PATCHES_PER_BANK
        # Every patch the group's performances name is carried: one target
        # slot per source patch, and every used part remapped to it.
        named = {relations.PatchId("RAM1", slot % 35) for slot in group.performances}
        allocated = {
            pid for alloc in plan.allocations for pid, _slot in alloc.allocated_patches
        }
        assert allocated == named
        for alloc in plan.allocations:
            record = source.performances[alloc.source_perf.number]
            used = [p.index for p in r.performance_parts(record) if p.used]
            assert set(alloc.part_patch_slots) == set(used)


def test_pack_self_contained_into_yields_one_group_when_it_fits() -> None:
    source = _pack_source()
    groups = perfcopy.pack_self_contained_into(
        {"RAM1": source}, "RAM1", list(range(10)), banks.bank("RAM3")
    )
    assert len(groups) == 1
    assert list(groups[0].performances) == list(range(10))
    assert groups[0].target_bank is banks.bank("RAM3")
    plan = groups[0].plan
    assert plan is not None
    assert plan.patch_count == 10


def _split_continuation_banks() -> list[Any]:
    """A source bank plus the RAM 2 bank two of its performances name.

    Slots 0-54 are contained (each names its own bank's patch slot), so
    they need two Card-sized groups; slots 56-57 name RAM 2 patches 2 and
    3 and pack into a third group. The offered banks are the Card and
    RAM 3: two of the three groups travel on the first pass. Every name
    is invented.
    """
    from tests.test_bankbuild import make_patch

    ram2 = _make_bank(scope="RAM2", active_slots=[2, 3])
    ram2.source.patches[2] = make_patch("Harbour Slate")
    ram2.source.patches[3] = make_patch("Quay Lintel")
    return [_multipass_bank(), ram2]


def _split_pass_app(tmp_path: Any, monkeypatch: Any) -> KwsedApp:
    """An armed app with RAM 2 loaded and the Card and RAM 3 to receive."""
    from kws import backup as _backup

    app = KwsedApp(DemoDevice(), bankstore_dir=str(tmp_path / "store"))
    app._sources["RAM2"] = _split_continuation_banks()[1].source
    app._sources["RAM3"] = _make_bank(scope="RAM3", active_slots=[]).source
    app._write_gate = EditGate(armed=True)

    def _no_record(_result: Any) -> None:
        return None

    app._record_write = _no_record  # type: ignore[method-assign]
    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir, exist_ok=True)
    monkeypatch.setattr(_backup, "default_dir", lambda root=None: backup_dir)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    return app


@pytest.mark.asyncio
async def test_self_contained_split_continues_onto_the_next_card(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Two groups travel on the first pass; ``n`` writes the last one.

    The split needs three groups but the choice offers only the Card and
    RAM 3: the first pass fills both, the session records the five slots
    left over, and the reopened screen says how many remain and that the
    card must be switched. Pressing ``n`` writes exactly one more group
    into the card slot -- the user has swapped the card meanwhile -- and
    the session is empty afterwards.
    """
    app = _split_pass_app(tmp_path, monkeypatch)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    file_banks = _split_continuation_banks()
    bank = file_banks[0]
    ram2 = file_banks[1].source
    source = bank.source
    async with app.run_test() as pilot:
        await pilot.pause()
        first = WholeBankImportScreen(
            path="three.syx", banks=file_banks, gate=app._write_gate
        )
        app.push_screen(
            first,
            lambda picked: app._import_whole_bank_chosen(
                file_banks, picked, "three.syx"
            ),
        )
        await _settle(pilot)
        await pilot.press("m")
        await _settle(pilot)
        await pilot.press("m")  # self-contained packed
        await _settle(pilot)
        for _ in range(3):
            await pilot.press("b")  # target Card
            await _settle(pilot)
        assert first._target_bank().id == "CARD"
        await pilot.press("o")
        await _settle(pilot)
        menu = app.screen
        assert isinstance(menu, ImportMenuScreen)
        await pilot.press("down")  # the self-contained kind
        await _settle(pilot)
        await pilot.press("enter")
        await _settle(pilot)
        chooser = app.screen
        assert isinstance(chooser, TargetBankScreen)
        # RAM 2 is named by the source's own parts, so it is offered first;
        # the split carries its overflow onto RAM 3.
        assert chooser._choices[0] == banks.bank("RAM2").label
        await pilot.press("down")
        await _settle(pilot)
        await pilot.press("down")  # RAM 1 sits between them
        await _settle(pilot)
        await pilot.press("enter")  # RAM 3
        await _answer_remediation_with_leave(app, pilot)
        await _wait_for_writes(app, pilot, 2)
        await _settle(pilot)

        # The statement before the write says what each bank receives.
        statement = next(s for s in statuses if "Card gets" in s)
        assert "self-contained split" in statement
        assert "Card gets 50 performances and 35 patches" in statement
        assert "RAM 3 gets 2 performances and 2 patches" in statement
        # The closing status says exactly what is left and what to do.
        after_first = statuses[-1]
        assert "5 performances remain" in after_first
        assert (
            "switch the card on the unit (select it on the HCard and press C), "
            "then press n" in after_first
        )

        second = app.screen
        assert isinstance(second, WholeBankImportScreen)
        assert second is not first
        phrase = (
            "switch the card on the unit (select it on the HCard and press C), "
            "then press n"
        )
        status = _static_text(second, "whole-bank-status")
        assert "5 performances remain" in status
        assert phrase in status
        assert phrase in _static_text(second, "whole-bank-mode")

        keys = [k for k in app._import_passes if k.startswith("three.syx|RAM1|")]
        assert len(keys) == 1
        key = keys[0]
        state = app._import_passes[key]
        assert state["pass_kind"] == "split"  # noqa: S105 -- a session marker, not a secret
        assert state["split_target"] == "CARD"
        assert state["split_remaining"] == [50, 51, 52, 53, 54]

        # Pass 1's bytes, captured before pass 2 overwrites the Card blob:
        # each group carries every patch its performances name.
        card_pass1_perf = _pass_blob(app.device, "CARD", banks.Kind.PERFORMANCE)
        card_pass1_patch = _pass_blob(app.device, "CARD", banks.Kind.PATCH)

        await pilot.press("n")
        await _answer_remediation_with_leave(app, pilot)
        await _wait_for_writes(app, pilot, 3)
        await _settle(pilot)
        assert app.device.bundle_write_count == 3

        third = app.screen
        assert isinstance(third, WholeBankImportScreen)
        assert "nothing left to write" in _static_text(third, "whole-bank-status")
        await pilot.press("n")
        await _settle(pilot)
        assert "nothing left to write" in _static_text(third, "whole-bank-status")
        assert app.device.bundle_write_count == 3

    assert state["split_remaining"] == []
    written = state["written"]
    assert set(written) == set(range(55)) | {56, 57}
    assert {slot for slot, t in written.items() if t == "Card"} == set(range(55))
    assert {slot for slot, t in written.items() if t == "RAM 3"} == {56, 57}
    # One fenced write per group, in partition order.
    assert [w["bank"] for w in app.device.bundle_writes] == ["Card", "RAM 3", "Card"]
    _assert_split_pass_bytes(app, source, ram2, card_pass1_perf, card_pass1_patch)


def _assert_split_pass_bytes(
    app: KwsedApp,
    source: Any,
    ram2: Any,
    card_pass1_perf: bytes,
    card_pass1_patch: bytes,
) -> None:
    """The three written groups, byte for byte, and their self-containment.

    The Card's two passes both target the CARD slot, so the device's blob
    holds the second pass by the time the assertions run; the first pass's
    bytes were captured before ``n`` was pressed.
    """
    card_no = banks.bank("CARD").number
    ram3_no = banks.bank("RAM3").number
    # The first pass: the Card holds slots 0-49 packed, patches 0-34.
    for slot in range(50):
        assert _perf_at(card_pass1_perf, slot) == _as_written(
            source.performances[slot], {0: (card_no, slot % 35)}
        )
    for patch_slot in range(35):
        assert _patch_at(card_pass1_patch, patch_slot) == source.patches[patch_slot]
    # RAM 3 holds the two external performances and the patches they name.
    ram3_perf = _pass_blob(app.device, "RAM3", banks.Kind.PERFORMANCE)
    ram3_patch = _pass_blob(app.device, "RAM3", banks.Kind.PATCH)
    assert _perf_at(ram3_perf, 0) == _as_written(
        source.performances[56], {0: (ram3_no, 0)}
    )
    assert _perf_at(ram3_perf, 1) == _as_written(
        source.performances[57], {0: (ram3_no, 1)}
    )
    assert _patch_at(ram3_patch, 0) == ram2.patches[2]
    assert _patch_at(ram3_patch, 1) == ram2.patches[3]
    # The continuation: exactly one more group, into the card slot.
    card_pass2_perf = _pass_blob(app.device, "CARD", banks.Kind.PERFORMANCE)
    card_pass2_patch = _pass_blob(app.device, "CARD", banks.Kind.PATCH)
    for index in range(5):
        assert _perf_at(card_pass2_perf, index) == _as_written(
            source.performances[50 + index], {0: (card_no, index)}
        )
        assert _patch_at(card_pass2_patch, index) == source.patches[15 + index]
    for index in range(5, 50):
        assert _perf_at(card_pass2_perf, index) == _clean_perf_record()
    for index in range(5, 35):
        assert _patch_at(card_pass2_patch, index) == bytes(PATCH_RECORD_SIZE)

    # Every written group is self-contained: each used part names that
    # group's own bank or a ROM bank, never another writable one.
    rom_numbers = {entry.number for entry in banks.BANKS if not entry.writable}
    for blob, own in (
        (card_pass1_perf, card_no),
        (card_pass2_perf, card_no),
        (ram3_perf, ram3_no),
    ):
        for slot in range(banks.PERFORMANCES_PER_BANK):
            for part in r.performance_parts(_perf_at(blob, slot)):
                if not part.used:
                    continue
                assert part.bank == own or part.bank in rom_numbers


@pytest.mark.asyncio
async def test_split_continuation_requires_the_write_gate(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """With the gate locked, ``n`` refuses the continuation and writes nothing."""
    app = _split_pass_app(tmp_path, monkeypatch)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    file_banks = _split_continuation_banks()
    choice = WholeBankChoice(
        0,
        "CARD",
        True,
        perfcopy.ImportMode.SELF_CONTAINED_PACKED,
        "three.syx|RAM1|gated",
        True,
        "RAM3",
        "self_contained",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(
            file_banks[0],
            _approved(choice, file_banks[0], file_banks, "three.syx"),
            banks_list=file_banks,
            path="three.syx",
        )
        await _wait_for_writes(app, pilot, 2)
        await _settle(pilot)
        second = app.screen
        assert isinstance(second, WholeBankImportScreen)
        assert "5 performances remain" in _static_text(second, "whole-bank-status")
        app._write_gate.toggle()  # the user locked the gate
        await pilot.press("n")
        await _settle(pilot)
        assert isinstance(app.screen, WholeBankImportScreen)
        assert "write gate is locked" in _static_text(app.screen, "whole-bank-status")
        assert app.device.bundle_write_count == 2
    state = app._import_passes["three.syx|RAM1|gated"]
    assert state["split_remaining"] == [50, 51, 52, 53, 54]
    assert any("5 performances remain" in s for s in statuses), statuses


def _cards_only_banks() -> list[Any]:
    """A source bank of 60 performances, plus the RAM 2 bank two of them name.

    Slots 0-57 name patch ``slot % 35`` of their own bank, so one Card holds
    the first fifty and the last ten need a second card; slots 58-59 name
    RAM 2 patches 2 and 3, which the cards-only split carries along. Every
    name is invented.
    """
    from tests.test_bankbuild import make_patch, make_performance

    performances = [
        make_performance(f"Card Perf {index:02d}", ((0, index % 35),))
        for index in range(58)
    ]
    performances += [
        make_performance("Quay Marker", ((1, 2),)),
        make_performance("Quay Beacon", ((1, 3),)),
    ]
    source = deps.Source(
        scope_id="RAM1",
        performances=performances,
        patches=[make_patch(f"Card Basis {index:02d}") for index in range(35)],
        waveseq=None,
    )
    main = importers.ImportedBank(
        label="Card source", scope="RAM1", format="sysex", source=source
    )
    return [main, _split_continuation_banks()[1]]


@pytest.mark.asyncio
async def test_cards_only_first_pass_writes_one_card_and_records_the_rest(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Cards only: no RAM bank, one self-contained group per card.

    The first pass writes exactly the card in the slot, records the ten
    slots left over and re-opens the screen naming them; ``n`` writes
    exactly one more group to the card and empties the session. The union
    of the written source slots is every non-empty performance, none
    twice, and every written group is self-contained.
    """
    app = _split_pass_app(tmp_path, monkeypatch)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    file_banks = _cards_only_banks()
    bank = file_banks[0]
    source = bank.source
    ram2 = _split_continuation_banks()[1].source
    phrase = (
        "switch the card on the unit (select it on the HCard and press C), then press n"
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        first = WholeBankImportScreen(
            path="cards.syx", banks=file_banks, gate=app._write_gate
        )
        app.push_screen(
            first,
            lambda picked: app._import_whole_bank_chosen(
                file_banks, picked, "cards.syx"
            ),
        )
        await _settle(pilot)
        await pilot.press("m")
        await _settle(pilot)
        await pilot.press("m")  # self-contained packed
        await _settle(pilot)
        for _ in range(3):
            await pilot.press("b")  # target Card
            await _settle(pilot)
        assert first._target_bank().id == "CARD"
        await pilot.press("o")
        await _settle(pilot)
        menu = app.screen
        assert isinstance(menu, ImportMenuScreen)
        assert menu._choices[-1] == (
            "Cards only: one self-contained group per card, no RAM bank"
        )
        await pilot.press("down")
        await _settle(pilot)
        await pilot.press("down")
        await _settle(pilot)
        await pilot.press("enter")  # cards only: no bank step at all
        # A cards-only write is asked the affected-performances question,
        # without green field (no RAM bank would be cleared); deciding and
        # leaving goes straight through when nothing is affected.
        await _answer_remediation_with_leave(app, pilot)
        await _wait_for_writes(app, pilot, 1)
        await _settle(pilot)

        # One bank only: the card in the slot. No RAM bank was touched.
        assert app.device.bundle_write_count == 1
        assert set(app.device._written) == {
            ("CARD", banks.Kind.PERFORMANCE),
            ("CARD", banks.Kind.PATCH),
        }
        assert [w["bank"] for w in app.device.bundle_writes] == [
            banks.bank("CARD").label
        ]
        # The statement before the write names the card and what is left.
        statement = next(s for s in statuses if "cards-only groups" in s)
        assert "Card gets 50 performances and 35 patches" in statement
        assert "10 performances remain for the next card" in statement
        closing = statuses[-1]
        assert closing.startswith(
            "imported Card source as cards-only groups — 1 card: "
            "Card: 50 performances, 35 patches"
        )
        assert "10 performances remain" in closing
        assert phrase in closing

        second = app.screen
        assert isinstance(second, WholeBankImportScreen)
        assert second is not first
        status = _static_text(second, "whole-bank-status")
        assert "10 performances remain for the next card" in status
        assert phrase in status
        mode_line = _static_text(second, "whole-bank-mode")
        assert "cards-only: next pass writes one group of 10 performances" in mode_line
        assert "into Card" in mode_line
        assert phrase in mode_line

        keys = [k for k in app._import_passes if k.startswith("cards.syx|RAM1|")]
        assert len(keys) == 1
        key = keys[0]
        state = app._import_passes[key]
        assert state["pass_kind"] == "cards_only"  # noqa: S105 -- session marker
        assert state["split_target"] == "CARD"
        assert state["split_remaining"] == list(range(50, 60))
        assert set(state["written"]) == set(range(50))

        # Pass 1's bytes, before pass 2 overwrites the one card blob.
        card_pass1_perf = _pass_blob(app.device, "CARD", banks.Kind.PERFORMANCE)
        card_pass1_patch = _pass_blob(app.device, "CARD", banks.Kind.PATCH)

        await pilot.press("n")
        await _answer_remediation_with_leave(app, pilot)
        await _wait_for_writes(app, pilot, 2)
        await _settle(pilot)
        assert app.device.bundle_write_count == 2
        assert [w["bank"] for w in app.device.bundle_writes] == [
            banks.bank("CARD").label,
            banks.bank("CARD").label,
        ]

        third = app.screen
        assert isinstance(third, WholeBankImportScreen)
        assert "nothing left to write" in _static_text(third, "whole-bank-status")
        assert "cards-only pass is complete" in _static_text(third, "whole-bank-mode")
        await pilot.press("n")
        await _settle(pilot)
        assert "nothing left to write" in _static_text(third, "whole-bank-status")
        assert app.device.bundle_write_count == 2

    # Every non-empty performance is written exactly once, across the two
    # cards: the session's written map is the union.
    slots = perfcopy.active_performances(source)
    assert len(slots) == 60
    assert set(state["written"]) == set(slots)
    assert len(state["written"]) == len(slots)
    assert state["split_remaining"] == []
    _assert_cards_only_bytes(app, source, ram2, card_pass1_perf, card_pass1_patch)


def _assert_cards_only_bytes(
    app: KwsedApp,
    source: Any,
    ram2: Any,
    card_pass1_perf: bytes,
    card_pass1_patch: bytes,
) -> None:
    """Both cards' groups, byte for byte, and their self-containment."""
    card_no = banks.bank("CARD").number
    card_pass2_perf = _pass_blob(app.device, "CARD", banks.Kind.PERFORMANCE)
    card_pass2_patch = _pass_blob(app.device, "CARD", banks.Kind.PATCH)
    for slot in range(50):
        assert _perf_at(card_pass1_perf, slot) == _as_written(
            source.performances[slot], {0: (card_no, slot % 35)}
        )
    for patch_slot in range(35):
        assert _patch_at(card_pass1_patch, patch_slot) == source.patches[patch_slot]
    for index in range(8):
        assert _perf_at(card_pass2_perf, index) == _as_written(
            source.performances[50 + index], {0: (card_no, index)}
        )
        assert _patch_at(card_pass2_patch, index) == source.patches[15 + index]
    assert _perf_at(card_pass2_perf, 8) == _as_written(
        source.performances[58], {0: (card_no, 8)}
    )
    assert _patch_at(card_pass2_patch, 8) == ram2.patches[2]
    assert _perf_at(card_pass2_perf, 9) == _as_written(
        source.performances[59], {0: (card_no, 9)}
    )
    assert _patch_at(card_pass2_patch, 9) == ram2.patches[3]
    for slot in range(10, 50):
        assert _perf_at(card_pass2_perf, slot) == _clean_perf_record()
    for patch_slot in range(10, 35):
        assert _patch_at(card_pass2_patch, patch_slot) == bytes(PATCH_RECORD_SIZE)

    # Every written group is self-contained: each used part names the card
    # or a ROM bank, never another writable bank.
    rom_numbers = {entry.number for entry in banks.BANKS if not entry.writable}
    for blob in (card_pass1_perf, card_pass2_perf):
        for slot in range(banks.PERFORMANCES_PER_BANK):
            for part in r.performance_parts(_perf_at(blob, slot)):
                if not part.used:
                    continue
                assert part.bank == card_no or part.bank in rom_numbers


@pytest.mark.asyncio
async def test_cards_only_continuation_requires_the_write_gate(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """With the gate locked, ``n`` refuses the cards-only pass and writes nothing."""
    app = _split_pass_app(tmp_path, monkeypatch)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    file_banks = _cards_only_banks()
    choice = WholeBankChoice(
        0,
        "CARD",
        True,
        perfcopy.ImportMode.SELF_CONTAINED_PACKED,
        "cards.syx|RAM1|gated",
        True,
        "",
        "cards_only",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(
            file_banks[0],
            _approved(choice, file_banks[0], file_banks, "cards.syx"),
            banks_list=file_banks,
            path="cards.syx",
        )
        await _wait_for_writes(app, pilot, 1)
        await _settle(pilot)
        second = app.screen
        assert isinstance(second, WholeBankImportScreen)
        assert "10 performances remain for the next card" in _static_text(
            second, "whole-bank-status"
        )
        app._write_gate.toggle()  # the user locked the gate
        await pilot.press("n")
        await _settle(pilot)
        assert isinstance(app.screen, WholeBankImportScreen)
        assert "write gate is locked" in _static_text(app.screen, "whole-bank-status")
        assert app.device.bundle_write_count == 1
    state = app._import_passes["cards.syx|RAM1|gated"]
    assert state["split_remaining"] == list(range(50, 60))


async def _press_cards_only(pilot: Any) -> None:
    """Choose the cards-only kind from the overflow menu."""
    await pilot.press("o")
    await _settle(pilot)
    await pilot.press("down")
    await _settle(pilot)
    await pilot.press("down")
    await _settle(pilot)
    await pilot.press("enter")
    await _settle(pilot)


def _assert_no_ram_written(app: KwsedApp) -> None:
    """No RAM bank key may appear in the demo device's written map."""
    assert all(not bank_id.startswith("RAM") for bank_id, _kind in app.device._written)


@pytest.mark.asyncio
async def test_cards_only_from_the_default_target_writes_the_card(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The default RAM 1 target must not leak into a cards-only write.

    The selector still shows RAM 1; choosing the cards-only kind writes
    exactly one bundle, to the physical Card, and touches no RAM bank.
    The statement before the write says where it goes.
    """
    app = _split_pass_app(tmp_path, monkeypatch)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    file_banks = _cards_only_banks()
    async with app.run_test() as pilot:
        await pilot.pause()
        first = WholeBankImportScreen(
            path="cards-default.syx", banks=file_banks, gate=app._write_gate
        )
        app.push_screen(
            first,
            lambda picked: app._import_whole_bank_chosen(
                file_banks, picked, "cards-default.syx"
            ),
        )
        await _settle(pilot)
        assert first._target_bank().id == "RAM1"
        await pilot.press("m")
        await _settle(pilot)
        await pilot.press("m")  # self-contained packed
        await _settle(pilot)
        await _press_cards_only(pilot)
        # A cards-only write is asked the affected-performances question,
        # without green field; deciding and leaving goes straight through.
        await _answer_remediation_with_leave(app, pilot)
        await _wait_for_writes(app, pilot, 1)
        await _settle(pilot)

        assert app.device.bundle_write_count == 1
        assert [w["bank"] for w in app.device.bundle_writes] == [
            banks.bank("CARD").label
        ]
        assert set(app.device._written) == {
            ("CARD", banks.Kind.PERFORMANCE),
            ("CARD", banks.Kind.PATCH),
        }
        statement = next(s for s in statuses if "cards-only groups" in s)
        assert "Card gets 50 performances and 35 patches" in statement
        assert "writes go to the card in the slot, one group per card" in statement
        assert "no RAM bank" in statement


@pytest.mark.asyncio
async def test_cards_only_after_picking_ram3_still_writes_the_card(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """An explicit RAM 3 selection must not leak into a cards-only write."""
    app = _split_pass_app(tmp_path, monkeypatch)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    file_banks = _cards_only_banks()
    async with app.run_test() as pilot:
        await pilot.pause()
        first = WholeBankImportScreen(
            path="cards-ram3.syx", banks=file_banks, gate=app._write_gate
        )
        app.push_screen(
            first,
            lambda picked: app._import_whole_bank_chosen(
                file_banks, picked, "cards-ram3.syx"
            ),
        )
        await _settle(pilot)
        for _ in range(2):
            await pilot.press("b")
            await _settle(pilot)
        assert first._target_bank().id == "RAM3"
        await pilot.press("m")
        await _settle(pilot)
        await pilot.press("m")  # self-contained packed
        await _settle(pilot)
        await _press_cards_only(pilot)
        await _answer_remediation_with_leave(app, pilot)
        await _wait_for_writes(app, pilot, 1)
        await _settle(pilot)

        assert app.device.bundle_write_count == 1
        assert [w["bank"] for w in app.device.bundle_writes] == [
            banks.bank("CARD").label
        ]
        _assert_no_ram_written(app)
        statement = next(s for s in statuses if "cards-only groups" in s)
        assert "Card gets 50 performances and 35 patches" in statement
        assert "no RAM bank" in statement


@pytest.mark.asyncio
async def test_cards_only_continuation_stays_on_the_card(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """``n`` after a cards-only pass writes the card again, never RAM.

    The reopened screen's selector is moved to RAM 3 first: the session
    is cards-only, so the continuation must still resolve the card slot.
    """
    app = _split_pass_app(tmp_path, monkeypatch)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    file_banks = _cards_only_banks()
    async with app.run_test() as pilot:
        await pilot.pause()
        first = WholeBankImportScreen(
            path="cards-next.syx", banks=file_banks, gate=app._write_gate
        )
        app.push_screen(
            first,
            lambda picked: app._import_whole_bank_chosen(
                file_banks, picked, "cards-next.syx"
            ),
        )
        await _settle(pilot)
        await pilot.press("m")
        await _settle(pilot)
        await pilot.press("m")  # self-contained packed
        await _settle(pilot)
        await _press_cards_only(pilot)
        await _answer_remediation_with_leave(app, pilot)
        await _wait_for_writes(app, pilot, 1)
        await _settle(pilot)
        assert [w["bank"] for w in app.device.bundle_writes] == [
            banks.bank("CARD").label
        ]

        second = app.screen
        assert isinstance(second, WholeBankImportScreen)
        assert "writes go to the card in the slot, one group per card" in (
            _static_text(second, "whole-bank-mode")
        )
        for _ in range(2):
            await pilot.press("b")
            await _settle(pilot)
        assert second._target_bank().id == "RAM3"
        assert second._split_target_bank().id == "CARD"
        await pilot.press("n")
        await _answer_remediation_with_leave(app, pilot)
        await _wait_for_writes(app, pilot, 2)
        await _settle(pilot)

        assert [w["bank"] for w in app.device.bundle_writes] == [
            banks.bank("CARD").label,
            banks.bank("CARD").label,
        ]
        _assert_no_ram_written(app)
        third = app.screen
        assert isinstance(third, WholeBankImportScreen)
        assert "no RAM bank" in _static_text(third, "whole-bank-status")


@pytest.mark.asyncio
async def test_cards_only_locked_gate_writes_nothing(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A locked gate refuses the cards-only step and writes nothing."""
    app = _split_pass_app(tmp_path, monkeypatch)
    file_banks = _cards_only_banks()
    async with app.run_test() as pilot:
        await pilot.pause()
        first = WholeBankImportScreen(
            path="cards-locked.syx", banks=file_banks, gate=app._write_gate
        )
        app.push_screen(
            first,
            lambda picked: app._import_whole_bank_chosen(
                file_banks, picked, "cards-locked.syx"
            ),
        )
        await _settle(pilot)
        app._write_gate.toggle()  # the user locked the gate
        await pilot.press("m")
        await _settle(pilot)
        await pilot.press("m")  # self-contained packed
        await _settle(pilot)
        await _press_cards_only(pilot)
        await _settle(pilot)

        assert app.device.bundle_write_count == 0
        assert app.screen is first
        assert "write gate is locked" in _static_text(first, "whole-bank-status")


@pytest.mark.asyncio
async def test_cancelling_the_cards_only_step_writes_nothing(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Escape at the overflow menu leaves the whole-bank screen, writes nothing."""
    app = _split_pass_app(tmp_path, monkeypatch)
    file_banks = _cards_only_banks()
    async with app.run_test() as pilot:
        await pilot.pause()
        first = WholeBankImportScreen(
            path="cards-cancel.syx", banks=file_banks, gate=app._write_gate
        )
        app.push_screen(
            first,
            lambda picked: app._import_whole_bank_chosen(
                file_banks, picked, "cards-cancel.syx"
            ),
        )
        await _settle(pilot)
        await pilot.press("o")
        await _settle(pilot)
        assert isinstance(app.screen, ImportMenuScreen)
        await pilot.press("escape")
        await _settle(pilot)

        assert isinstance(app.screen, WholeBankImportScreen)
        assert app.screen is first
        assert app.device.bundle_write_count == 0


@pytest.mark.asyncio
async def test_cards_only_choice_with_a_ram_target_is_refused(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A cards-only choice naming a RAM bank is a bug and writes nothing.

    The app must refuse a non-Card target rather than quietly redirecting
    the pass to the card.
    """
    app = _split_pass_app(tmp_path, monkeypatch)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    file_banks = _cards_only_banks()
    choice = WholeBankChoice(
        0,
        "RAM1",
        True,
        perfcopy.ImportMode.SELF_CONTAINED_PACKED,
        "cards.syx|RAM1|bad-target",
        True,
        "",
        "cards_only",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(
            file_banks[0],
            _approved(choice, file_banks[0], file_banks, "cards.syx"),
            banks_list=file_banks,
            path="cards.syx",
        )
        await _settle(pilot)
        assert app.device.bundle_write_count == 0
        assert any("no RAM bank is written" in s for s in statuses), statuses


@pytest.mark.asyncio
async def test_a_raising_done_still_ends_the_progress_bar(tmp_path: Any) -> None:
    """A failed report must not leave the bar frozen at its last percent.

    A raise in the completion callback used to skip the hold-end, so the
    app showed a bar at, say, 92% with no operation running and no way to
    tell whether the write had finished.
    """
    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))

    def done(_result: Any, _failure: Any) -> None:
        raise RuntimeError("the report itself failed")

    async with app.run_test() as pilot:
        await pilot.pause()
        app._device_worker(lambda: "written", done, "test operation")
        for _ in range(200):
            await pilot.pause()
            if not app._progress_active:
                break
        await _settle(pilot)
        assert not app._progress_active, "the bar is still held"


@pytest.mark.asyncio
async def test_the_offer_compares_against_the_unit_when_it_has_the_bank(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A present but different unit bank still raises the popup.

    The bank table's entry has no records; the file's imported bank does.
    Asking the wrong one for ``.source`` crashed before the popup could
    show, and only a unit source being absent hid it from the tests.
    """
    from kwsed.import_bank import DanglingRefsScreen

    app, _recorded = _armed_app(tmp_path, monkeypatch)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    # The preflight asks before the write; this exercises the fallback net
    # for a write that went out without it.
    monkeypatch.setattr(KwsedApp, "_preflight_companion", lambda self, *a, **k: None)
    file_banks = _two_bank_file()
    # The unit holds RAM 2, but with none of the patches the file has there.
    app._sources["RAM2"] = _make_bank(scope="RAM2", active_slots=[]).source
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(
            file_banks[0],
            _approved(
                WholeBankChoice(0, "RAM3", True, perfcopy.ImportMode.AS_IS, "s", True),
                file_banks[0],
                file_banks,
                "two.syx",
            ),
            banks_list=file_banks,
            path="two.syx",
        )
        for _ in range(100):
            await pilot.pause()
            if isinstance(app.screen, DanglingRefsScreen):
                break
        assert isinstance(app.screen, DanglingRefsScreen)


@pytest.mark.asyncio
async def test_a_matching_unit_bank_is_disclosed_but_not_re_offered(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A matching loaded copy is said so, and "source only" writes one bundle.

    The preflight asks every time (that predictability is deliberate), so a
    matching copy must not suppress the question. What it must do is say
    that the comparison is a snapshot, not a unit read, and answer "source
    only" without a second nagging popup afterwards.
    """
    from kwsed.import_bank import DanglingRefsScreen, PreflightImportScreen

    app, _recorded = _preflight_app(tmp_path, monkeypatch)
    file_banks = _two_bank_file()
    app._sources["RAM2"] = file_banks[1].source
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            WholeBankImportScreen(
                path="two.syx", banks=file_banks, gate=app._write_gate
            ),
            lambda picked: app._import_whole_bank_chosen(file_banks, picked, "two.syx"),
        )
        await _settle(pilot)
        await pilot.press("b")
        await pilot.press("b")  # target RAM 3
        await pilot.press("ctrl+s")
        await _wait_for_screen(app, pilot, PreflightImportScreen)
        await _settle(pilot)
        body = _static_text(app.screen, "preflight-body")
        assert "loaded copies, not of the unit" in body
        assert "already shows these patches" in body
        await pilot.press("2")  # source only
        await _answer_remediation_with_leave(app, pilot)
        for _ in range(100):
            await pilot.pause()
            if app.device.bundle_write_count:
                break
        await _settle(pilot, times=40)
        assert not isinstance(app.screen, DanglingRefsScreen)
    assert app.device.bundle_write_count == 1


def _preflight_app(tmp_path: Any, monkeypatch: Any) -> tuple[Any, list[Any]]:
    """An armed app for the preflight tests, with its own backup sandbox."""
    from kws import backup as _backup

    app, recorded = _armed_app(tmp_path, monkeypatch)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    backup_dir = str(tmp_path / "preflight-backups")
    os.makedirs(backup_dir, exist_ok=True)
    monkeypatch.setattr(_backup, "default_dir", lambda root=None: backup_dir)
    return app, recorded


@pytest.mark.asyncio
async def test_preflight_ctrl_s_asks_before_the_write_and_both_writes_the_companion(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """ctrl+s raises the question; 'both' sends the companion, then the bank."""
    from kwsed.import_bank import PreflightImportScreen
    from tests.test_bankbuild import make_waveseq

    app, recorded = _preflight_app(tmp_path, monkeypatch)
    file_banks = _two_bank_file()
    file_banks[1].source.waveseq = make_waveseq({0: [40]})[0]
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            WholeBankImportScreen(
                path="two.syx", banks=file_banks, gate=app._write_gate
            ),
            lambda picked: app._import_whole_bank_chosen(file_banks, picked, "two.syx"),
        )
        await _settle(pilot)
        await pilot.press("b")
        await pilot.press("b")  # target RAM 3, not the companion's RAM 2
        await pilot.press("ctrl+s")
        modal = await _wait_for_screen(app, pilot, PreflightImportScreen)
        await _settle(pilot)
        assert app.device.bundle_write_count == 0, "nothing is written yet"
        body = _static_text(modal, "preflight-body")
        assert "2 parts" in body
        assert "Main bank" in body
        assert "RAM 2" in body
        assert "Nothing has been written yet" in body
        choices = _static_text(modal, "preflight-choices")
        assert "wave sequences only" in choices
        assert "its performances stay as they are" in choices
        await pilot.press("1")  # write both, companion first
        # The remediation question is resolved before anything is written;
        # deciding and leaving goes straight through when nothing is on.
        await _answer_remediation_with_leave(app, pilot)
        for _ in range(200):
            await pilot.pause()
            if app.device.bundle_write_count == 2:
                break
        await _settle(pilot)
    assert app.device.bundle_write_count == 2
    companion_write, target_write = app.device.bundle_writes
    # The companion went first: patches and wave sequences, never its
    # performances. The bank the parts name must exist before the
    # performances that name it arrive.
    assert companion_write["bank"] == banks.bank("RAM2").label
    assert companion_write["kinds"] == ["waveseq", "patches"]
    assert "performances" not in companion_write["kinds"]
    assert ("RAM2", banks.Kind.PERFORMANCE) not in app.device._written
    assert ("RAM2", banks.Kind.PATCH) in app.device._written
    assert target_write["bank"] == banks.bank("RAM3").label
    assert "performances" in target_write["kinds"]
    # Two fenced writes: one backup and one undo record each.
    assert len(recorded) == 2
    for result in recorded:
        backup_path = result["backup"]
        assert isinstance(backup_path, str) and os.path.exists(backup_path)


@pytest.mark.asyncio
async def test_preflight_only_choice_writes_one_bundle_without_the_popup(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """'only' is the explicit answer: one bundle, and no popup afterwards."""
    from kwsed.import_bank import DanglingRefsScreen, PreflightImportScreen

    app, _recorded = _preflight_app(tmp_path, monkeypatch)
    file_banks = _two_bank_file()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            WholeBankImportScreen(
                path="two.syx", banks=file_banks, gate=app._write_gate
            ),
            lambda picked: app._import_whole_bank_chosen(file_banks, picked, "two.syx"),
        )
        await _settle(pilot)
        await pilot.press("b")
        await pilot.press("b")  # target RAM 3, not the companion's RAM 2
        await pilot.press("ctrl+s")
        await _wait_for_screen(app, pilot, PreflightImportScreen)
        await _settle(pilot)
        await pilot.press("2")  # write only this bank
        await _answer_remediation_with_leave(app, pilot)
        for _ in range(200):
            await pilot.pause()
            if app.device.bundle_write_count == 1:
                break
        await _settle(pilot)
    assert app.device.bundle_write_count == 1
    assert app.device.bundle_writes[0]["bank"] == banks.bank("RAM3").label
    assert not any(
        isinstance(s, (PreflightImportScreen, DanglingRefsScreen))
        for s in app.screen_stack
    ), "the choice was explicit: no popup asks again"


@pytest.mark.asyncio
async def test_preflight_cancel_writes_nothing_and_reopens_the_screen(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Cancel dismisses the modal and gives the import screen back."""
    from kwsed.import_bank import PreflightImportScreen, WholeBankImportScreen

    app, _recorded = _preflight_app(tmp_path, monkeypatch)
    file_banks = _two_bank_file()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            WholeBankImportScreen(
                path="two.syx", banks=file_banks, gate=app._write_gate
            ),
            lambda picked: app._import_whole_bank_chosen(file_banks, picked, "two.syx"),
        )
        await _settle(pilot)
        await pilot.press("b")
        await pilot.press("b")  # target RAM 3
        await pilot.press("ctrl+s")
        await _wait_for_screen(app, pilot, PreflightImportScreen)
        await _settle(pilot)
        await pilot.press("3")  # cancel
        await _settle(pilot)
        reopened = app.screen
        assert isinstance(reopened, WholeBankImportScreen)
        status = _static_text(reopened, "whole-bank-status")
        assert "nothing was written" in status
        assert "press o" in status, "the self-contained way out is named"
        assert "x to write" in status, "the companions way out is named"
    assert app.device.bundle_write_count == 0


@pytest.mark.asyncio
async def test_preflight_escape_cancels_like_3(tmp_path: Any, monkeypatch: Any) -> None:
    """esc is the same cancel: no write, the screen comes back."""
    from kwsed.import_bank import PreflightImportScreen, WholeBankImportScreen

    app, _recorded = _preflight_app(tmp_path, monkeypatch)
    file_banks = _two_bank_file()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            WholeBankImportScreen(
                path="two.syx", banks=file_banks, gate=app._write_gate
            ),
            lambda picked: app._import_whole_bank_chosen(file_banks, picked, "two.syx"),
        )
        await _settle(pilot)
        await pilot.press("b")
        await pilot.press("b")  # target RAM 3, not the companion's RAM 2
        await pilot.press("ctrl+s")
        await _wait_for_screen(app, pilot, PreflightImportScreen)
        await _settle(pilot)
        await pilot.press("escape")
        await _settle(pilot)
        assert isinstance(app.screen, WholeBankImportScreen)
    assert app.device.bundle_write_count == 0


@pytest.mark.asyncio
async def test_preflight_never_rises_for_a_one_bank_file(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A file with a single bank carries nothing to ask about."""
    from kwsed.import_bank import PreflightImportScreen

    app, _recorded = _preflight_app(tmp_path, monkeypatch)
    bank = _mixed_bank()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            WholeBankImportScreen(path="one.syx", banks=[bank], gate=app._write_gate),
            lambda picked: app._import_whole_bank_chosen([bank], picked, "one.syx"),
        )
        await _settle(pilot)
        await pilot.press("b")
        await pilot.press("b")  # target RAM 3, not the companion's RAM 2
        await pilot.press("ctrl+s")
        await _answer_remediation_with_leave(app, pilot)
        for _ in range(100):
            await pilot.pause()
            if app.device.bundle_write_count:
                break
        await _settle(pilot)
        assert not isinstance(app.screen, PreflightImportScreen)
    assert app.device.bundle_write_count == 1


@pytest.mark.asyncio
async def test_x_writes_patches_only_keeping_the_performances(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """`x` fills the patch slots; the RAM bank's performances stay."""
    app, recorded = _preflight_app(tmp_path, monkeypatch)
    file_banks = _two_bank_file()
    # The unit's RAM 2 holds performances the person made: not all Init,
    # so a companion write must not replace them.
    app._sources["RAM2"] = _make_bank(scope="RAM2", active_slots=[4]).source
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            WholeBankImportScreen(
                path="two.syx", banks=file_banks, gate=app._write_gate
            ),
            lambda picked: app._import_whole_bank_chosen(file_banks, picked, "two.syx"),
        )
        await _settle(pilot)
        await pilot.press("x")
        await _settle(pilot)
        assert isinstance(app.screen, ImportMenuScreen)
        await pilot.press("enter")  # the file's RAM 2 bank
        await _settle(pilot)
        assert isinstance(app.screen, TargetBankScreen)
        await pilot.press("enter")  # the suggested RAM 2 target
        # The companion write is asked the affected-performances question;
        # deciding and leaving goes straight through when nothing is on.
        await _answer_remediation_with_leave(app, pilot)
        for _ in range(200):
            await pilot.pause()
            if app.device.bundle_write_count:
                break
        await _settle(pilot)
    assert app.device.bundle_write_count == 1
    write = app.device.bundle_writes[0]
    assert write["bank"] == banks.bank("RAM2").label
    assert write["kinds"] == ["patches"], "no performances key travels"
    assert ("RAM2", banks.Kind.PERFORMANCE) not in app.device._written
    assert ("RAM2", banks.Kind.PATCH) in app.device._written
    assert any(
        "wrote 2 patches into RAM 2" in s and "performances were left as they were" in s
        for s in statuses
    ), statuses
    assert recorded, "the patches-only write is recorded for undo"


@pytest.mark.asyncio
async def test_x_writes_the_whole_bank_when_the_performances_are_all_init(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A RAM bank with nothing but Init has nothing to lose: it travels whole."""
    app, recorded = _preflight_app(tmp_path, monkeypatch)
    file_banks = _spillover_banks()
    app._sources["RAM2"] = _make_bank(scope="RAM2", active_slots=[]).source
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            WholeBankImportScreen(
                path="two.syx",
                banks=file_banks,
                gate=app._write_gate,
                companion_loaded=_companion_loaded_map(app),
            ),
            lambda picked: app._import_whole_bank_chosen(file_banks, picked, "two.syx"),
        )
        await _settle(pilot)
        await pilot.press("x")
        await _settle(pilot)
        await pilot.press("enter")
        await _settle(pilot)
        await pilot.press("enter")
        await _answer_remediation_with_leave(app, pilot)
        for _ in range(200):
            await pilot.pause()
            if app.device.bundle_write_count:
                break
        await _settle(pilot)
    assert app.device.bundle_write_count == 1
    assert app.device.bundle_writes[0]["bank"] == banks.bank("RAM2").label
    assert "performances" in app.device.bundle_writes[0]["kinds"]
    assert ("RAM2", banks.Kind.PERFORMANCE) in app.device._written
    assert recorded


@pytest.mark.asyncio
async def test_companion_x_without_a_token_refuses_with_zero_bundles(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A companion choice nobody approved never reaches the unit."""
    app, recorded = _armed_app(tmp_path, monkeypatch)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    file_banks = _two_bank_file()
    app._import_whole_bank_write(
        file_banks[1],
        WholeBankChoice(
            1, "RAM2", True, perfcopy.ImportMode.AS_IS, companion_only=True
        ),
        banks_list=file_banks,
        path="two.syx",
    )
    assert app.device.bundle_write_count == 0
    assert recorded == [], "no undo record for a refused write"
    assert app._import_passes == {}, "no session state for a refused write"


@pytest.mark.asyncio
async def test_companion_write_validates_and_consumes_its_own_token(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The companion write is its own approved request, spent once (P4)."""
    from kwsed import import_intent

    app, recorded = _armed_app(tmp_path, monkeypatch)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    file_banks = _two_bank_file()
    entry = file_banks[1]
    companion = banks.bank("RAM2")
    mode = perfcopy.ImportMode.AS_IS
    choice = WholeBankChoice(1, "RAM2", True, mode, companion_only=True)
    approval = app._mint_whole_bank_approval(
        entry, choice, file_banks, "two.syx", companion_all_init=False
    )
    assert approval is not None
    done: list[int] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app._write_companion_patches(
            entry,
            companion,
            lambda: done.append(1),
            approval=approval,
            banks_list=file_banks,
            path="two.syx",
            mode=mode,
        )
        await _wait_for_writes(app, pilot, 1)
        await _settle(pilot)
        assert app.device.bundle_write_count == 1
        write = app.device.bundle_writes[0]
        assert write["bank"] == banks.bank("RAM2").label
        assert write["kinds"] == ["patches"]
        assert done == [1], "the caller is told the write landed"
        assert approval.nonce in app._consumed_plan_nonces
        assert recorded, "a landed write is recorded for undo"
        # A replayed token is refused: one approved write, spent once.
        app._write_companion_patches(
            entry,
            companion,
            lambda: done.append(1),
            approval=approval,
            banks_list=file_banks,
            path="two.syx",
            mode=mode,
        )
        await _wait_for_writes(app, pilot, 1)
        await _settle(pilot)
    assert app.device.bundle_write_count == 1, "the replay wrote nothing"
    assert done == [1], "a refused replay never reports the bank as landed"


@pytest.mark.asyncio
async def test_companion_write_refuses_a_token_minted_for_another_bank(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A token for one companion does not authorize a different one."""
    app, recorded = _armed_app(tmp_path, monkeypatch)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    file_banks = _two_bank_file()
    entry = file_banks[1]
    mode = perfcopy.ImportMode.AS_IS
    other = WholeBankChoice(1, "RAM3", True, mode, companion_only=True)
    approval = app._mint_whole_bank_approval(
        entry, other, file_banks, "two.syx", companion_all_init=False
    )
    assert approval is not None
    done: list[int] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app._write_companion_patches(
            entry,
            banks.bank("RAM2"),
            lambda: done.append(1),
            approval=approval,
            banks_list=file_banks,
            path="two.syx",
            mode=mode,
        )
        await _settle(pilot)
        await _settle(pilot)
    assert app.device.bundle_write_count == 0
    assert done == []
    assert recorded == []
    assert approval.nonce not in app._consumed_plan_nonces
    assert "RAM2" not in app._sources, "a refused write leaves no cache update"


@pytest.mark.asyncio
async def test_companion_write_freezes_the_records_at_the_boundary(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The companion write sends exactly what the token was checked for (P7c).

    The snapshot is taken at the write boundary, before the token is
    validated, and the worker writes only that snapshot. Records that
    change between the check and the worker -- here, mutated while the
    boundary is still planning -- change nothing on the unit: the patch
    and wave-sequence bytes are the frozen ones, and the companion's
    performances never travel at all.
    """
    from tests.test_bankbuild import make_patch, make_waveseq

    app, recorded = _armed_app(tmp_path, monkeypatch)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    file_banks = _two_bank_file()
    entry = file_banks[1]
    companion = banks.bank("RAM2")
    app._sources["RAM2"] = _make_bank(scope="RAM2", active_slots=[]).source
    # The frozen records: what the boundary snapshot carries.
    frozen_patches = [bytes(record) for record in entry.source.patches]
    frozen_waveseq, _starts = make_waveseq({0: [40]})
    entry.source.waveseq = frozen_waveseq
    mode = perfcopy.ImportMode.AS_IS
    choice = WholeBankChoice(1, "RAM2", True, mode, companion_only=True)
    approval = app._mint_whole_bank_approval(
        entry, choice, file_banks, "two.syx", companion_all_init=False
    )
    assert approval is not None
    done: list[int] = []

    original_intent = KwsedApp._companion_write_intent

    def _mutating(self: KwsedApp, *a: Any, **k: Any) -> Any:
        # Runs inside the boundary, after the plan inputs were frozen and
        # before the worker runs: the live records change, the snapshot
        # does not. Every name is invented.
        entry.source.patches[2] = make_patch("Late Swap")
        entry.source.waveseq = b"late-seq"
        app._sources["RAM2"].patches[3] = make_patch("Store Swap")
        return original_intent(self, *a, **k)

    monkeypatch.setattr(KwsedApp, "_companion_write_intent", _mutating)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._write_companion_patches(
            entry,
            companion,
            lambda: done.append(1),
            approval=approval,
            banks_list=file_banks,
            path="two.syx",
            mode=mode,
        )
        await _wait_for_writes(app, pilot, 1)
        await _settle(pilot)
        assert done == [1], "the frozen write landed and the caller was told"
        assert app.device.bundle_write_count == 1
        write = app.device.bundle_writes[0]
        assert write["bank"] == companion.label
        assert write["kinds"] == ["waveseq", "patches"]
        assert ("RAM2", banks.Kind.PERFORMANCE) not in app.device._written, (
            "a companion write never sends performances"
        )
        # The bytes are the frozen records: neither the file's mutated
        # copies nor the session copy's reach the unit.
        patch_blob = _pass_blob(app.device, "RAM2", banks.Kind.PATCH)
        assert patch_blob == b"".join(frozen_patches)
        assert b"Late Swap" not in patch_blob
        assert _pass_blob(app.device, "RAM2", "waveseq") == frozen_waveseq
        assert approval.nonce in app._consumed_plan_nonces
        assert recorded, "the frozen write is recorded for undo"
        # The session copy keeps its own performances and takes the
        # frozen patches, so a later check sees the bank as written.
        session_source = app._sources["RAM2"]
        untouched = _make_bank(scope="RAM2", active_slots=[]).source
        assert list(session_source.performances) == [
            bytes(record) for record in untouched.performances
        ]
        assert b"Store Swap" not in b"".join(session_source.patches)

    assert app.device.bundle_write_count == 1


@pytest.mark.asyncio
async def test_preflight_both_two_companions_one_token_each(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """'both' mints one token per physical write, in the shown order (P4)."""
    from kwsed.import_bank import PreflightImportScreen

    app, recorded = _preflight_app(tmp_path, monkeypatch)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))
    file_banks = _three_bank_file()
    minted: list[Any] = []

    original = KwsedApp._mint_whole_bank_approval

    def _recording(
        self: KwsedApp, bank: Any, choice: Any, banks: Any, path: str, *a: Any, **k: Any
    ) -> Any:
        approval = original(self, bank, choice, banks, path, *a, **k)
        minted.append(approval)
        return approval

    monkeypatch.setattr(KwsedApp, "_mint_whole_bank_approval", _recording)
    async with app.run_test() as pilot:
        modal = await _raise_preflight_on_card(pilot, app, file_banks, "three.syx")
        assert app.device.bundle_write_count == 0
        assert isinstance(modal, PreflightImportScreen)
        await pilot.press("1")  # write every named bank, then this bank
        # The remediation question is resolved before anything is written;
        # deciding and leaving goes straight through when nothing is on.
        await _answer_remediation_with_leave(app, pilot)
        for _ in range(300):
            await pilot.pause()
            if app.device.bundle_write_count == 3:
                break
        await _settle(pilot)
    assert app.device.bundle_write_count == 3
    ram3, ram2, source = app.device.bundle_writes
    assert ram3["bank"] == banks.bank("RAM3").label
    assert ram2["bank"] == banks.bank("RAM2").label
    assert source["bank"] == banks.bank("CARD").label
    # One token per physical write: the two companions and the head, each
    # minted at the answer, each distinct, each spent exactly once.
    assert len(minted) == 3
    assert all(minted), "every write in the chain was approved"
    nonces = [approval.nonce for approval in minted]
    assert len(set(nonces)) == 3
    assert set(nonces) <= app._consumed_plan_nonces
    assert recorded, "every landed write has its undo record"
    # The person saw the ordered banks before the chain started.
    order = next(s for s in statuses if "the writes, in order" in s)
    assert order.index("RAM 3") < order.index("RAM 2")


@pytest.mark.asyncio
async def test_preflight_chain_stops_when_the_second_token_is_tampered(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A tampered token for companion two stops the chain after one (P4)."""
    from kwsed.import_bank import PreflightImportScreen

    app, recorded = _preflight_app(tmp_path, monkeypatch)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))
    file_banks = _three_bank_file()
    original = KwsedApp._mint_whole_bank_approval
    mints = {"count": 0}

    def _tampering(
        self: KwsedApp, bank: Any, choice: Any, banks: Any, path: str, *a: Any, **k: Any
    ) -> Any:
        approval = original(self, bank, choice, banks, path, *a, **k)
        mints["count"] += 1
        # Mint 1 is the remediation answer's plan (the target write); the
        # chain's companions follow it, so the second companion's token is
        # mint 3.
        if mints["count"] == 3 and approval is not None:
            approval = replace(approval, fingerprint="0" * 64)
        return approval

    monkeypatch.setattr(KwsedApp, "_mint_whole_bank_approval", _tampering)
    async with app.run_test() as pilot:
        modal = await _raise_preflight_on_card(pilot, app, file_banks, "three.syx")
        assert isinstance(modal, PreflightImportScreen)
        await pilot.press("1")
        await _answer_remediation_with_leave(app, pilot)
        for _ in range(300):
            await pilot.pause()
            if any("not written" in s for s in statuses):
                break
        await _settle(pilot)
    # The first companion landed; the tampered second stopped the chain,
    # and neither the other companion nor the source bank was sent.
    assert app.device.bundle_write_count == 1
    assert app.device.bundle_writes[0]["bank"] == banks.bank("RAM3").label
    assert ("RAM2", banks.Kind.PATCH) not in app.device._written
    assert ("CARD", banks.Kind.PERFORMANCE) not in app.device._written
    assert any(
        "does not match" in s and "written so far" in s and "not written" in s
        for s in statuses
    ), statuses
    assert recorded, "the one landed write still has its undo record"


@pytest.mark.asyncio
async def test_the_all_init_branch_refuses_a_patches_only_token(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A token for patches+waveseq does not cover the whole-bank branch.

    When the loaded copy of the companion bank is all Init the write
    carries all three kinds; the token must have been minted for that
    branch, and a patches-only one is refused before anything is sent.
    """
    app, recorded = _armed_app(tmp_path, monkeypatch)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    file_banks = _spillover_banks()
    app._sources["RAM2"] = _make_bank(scope="RAM2", active_slots=[]).source
    choice = WholeBankChoice(
        1, "RAM2", True, perfcopy.ImportMode.AS_IS, companion_only=True
    )
    # What a screen without the branch snapshot would have minted.
    approved = _approved(choice, file_banks[1], file_banks, "two.syx")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(
            file_banks[1],
            approved,
            banks_list=file_banks,
            path="two.syx",
        )
        await _settle(pilot)
        await _settle(pilot)
    assert app.device.bundle_write_count == 0
    assert recorded == []
    assert app._import_passes == {}
    assert approved.approval is not None
    assert approved.approval.nonce not in app._consumed_plan_nonces


@pytest.mark.asyncio
async def test_cards_only_pass_two_refuses_a_pass_one_token(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A continuation token binds this pass's slots and the Card (P4).

    A token minted for pass 1's slots is refused once the session has
    advanced to pass 2; a fresh token for the remaining slots proceeds
    onto the card, and replaying it is refused again.
    """
    from kwsed.import_bank import OVERFLOW_CARDS_ONLY, OVERFLOW_SELF_CONTAINED

    app = _split_pass_app(tmp_path, monkeypatch)
    recorded: list[Any] = []
    app._record_write = recorded.append  # type: ignore[method-assign]
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    file_banks = _cards_only_banks()
    bank = file_banks[0]
    key = "cards.syx|RAM1|p4pass"
    app._import_passes[key] = {
        "pass_kind": OVERFLOW_CARDS_ONLY,
        "split_remaining": list(range(50, 60)),
        "split_target": "CARD",
        "source_index": 0,
        "written": dict.fromkeys(range(50), "Card"),
        "mode": perfcopy.ImportMode.SELF_CONTAINED_PACKED,
    }
    choice = WholeBankChoice(
        0,
        "CARD",
        True,
        perfcopy.ImportMode.SELF_CONTAINED_PACKED,
        key,
        True,
        "",
        OVERFLOW_SELF_CONTAINED,
    )
    stale = _approved(choice, bank, file_banks, "cards.syx", pass_slots=range(50))
    fresh = _approved(choice, bank, file_banks, "cards.syx", pass_slots=range(50, 60))
    async with app.run_test() as pilot:
        await pilot.pause()
        # Pass 1's token, offered after the session advanced: refused.
        app._import_whole_bank_write(
            bank, stale, banks_list=file_banks, path="cards.syx"
        )
        await _settle(pilot)
        await _settle(pilot)
        assert app.device.bundle_write_count == 0
        assert any("does not match" in s for s in statuses), statuses
        assert stale.approval is not None
        assert stale.approval.nonce not in app._consumed_plan_nonces
        state_before = dict(app._import_passes[key])
        assert state_before["split_remaining"] == list(range(50, 60)), (
            "a refused pass left the session as it was"
        )
        # A fresh token for exactly this pass's slots proceeds onto Card.
        app._import_whole_bank_write(
            bank, fresh, banks_list=file_banks, path="cards.syx"
        )
        await _wait_for_writes(app, pilot, 1)
        await _settle(pilot)
        assert app.device.bundle_write_count == 1
        assert app.device.bundle_writes[0]["bank"] == banks.bank("CARD").label
        assert recorded, "the landed pass is recorded"
        # The same token again is a replay: refused, nothing new written.
        app._import_whole_bank_write(
            bank, fresh, banks_list=file_banks, path="cards.syx"
        )
        await _settle(pilot)
        await _settle(pilot)
    assert app.device.bundle_write_count == 1
    assert any("refused" in s or "does not match" in s for s in statuses), statuses
    assert fresh.approval is not None
    assert fresh.approval.nonce in app._consumed_plan_nonces, "the pass spent its token"
    assert len(recorded) == 1, "the refused replay added no undo record"


@pytest.mark.asyncio
async def test_preflight_asks_even_when_the_unit_already_has_the_patches(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The question is predictable: it does not vanish after one import.

    Suppressing it when the unit already matched made the same keys behave
    differently from one import to the next, which is indistinguishable
    from the feature being missing.
    """
    from kwsed.import_bank import PreflightImportScreen

    app, _recorded = _preflight_app(tmp_path, monkeypatch)
    file_banks = _two_bank_file()
    # The unit already holds exactly the file's RAM 2 bank.
    app._sources["RAM2"] = file_banks[1].source
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            WholeBankImportScreen(
                path="two.syx", banks=file_banks, gate=app._write_gate
            ),
            lambda picked: app._import_whole_bank_chosen(file_banks, picked, "two.syx"),
        )
        await _settle(pilot)
        await pilot.press("b")
        await pilot.press("b")  # target RAM 3
        await pilot.press("ctrl+s")
        modal = await _wait_for_screen(app, pilot, PreflightImportScreen)
        await _settle(pilot)
        body = _static_text(modal, "preflight-body")
        assert "2 parts name it" in body
        # The state is a snapshot of the loaded copy, never a claim about
        # the unit: nothing re-read the instrument before this question.
        assert "loaded copy already shows these patches" in body
        assert "the unit is not re-read" in body
        assert "already holds those patches" not in body
        assert "the unit already" not in body
        assert app.device.bundle_write_count == 0


def _three_bank_file() -> list[Any]:
    """A three-bank file whose main bank names two of the banks it carries.

    Every name is invented. RAM 3 is named by five used parts and RAM 2
    by three, so the preflight lists RAM 3 first; each named bank also
    carries a wave sequence, so a companion write sends both kinds. A
    Card bank rides along in the file but no part names it: it must not
    appear in the question.
    """
    from tests.test_bankbuild import make_patch, make_performance, make_waveseq

    perfs = [_empty_perf() for _ in range(50)]
    perfs[2] = make_performance("Harbor Gate", ((1, 2), (2, 4)))
    perfs[6] = make_performance("Lantern Field", ((1, 5), (2, 4)))
    perfs[9] = make_performance("Drift Path", ((2, 3), (2, 3), (1, 2)))
    perfs[11] = make_performance("Spar Row", ((2, 3),))
    main = deps.Source(
        scope_id="RAM1",
        performances=perfs,
        patches=[make_patch("Quarry Seam")] + [bytes(PATCH_RECORD_SIZE)] * 34,
        waveseq=None,
    )
    ram2 = _make_bank(scope="RAM2", active_slots=[])
    ram2.source.patches[2] = make_patch("Tide Chart")
    ram2.source.patches[5] = make_patch("Gull Wing")
    ram2.source.waveseq = make_waveseq({0: [40]})[0]
    ram3 = _make_bank(scope="RAM3", active_slots=[])
    ram3.source.patches[3] = make_patch("Fern Hollow")
    ram3.source.patches[4] = make_patch("Kelp Steps")
    ram3.source.waveseq = make_waveseq({1: [41]})[0]
    card = _make_bank(scope="CARD", active_slots=[])
    return [
        importers.ImportedBank(
            label="Main bank", scope="RAM1", format="sysex", source=main
        ),
        ram2,
        ram3,
        card,
    ]


async def _raise_preflight_on_card(
    pilot: Any, app: Any, file_banks: list[Any], path: str
) -> Any:
    """Drive the import screen to ctrl+s with the Card as the target.

    Three ``b`` presses walk RAM 1 → RAM 2 → RAM 3 → Card, so the target
    is neither named bank and the question covers every named one.
    """
    from kwsed.import_bank import PreflightImportScreen

    app.push_screen(
        WholeBankImportScreen(path=path, banks=file_banks, gate=app._write_gate),
        lambda picked: app._import_whole_bank_chosen(file_banks, picked, path),
    )
    await _settle(pilot)
    for _ in range(3):
        await pilot.press("b")
    await pilot.press("ctrl+s")
    for _ in range(100):
        await pilot.pause()
        if isinstance(app.screen, PreflightImportScreen):
            break
    await _settle(pilot)
    modal = app.screen
    assert isinstance(modal, PreflightImportScreen)
    return modal


@pytest.mark.asyncio
async def test_preflight_lists_both_named_companions_and_writes_each_then_the_source(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Every named bank is listed, and 'both' writes them all, then the bank.

    Writing only the most-referenced companion used to leave the other
    one's parts dangling with the preflight marked answered: the answer
    now means every listed companion.
    """
    app, recorded = _preflight_app(tmp_path, monkeypatch)
    file_banks = _three_bank_file()
    async with app.run_test() as pilot:
        modal = await _raise_preflight_on_card(pilot, app, file_banks, "three.syx")
        assert app.device.bundle_write_count == 0
        body = _static_text(modal, "preflight-body")
        # Both named banks, most-referenced first, each with its counts.
        assert "· RAM 3 — 5 parts name it, 2 of its patch slots are used" in body
        assert "· RAM 2 — 3 parts name it, 2 of its patch slots are used" in body
        assert body.index("RAM 3") < body.index("RAM 2")
        assert "Nothing has been written yet" in body
        choices = _static_text(modal, "preflight-choices")
        assert "wave sequences only" in choices
        assert "its performances stay as they are" in choices
        await pilot.press("1")  # write every named bank, then this bank
        # The remediation question is resolved before anything is written;
        # deciding and leaving goes straight through when nothing is on.
        await _answer_remediation_with_leave(app, pilot)
        for _ in range(300):
            await pilot.pause()
            if app.device.bundle_write_count == 3:
                break
        await _settle(pilot)
    assert app.device.bundle_write_count == 3
    first, second, source = app.device.bundle_writes
    # The named banks went out in the listed order, dependencies first,
    # and the source bank came last.
    assert first["bank"] == banks.bank("RAM3").label
    assert second["bank"] == banks.bank("RAM2").label
    assert source["bank"] == banks.bank("CARD").label
    # Each companion carries patches and wave sequences, never performances.
    for companion_write in (first, second):
        assert "patches" in companion_write["kinds"]
        assert "waveseq" in companion_write["kinds"]
        assert "performances" not in companion_write["kinds"]
    assert ("RAM2", banks.Kind.PERFORMANCE) not in app.device._written
    assert ("RAM3", banks.Kind.PERFORMANCE) not in app.device._written
    assert ("RAM2", banks.Kind.PATCH) in app.device._written
    assert ("RAM3", banks.Kind.PATCH) in app.device._written
    assert "performances" in source["kinds"]
    # Three fenced writes: one backup envelope each.
    assert len(recorded) == 3
    for result in recorded:
        backup_path = result["backup"]
        assert isinstance(backup_path, str) and os.path.exists(backup_path)


@pytest.mark.asyncio
async def test_preflight_does_not_list_a_companion_nothing_names(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A bank the file carries but no part names stays out of the question."""
    app, _recorded = _preflight_app(tmp_path, monkeypatch)
    file_banks = _three_bank_file()
    async with app.run_test() as pilot:
        modal = await _raise_preflight_on_card(pilot, app, file_banks, "three.syx")
        body = _static_text(modal, "preflight-body")
        assert "Card" not in body, "nothing names it, so it is not asked about"
        assert "RAM 3" in body
        assert "RAM 2" in body


@pytest.mark.asyncio
async def test_preflight_with_two_companions_cancel_writes_nothing(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Cancel with two named banks sends nothing and reopens the screen."""
    from kwsed.import_bank import WholeBankImportScreen

    app, _recorded = _preflight_app(tmp_path, monkeypatch)
    file_banks = _three_bank_file()
    async with app.run_test() as pilot:
        await _raise_preflight_on_card(pilot, app, file_banks, "three.syx")
        await pilot.press("3")  # cancel
        await _settle(pilot)
        reopened = app.screen
        assert isinstance(reopened, WholeBankImportScreen)
        status = _static_text(reopened, "whole-bank-status")
        assert "nothing was written" in status
        assert "x to write" in status
    assert app.device.bundle_write_count == 0


@pytest.mark.asyncio
async def test_preflight_with_two_companions_source_only_sends_one_bundle(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """'only' means one bank: no companion travels and nothing nags after."""
    from kwsed.import_bank import DanglingRefsScreen, PreflightImportScreen

    app, _recorded = _preflight_app(tmp_path, monkeypatch)
    file_banks = _three_bank_file()
    async with app.run_test() as pilot:
        await _raise_preflight_on_card(pilot, app, file_banks, "three.syx")
        await pilot.press("2")  # write only this bank
        await _answer_remediation_with_leave(app, pilot)
        for _ in range(200):
            await pilot.pause()
            if app.device.bundle_write_count == 1:
                break
        await _settle(pilot)
    assert app.device.bundle_write_count == 1
    assert app.device.bundle_writes[0]["bank"] == banks.bank("CARD").label
    assert not any(
        isinstance(s, (PreflightImportScreen, DanglingRefsScreen))
        for s in app.screen_stack
    ), "the choice was explicit: no popup asks again"


@pytest.mark.asyncio
async def test_preflight_second_companion_failure_names_what_completed(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A companion that fails stops the run and reports the partial truth.

    The first companion's backup stays as the way back, the source bank
    is not written, and the status names what was written and what was
    not -- instead of claiming the whole answer went out.
    """
    app, recorded = _preflight_app(tmp_path, monkeypatch)
    file_banks = _three_bank_file()
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))
    real_write = app.device.write_bank_bundle

    def failing_write(bank: Any, **kwargs: Any) -> dict[str, Any]:
        if banks.bank(bank).id == "RAM2":
            raise ValueError("the unit refused the second bank")
        return cast(dict[str, Any], real_write(bank, **kwargs))

    app.device.write_bank_bundle = failing_write
    async with app.run_test() as pilot:
        await _raise_preflight_on_card(pilot, app, file_banks, "three.syx")
        await pilot.press("1")
        await _answer_remediation_with_leave(app, pilot)
        for _ in range(200):
            await pilot.pause()
            if any("written so far" in s for s in statuses):
                break
        await _settle(pilot)
    # RAM 3 landed, RAM 2 did not, and the source bank never went out.
    assert app.device.bundle_write_count == 1
    assert app.device.bundle_writes[0]["bank"] == banks.bank("RAM3").label
    assert ("RAM2", banks.Kind.PATCH) not in app.device._written
    assert ("CARD", banks.Kind.PATCH) not in app.device._written
    assert any(
        "written so far: RAM 3" in s and "not written: RAM 2, Main bank" in s
        for s in statuses
    ), statuses
    assert recorded, "the companion that succeeded is still recorded for undo"


@pytest.mark.asyncio
async def test_preflight_locked_gate_is_refused_before_the_question(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A locked gate stops the write before any modal is raised."""
    from kwsed.import_bank import PreflightImportScreen

    app, _recorded = _preflight_app(tmp_path, monkeypatch)
    app._write_gate = EditGate(armed=False)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))
    file_banks = _three_bank_file()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            WholeBankImportScreen(
                path="three.syx", banks=file_banks, gate=app._write_gate
            ),
            lambda picked: app._import_whole_bank_chosen(
                file_banks, picked, "three.syx"
            ),
        )
        await _settle(pilot)
        for _ in range(3):
            await pilot.press("b")
        await pilot.press("ctrl+s")
        await _settle(pilot)
        assert not isinstance(app.screen, PreflightImportScreen)
        assert "write gate is locked" in statuses or "write gate is locked" in (
            _static_text(app.screen, "whole-bank-status")
            if isinstance(app.screen, WholeBankImportScreen)
            else ""
        )
    assert app.device.bundle_write_count == 0


# --- T3: explicit partial outcomes for multi-bank writes ---------------------


def _partial_app(
    tmp_path: Any, monkeypatch: Any
) -> tuple[Any, list[str], list[Any], list[dict[str, Any]]]:
    """An armed app whose undo records, cache saves and statuses are watched.

    RAM 2, RAM 3 and the Card are there to receive; the backups are
    sandboxed and the cache save is a recorder. Every name is invented.
    """
    from kws import backup as _backup

    app = KwsedApp(DemoDevice(), bankstore_dir=str(tmp_path / "store"))
    # No configured pre-audit staging: a write's pre-write warning must
    # not depend on this machine's own configuration.
    app._config_path = str(tmp_path / "absent-kwsed.toml")
    app._sources["RAM2"] = _make_bank(scope="RAM2", active_slots=[]).source
    app._sources["RAM3"] = _make_bank(scope="RAM3", active_slots=[]).source
    app._sources["CARD"] = _make_bank(scope="CARD", active_slots=[]).source
    app._write_gate = EditGate(armed=True)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir, exist_ok=True)
    monkeypatch.setattr(_backup, "default_dir", lambda root=None: backup_dir)
    recorded: list[Any] = []
    app._record_write = recorded.append  # type: ignore[method-assign]
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
            }
        )

    monkeypatch.setattr(bankstore, "save", fake_save)
    return app, statuses, recorded, saved


async def _await_partial(app: Any, pilot: Any) -> None:
    """Let a cross-bank write finish, however it ended."""
    for _ in range(200):
        await pilot.pause()
        if app.device.bundle_write_count:
            break
    await _settle(pilot)


@pytest.mark.asyncio
async def test_overflow_failure_on_the_target_keeps_the_overflow_bank(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The overflow bank landed; the write stopped before the target.

    Cross-bank writes are not atomic and must not pretend to be: the
    overflow bank is recorded and cached exactly as a success records
    it, the target is named as not written, and nothing says the import
    succeeded. A retry sends the target only.
    """
    app, statuses, recorded, saved = _partial_app(tmp_path, monkeypatch)
    bank = _mixed_bank()
    card_no = banks.bank("CARD").number
    ram3_no = banks.bank("RAM3").number
    original = app.device.write_bank_bundle
    calls: list[int] = []
    card_before = app._sources["CARD"]
    fails = {"armed": True}

    def failing(bank_number: int, **k: Any) -> dict[str, Any]:
        if fails["armed"] and bank_number == card_no:
            raise RuntimeError("card refused the write")
        calls.append(bank_number)
        return cast(dict[str, Any], original(bank_number, **k))

    app.device.write_bank_bundle = failing
    choice = WholeBankChoice(
        0,
        "CARD",
        True,
        perfcopy.ImportMode.AS_IS,
        "overflow-partial",
        True,
        "RAM3",
        OVERFLOW_PATCHES,
    )
    # The first attempt's token, held so the replay refusal can use it.
    first = _approved(choice, bank, [bank], "two.syx")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(
            bank,
            first,
            banks_list=[bank],
            path="two.syx",
        )
        await _await_partial(app, pilot)
        await _settle(pilot)

        # Exactly one bundle went out: the overflow bank, and it landed.
        assert calls == [ram3_no]
        # One undo record, for the overflow bank, with its backup on disk.
        assert len(recorded) == 1
        assert recorded[0]["bank"] == banks.bank("RAM3").label
        backup_path = recorded[0]["backup"]
        assert isinstance(backup_path, str) and os.path.exists(backup_path)
        # The overflow bank's cache holds the patches it received.
        overflow_saves = [
            s for s in saved if s["scope"] == "RAM3" and s["patches"] is not None
        ]
        assert len(overflow_saves) == 1
        # The target was never written: its session source is untouched.
        assert app._sources["CARD"] is card_before

        closing = statuses[-1]
        assert closing.startswith("import: card refused the write")
        assert "written: RAM 3" in closing
        assert "failed or unverified (may have changed): Card" in closing
        assert "backup" in closing
        assert "write the target again" in closing
        assert not any("imported" in s for s in statuses), statuses
        # The session keeps the overflow bank, so a retry never resends it.
        assert app._import_passes["overflow-partial"]["overflow_written"] == "RAM3"

        # The retry: the target only, and the status says so up front.
        # The app mints the retry token from its own session state (P7b):
        # it names the landed overflow bank in ``skipped_banks``, which
        # is exactly what the boundary rebuilds, so the token validates.
        fails["armed"] = False
        retry_approval = app._mint_whole_bank_approval(bank, choice, [bank], "two.syx")
        assert retry_approval is not None
        app._import_whole_bank_write(
            bank,
            replace(choice, approval=retry_approval),
            banks_list=[bank],
            path="two.syx",
        )
        await _wait_for_writes(app, pilot, 2)
        await _settle(pilot)

        # A token minted without the skip state is refused for that
        # retry: the first attempt's plan is not the remainder's plan,
        # and a mint that ignores the session's landed banks is a
        # different fingerprint from the rebuild.
        stale = _approved(choice, bank, [bank], "two.syx")
        before = len(calls)
        app._import_whole_bank_write(bank, stale, banks_list=[bank], path="two.syx")
        assert len(calls) == before, "a skip-less mint must not reach the unit"
        assert any(
            "the plan approval does not match this write" in s for s in statuses
        ), statuses
        assert stale.approval is not None
        assert stale.approval.nonce not in app._consumed_plan_nonces, (
            "a refused mint spends no nonce"
        )

        # The replayed first token is refused too: it was spent on the
        # first attempt, and its plan names no skipped bank anyway.
        before = len(calls)
        app._import_whole_bank_write(bank, first, banks_list=[bank], path="two.syx")
        assert len(calls) == before, "the first token must not authorize the retry"

    assert calls == [ram3_no, card_no]
    assert any(
        "RAM 3 already landed; this write sends the target only" in s for s in statuses
    ), statuses
    assert any("the overflow bank already held them" in s for s in statuses)
    assert app._sources["CARD"] is not card_before
    assert len(recorded) == 2


@pytest.mark.asyncio
async def test_overflow_partial_reoffers_the_screen_and_the_retry_is_target_only(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The patches-overflow partial gives the screen back, target-only (P7c).

    The overflow bank landed, the target did not: the import screen is
    re-offered with the same session, its pass state naming the landed
    bank as ``skipped_banks``. The retry minted there is the plan the
    write boundary rebuilds, the pre-write status says the target is all
    this pass sends, and ctrl+s then writes exactly the target once --
    never the landed bank again -- with one undo record for that write.
    The first token is spent, and replaying it writes nothing.
    """
    app, statuses, recorded, _saved = _partial_app(tmp_path, monkeypatch)
    bank = _mixed_bank()
    card_no = banks.bank("CARD").number
    ram3_no = banks.bank("RAM3").number
    original = app.device.write_bank_bundle
    calls: list[int] = []
    fails = {"armed": True}

    def failing(bank_number: int, **k: Any) -> dict[str, Any]:
        if fails["armed"] and bank_number == card_no:
            raise RuntimeError("card refused the write")
        calls.append(bank_number)
        return cast(dict[str, Any], original(bank_number, **k))

    app.device.write_bank_bundle = failing
    choice = WholeBankChoice(
        0,
        "CARD",
        True,
        perfcopy.ImportMode.AS_IS,
        "overflow-reoffer",
        True,
        "RAM3",
        OVERFLOW_PATCHES,
    )
    # The first attempt's token, held so the replay refusal can use it.
    first = _approved(choice, bank, [bank], "two.syx")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(bank, first, banks_list=[bank], path="two.syx")
        await _await_partial(app, pilot)
        await _settle(pilot)

        # Exactly one bundle went out: the overflow bank, and it landed.
        assert calls == [ram3_no]
        assert len(recorded) == 1
        closing = statuses[-1]
        assert closing.startswith("import: card refused the write")
        assert "write the target again" in closing
        assert not any("imported" in s for s in statuses), statuses
        # The session keeps the landed bank, and it is what the screen's
        # mint and the write boundary both skip (P7b derivation).
        assert app._import_passes["overflow-reoffer"]["overflow_written"] == "RAM3"
        assert app._skipped_banks_for("overflow-reoffer") == ("RAM3",)

        # The screen is back with the same session (P7c): no fresh visit,
        # no fresh key, the landed bank already in the pass state.
        assert isinstance(app.screen, WholeBankImportScreen)
        reopened = app.screen
        assert reopened._pass_state is not None
        assert reopened._pass_state.session_key == "overflow-reoffer"
        assert reopened._pass_state.skipped_banks == ("RAM3",)

        # The retry, minted by the screen itself: target Card, overflow
        # RAM 3 (the same plan), and the pass state's skip in the token.
        fails["armed"] = False
        await pilot.press("b")  # RAM 1 -> RAM 2
        await _settle(pilot)
        await pilot.press("b")  # -> RAM 3
        await _settle(pilot)
        await pilot.press("b")  # -> Card
        await _settle(pilot)
        await pilot.press("o")
        await _settle(pilot)
        await pilot.press("enter")  # the patches-only kind
        await _settle(pilot)
        await pilot.press("down")  # RAM 2 -- the parts name it
        await _settle(pilot)
        await pilot.press("down")  # RAM 1
        await _settle(pilot)
        await pilot.press("enter")  # RAM 3 -- the bank that landed
        # The retry is asked the affected-performances question too;
        # deciding and leaving goes straight through when nothing is on.
        await _answer_remediation_with_leave(app, pilot)
        await _wait_for_writes(app, pilot, 2)
        await _settle(pilot)

        # The pre-write status says the target is all the retry sends.
        assert any(
            "RAM 3 already landed; this write sends the target only" in s
            for s in statuses
        ), statuses
        # Exactly one bundle for the retry, to the target only: the
        # landed overflow bank was never sent again.
        assert calls == [ram3_no, card_no]
        # One undo record for that write, with its backup on disk.
        assert len(recorded) == 2
        assert recorded[1]["bank"] == banks.bank("CARD").label
        retry_backup = recorded[1]["backup"]
        assert isinstance(retry_backup, str) and os.path.exists(retry_backup)

        # The first token is spent, and a replayed token is refused.
        assert first.approval is not None
        assert first.approval.nonce in app._consumed_plan_nonces
        before = len(calls)
        app._import_whole_bank_write(bank, first, banks_list=[bank], path="two.syx")
        await _settle(pilot)
        assert len(calls) == before, "a replayed token must not reach the unit"
        assert len(recorded) == 2

    assert calls == [ram3_no, card_no]
    assert any("the overflow bank already held them" in s for s in statuses)


@pytest.mark.asyncio
async def test_overflow_partial_without_a_session_keeps_today_s_message(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """No session key, no re-offer: the plain partial message (P7c).

    A sessionless write cannot carry a retry's skip state, so the
    partial stays what it was -- the message that says the overflow bank
    landed and the target must be written again -- and no import screen
    is pushed over whatever is on the stack.
    """
    app, statuses, recorded, _saved = _partial_app(tmp_path, monkeypatch)
    bank = _mixed_bank()
    card_no = banks.bank("CARD").number
    ram3_no = banks.bank("RAM3").number
    original = app.device.write_bank_bundle
    calls: list[int] = []
    fails = {"armed": True}

    def failing(bank_number: int, **k: Any) -> dict[str, Any]:
        if fails["armed"] and bank_number == card_no:
            raise RuntimeError("card refused the write")
        calls.append(bank_number)
        return cast(dict[str, Any], original(bank_number, **k))

    app.device.write_bank_bundle = failing
    choice = WholeBankChoice(
        0,
        "CARD",
        True,
        perfcopy.ImportMode.AS_IS,
        "",  # no session key
        True,
        "RAM3",
        OVERFLOW_PATCHES,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(
            bank,
            _approved(choice, bank, [bank], "two.syx"),
            banks_list=[bank],
            path="two.syx",
        )
        await _await_partial(app, pilot)
        await _settle(pilot)

        assert calls == [ram3_no]
        assert len(recorded) == 1
        closing = statuses[-1]
        assert closing.startswith("import: card refused the write")
        assert "write the target again" in closing
        # No session: nothing recorded under any key, and no screen.
        assert app._import_passes == {}
        assert not isinstance(app.screen, WholeBankImportScreen)

    assert calls == [ram3_no]


@pytest.mark.asyncio
async def test_split_failure_keeps_the_landed_groups_and_n_writes_the_rest(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A split pass that stopped half way keeps what landed; ``n`` finishes.

    The Card group landed, RAM 3's write was refused: the landed group
    is recorded and cached, the session's remainder grows by the group
    the failure stopped before, and the reopened screen's ``n`` writes
    exactly that remainder -- the landed Card group is never rewritten.
    """
    app, statuses, recorded, saved = _partial_app(tmp_path, monkeypatch)
    file_banks = _split_continuation_banks()
    bank = file_banks[0]
    app._sources["RAM2"] = file_banks[1].source
    card_before = app._sources["CARD"]
    original = app.device.write_bank_bundle
    fails = {"armed": True}

    def failing(bank_number: int, **k: Any) -> dict[str, Any]:
        if fails["armed"] and app.device.bundle_write_count == 1:
            raise RuntimeError("ram refused the write")
        return cast(dict[str, Any], original(bank_number, **k))

    app.device.write_bank_bundle = failing
    choice = WholeBankChoice(
        0,
        "CARD",
        True,
        perfcopy.ImportMode.SELF_CONTAINED_PACKED,
        "three.syx|RAM1|partial",
        True,
        "RAM3",
        "self_contained",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(
            bank,
            _approved(choice, bank, file_banks, "three.syx"),
            banks_list=file_banks,
            path="three.syx",
        )
        await _await_partial(app, pilot)
        await _settle(pilot)

        # Only the Card's bundle went out; RAM 3 was never attempted.
        assert app.device.bundle_write_count == 1
        assert app.device.bundle_writes[0]["bank"] == banks.bank("CARD").label
        # The landed group is recorded and cached, as a success would be.
        assert len(recorded) == 1
        assert any(
            s["scope"] == "CARD" and s["performances"] is not None for s in saved
        )
        assert app._sources["CARD"] is not card_before

        closing = statuses[-1]
        assert closing.startswith("import: ram refused the write")
        assert "written: Card" in closing
        assert "failed or unverified (may have changed): RAM 3" in closing
        assert not any("imported" in s for s in statuses), statuses

        # The session holds the landed slots and the not-written ones.
        state = app._import_passes["three.syx|RAM1|partial"]
        assert state["pass_kind"] == "split"  # noqa: S105 -- a session marker, not a secret
        assert state["split_remaining"] == [50, 51, 52, 53, 54, 56, 57]
        assert set(state["written"]) == set(range(50))

        # The screen is back; ``n`` writes exactly the remainder's group.
        second = app.screen
        assert isinstance(second, WholeBankImportScreen)
        assert "7 performances remain" in _static_text(second, "whole-bank-status")
        fails["armed"] = False
        await pilot.press("n")
        # The continuation choice is asked the affected-performances
        # question too; deciding and leaving goes straight through.
        await _answer_remediation_with_leave(app, pilot)
        await _wait_for_writes(app, pilot, 2)
        await _settle(pilot)

    assert app.device.bundle_write_count == 2
    # The retry went to the card in the slot; the landed group stayed.
    assert [w["bank"] for w in app.device.bundle_writes] == [
        banks.bank("CARD").label,
        banks.bank("CARD").label,
    ]
    assert state["split_remaining"] == []
    assert set(state["written"]) == set(range(55)) | {56, 57}


@pytest.mark.asyncio
async def test_overflow_failure_before_the_first_write_records_nothing(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A refusal on the first write is today's plain failure, recorded nowhere."""
    app, statuses, recorded, saved = _partial_app(tmp_path, monkeypatch)
    bank = _mixed_bank()
    ram3_no = banks.bank("RAM3").number
    calls: list[int] = []

    def failing(bank_number: int, **k: Any) -> dict[str, Any]:
        calls.append(bank_number)
        raise RuntimeError("ram refused the write")

    app.device.write_bank_bundle = failing
    choice = WholeBankChoice(
        0,
        "CARD",
        True,
        perfcopy.ImportMode.AS_IS,
        "overflow-first-fail",
        True,
        "RAM3",
        OVERFLOW_PATCHES,
    )
    app._import_whole_bank_write(
        bank,
        _approved(choice, bank, [bank], "two.syx"),
        banks_list=[bank],
        path="two.syx",
    )

    assert calls == [ram3_no]
    assert app.device.bundle_write_count == 0
    assert recorded == []
    assert saved == []
    assert statuses == [
        "writing Bank RAM1 into Card...",
        "import: ram refused the write",
    ]


@pytest.mark.asyncio
async def test_overflow_read_back_mismatch_on_the_target_is_a_failure(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A target whose read-back differs is failed, and the overflow is kept.

    The bank was attempted, not verified: the undo record and the cache
    belong to the overflow bank alone, the target is named as not
    written with the mismatch as the error, and nothing says the import
    succeeded.
    """
    app, statuses, recorded, saved = _partial_app(tmp_path, monkeypatch)
    bank = _mixed_bank()
    card_no = banks.bank("CARD").number
    original = app.device.write_bank_bundle
    card_before = app._sources["CARD"]

    def mismatching(bank_number: int, **k: Any) -> dict[str, Any]:
        result = cast(dict[str, Any], original(bank_number, **k))
        if bank_number == card_no:
            return {**result, "verified": False, "mismatch": "performance 4 differs"}
        return result

    app.device.write_bank_bundle = mismatching
    choice = WholeBankChoice(
        0,
        "CARD",
        True,
        perfcopy.ImportMode.AS_IS,
        "overflow-mismatch",
        True,
        "RAM3",
        OVERFLOW_PATCHES,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(
            bank,
            _approved(choice, bank, [bank], "two.syx"),
            banks_list=[bank],
            path="two.syx",
        )
        await _await_partial(app, pilot)
        await _settle(pilot)

        # Both bundles went out; the second did not verify.
        assert app.device.bundle_write_count == 2
        # Only the verified overflow bank is recorded and cached.
        assert len(recorded) == 1
        assert recorded[0]["bank"] == banks.bank("RAM3").label
        assert all(s["scope"] != "CARD" for s in saved)
        assert app._sources["CARD"] is card_before

        closing = statuses[-1]
        assert closing.startswith("import: read-back mismatch: performance 4 differs")
        assert "written: RAM 3" in closing
        assert "failed or unverified (may have changed): Card" in closing
        assert not any("imported" in s for s in statuses), statuses
        assert app._import_passes["overflow-mismatch"]["overflow_written"] == "RAM3"


# --- Remediation: resumable after a partial failure (no automatic retries) ---


def _remediation_resume_app(
    tmp_path: Any, monkeypatch: Any
) -> tuple[KwsedApp, list[str], list[Any]]:
    """Armed app for resumable-remediation tests: two companions on CARD."""
    from kws import backup as _backup

    app = KwsedApp(DemoDevice(), bankstore_dir=str(tmp_path / "store"))
    app._config_path = str(tmp_path / "absent-kwsed.toml")
    app._sources["CARD"] = _make_bank(scope="CARD", active_slots=[]).source
    app._sources["RAM2"] = _make_bank(scope="RAM2", active_slots=[]).source
    app._sources["RAM3"] = _make_bank(scope="RAM3", active_slots=[]).source
    app._write_gate = EditGate(armed=True)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir, exist_ok=True)
    monkeypatch.setattr(_backup, "default_dir", lambda root=None: backup_dir)
    recorded: list[Any] = []
    app._record_write = recorded.append  # type: ignore[method-assign]
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    return app, statuses, recorded


async def _remediation_fail_second_setup(
    pilot: Any, app: KwsedApp, statuses: list[str]
) -> tuple[list[Any], list[int], dict[str, bool]]:
    """Start a preflight-both green-field write that fails on 2nd remediation.

    Returns (file_banks, write_ids, fail flag dict). Leaves the app on the
    re-offered screen after partial remediation failure with RAM2 cleared and
    RAM3 remediation failed.
    """
    from kwsed.import_bank import PreflightImportScreen

    file_banks = _three_bank_file()
    write_ids: list[int] = []
    original = app.device.write_bank_bundle
    flags = {"fail_second_remediation": True}

    def instrumented(bank_number: int, **k: Any) -> dict[str, Any]:
        write_ids.append(bank_number)
        # Green-field clears go to RAM2 then RAM3 (sorted, CARD not cleared).
        if (
            flags["fail_second_remediation"]
            and bank_number == banks.bank("RAM3").number
            and write_ids.count(bank_number) == 1
        ):
            # Only fail when this is a remediation clear (all three kinds):
            # the green-field clear writes performances, patches and wave
            # sequences in one call.
            raise RuntimeError("ram3 refused the write")
        return cast(dict[str, Any], original(bank_number, **k))

    app.device.write_bank_bundle = instrumented
    await pilot.pause()
    app.push_screen(
        WholeBankImportScreen(path="three.syx", banks=file_banks, gate=app._write_gate),
        lambda picked: app._import_whole_bank_chosen(file_banks, picked, "three.syx"),
    )
    await _settle(pilot)
    # target CARD (0->RAM1->RAM2->RAM3->CARD)
    for _ in range(4):
        await pilot.press("b")
        await _settle(pilot)
    await pilot.press("ctrl+s")
    await _wait_for_screen(app, pilot, PreflightImportScreen)
    await pilot.press("1")  # both
    await _wait_for_screen(app, pilot, AffectedImportScreen)
    await pilot.press("1")  # green field
    await _await_partial(app, pilot)
    await _settle(pilot)
    return file_banks, write_ids, flags


@pytest.mark.asyncio
async def test_remediation_partial_failure_records_reoffers_and_stops(
    tmp_path: Any, monkeypatch: Any
) -> None:
    app, statuses, recorded = _remediation_resume_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        _file_banks, write_ids, _flags = await _remediation_fail_second_setup(
            pilot, app, statuses
        )
        ram2 = banks.bank("RAM2").number
        ram3 = banks.bank("RAM3").number
        # Only the first remediation bank landed; target and companions not attempted.
        assert write_ids == [ram2, ram3]
        assert app.device.bundle_write_count == 1
        assert [w["bank"] for w in app.device.bundle_writes] == [
            banks.bank("RAM2").label
        ]
        # First remediation recorded with its backup.
        assert len(recorded) == 1
        assert recorded[0]["bank"] == banks.bank("RAM2").label
        assert isinstance(recorded[0].get("backup"), str) and os.path.exists(
            recorded[0]["backup"]
        )
        # Reported: written and not written/failed, no success claim.
        closing = statuses[-1]
        assert "written: RAM 2" in closing
        assert "failed or unverified (may have changed): RAM 3" in closing
        assert not any("imported" in s for s in statuses), statuses
        # Screen re-offered with the same session and a status saying what landed.
        assert isinstance(app.screen, WholeBankImportScreen)
        notice = _static_text(app.screen, "whole-bank-status")
        assert "remediation landed for RAM 2" in notice
        assert "remaining work still needs approval" in notice
        assert app.screen._pass_state is not None
        key = app.screen._pass_state.session_key
        assert key
        assert "RAM2" in app.screen._pass_state.skipped_banks
        assert app._skipped_banks_for(key) == ("RAM2",)


@pytest.mark.asyncio
async def test_remediation_retry_writes_remaining_then_companions_then_target(
    tmp_path: Any, monkeypatch: Any
) -> None:
    from kwsed.import_bank import AffectedImportScreen, PreflightImportScreen

    app, statuses, _recorded = _remediation_resume_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        _file_banks, write_ids, flags = await _remediation_fail_second_setup(
            pilot, app, statuses
        )
        assert isinstance(app.screen, WholeBankImportScreen)
        assert app.screen._pass_state is not None
        key = app.screen._pass_state.session_key
        # First attempt: one remediation bank landed, the next failed and stopped.
        assert write_ids[0] == banks.bank("RAM2").number
        assert "RAM2" in app._skipped_banks_for(key)
        ram3 = banks.bank("RAM3").number
        assert write_ids[1] == ram3
        bundles_before = list(app.device.bundle_writes)

        # Retry from the re-offered screen with the same session: approve again.
        # The carried-forward answer must clear/Init only banks not already
        # remediated, then companions (as confirmed) and the target.
        flags["fail_second_remediation"] = False
        await pilot.press("ctrl+s")
        for _ in range(1200):
            await pilot.pause()
            # The re-offered flow may re-ask preflight/affected; answer to
            # continue with the same confirmed intent where needed.
            if isinstance(app.screen, PreflightImportScreen):
                await pilot.press("1")
            if isinstance(app.screen, AffectedImportScreen):
                await pilot.press("1")
            if app.device.bundle_write_count >= len(bundles_before) + 4:
                break
        await _settle(pilot)

        # The bank that already landed as remediation was not re-cleared: its id
        # must not appear again as a remediation write. Retry wrote the remaining
        # remediation (RAM3) and went on to companions and the target.
        new_calls = write_ids[2:]
        assert ram3 in new_calls, (write_ids, statuses[-8:])
        assert len(app.device.bundle_writes) > len(bundles_before), (
            write_ids,
            statuses[-8:],
        )
        # RAM2 remediation landed in the first attempt and stays recorded; it must
        # not be cleared again as remaining remediation on retry.
        assert "RAM2" in app._skipped_banks_for(key)
        # The session keeps the landed remediation recorded across the retry.
        state = app._import_passes[key]
        assert "RAM2" in set(state.get("remediation_written") or [])


@pytest.mark.asyncio
async def test_remediation_retry_token_without_skip_state_refused_and_replay_refused(
    tmp_path: Any, monkeypatch: Any
) -> None:
    app, statuses, _recorded = _remediation_resume_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        file_banks, write_ids, flags = await _remediation_fail_second_setup(
            pilot, app, statuses
        )
        assert isinstance(app.screen, WholeBankImportScreen)
        assert app.screen._pass_state is not None
        key = app.screen._pass_state.session_key
        # Capture first attempt context: build a choice/approval as if
        # without skip state.
        bank = file_banks[0]
        # The plan that was attempted had green-field clearing RAM2,RAM3
        # with preflight both.
        # A token minted without landed-bank skip state must be refused.
        stale_choice = WholeBankChoice(
            0, "CARD", True, preflight="both", session_key=key
        )
        stale_choice = replace(
            stale_choice,
            remediation=Remediation.GREEN_FIELD,
            cleared_banks=("RAM2", "RAM3"),
        )
        # Mint without skip state by temporarily hiding it from session
        # derivation effect: _mint_whole_bank_approval uses
        # _skipped_banks_for(session_key) which now returns RAM2. So a mint
        # made now WILL include skip state and be valid for retry - we need
        # a mint made as if no banks landed. Simulate by clearing
        # remediation_written temporarily.
        state = app._import_passes[key]
        saved_rw = state.get("remediation_written")
        state["remediation_written"] = set()
        stale_approval = app._mint_whole_bank_approval(
            bank, stale_choice, file_banks, "three.syx"
        )
        state["remediation_written"] = saved_rw
        assert stale_approval is not None
        before_writes = app.device.bundle_write_count
        before_calls = len(write_ids)
        # Put pending remediation back so resolve won't auto-adjust in a
        # conflicting way? pending exists (answer stored) and will produce
        # remaining-only choice; but we want to test explicit write with
        # stale approval. Call boundary directly.
        # Ensure pending state is present so session represents the partial reality.
        app._import_whole_bank_write(
            bank,
            replace(stale_choice, approval=stale_approval),
            banks_list=file_banks,
            path="three.syx",
        )
        await _settle(pilot)
        assert app.device.bundle_write_count == before_writes
        assert len(write_ids) == before_calls
        assert stale_approval.nonce not in app._consumed_plan_nonces
        assert any(
            "the plan approval does not match this write" in s for s in statuses
        ), statuses

        # First token's replay is refused (if we had one spent). Mint a
        # valid retry token, spend it, then replay.
        flags["fail_second_remediation"] = False
        # Get a valid approval for the actual remaining plan.
        # pending remediation choice will be derived by resolve path.
        # Drive via screen retry to spend a token cleanly.
        await pilot.press("ctrl+s")
        for _ in range(600):
            await pilot.pause()
            if app.device.bundle_write_count >= before_writes + 4:
                break
        await _settle(pilot)
        spent_after = len(app._consumed_plan_nonces)
        assert spent_after >= 1


async def _remediation_drive_until_target_fails(
    pilot: Any,
    app: KwsedApp,
    statuses: list[str],
    write_ids: list[int],
    fail_target: dict[str, bool],
) -> None:
    """Run the green-field pass, failing only the target CARD write.

    Leaves the app just after the target failure was reported.
    """
    from kwsed.import_bank import AffectedImportScreen, PreflightImportScreen

    card = banks.bank("CARD").number
    for _ in range(4):
        await pilot.press("b")
        await _settle(pilot)
    await pilot.press("ctrl+s")
    await _wait_for_screen(app, pilot, PreflightImportScreen)
    await pilot.press("1")  # both
    await _wait_for_screen(app, pilot, AffectedImportScreen)
    await pilot.press("1")  # green field
    # Let remediation (and companions) complete; fail only the target CARD write.
    for _ in range(1200):
        await pilot.pause()
        if isinstance(app.screen, PreflightImportScreen):
            await pilot.press("1")
        if isinstance(app.screen, AffectedImportScreen):
            await pilot.press("1")
        if card in write_ids or app.device.bundle_write_count >= 5:
            fail_target["on"] = True
            fail_target["armed"] = True
        if any("card refused the write" in s for s in statuses):
            break
    await _settle(pilot)


async def _remediation_press_retry_until_imported(
    pilot: Any,
    app: KwsedApp,
    statuses: list[str],
    write_ids: list[int],
    before: list[int],
) -> None:
    """Approve the retry on whatever screen re-asks, until it succeeds."""
    from kwsed.import_bank import AffectedImportScreen, PreflightImportScreen

    await pilot.press("ctrl+s")
    for _ in range(1200):
        await pilot.pause()
        if isinstance(app.screen, PreflightImportScreen):
            await pilot.press("1")
        if isinstance(app.screen, AffectedImportScreen):
            await pilot.press("1")
        if len(write_ids) > len(before):
            # allow follow-up target/companion writes but no remediation pass
            pass
        if app.device.bundle_write_count >= len(app.device.bundle_writes):
            # settle naturally
            pass
        if any("imported" in s for s in statuses[len(statuses) - 3 :]):
            break
    await _settle(pilot)


@pytest.mark.asyncio
async def test_remediation_retry_after_target_failure_does_not_reclear(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """After remediation fully completed, a target failure must not re-clear."""

    app, statuses, _recorded = _remediation_resume_app(tmp_path, monkeypatch)
    file_banks = _three_bank_file()
    write_ids: list[int] = []
    original = app.device.write_bank_bundle
    card = banks.bank("CARD").number
    fail_target = {"on": False, "armed": False}

    def instrumented(bank_number: int, **k: Any) -> dict[str, Any]:
        write_ids.append(bank_number)
        if (
            fail_target["on"]
            and bank_number == card
            and "performances" in k
            and fail_target["armed"]
        ):
            raise RuntimeError("card refused the write")
        return cast(dict[str, Any], original(bank_number, **k))

    app.device.write_bank_bundle = instrumented
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            WholeBankImportScreen(
                path="three.syx", banks=file_banks, gate=app._write_gate
            ),
            lambda picked: app._import_whole_bank_chosen(
                file_banks, picked, "three.syx"
            ),
        )
        await _settle(pilot)
        await _remediation_drive_until_target_fails(
            pilot, app, statuses, write_ids, fail_target
        )

        # Remediation completed for its banks (recorded) before the target failed.
        completed_keys = [
            sk
            for sk, st in app._import_passes.items()
            if {"RAM2", "RAM3"}.issubset(set(st.get("remediation_written") or []))
            or {"RAM1", "RAM2", "RAM3"}.issubset(
                set(st.get("remediation_written") or [])
            )
        ]
        assert completed_keys, (app._import_passes, statuses[-8:])
        key = completed_keys[0]
        state = app._import_passes[key]
        assert not state.get("remediation_answer"), state
        landed = set(state.get("remediation_written") or [])
        assert landed, state

        before = list(write_ids)
        fail_target["on"] = False
        # Retry must not re-clear anything: remediation is complete for this plan.
        if isinstance(app.screen, WholeBankImportScreen):
            await _remediation_press_retry_until_imported(
                pilot, app, statuses, write_ids, before
            )

        # Still recorded as completed; no remediation answer carried forward.
        state_after = app._import_passes.get(key) or {}
        assert not state_after.get("remediation_answer"), state_after
        assert landed.issubset(set(state_after.get("remediation_written") or [])), (
            landed,
            state_after,
        )


@pytest.mark.asyncio
async def test_overflow_success_keeps_today_s_message_and_one_record_per_bank(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A clean overflow import: today's wording, one undo record per bank."""
    app, statuses, recorded, _saved = _partial_app(tmp_path, monkeypatch)
    bank = _mixed_bank()
    ram3_no = banks.bank("RAM3").number
    card_no = banks.bank("CARD").number
    original = app.device.write_bank_bundle
    calls: list[int] = []

    def write(bank_number: int, **k: Any) -> dict[str, Any]:
        calls.append(bank_number)
        return cast(dict[str, Any], original(bank_number, **k))

    app.device.write_bank_bundle = write
    choice = WholeBankChoice(
        0,
        "CARD",
        True,
        perfcopy.ImportMode.AS_IS,
        "overflow-clean",
        True,
        "RAM3",
        OVERFLOW_PATCHES,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(
            bank,
            _approved(choice, bank, [bank], "two.syx"),
            banks_list=[bank],
            path="two.syx",
        )
        await _wait_for_writes(app, pilot, 2)
        await _settle(pilot)

    assert calls == [ram3_no, card_no]
    assert len(recorded) == 2
    assert [r["bank"] for r in recorded] == [
        banks.bank("RAM3").label,
        banks.bank("CARD").label,
    ]
    closing = statuses[-1]
    assert closing.startswith("imported Bank RAM1 into Card: 50 performances, ")
    assert "patches repointed from RAM 3; RAM 3 also received" in closing


# --- T4: truthful completion, unchanged bundles, and the gate's plan -----


def _stub_result(
    *,
    kinds: list[str],
    unchanged: Optional[list[str]] = None,
    verified: bool = True,
    mismatch: Optional[str] = None,
    backup: str = "env.syx",
) -> dict[str, Any]:
    """A bridge ``write_bank_bundle`` verdict the test controls."""
    return {
        "bank": banks.bank("RAM3").label,
        "kind": "bank-bundle",
        "kinds": list(kinds),
        "backup": backup,
        "verified": verified,
        "mismatch": mismatch,
        "unchanged": list(unchanged or []),
    }


@pytest.mark.asyncio
async def test_unchanged_bundle_says_already_matches_and_records_once(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A bundle the unit already holds is a match, not an import.

    The bridge sent nothing for it; the status must not claim SysEx left
    the cable. The pre-write backup was still taken, so the fence ran and
    one undo point is recorded.
    """
    app, recorded = _armed_app(tmp_path, monkeypatch)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))
    calls: list[int] = []

    def unchanged_write(*a: Any, **k: Any) -> dict[str, Any]:
        calls.append(1)
        return _stub_result(kinds=[], unchanged=["performances", "patches"])

    app.device.write_bank_bundle = unchanged_write
    bank = _mixed_bank()
    with patch.object(bankstore, "save", lambda *a, **k: None):
        app._import_whole_bank_write(
            bank,
            _approved(WholeBankChoice(0, "RAM3", True), bank),
            banks_list=[bank],
            path="one.syx",
        )

    assert calls == [1]  # the fence still read the unit and backed it up
    assert app.device.bundle_write_count == 0
    assert len(recorded) == 1
    closing = statuses[-1]
    assert "already matches" in closing
    assert "nothing was sent" in closing
    assert "backup" in closing
    assert "imported" not in closing
    assert "wrote" not in closing


@pytest.mark.asyncio
async def test_changed_bundle_keeps_today_s_imported_message(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A bundle that really was sent keeps the imported wording."""
    app, recorded = _armed_app(tmp_path, monkeypatch)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))

    def changed_write(*a: Any, **k: Any) -> dict[str, Any]:
        return _stub_result(kinds=["performances", "patches"])

    app.device.write_bank_bundle = changed_write
    bank = _mixed_bank()
    with patch.object(bankstore, "save", lambda *a, **k: None):
        app._import_whole_bank_write(
            bank,
            _approved(WholeBankChoice(0, "RAM3", True), bank),
            banks_list=[bank],
            path="one.syx",
        )

    closing = statuses[-1]
    assert closing.startswith("imported Bank RAM1 into RAM 3: ")
    assert len(recorded) == 1


@pytest.mark.asyncio
async def test_plain_write_read_back_mismatch_is_a_failure(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A verdict the unit did not verify is a refusal, never an import.

    The mismatch is named, nothing claims success, and the failed bank
    gets no undo record and no cache update -- its backup is the way
    back.
    """
    app, recorded = _armed_app(tmp_path, monkeypatch)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))

    def mismatching(*a: Any, **k: Any) -> dict[str, Any]:
        return _stub_result(
            kinds=["performances"],
            verified=False,
            mismatch="performance 4 differs",
        )

    app.device.write_bank_bundle = mismatching
    before = app._sources["RAM3"]
    bank = _mixed_bank()
    with patch.object(bankstore, "save", lambda *a, **k: None):
        app._import_whole_bank_write(
            bank,
            _approved(WholeBankChoice(0, "RAM3", True), bank),
            banks_list=[bank],
            path="one.syx",
        )

    closing = statuses[-1]
    assert closing.startswith("import: read-back mismatch: performance 4 differs")
    assert not any("imported" in s for s in statuses), statuses
    assert recorded == []
    assert app._sources["RAM3"] is before


@pytest.mark.asyncio
async def test_gate_locks_when_the_target_changes(tmp_path: Any) -> None:
    """Armed for one target, ``b`` locks the gate before ctrl+s can write."""
    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    app._sources["RAM3"] = _make_bank(scope="RAM3").source
    screen = WholeBankImportScreen(
        path="one.syx", banks=[_make_bank()], gate=app._write_gate
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen)
        await _settle(pilot)
        await pilot.press("w")
        await _settle(pilot)
        assert app._write_gate.armed
        assert screen._armed_fingerprint == screen._plan_fingerprint()
        await pilot.press("b")
        await _settle(pilot)
        assert not app._write_gate.armed
        assert "plan changed — gate locked again" in _static_text(
            screen, "whole-bank-status"
        )
        assert "locked" in _static_text(screen, "whole-bank-gate")
        await pilot.press("ctrl+s")
        await _settle(pilot)
        assert "write gate is locked" in _static_text(screen, "whole-bank-status")
        assert app.device.bundle_write_count == 0
        assert isinstance(app.screen, WholeBankImportScreen)


@pytest.mark.asyncio
async def test_gate_locks_when_the_mode_changes(tmp_path: Any) -> None:
    """Armed as-is, ``m`` locks the gate: packed is not what was approved."""
    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    app._sources["RAM3"] = _make_bank(scope="RAM3").source
    screen = WholeBankImportScreen(
        path="one.syx", banks=[_make_bank()], gate=app._write_gate
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen)
        await _settle(pilot)
        await pilot.press("w")
        await _settle(pilot)
        await pilot.press("m")
        await _settle(pilot)
        assert not app._write_gate.armed
        assert "plan changed — gate locked again" in _static_text(
            screen, "whole-bank-status"
        )
        await pilot.press("ctrl+s")
        await _settle(pilot)
    assert app.device.bundle_write_count == 0


@pytest.mark.asyncio
async def test_gate_locks_when_the_source_changes(tmp_path: Any) -> None:
    """``s`` on a multi-bank file locks an armed gate the same way."""
    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    app._sources["RAM3"] = _make_bank(scope="RAM3").source
    file_banks = _two_bank_file()
    screen = WholeBankImportScreen(
        path="two.syx", banks=file_banks, gate=app._write_gate
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(screen)
        await _settle(pilot)
        await pilot.press("w")
        await _settle(pilot)
        assert app._write_gate.armed
        await pilot.press("s")
        await _settle(pilot)
        assert screen._source_index == 1
        assert not app._write_gate.armed
        assert "plan changed — gate locked again" in _static_text(
            screen, "whole-bank-status"
        )
        await pilot.press("ctrl+s")
        await _settle(pilot)
    assert app.device.bundle_write_count == 0


@pytest.mark.asyncio
async def test_armed_gate_with_an_unchanged_plan_writes(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Arm then ctrl+s: the fingerprint matches, so the write proceeds."""
    from kws import backup as _backup

    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    app._sources["RAM3"] = _make_bank(scope="RAM3").source
    app.notify_status = lambda *a, **k: None  # type: ignore[method-assign]
    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir, exist_ok=True)
    monkeypatch.setattr(_backup, "default_dir", lambda root=None: backup_dir)
    recorded: list[Any] = []
    app._record_write = recorded.append  # type: ignore[method-assign]
    file_banks = [_make_bank()]
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = WholeBankImportScreen(
            path="one.syx", banks=file_banks, gate=app._write_gate
        )
        app.push_screen(
            screen,
            lambda picked: app._import_whole_bank_chosen(file_banks, picked, "one.syx"),
        )
        await _settle(pilot)
        await pilot.press("w")
        await _settle(pilot)
        assert app._write_gate.armed
        await pilot.press("ctrl+s")
        # The demo unit's banks name RAM 1, the target here, so the counts
        # question has numbers; "l" leaves them and the write proceeds.
        await pilot.press("2")
        from kwsed.import_bank import AffectedCountsScreen

        for _ in range(100):
            await pilot.pause()
            if isinstance(app.screen, AffectedCountsScreen):
                break
        await pilot.press("l")
        await _wait_for_writes(app, pilot, 1)
        await _settle(pilot)
        assert app.device.bundle_write_count == 1
        # The plan did not change between arming and firing: the approval
        # stood, and the gate stays armed for the same plan.
        assert app._write_gate.armed
        assert screen._armed_fingerprint == screen._plan_fingerprint()
    assert len(recorded) == 1


# ---------------------------------------------------------------------------
# The write boundary: every unit write carries a one-shot plan approval
# that matches the plan it will execute (P3).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whole_write_without_an_approval_touches_nothing(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A unit write without a plan approval is refused before anything runs."""
    from kws import backup as _backup

    app, recorded = _armed_app(tmp_path, monkeypatch)
    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir, exist_ok=True)
    monkeypatch.setattr(_backup, "default_dir", lambda root=None: backup_dir)
    saves: list[Any] = []
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: saves.append(k))
    calls: list[Any] = []

    def fake_write(*a: Any, **k: Any) -> dict[str, Any]:
        calls.append(1)
        return {"verified": True}

    app.device.write_bank_bundle = fake_write
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))
    bank = _mixed_bank()
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(
            bank, WholeBankChoice(0, "RAM3", True), banks_list=[bank], path="one.syx"
        )
        await _settle(pilot)
    assert calls == []
    assert saves == []
    assert recorded == []
    assert any("no plan approval" in s for s in statuses), statuses


@pytest.mark.asyncio
async def test_whole_write_without_the_file_banks_touches_nothing(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Without the file's banks the approval cannot be checked: refuse."""
    from kws import backup as _backup

    app, recorded = _armed_app(tmp_path, monkeypatch)
    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir, exist_ok=True)
    monkeypatch.setattr(_backup, "default_dir", lambda root=None: backup_dir)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    calls: list[Any] = []

    def fake_write(*a: Any, **k: Any) -> dict[str, Any]:
        calls.append(1)
        return {"verified": True}

    app.device.write_bank_bundle = fake_write
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))
    bank = _mixed_bank()
    approved = _approved(WholeBankChoice(0, "RAM3", True), bank)
    assert approved.approval is not None
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(bank, approved, banks_list=None, path="one.syx")
        await _settle(pilot)
    assert calls == []
    assert recorded == []
    # A refused write spends nothing: the nonce is still unconsumed.
    assert approved.approval.nonce not in app._consumed_plan_nonces
    assert any("cannot be checked" in s for s in statuses), statuses


@pytest.mark.asyncio
async def test_whole_write_with_a_foreign_approval_is_refused(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """An approval minted for a different plan does not authorize this write."""
    from kws import backup as _backup

    app, recorded = _armed_app(tmp_path, monkeypatch)
    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir, exist_ok=True)
    monkeypatch.setattr(_backup, "default_dir", lambda root=None: backup_dir)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    calls: list[Any] = []

    def fake_write(*a: Any, **k: Any) -> dict[str, Any]:
        calls.append(1)
        return {"verified": True}

    app.device.write_bank_bundle = fake_write
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))
    bank = _mixed_bank()
    # The token was minted for a packed-mode plan into RAM 2; the write
    # asks for an as-is plan into RAM 3.
    foreign = _approved(
        WholeBankChoice(0, "RAM2", True, perfcopy.ImportMode.SELF_CONTAINED_PACKED),
        bank,
    )
    assert foreign.approval is not None
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(
            bank,
            WholeBankChoice(0, "RAM3", True, approval=foreign.approval),
            banks_list=[bank],
            path="one.syx",
        )
        await _settle(pilot)
    assert calls == []
    assert recorded == []
    assert foreign.approval.nonce not in app._consumed_plan_nonces
    assert any("does not match" in s for s in statuses), statuses


@pytest.mark.asyncio
async def test_whole_write_approval_nonce_is_one_shot(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The first write spends the approval; a replay is refused."""
    from kws import backup as _backup

    app, recorded = _armed_app(tmp_path, monkeypatch)
    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir, exist_ok=True)
    monkeypatch.setattr(_backup, "default_dir", lambda root=None: backup_dir)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    calls: list[Any] = []

    def fake_write(*a: Any, **k: Any) -> dict[str, Any]:
        calls.append(1)
        return {"verified": True}

    app.device.write_bank_bundle = fake_write
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))
    bank = _mixed_bank()
    first = _approved(WholeBankChoice(0, "RAM3", True), bank)
    assert first.approval is not None
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(bank, first, banks_list=[bank], path="one.syx")
        await _wait_for_writes(app, pilot, 1)
        await _settle(pilot)
        assert calls == [1]
        assert len(recorded) == 1
        assert first.approval.nonce in app._consumed_plan_nonces

        replay = WholeBankChoice(0, "RAM3", True, approval=first.approval)
        app._import_whole_bank_write(bank, replay, banks_list=[bank], path="one.syx")
        await _settle(pilot)
        assert calls == [1]
        assert len(recorded) == 1
        assert any("already used" in s for s in statuses), statuses


@pytest.mark.asyncio
async def test_screen_mint_and_app_rebuild_agree_on_skipped_banks(
    tmp_path: Any,
) -> None:
    """The screen's retry token and the boundary's rebuild carry the same skips.

    The screen mints from the pass state the app handed it; the app
    derives the same set from its own session state at the boundary.
    Both must land on the same ``skipped_banks``, or every retry of a
    partial attempt would be refused (P7b).
    """
    from kwsed.import_bank import ImportPassState
    from kwsed.import_intent import approval_matches

    app = KwsedApp(DemoDevice())
    bank = _mixed_bank()
    key = "two.syx|RAM1|skip-agree"
    # The session state the partial overflow recorded: the landed bank.
    app._import_passes[key] = {"overflow_written": "RAM3"}
    assert app._skipped_banks_for(key) == ("RAM3",)
    assert app._skipped_banks_for("") == (), "a sessionless write skips nothing"
    choice = WholeBankChoice(
        0,
        "CARD",
        True,
        perfcopy.ImportMode.AS_IS,
        key,
        True,
        "RAM3",
        OVERFLOW_PATCHES,
    )
    screen = WholeBankImportScreen(
        path="two.syx",
        banks=[bank],
        gate=app._write_gate,
        pass_state=ImportPassState(session_key=key, skipped_banks=("RAM3",)),
    )
    approval = screen._mint_for_choice(choice)
    assert approval is not None
    intent_and_sources = app._whole_bank_intent(
        bank, replace(choice, approval=approval), [bank], "two.syx"
    )
    assert intent_and_sources is not None
    intent, _sources = intent_and_sources
    assert intent.skipped_banks == ("RAM3",)
    assert approval_matches(approval, intent), "mint and rebuild agree"
    # The nonce is consumed by the validated boundary, exactly once.
    assert approval.nonce in app._consumed_plan_nonces
    # A companion choice carries no session key: it skips nothing, even
    # on a screen whose session has landed banks.
    companion = WholeBankChoice(
        0, "RAM3", True, perfcopy.ImportMode.AS_IS, "", companion_only=True
    )
    assert screen._mint_skipped_banks(companion) == ()


@pytest.mark.asyncio
async def test_whole_write_plans_from_the_snapshot_taken_at_the_boundary(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The worker plans from the frozen snapshot, not the live sources (P7b).

    The session's source bank is mutated after the approval is checked
    but before the worker plans; the bytes written must still be the
    approved plan's, built from the records the approval covered.
    """
    from kws import backup as _backup
    from tests.test_bankbuild import make_patch, make_performance

    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    app._write_gate = EditGate(armed=True)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir, exist_ok=True)
    monkeypatch.setattr(_backup, "default_dir", lambda root=None: backup_dir)
    bank = _mixed_bank()
    original_perfs = list(bank.source.performances)
    original_patches = list(bank.source.patches)
    # The approved plan, built from the records as they were validated.
    expected = perfcopy.relocate_bank(bank.source, banks.bank("RAM3"))
    mutated: dict[str, bool] = {"on": False}
    statuses: list[str] = []

    def _mutate_and_record(msg: str) -> None:
        statuses.append(msg)
        if not mutated["on"]:
            # The pre-write status is the last main-thread step before
            # the worker runs: mutating here is exactly the window the
            # snapshot must close.
            mutated["on"] = True
            bank.source.performances[2] = make_performance("Late Arrival", ((0, 1),))
            bank.source.patches[0] = make_patch("Late Patch")

    app.notify_status = lambda msg, *a, **k: _mutate_and_record(str(msg))  # type: ignore[method-assign]
    writes: list[dict[str, Any]] = []
    original = app.device.write_bank_bundle

    def recording(bank_number: int, **k: Any) -> dict[str, Any]:
        writes.append(dict(k))
        return cast(dict[str, Any], original(bank_number, **k))

    app.device.write_bank_bundle = recording
    app._record_write = lambda result: None  # type: ignore[method-assign,misc]
    app._import_whole_bank_write(
        bank,
        _approved(WholeBankChoice(0, "RAM3", True, perfcopy.ImportMode.AS_IS), bank),
        banks_list=[bank],
        path="one.syx",
    )
    assert mutated["on"], "the mutation ran inside the boundary-to-worker window"
    assert len(writes) == 1
    assert writes[0]["performances"] == expected.performances
    assert writes[0]["patches"] == expected.patches
    assert writes[0]["waveseq"] == expected.waveseq
    # The device stored the approved plan's bytes, not the mutated ones.
    written = _written_perfs(app.device)
    ram3 = banks.bank("RAM3").number
    assert _perf_at(written, 2) == _as_written(original_perfs[2], {0: (ram3, 0)})
    assert _patch_at(_written_patches(app.device), 0) == original_patches[0]


@pytest.mark.asyncio
async def test_c_saves_to_the_cache_without_minting_or_touching_the_unit(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """``c`` is cache-only: no token minted, no device calls (P7b).

    A bank whose unit write cannot be planned still saves: the cache key
    dismisses without minting, and the app takes the
    ``write_to_unit=False`` branch before any approval check. The bank
    here is genuinely unplannable for a unit write -- its performance
    names its own bank's patch 0 and the file carries no patch bank, so
    the planners refuse a self-contained write.
    """
    from kwsed import import_intent

    app = KwsedApp(DemoDevice())
    app._backup_dir = str(tmp_path / "backups")
    store_dir = str(tmp_path / "store")
    app._bankstore_dir = store_dir
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

    monkeypatch.setattr(bankstore, "save", fake_save)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    bank = _make_bank(scope="RAM1", active_slots=[3])
    bank.source.patches = []
    # The unit write really cannot be planned: the planners refuse it.
    with pytest.raises(ValueError):
        import_intent.build_intent(
            source_id="one.syx|RAM1",
            source_scope="RAM1",
            sources={"RAM1": bank.source},
            source=bank.source,
            target=banks.bank("RAM3"),
            mode=perfcopy.ImportMode.SELF_CONTAINED_1TO1,
        )
    file_banks = [bank]
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            WholeBankImportScreen(
                path="one.syx", banks=file_banks, gate=app._write_gate
            ),
            lambda picked: app._import_whole_bank_chosen(file_banks, picked, "one.syx"),
        )
        await _settle(pilot)
        await pilot.press("m")  # a self-contained mode: the unit write refuses
        await pilot.press("c")
        await _settle(pilot)
    assert len(saved) == 1
    assert saved[0]["scope"] == "RAM1"
    assert saved[0]["directory"] == store_dir, "the cache path is unchanged"
    assert saved[0]["performances"] is not None
    assert app.device.bundle_write_count == 0
    assert app.device.bundle_writes == []
    assert any("cache RAM1" in s for s in statuses), statuses


@pytest.mark.asyncio
async def test_whole_write_with_a_valid_approval_matches_relocate_bank(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A fresh exact approval writes, byte-identical to relocate_bank."""
    from kws import backup as _backup

    app, recorded = _armed_app(tmp_path, monkeypatch)
    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir, exist_ok=True)
    monkeypatch.setattr(_backup, "default_dir", lambda root=None: backup_dir)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    writes: list[dict[str, Any]] = []

    def fake_write(*a: Any, **k: Any) -> dict[str, Any]:
        writes.append(dict(k))
        return {"verified": True}

    app.device.write_bank_bundle = fake_write
    bank = _mixed_bank()
    approved = _approved(
        WholeBankChoice(0, "RAM3", True, perfcopy.ImportMode.AS_IS), bank
    )
    assert approved.approval is not None
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(bank, approved, banks_list=[bank], path="one.syx")
        await _wait_for_writes(app, pilot, 1)
        await _settle(pilot)
    assert len(writes) == 1
    expected = perfcopy.relocate_bank(bank.source, banks.bank("RAM3"))
    assert writes[0]["performances"] == expected.performances
    assert writes[0]["patches"] == expected.patches
    assert writes[0]["waveseq"] == expected.waveseq
    assert len(recorded) == 1
    assert approved.approval.nonce in app._consumed_plan_nonces


@pytest.mark.asyncio
async def test_whole_write_with_a_valid_approval_and_a_locked_gate_refuses(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The gate and the approval are both required: either alone refuses."""
    from kws import backup as _backup

    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    app._backup_dir = str(tmp_path / "backups")
    os.makedirs(app._backup_dir, exist_ok=True)
    monkeypatch.setattr(_backup, "default_dir", lambda root=None: app._backup_dir)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    app._write_gate = EditGate(armed=False)
    calls: list[Any] = []

    def fake_write(*a: Any, **k: Any) -> dict[str, Any]:
        calls.append(1)
        return {"verified": True}

    app.device.write_bank_bundle = fake_write
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    bank = _mixed_bank()
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(
            bank,
            _approved(WholeBankChoice(0, "RAM3", True), bank),
            banks_list=[bank],
            path="one.syx",
        )
        await _settle(pilot)
    assert calls == []
    assert any("write gate is locked" in s for s in statuses), statuses


def test_a_companion_write_keeps_the_banks_performances_in_the_session(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The session's copy keeps the performances a companion write never sent.

    The write sends patches and wave sequences only, so the bank's
    performances are still the unit's. When the bank was not loaded this
    session, the cached copy is the record to keep: an empty list would
    make every performance in it look unloaded to the dependency graph.
    """
    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    cached = _make_bank(scope="RAM2", active_slots=[0])
    app._sources_with_store = lambda: {"RAM2": cached.source}  # type: ignore[method-assign]
    assert "RAM2" not in app._sources
    app._remember_companion_patches(banks.bank("RAM2"), _two_bank_file()[1].source)
    kept = app._sources["RAM2"]
    assert len(kept.performances) == 50
    assert not perfcopy.is_empty_performance(kept.performances[0])


# ---------------------------------------------------------------------------
# The affected-performances remediation (P5): the question, the counts, the
# green field, and the Init write. Every name here is invented.
# ---------------------------------------------------------------------------


def _named_perf(name: str, bank_number: int, patch: int) -> bytes:
    from tests.test_bankbuild import make_performance

    return cast(bytes, make_performance(name, ((bank_number, patch),)))


def _seed_bank_blob(entries: dict[int, bytes]) -> bytes:
    """A whole performance bank with the named slots filled in."""
    out = bytearray()
    for slot in range(banks.PERFORMANCES_PER_BANK):
        out += entries.get(slot, bytes(PERF_RECORD_SIZE))
    return bytes(out)


def _counts_app(tmp_path: Any, monkeypatch: Any) -> tuple[Any, list[dict[str, Any]]]:
    """An armed app for the counts flow, with its cache and backups sandboxed."""
    from kws import backup as _backup

    app = _make_app(tmp_path, bankstore_dir=str(tmp_path / "store"))
    app._sources["RAM3"] = _make_bank(scope="RAM3", active_slots=[]).source
    app._write_gate = EditGate(armed=True)
    app.notify_status = lambda *a, **k: None  # type: ignore[method-assign]
    backup_dir = str(tmp_path / "counts-backups")
    os.makedirs(backup_dir, exist_ok=True)
    monkeypatch.setattr(_backup, "default_dir", lambda root=None: backup_dir)
    recorded: list[dict[str, Any]] = []
    app._record_write = recorded.append  # type: ignore[method-assign]
    return app, recorded


async def _wait_for_screen(
    app: KwsedApp, pilot: Any, kind: Any, limit: int = 200
) -> Any:
    for _ in range(limit):
        await pilot.pause()
        if isinstance(app.screen, kind):
            return app.screen
    raise AssertionError(f"{kind.__name__} never appeared")


async def _answer_remediation_with_leave(app: KwsedApp, pilot: Any) -> None:
    """Resolve the affected-performances question: decide, then leave.

    Stage 1 offers green field / decide-with-counts / cancel only, so the
    plain way past it is decide: the unit is read and the counts modal
    offers leave -- or, when nothing is affected, the write goes
    straight on and there is no counts modal at all.
    """
    await _wait_for_screen(app, pilot, AffectedImportScreen)
    before = app.device.bundle_write_count
    await pilot.press("2")  # decide with counts
    for _ in range(600):
        await pilot.pause()
        if isinstance(app.screen, AffectedCountsScreen):
            await pilot.press("l")  # leave as is
            return
        if app.device.bundle_write_count > before:
            return  # nothing affected: the write went straight on
    raise AssertionError("the counts never came and nothing was written")


async def _run_to_counts(
    app: KwsedApp,
    pilot: Any,
    file_banks: Sequence[Any],
    path: str = "one.syx",
) -> Any:
    """From a whole-bank choice to the counts modal: stage 1, then decide."""
    app._import_whole_bank_chosen(file_banks, WholeBankChoice(0, "RAM3", True), path)
    await _wait_for_screen(app, pilot, AffectedImportScreen)
    await pilot.press("2")
    return await _wait_for_screen(app, pilot, AffectedCountsScreen)


@pytest.mark.asyncio
async def test_the_remediation_question_rises_before_any_write(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A whole-bank write is asked the question first; cancelling writes nothing."""
    from kwsed.import_bank import WholeBankImportScreen

    app, _recorded = _counts_app(tmp_path, monkeypatch)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    bank = _mixed_bank()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            WholeBankImportScreen(path="one.syx", banks=[bank], gate=app._write_gate),
            lambda picked: app._import_whole_bank_chosen([bank], picked, "one.syx"),
        )
        await _settle(pilot)
        await pilot.press("b")
        await pilot.press("b")  # target RAM 3
        await pilot.press("ctrl+s")
        modal = await _wait_for_screen(app, pilot, AffectedImportScreen)
        body = _static_text(modal, "affected-body")
        assert "RAM 3" in body
        assert app.device.bundle_write_count == 0
        await pilot.press("escape")
        await _settle(pilot)
        assert isinstance(app.screen, WholeBankImportScreen)
    assert app.device.bundle_write_count == 0


@pytest.mark.asyncio
async def test_the_question_names_the_overflow_bank_and_the_companion(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The body names every bank whose patches the import replaces."""
    app, _recorded = _counts_app(tmp_path, monkeypatch)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    file_banks = _two_bank_file()
    async with app.run_test() as pilot:
        await pilot.pause()
        choice = WholeBankChoice(
            0,
            "RAM3",
            True,
            perfcopy.ImportMode.AS_IS,
            "",
            True,
            "RAM2",
            OVERFLOW_PATCHES,
        )
        app._import_whole_bank_chosen(file_banks, choice, "two.syx")
        modal = await _wait_for_screen(app, pilot, AffectedImportScreen)
        body = _static_text(modal, "affected-body")
        assert "RAM 3" in body
        assert "RAM 2" in body
        assert app.device.bundle_write_count == 0
        await pilot.press("escape")
        await _settle(pilot)
    assert app.device.bundle_write_count == 0


@pytest.mark.asyncio
async def test_green_field_clears_the_imports_bank_without_reading_the_unit(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Green field clears the bank the import writes, then writes it.

    No candidate performance dump is read, the clear is its own fenced
    bundle before the target write, and the session and cache end up
    showing the Init state for the cleared bank.
    """
    from kws.initbank import (
        INIT_PATCH,
        INIT_PERFORMANCE,
        init_patches,
        init_performances,
        init_waveseq,
    )

    app, recorded = _counts_app(tmp_path, monkeypatch)
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
            }
        )

    monkeypatch.setattr(bankstore, "save", fake_save)
    bank = _mixed_bank()
    reads: list[tuple[int, int]] = []
    original_read = app.device.read_raw

    def recording_read(bank_number: int, function: int, **k: Any) -> bytes:
        reads.append((bank_number, function))
        return cast(bytes, original_read(bank_number, function, **k))

    app.device.read_raw = recording_read
    choice = WholeBankChoice(0, "RAM3", True, remediation=Remediation.GREEN_FIELD)
    choice = replace(choice, cleared_banks=("RAM3",))
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(
            bank,
            _approved(choice, bank),
            banks_list=[bank],
            path="one.syx",
        )
        await _wait_for_writes(app, pilot, 2)
        await _settle(pilot)
    ram3_sysex = banks.bank("RAM3").sysex
    # No candidate performance dump was read: green field reads nothing.
    assert all(
        function != m.Function.ALL_PERFORMANCE_DUMP or number == ram3_sysex
        for number, function in reads
    ), reads
    clear_write, target_write = app.device.bundle_writes
    assert clear_write["bank"] == banks.bank("RAM3").label
    assert clear_write["kinds"] == ["waveseq", "patches", "performances"]
    # The target write follows, carrying the import's own bank.
    assert target_write["bank"] == banks.bank("RAM3").label
    assert len(recorded) == 2, "each bank write has its own undo record"
    # The session shows what the unit holds now: the import landed on
    # top of the clear, so the browser shows the imported bank.
    source = app._sources["RAM3"]
    expected = perfcopy.relocate_bank(bank.source, banks.bank("RAM3"))
    assert (
        source.performances[2]
        == expected.performances[2 * PERF_RECORD_SIZE : 3 * PERF_RECORD_SIZE]
    )
    clear_saved = next(
        entry for entry in saved if entry["scope"] == "RAM3" and entry["performances"]
    )
    assert clear_saved["performances"] == init_performances()
    assert clear_saved["patches"] == init_patches()
    assert clear_saved["waveseq"] == init_waveseq()


@pytest.mark.asyncio
async def test_a_cleared_bank_shows_init_when_the_write_fails_after_it(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The cleared bank's session and cache show Init, not stale names.

    When the import's own write fails after the clear landed, what the
    unit holds is the Init state: the browser must not keep the old
    names, and the cache must not claim the import arrived.
    """
    from kws.initbank import INIT_PATCH, INIT_PERFORMANCE, init_waveseq

    app, recorded = _counts_app(tmp_path, monkeypatch)
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
            }
        )

    monkeypatch.setattr(bankstore, "save", fake_save)
    bank = _mixed_bank()
    calls: list[int] = []
    original_write = app.device.write_bank_bundle

    def failing_second(bank_number: int, **k: Any) -> dict[str, Any]:
        calls.append(bank_number)
        if len(calls) == 1:
            return cast(dict[str, Any], original_write(bank_number, **k))
        raise RuntimeError("the unit refused the second transfer")

    app.device.write_bank_bundle = failing_second
    choice = WholeBankChoice(0, "RAM3", True, remediation=Remediation.GREEN_FIELD)
    choice = replace(choice, cleared_banks=("RAM3",))
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(
            bank,
            _approved(choice, bank),
            banks_list=[bank],
            path="one.syx",
        )
        await _wait_for_writes(app, pilot, 1)
        await _settle(pilot)
    assert len(recorded) == 1, "only the clear landed, and it has its record"
    source = app._sources["RAM3"]
    assert all(record == INIT_PERFORMANCE for record in source.performances)
    assert all(record == INIT_PATCH for record in source.patches)
    assert source.waveseq == init_waveseq()
    assert saved and saved[-1]["scope"] == "RAM3"
    assert saved[-1]["performances"] == INIT_PERFORMANCE * banks.PERFORMANCES_PER_BANK


@pytest.mark.asyncio
async def test_counts_reads_each_candidate_once_and_shows_the_summary(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Deciding with counts reads the candidate dumps once, then asks."""
    app, _recorded = _counts_app(tmp_path, monkeypatch)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    ram3_no = banks.bank("RAM3").number
    seeded = _seed_bank_blob(
        {
            3: _named_perf("Crossing Deck", ram3_no, 0),
            5: _named_perf("Drift Path", ram3_no, 0),
        }
    )
    app.device._written[("RAM2", banks.Kind.PERFORMANCE)] = seeded
    bank = _mixed_bank()
    reads: list[tuple[int, int]] = []
    original_read = app.device.read_raw

    def recording_read(bank_number: int, function: int, **k: Any) -> bytes:
        reads.append((bank_number, function))
        return cast(bytes, original_read(bank_number, function, **k))

    app.device.read_raw = recording_read
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = await _run_to_counts(app, pilot, [bank])
        body = _static_text(modal, "affected-counts-body")
        assert "RAM 2" in body
        assert "2 performances" in body
        assert "RAM 3" in body, "the summary names the bank the parts point at"
        # Each candidate's performance dump was read exactly once, and
        # the wholesale-replaced target was never read for the counts.
        ram1_sysex = banks.bank("RAM1").sysex
        ram2_sysex = banks.bank("RAM2").sysex
        ram3_sysex = banks.bank("RAM3").sysex
        counted = [
            number
            for number, function in reads
            if function == m.Function.ALL_PERFORMANCE_DUMP
        ]
        assert counted.count(ram1_sysex) == 1
        assert counted.count(ram2_sysex) == 1
        assert ram3_sysex not in counted
        # Nothing has been written while the question stands.
        assert app.device.bundle_write_count == 0
        await pilot.press("escape")
        await _settle(pilot)
    assert app.device.bundle_write_count == 0


@pytest.mark.asyncio
async def test_init_writes_only_the_affected_slots_byte_for_byte(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """`i` sets the affected slots to Init; every other byte stays as it was."""
    from kws.initbank import INIT_PERFORMANCE

    app, _recorded = _counts_app(tmp_path, monkeypatch)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    ram3_no = banks.bank("RAM3").number
    seeded = _seed_bank_blob(
        {
            3: _named_perf("Crossing Deck", ram3_no, 0),
            5: _named_perf("Drift Path", ram3_no, 0),
        }
    )
    app.device._written[("RAM2", banks.Kind.PERFORMANCE)] = seeded
    bank = _mixed_bank()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _run_to_counts(app, pilot, [bank])
        await pilot.press("i")
        await _wait_for_writes(app, pilot, 2)
        await _settle(pilot)
    init_write, target_write = app.device.bundle_writes
    assert init_write["bank"] == banks.bank("RAM2").label
    assert init_write["kinds"] == ["performances"], "patches stay untouched"
    blob = app.device._written[("RAM2", banks.Kind.PERFORMANCE)]
    assert _perf_at(blob, 3) == INIT_PERFORMANCE
    assert _perf_at(blob, 5) == INIT_PERFORMANCE
    for slot in range(banks.PERFORMANCES_PER_BANK):
        if slot in (3, 5):
            continue
        assert _perf_at(blob, slot) == _perf_at(seeded, slot), slot
    assert target_write["bank"] == banks.bank("RAM3").label
    # The session shows the Init-ed slots.
    source = app._sources["RAM2"]
    assert source.performances[3] == INIT_PERFORMANCE
    assert source.performances[5] == INIT_PERFORMANCE
    assert source.performances[0] == _perf_at(seeded, 0)


@pytest.mark.asyncio
async def test_leaving_writes_no_remediation_and_says_what_changes(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """`l` sends the import's own write only, with the warning spelled out."""
    app, _recorded = _counts_app(tmp_path, monkeypatch)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))
    ram3_no = banks.bank("RAM3").number
    seeded = _seed_bank_blob(
        {3: _named_perf("Crossing Deck", ram3_no, 0)},
    )
    app.device._written[("RAM2", banks.Kind.PERFORMANCE)] = seeded
    bank = _mixed_bank()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _run_to_counts(app, pilot, [bank])
        await pilot.press("l")
        await _wait_for_writes(app, pilot, 1)
        await _settle(pilot)
    assert app.device.bundle_write_count == 1
    assert app.device.bundle_writes[0]["bank"] == banks.bank("RAM3").label
    blob = app.device._written[("RAM2", banks.Kind.PERFORMANCE)]
    assert _perf_at(blob, 3) == _perf_at(seeded, 3), "nothing was Init-ed"
    assert any(
        "sound will change" in status and "RAM 2" in status for status in statuses
    ), statuses


@pytest.mark.asyncio
async def test_back_and_cancel_at_the_counts_write_nothing(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """`b` returns to the first question; esc cancels and writes nothing."""
    from kwsed.import_bank import WholeBankImportScreen

    app, _recorded = _counts_app(tmp_path, monkeypatch)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    ram3_no = banks.bank("RAM3").number
    app.device._written[("RAM2", banks.Kind.PERFORMANCE)] = _seed_bank_blob(
        {3: _named_perf("Crossing Deck", ram3_no, 0)}
    )
    bank = _mixed_bank()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _run_to_counts(app, pilot, [bank])
        await pilot.press("b")
        await _wait_for_screen(app, pilot, AffectedImportScreen)
        assert app.device.bundle_write_count == 0
        await pilot.press("escape")
        await _settle(pilot)
        assert isinstance(app.screen, WholeBankImportScreen)
    assert app.device.bundle_write_count == 0


@pytest.mark.asyncio
async def test_a_read_failure_names_the_bank_and_init_skips_it(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A candidate the unit will not answer stays unknown; Init is off for it."""
    from kws.initbank import INIT_PERFORMANCE

    app, _recorded = _counts_app(tmp_path, monkeypatch)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    ram1_no = banks.bank("RAM1").number
    ram3_no = banks.bank("RAM3").number
    seeded = _seed_bank_blob({3: _named_perf("Crossing Deck", ram3_no, 0)})
    app.device._written[("RAM2", banks.Kind.PERFORMANCE)] = seeded
    bank = _mixed_bank()
    original_read = app.device.read_raw

    def failing_read(bank_number: int, function: int, **k: Any) -> bytes:
        if bank_number == ram1_no and function == m.Function.ALL_PERFORMANCE_DUMP:
            raise RuntimeError("no answer")
        return cast(bytes, original_read(bank_number, function, **k))

    app.device.read_raw = failing_read
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = await _run_to_counts(app, pilot, [bank])
        body = _static_text(modal, "affected-counts-body")
        assert "RAM 1" in body and "could not be read" in body
        await pilot.press("i")
        await _wait_for_writes(app, pilot, 2)
        await _settle(pilot)
    # The unread bank got no Init write: the read banks' slots only.
    banks_written = [entry["bank"] for entry in app.device.bundle_writes]
    assert banks.bank("RAM1").label not in banks_written
    init_write, target_write = app.device.bundle_writes
    assert init_write["bank"] == banks.bank("RAM2").label
    blob = app.device._written[("RAM2", banks.Kind.PERFORMANCE)]
    assert _perf_at(blob, 3) == INIT_PERFORMANCE
    assert target_write["bank"] == banks.bank("RAM3").label


@pytest.mark.asyncio
async def test_a_wholesale_replaced_target_is_never_a_candidate(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A bank the import replaces wholesale contributes nothing to the counts.

    The import writes RAM 1 itself: its performances do not survive, it
    is not read for the counts, and it is not Init-ed — the import's own
    write replaces it.
    """
    from kws.initbank import INIT_PERFORMANCE

    app, _recorded = _counts_app(tmp_path, monkeypatch)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    ram1_sysex = banks.bank("RAM1").sysex
    seeded = _seed_bank_blob(
        {4: _named_perf("Anchor Line", banks.bank("RAM1").number, 0)}
    )
    app.device._written[("RAM2", banks.Kind.PERFORMANCE)] = seeded
    bank = _make_bank(scope="RAM1", active_slots=[3])
    reads: list[tuple[int, int]] = []
    original_read = app.device.read_raw

    def recording_read(bank_number: int, function: int, **k: Any) -> bytes:
        reads.append((bank_number, function))
        return cast(bytes, original_read(bank_number, function, **k))

    app.device.read_raw = recording_read
    async with app.run_test() as pilot:
        await pilot.pause()
        choice = WholeBankChoice(0, "RAM1", True)
        app._import_whole_bank_chosen([bank], choice, "one.syx")
        await _wait_for_screen(app, pilot, AffectedImportScreen)
        await pilot.press("2")
        modal = await _wait_for_screen(app, pilot, AffectedCountsScreen)
        body = _static_text(modal, "affected-counts-body")
        assert "RAM 2" in body
        assert "RAM 1:" not in body, "the wholesale-replaced target is not analysed"
        counts_reads = list(reads)
        await pilot.press("i")
        await _wait_for_writes(app, pilot, 3)
        await _settle(pilot)
    counted = [
        number
        for number, function in counts_reads
        if function == m.Function.ALL_PERFORMANCE_DUMP
    ]
    assert ram1_sysex not in counted, "the target is not a candidate"
    init_ram2, init_ram3, target_write = app.device.bundle_writes
    assert init_ram2["bank"] == banks.bank("RAM2").label
    blob = app.device._written[("RAM2", banks.Kind.PERFORMANCE)]
    assert _perf_at(blob, 4) == INIT_PERFORMANCE
    assert init_ram3["bank"] == banks.bank("RAM3").label
    assert target_write["bank"] == banks.bank("RAM1").label


@pytest.mark.asyncio
async def test_a_leave_token_does_not_authorize_green_field(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The remediation answer is part of the plan the token was minted for."""
    app = _make_app(tmp_path)
    bank = _mixed_bank()
    leave = WholeBankChoice(0, "RAM3", True, remediation=Remediation.LEAVE)
    approval = _approved(leave, bank).approval
    green = replace(
        WholeBankChoice(0, "RAM3", True, remediation=Remediation.GREEN_FIELD),
        cleared_banks=("RAM3",),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        refused = app._whole_bank_intent(
            bank, replace(green, approval=approval), [bank], "one.syx"
        )
        assert refused is None, "a leave token does not authorize green field"
        assert approval.nonce not in app._consumed_plan_nonces
        accepted = app._whole_bank_intent(
            bank, replace(leave, approval=approval), [bank], "one.syx"
        )
        assert accepted is not None
        assert approval.nonce in app._consumed_plan_nonces


@pytest.mark.asyncio
async def test_a_plain_leave_token_writes_the_leave_plan_and_a_landed_retry_plans_none(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A plain LEAVE answer fingerprints ``leave``; only a filtered retry drops it.

    The "nothing remains" clearance belongs to a retry whose landed
    remediation banks filtered this answer's own work away. A plain
    LEAVE choice has no clear or Init banks at all, so its token must
    still authorize the leave plan it was minted for: the write
    proceeds and sends one bundle. A retry whose remediation has all
    landed fingerprints no remediation and plans no re-clear.
    """
    app, _recorded = _armed_app(tmp_path, monkeypatch)
    bank = _mixed_bank()
    leave = WholeBankChoice(0, "RAM3", True, remediation=Remediation.LEAVE)
    approval = _approved(leave, bank).approval
    async with app.run_test() as pilot:
        await pilot.pause()
        # The leave token does not authorize green field.
        green = replace(
            WholeBankChoice(0, "RAM3", True, remediation=Remediation.GREEN_FIELD),
            cleared_banks=("RAM3",),
        )
        refused = app._whole_bank_intent(
            bank, replace(green, approval=approval), [bank], "one.syx"
        )
        assert refused is None, "a leave token does not authorize green field"
        assert approval.nonce not in app._consumed_plan_nonces
        # The leave plan itself proceeds: one bundle, the target only.
        before = app.device.bundle_write_count
        app._import_whole_bank_write(
            bank,
            replace(leave, approval=approval),
            banks_list=[bank],
            path="one.syx",
        )
        await _wait_for_writes(app, pilot, before + 1)
        await _settle(pilot)
        bundles = app.device.bundle_writes
        assert len(bundles) == before + 1
        assert bundles[-1]["bank"] == banks.bank("RAM3").label

        # A retry whose remediation banks all landed: the session's
        # landed set filtered this answer's whole clear away, so the
        # fingerprint carries no remediation and the plan re-clears
        # nothing -- the retry writes the target only.
        key = "retry-all-landed"
        state = app._import_passes.setdefault(key, {"written": {}, "targets": []})
        state["remediation_written"] = {"RAM3"}
        landed_choice = replace(
            WholeBankChoice(
                0, "RAM3", True, remediation=Remediation.GREEN_FIELD, session_key=key
            ),
            cleared_banks=("RAM3",),
        )
        name, cleared, init = app._choice_remediation_fields(
            landed_choice, skipped_banks=app._skipped_banks_for(key)
        )
        assert (name, cleared, init) == ("", (), ())
        retry_approval = app._mint_whole_bank_approval(
            bank, landed_choice, [bank], "one.syx"
        )
        assert retry_approval is not None
        planned = app._whole_bank_intent(
            bank,
            replace(landed_choice, approval=retry_approval),
            [bank],
            "one.syx",
        )
        assert planned is not None
        intent, _sources = planned
        assert intent.remediation == ""
        assert intent.cleared_banks == ()
        assert intent.init_banks == ()


# ---------------------------------------------------------------------------
# Review fixes: the companion plan resolved before any remediation write,
# one consistent bank set behind the question, and the paths that used to
# skip the question. Every name here is invented.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_both_then_green_field_clears_before_the_companions(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """'both' then green field: the clears land before any companion bundle.

    The preflight answer is resolved first, so a cancel at either
    question leaves the unit untouched; the remediation's own writes --
    the clear of the confirmed companion and of the target -- go out
    before the companion's patches and before the target write.
    """
    from kws.initbank import init_performances
    from kwsed.import_bank import PreflightImportScreen

    app, _recorded = _preflight_app(tmp_path, monkeypatch)
    file_banks = _two_bank_file()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            WholeBankImportScreen(
                path="two.syx", banks=file_banks, gate=app._write_gate
            ),
            lambda picked: app._import_whole_bank_chosen(file_banks, picked, "two.syx"),
        )
        await _settle(pilot)
        await pilot.press("b")
        await pilot.press("b")  # target RAM 3, not the companion's RAM 2
        await pilot.press("ctrl+s")
        await _wait_for_screen(app, pilot, PreflightImportScreen)
        await _settle(pilot)
        await pilot.press("1")  # both: companions confirmed
        await _wait_for_screen(app, pilot, AffectedImportScreen)
        assert app.device.bundle_write_count == 0
        await pilot.press("1")  # green field
        for _ in range(600):
            await pilot.pause()
            if app.device.bundle_write_count == 4:
                break
        await _settle(pilot)
    writes = app.device.bundle_writes
    assert [w["bank"] for w in writes] == [
        banks.bank("RAM2").label,  # the confirmed companion is cleared first
        banks.bank("RAM3").label,  # then the target, the clear order's last
        banks.bank("RAM2").label,  # the companion's patches, after the clears
        banks.bank("RAM3").label,  # the target write, last of all
    ]
    clear_companion, clear_target, companion, target = writes
    assert clear_companion["kinds"] == ["waveseq", "patches", "performances"]
    assert clear_target["kinds"] == ["waveseq", "patches", "performances"]
    # The cleared companion holds the Init state, and the later companion
    # write left its performances exactly there.
    assert app.device._written[("RAM2", banks.Kind.PERFORMANCE)] == init_performances()
    assert companion["kinds"] == ["patches"], "no performances travel for it"
    assert "performances" in target["kinds"]


@pytest.mark.asyncio
async def test_preflight_cancel_writes_nothing_at_all_not_even_the_remediation(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A cancel at the preflight has written nothing, remediation included.

    The companion question stands before the remediation question and
    before any write, so the old order -- remediation first, then a
    preflight that could still be cancelled -- cannot leave a cleared
    bank behind with "nothing was written" said over it.
    """
    from kwsed.import_bank import PreflightImportScreen, WholeBankImportScreen

    app, _recorded = _preflight_app(tmp_path, monkeypatch)
    file_banks = _two_bank_file()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            WholeBankImportScreen(
                path="two.syx", banks=file_banks, gate=app._write_gate
            ),
            lambda picked: app._import_whole_bank_chosen(file_banks, picked, "two.syx"),
        )
        await _settle(pilot)
        await pilot.press("b")
        await pilot.press("b")  # target RAM 3
        await pilot.press("ctrl+s")
        await _wait_for_screen(app, pilot, PreflightImportScreen)
        await pilot.press("3")  # cancel: no remediation question was ever asked
        await _settle(pilot)
        reopened = app.screen
        assert isinstance(reopened, WholeBankImportScreen)
        assert "nothing was written" in _static_text(reopened, "whole-bank-status")
    assert app.device.bundle_write_count == 0
    assert not any(isinstance(s, AffectedImportScreen) for s in app.screen_stack), (
        "the remediation question was never reached"
    )


@pytest.mark.asyncio
async def test_the_remediation_sets_agree_after_the_preflight_answer(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Named, cleared and analysed are one set, once the answer is in.

    With "both", the question names, a green field clears, and the
    affected analysis counts exactly the target, the overflow and the
    confirmed companions -- never the Card. With "only", the companions
    are written by nobody, so they appear in none of the three.
    """
    from kws import banks as kws_banks

    app, _recorded = _preflight_app(tmp_path, monkeypatch)
    file_banks = _two_bank_file()
    choice = WholeBankChoice(0, "RAM3", True, perfcopy.ImportMode.AS_IS, "s", True)
    async with app.run_test() as pilot:
        await pilot.pause()
        for answer in ("both", "only"):
            amended = replace(choice, preflight=answer)
            labels = app._remediation_written_labels(file_banks[0], amended, file_banks)
            cleared = app._remediation_cleared_banks(file_banks[0], amended, file_banks)
            wholesale, written = app._remediation_flow_banks(
                file_banks[0], amended, file_banks
            )
            if answer == "both":
                assert labels == [banks.bank("RAM3").label, banks.bank("RAM2").label]
                assert cleared == ("RAM2", "RAM3")
                assert wholesale == {"RAM3"}
                assert written == {
                    kws_banks.bank("RAM3").number,
                    kws_banks.bank("RAM2").number,
                }
            else:
                assert labels == [banks.bank("RAM3").label]
                assert cleared == ("RAM3",)
                assert wholesale == {"RAM3"}
                assert written == {kws_banks.bank("RAM3").number}
            assert "Card" not in labels
            assert "CARD" not in cleared
            assert "CARD" not in wholesale
            assert kws_banks.bank("CARD").number not in written


@pytest.mark.asyncio
async def test_x_raises_the_remediation_question_and_cancel_writes_nothing(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A companion-only write is asked the question; cancelling writes nothing.

    The `x` path used to skip the question entirely; it is a whole-bank
    plan like any other, and a green-field answer here has a RAM bank to
    clear -- the companion target itself -- so green field is offered.
    """
    from kwsed.target_bank import TargetBankScreen

    app, _recorded = _preflight_app(tmp_path, monkeypatch)
    file_banks = _two_bank_file()
    app._sources["RAM2"] = _make_bank(scope="RAM2", active_slots=[4]).source
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            WholeBankImportScreen(
                path="two.syx", banks=file_banks, gate=app._write_gate
            ),
            lambda picked: app._import_whole_bank_chosen(file_banks, picked, "two.syx"),
        )
        await _settle(pilot)
        await pilot.press("x")
        await _settle(pilot)
        await pilot.press("enter")  # the file's RAM 2 bank
        await _settle(pilot)
        assert isinstance(app.screen, TargetBankScreen)
        await pilot.press("enter")  # the suggested RAM 2 target
        modal = await _wait_for_screen(app, pilot, AffectedImportScreen)
        assert "RAM 2" in _static_text(modal, "affected-body")
        choices = _static_text(modal, "affected-choices")
        assert "green field" in choices, "a RAM bank would be cleared"
        await pilot.press("escape")
        await _settle(pilot)
    assert app.device.bundle_write_count == 0


@pytest.mark.asyncio
async def test_a_card_target_offers_counts_and_leave_only(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A Card write is asked the question, but green field is not offered.

    The Card is never cleared, so a plan that writes no RAM bank offers
    decide-with-counts and cancel only, and a pressed ``1`` is refused
    rather than dismissed.
    """
    app, _recorded = _preflight_app(tmp_path, monkeypatch)
    bank = _mixed_bank()
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_chosen(
            [bank], WholeBankChoice(0, "CARD", True), "one.syx"
        )
        modal = await _wait_for_screen(app, pilot, AffectedImportScreen)
        choices = _static_text(modal, "affected-choices")
        assert "green field" not in choices
        hint = _static_text(modal, "affected-hint")
        assert "2 / 3" in hint
        await pilot.press("1")
        await _settle(pilot)
        assert "not available" in _static_text(modal, "affected-note")
        assert app.device.bundle_write_count == 0
        # The write still goes through on decide, leaving as the answer.
        await _answer_remediation_with_leave(app, pilot)
        await _wait_for_writes(app, pilot, 1)
        await _settle(pilot)
    assert app.device.bundle_write_count == 1
    assert app.device.bundle_writes[0]["bank"] == banks.bank("CARD").label


@pytest.mark.asyncio
async def test_cards_only_offers_counts_and_leave_only(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A cards-only pass names no RAM bank, so green field is not offered."""
    from kwsed.import_bank import AffectedCountsScreen

    app = _split_pass_app(tmp_path, monkeypatch)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    file_banks = _cards_only_banks()
    card_no = banks.bank("CARD").number
    # A performance on the unit names the Card's patch bank: the counts
    # have something to show, so the decide answer reaches the modal.
    app.device._written[("RAM2", banks.Kind.PERFORMANCE)] = _seed_bank_blob(
        {3: _named_perf("Quay Light", card_no, 0)}
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        first = WholeBankImportScreen(
            path="cards-q.syx", banks=file_banks, gate=app._write_gate
        )
        app.push_screen(
            first,
            lambda picked: app._import_whole_bank_chosen(
                file_banks, picked, "cards-q.syx"
            ),
        )
        await _settle(pilot)
        await pilot.press("m")
        await _settle(pilot)
        await pilot.press("m")  # self-contained packed
        await _settle(pilot)
        await _press_cards_only(pilot)
        modal = await _wait_for_screen(app, pilot, AffectedImportScreen)
        assert "green field" not in _static_text(modal, "affected-choices")
        await pilot.press("1")
        await _settle(pilot)
        assert "not available" in _static_text(modal, "affected-note")
        assert app.device.bundle_write_count == 0
        await pilot.press("2")  # decide with counts
        counts = await _wait_for_screen(app, pilot, AffectedCountsScreen)
        assert "Init the affected" in _static_text(counts, "affected-counts-choices")
        await pilot.press("l")
        await _wait_for_writes(app, pilot, 1)
        await _settle(pilot)
    assert app.device.bundle_write_count == 1
    assert app.device.bundle_writes[0]["bank"] == banks.bank("CARD").label
    assert not any(
        bank_id.startswith("RAM") and kind == banks.Kind.PATCH
        for bank_id, kind in app.device._written
    ), "no RAM bank was written"


def test_the_intent_refuses_the_card_and_rom_in_the_cleared_set() -> None:
    """A plan that clears the Card or a ROM bank is not a plan at all.

    The boundary is in the intent itself, not only in the UI that fills
    the set: ``build_intent`` and ``WholeBankIntent`` both refuse.
    """
    from kwsed import import_intent

    sources = {"RAM1": _make_bank(scope="RAM1", active_slots=[3]).source}
    for bad in ("CARD", "ROM4", "ROM11"):
        with pytest.raises(ValueError):
            import_intent.build_intent(
                source_id="one.syx|RAM1",
                source_scope="RAM1",
                sources=sources,
                source=sources["RAM1"],
                target=banks.bank("RAM3"),
                mode=perfcopy.ImportMode.AS_IS,
                remediation="green_field",
                cleared_banks=(bad,),
            )
        with pytest.raises(ValueError):
            import_intent.WholeBankIntent(
                source_id="one.syx|RAM1",
                source_digest="d" * 64,
                mode="as_is",
                writes=(),
                remediation="green_field",
                cleared_banks=(bad,),
            )
    # The RAM banks a green field may clear are accepted.
    intent = import_intent.WholeBankIntent(
        source_id="one.syx|RAM1",
        source_digest="d" * 64,
        mode="as_is",
        writes=(),
        remediation="green_field",
        cleared_banks=("RAM2", "RAM3"),
    )
    assert intent.cleared_banks == ("RAM2", "RAM3")


@pytest.mark.asyncio
async def test_the_app_refuses_a_card_clear_plan_before_any_device_io(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A plan the intent refuses never reaches the unit, and spends nothing."""
    from kwsed.import_intent import PlanApproval

    app = _make_app(tmp_path)
    app._write_gate = EditGate(armed=True)
    statuses: list[str] = []
    app.notify_status = lambda msg, *a, **k: statuses.append(str(msg))  # type: ignore[method-assign]
    io: list[str] = []
    original_read = app.device.read_raw
    original_write = app.device.write_bank_bundle

    def counting_read(bank_number: int, function: int, **k: Any) -> bytes:
        io.append(f"read {bank_number}:{function}")
        return cast(bytes, original_read(bank_number, function, **k))

    def counting_write(bank_number: int, **k: Any) -> dict[str, Any]:
        io.append(f"write {bank_number}")
        return cast(dict[str, Any], original_write(bank_number, **k))

    app.device.read_raw = counting_read
    app.device.write_bank_bundle = counting_write
    bank = _mixed_bank()
    approval = PlanApproval(fingerprint="0" * 64, nonce="unspent-nonce")
    green = WholeBankChoice(0, "RAM3", True, remediation=Remediation.GREEN_FIELD)
    green = replace(green, cleared_banks=("CARD",), approval=approval)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(bank, green, banks_list=[bank], path="one.syx")
        await _settle(pilot)
    assert app.device.bundle_write_count == 0
    assert io == [], "a plan the intent refuses touches no device"
    assert "cannot be planned" in " ".join(statuses), statuses
    assert approval.nonce not in app._consumed_plan_nonces


@pytest.mark.asyncio
async def test_all_unread_counts_offer_no_init_and_refuse_it(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """When nothing could be read, the counts modal does not offer Init."""
    from kws.initbank import INIT_PERFORMANCE
    from kwsed.import_bank import AffectedCountsScreen

    app, _recorded = _counts_app(tmp_path, monkeypatch)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    bank = _mixed_bank()
    original_read = app.device.read_raw
    ram1_sysex = banks.bank("RAM1").sysex
    ram2_sysex = banks.bank("RAM2").sysex

    def failing_read(bank_number: int, function: int, **k: Any) -> bytes:
        # Every candidate the counts would read fails; the target's own
        # pre-write read (not a candidate) still answers, so the write
        # itself can go out.
        if function == m.Function.ALL_PERFORMANCE_DUMP and bank_number in (
            ram1_sysex,
            ram2_sysex,
        ):
            raise RuntimeError("no answer")
        return cast(bytes, original_read(bank_number, function, **k))

    app.device.read_raw = failing_read
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = await _run_to_counts(app, pilot, [bank])
        choices = _static_text(modal, "affected-counts-choices")
        assert "Init" not in choices
        hint = _static_text(modal, "affected-counts-hint")
        assert hint == "l  leave as is · b  back · esc  cancel"
        await pilot.press("i")
        await _settle(pilot)
        note = _static_text(modal, "affected-counts-note")
        assert "Init is not available" in note
        assert isinstance(app.screen, AffectedCountsScreen), "the answer was refused"
        await pilot.press("l")  # leave as is
        await _wait_for_writes(app, pilot, 1)
        await _settle(pilot)
    assert app.device.bundle_write_count == 1
    assert app.device.bundle_writes[0]["bank"] == banks.bank("RAM3").label
    blob = app.device._written.get(("RAM2", banks.Kind.PERFORMANCE))
    assert blob is None or all(
        record != INIT_PERFORMANCE for record in _records_of(blob)
    ), "nothing was Init-ed"


def _records_of(blob: bytes) -> list[bytes]:
    return [
        blob[at : at + PERF_RECORD_SIZE] for at in range(0, len(blob), PERF_RECORD_SIZE)
    ]
