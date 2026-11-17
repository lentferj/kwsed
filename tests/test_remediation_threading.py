# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors

"""Remediation completion must run on the main thread.

A green-field remediation's fenced writes run in a device worker; every
completion, continuation, status and bookkeeping after them must run on
the main thread, even when the completion is invoked from a worker
thread. A completion that runs on the worker raises on the DOM and
strands the companion and target writes.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from kwsed.app import BankWriteOutcome, KwsedApp, MultiBankOutcome
from kwsed.demo import DemoDevice
from kwsed.import_bank import Remediation


@dataclass
class _Choice:
    """Minimal whole-bank choice carrying the fields the completion reads."""

    remediation: Any = Remediation.GREEN_FIELD
    cleared_banks: tuple[str, ...] = ("RAM1",)
    init_banks: tuple[tuple[str, tuple[int, ...]], ...] = ()
    session_key: str = "session-1"
    preflight: str = ""
    remediation_note: str = "cleared first"
    mode: str = "as_is"
    approval: Any = None


def _outcome(bank_id: str = "RAM1", kinds: Any = None) -> MultiBankOutcome:
    bank = SimpleNamespace(id=bank_id, label=bank_id)
    if kinds is None:
        kinds = ["performances", "patches", "waveseq"]
    completed = [
        BankWriteOutcome(
            bank=bank,
            result={"verified": True, "kinds": kinds},
            verified=True,
        )
    ]
    return MultiBankOutcome(completed=completed)


def _run_off_main(
    app: KwsedApp, fn: Any, *args: Any, **kwargs: Any
) -> threading.Thread:
    errors: list[BaseException] = []

    def target() -> None:
        try:
            fn(*args, **kwargs)
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    assert not errors, errors
    return thread


def test_remediation_done_dispatches_to_main_thread(monkeypatch: Any) -> None:
    """A completion invoked off the main thread runs its bookkeeping on it."""
    app = KwsedApp(DemoDevice())
    main = threading.main_thread()
    monkeypatch.setattr(type(app), "is_running", property(lambda self: True))

    dispatched: list[threading.Thread] = []
    notify_threads: list[threading.Thread] = []
    continued: list[threading.Thread] = []

    def call_from_thread(fn: Any, *args: Any, **kwargs: Any) -> None:
        dispatched.append(threading.current_thread())
        real_current = threading.current_thread
        threading.current_thread = lambda: main
        try:
            fn(*args, **kwargs)
        finally:
            threading.current_thread = real_current

    app.call_from_thread = call_from_thread  # type: ignore[assignment]
    app.notify_status = lambda *a, **k: notify_threads.append(  # type: ignore[method-assign]
        threading.current_thread()
    )
    app._mint_whole_bank_approval = lambda *a, **k: SimpleNamespace()  # type: ignore[assignment]
    app._import_whole_bank_write = (  # type: ignore[method-assign]
        lambda *a, **k: continued.append(threading.current_thread())
    )

    choice = _Choice()
    _run_off_main(
        app,
        app._remediation_write_done,
        (_outcome(), {"RAM1": object()}),
        None,
        bank=SimpleNamespace(label="RAM1", scope="RAM1"),
        choice=choice,
        banks_list=[object()],
        path="x.syx",
        sources={},
    )

    assert dispatched and dispatched[0] is not main
    assert notify_threads == [main]
    assert continued == [main]


def test_remediation_done_unchanged_first_bank_still_continues_on_main(
    monkeypatch: Any,
) -> None:
    """A verified no-op first remediation write still continues on the main thread."""
    app = KwsedApp(DemoDevice())
    main = threading.main_thread()
    monkeypatch.setattr(type(app), "is_running", property(lambda self: True))

    notify_threads: list[threading.Thread] = []
    continued: list[threading.Thread] = []

    def call_from_thread(fn: Any, *args: Any, **kwargs: Any) -> None:
        real_current = threading.current_thread
        threading.current_thread = lambda: main
        try:
            fn(*args, **kwargs)
        finally:
            threading.current_thread = real_current

    app.call_from_thread = call_from_thread  # type: ignore[assignment]
    app.notify_status = lambda *a, **k: notify_threads.append(  # type: ignore[method-assign]
        threading.current_thread()
    )
    app._mint_whole_bank_approval = lambda *a, **k: SimpleNamespace()  # type: ignore[assignment]
    app._import_whole_bank_write = (  # type: ignore[method-assign]
        lambda *a, **k: continued.append(threading.current_thread())
    )

    # First (and only) remediation bank already matched: kinds empty/unchanged.
    outcome = _outcome(kinds=[])
    choice = _Choice()
    _run_off_main(
        app,
        app._remediation_write_done,
        (outcome, {"RAM1": object()}),
        None,
        bank=SimpleNamespace(label="RAM1", scope="RAM1"),
        choice=choice,
        banks_list=[object()],
        path="x.syx",
        sources={},
    )

    assert outcome.ok
    assert notify_threads == [main]
    assert continued == [main], "continuation to companion/target must follow"
