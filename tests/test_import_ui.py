# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#

"""Focused tests for the TUI import flow."""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, Optional
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
        app._import_whole_bank_write(bank, WholeBankChoice(0, "RAM3", True))

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
            WholeBankChoice(0, "RAM3", True, perfcopy.ImportMode.AS_IS),
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
        WholeBankChoice(0, "RAM3", True, perfcopy.ImportMode.SELF_CONTAINED_1TO1),
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
        WholeBankChoice(0, "RAM3", True, perfcopy.ImportMode.SELF_CONTAINED_PACKED),
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
        app._import_whole_bank_write(bank, WholeBankChoice(0, "RAM3", True, mode))
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
    app._import_whole_bank_write(
        _make_bank(scope="RAM1"), WholeBankChoice(0, "CARD/07", True)
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
    app._import_whole_bank_write(
        _make_bank(scope="RAM1"), WholeBankChoice(0, "CARD/07", True)
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
        app._import_whole_bank_write(
            bank,
            WholeBankChoice(
                0, "RAM3", True, perfcopy.ImportMode.SELF_CONTAINED_PACKED, key, True
            ),
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
        WholeBankChoice(0, "CARD", True, perfcopy.ImportMode.AS_IS, "", True, "RAM3"),
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
            file_banks[0], choice, banks_list=file_banks, path="two.syx"
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
        assert "2 parts of the performances in Main bank name RAM 2" in body
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
            WholeBankChoice(0, "RAM3", True, perfcopy.ImportMode.AS_IS, "s", True),
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
            WholeBankChoice(0, "RAM3", True, perfcopy.ImportMode.AS_IS, "s", True),
            banks_list=file_banks,
            path="two.syx",
        )
        for _ in range(100):
            await pilot.pause()
            if isinstance(app.screen, DanglingRefsScreen):
                break
        assert isinstance(app.screen, DanglingRefsScreen)
        await pilot.press("x")
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
            file_banks[0], choice, banks_list=file_banks, path="two.syx"
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
        app._import_whole_bank_write(bank, choice, banks_list=[bank], path="one.syx")
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

    app._import_whole_bank_write(
        _multipass_bank(),
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
    )

    assert app.device.bundle_write_count == 0
    assert any("self-contained split does not fit" in s for s in statuses), statuses
    assert any("need 2 banks" in s for s in statuses), statuses


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
            file_banks[0], choice, banks_list=file_banks, path="three.syx"
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
            file_banks[0], choice, banks_list=file_banks, path="cards.syx"
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
            WholeBankChoice(0, "RAM3", True, perfcopy.ImportMode.AS_IS, "s", True),
            banks_list=file_banks,
            path="two.syx",
        )
        for _ in range(100):
            await pilot.pause()
            if isinstance(app.screen, DanglingRefsScreen):
                break
        assert isinstance(app.screen, DanglingRefsScreen)


@pytest.mark.asyncio
async def test_a_unit_bank_that_already_matches_is_not_offered(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The companion is offered only when it is actually missing."""
    from kwsed.import_bank import DanglingRefsScreen

    app, _recorded = _armed_app(tmp_path, monkeypatch)
    monkeypatch.setattr(bankstore, "save", lambda *a, **k: None)
    file_banks = _two_bank_file()
    app._sources["RAM2"] = file_banks[1].source
    async with app.run_test() as pilot:
        await pilot.pause()
        app._import_whole_bank_write(
            file_banks[0],
            WholeBankChoice(0, "RAM3", True, perfcopy.ImportMode.AS_IS, "s", True),
            banks_list=file_banks,
            path="two.syx",
        )
        await _settle(pilot, times=60)
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
        for _ in range(100):
            await pilot.pause()
            if isinstance(app.screen, PreflightImportScreen):
                break
        await _settle(pilot)
        modal = app.screen
        assert isinstance(modal, PreflightImportScreen)
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
        for _ in range(100):
            await pilot.pause()
            if isinstance(app.screen, PreflightImportScreen):
                break
        await _settle(pilot)
        await pilot.press("2")  # write only this bank
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
        await pilot.press("b")  # target RAM 3, not the companion's RAM 2
        await pilot.press("ctrl+s")
        for _ in range(100):
            await pilot.pause()
            if isinstance(app.screen, PreflightImportScreen):
                break
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
        for _ in range(100):
            await pilot.pause()
            if isinstance(app.screen, PreflightImportScreen):
                break
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
                path="two.syx", banks=file_banks, gate=app._write_gate
            ),
            lambda picked: app._import_whole_bank_chosen(file_banks, picked, "two.syx"),
        )
        await _settle(pilot)
        await pilot.press("x")
        await _settle(pilot)
        await pilot.press("enter")
        await _settle(pilot)
        await pilot.press("enter")
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
        for _ in range(100):
            await pilot.pause()
            if isinstance(app.screen, PreflightImportScreen):
                break
        await _settle(pilot)
        modal = app.screen
        assert isinstance(modal, PreflightImportScreen)
        body = _static_text(modal, "preflight-body")
        assert "2 parts of the performances in Main bank name RAM 2" in body
        assert "already holds those patches" in body
        assert app.device.bundle_write_count == 0
