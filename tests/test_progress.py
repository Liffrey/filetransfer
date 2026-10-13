import importlib
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from main_window import compute_task_progress


def test_compute_task_progress_basic():
    percent, label = compute_task_progress(3, 10, elapsed_seconds=10)
    assert percent == 30
    assert "3/10 dosya (30%)" in label
    assert "kaldı" in label


def test_compute_task_progress_zero_total():
    percent, label = compute_task_progress(0, 0)
    assert percent == 0
    assert label == "0%"


def test_import_engine_config_when_cwd_is_elsewhere(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(tmp_path)
    for name in list(sys.modules):
        if name == "engine.config" or name.startswith("engine."):
            sys.modules.pop(name, None)
    sys.path[:] = [p for p in sys.path if p not in ("", str(root))]

    module = importlib.import_module("engine.config")

    assert hasattr(module, "JobConfigStore")
    assert hasattr(module, "TransferJob")


def test_terminate_process_tree_uses_taskkill(monkeypatch):
    from robocopy_runner import _terminate_process_tree

    proc = SimpleNamespace(pid=4321)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    _terminate_process_tree(proc)

    assert any("taskkill" in str(cmd[0]).lower() for cmd, _ in calls)
    assert any("/t" in str(cmd).lower() for cmd, _ in calls)
