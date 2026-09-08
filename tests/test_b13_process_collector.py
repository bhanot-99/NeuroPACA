# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""B13-B2 · `ProcessCollector` — the per-app RAM/CPU/runtime census (D-19).

`collect()` is pure and synchronous; these drive it with a fake
`psutil.process_iter` so no test touches the real process table (rules.md §8).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import psutil
import pytest

from neuropaca.sensing.collectors import process as process_mod
from neuropaca.sensing.collectors.process import ProcessCollector


class _FakeMem:
    def __init__(self, rss: float) -> None:
        self.rss = rss


class _FakeProc:
    def __init__(self, *, name: str, rss_mb: float, cpu: float = 0.0, start: float | None = None):
        self._start = start if start is not None else time.time() - 100.0
        self.info: dict[str, Any] = {
            "name": name,
            "memory_info": _FakeMem(rss_mb * 1024 * 1024),
            "cpu_percent": cpu,
            "create_time": self._start,
        }

    def cpu_percent(self, _interval: Any = None) -> float:
        return float(self.info["cpu_percent"])


def _install(monkeypatch: pytest.MonkeyPatch, procs: list[Any]) -> None:
    def fake_iter(attrs: Any = None) -> list[Any]:
        return list(procs)

    monkeypatch.setattr(process_mod.psutil, "process_iter", fake_iter)


def test_groups_same_name_processes_and_sums_rss(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [_FakeProc(name="brave", rss_mb=120.0) for _ in range(15)])
    snap = ProcessCollector(min_rss_mb=200.0).collect()
    rows = snap.data["processes"]
    assert len(rows) == 1
    assert rows[0]["name"] == "brave"
    assert rows[0]["proc_count"] == 15
    assert rows[0]["rss_mb"] == pytest.approx(1800.0, rel=1e-3)


def test_threshold_is_on_the_group_total(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        [
            _FakeProc(name="big", rss_mb=250.0),
            *[_FakeProc(name="mid", rss_mb=40.0) for _ in range(5)],  # 200 total -> kept
            *[_FakeProc(name="small", rss_mb=30.0) for _ in range(5)],  # 150 total -> dropped
        ],
    )
    names = {r["name"] for r in ProcessCollector(min_rss_mb=200.0).collect().data["processes"]}
    assert names == {"big", "mid"}


def test_sorted_by_rss_descending(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        [
            _FakeProc(name="a", rss_mb=300.0),
            _FakeProc(name="b", rss_mb=900.0),
            _FakeProc(name="c", rss_mb=500.0),
        ],
    )
    rows = ProcessCollector(min_rss_mb=100.0).collect().data["processes"]
    assert [r["name"] for r in rows] == ["b", "c", "a"]


def test_running_seconds_uses_the_earliest_create_time(monkeypatch: pytest.MonkeyPatch) -> None:
    now = time.time()
    _install(
        monkeypatch,
        [
            _FakeProc(name="svc", rss_mb=150.0, start=now - 100.0),
            _FakeProc(name="svc", rss_mb=150.0, start=now - 40.0),
        ],
    )
    row = ProcessCollector(min_rss_mb=100.0).collect().data["processes"][0]
    assert row["running_seconds"] == pytest.approx(100.0, abs=2.0)


def test_exclude_names_are_filtered(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        [_FakeProc(name="neuropacad", rss_mb=1500.0), _FakeProc(name="brave", rss_mb=800.0)],
    )
    rows = (
        ProcessCollector(min_rss_mb=200.0, exclude_names=["neuropacad"]).collect().data["processes"]
    )
    assert [r["name"] for r in rows] == ["brave"]


def test_access_denied_on_one_process_does_not_abort_the_census(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Denied:
        @property
        def info(self) -> dict[str, Any]:
            raise psutil.AccessDenied()

        def cpu_percent(self, _i: Any = None) -> float:
            raise psutil.AccessDenied()

    _install(monkeypatch, [_Denied(), _FakeProc(name="brave", rss_mb=800.0)])
    rows = ProcessCollector(min_rss_mb=200.0).collect().data["processes"]
    assert [r["name"] for r in rows] == ["brave"]


def test_collect_returns_a_typed_process_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [_FakeProc(name="brave", rss_mb=800.0)])
    snap = ProcessCollector(min_rss_mb=200.0).collect()
    assert snap.collector_name == "process"
    assert isinstance(snap.data["processes"], list)
    assert snap.data["group_count"] == 1
    assert set(snap.data["processes"][0]) == {
        "name",
        "rss_mb",
        "cpu_percent",
        "proc_count",
        "running_seconds",
    }


def test_source_never_reads_cmdline_exe_environ_or_connections() -> None:
    """Privacy (rules.md §6): the census reads process *names* only. Scan the AST
    for identifiers / string constants — not the docstring, which names the
    forbidden fields to explain why they are absent."""
    import ast

    tree = ast.parse(Path(process_mod.__file__).read_text(encoding="utf-8"))
    forbidden = {"cmdline", "exe", "environ", "open_files", "connections", "cwd", "username"}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    consts = {
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    leaked = forbidden & (attrs | consts)
    assert not leaked, f"ProcessCollector must not read {leaked} (rules.md §6)"


def test_snapshot_stays_bounded_under_process_churn(monkeypatch: pytest.MonkeyPatch) -> None:
    # 2000 distinct short-lived names, each well under threshold -> nothing kept,
    # snapshot bounded by the filter regardless of table size.
    _install(monkeypatch, [_FakeProc(name=f"p{i}", rss_mb=5.0) for i in range(2000)])
    snap = ProcessCollector(min_rss_mb=200.0).collect()
    assert snap.data["processes"] == []


# gen-ref: a7ac755e
