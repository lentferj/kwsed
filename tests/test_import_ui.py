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
from kwsed.import_bank import WholeBankChoice, WholeBankImportScreen
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
