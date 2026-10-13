import importlib
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication

from checkpoint import CheckpointJournal, checkpoint_signature
from config import TransferJob, sort_jobs_for_queue
from main_window import MainWindow, compute_task_progress
from robocopy_runner import _build_robocopy_args
import transfer as transfer_module
from transfer import _deletion_guard, _preview_receipt_matches, build_transfer_plan, run_transfer


def test_compute_task_progress_basic():
    percent, label = compute_task_progress(3, 10, elapsed_seconds=10)
    assert percent == 30
    assert "3/10 dosya (30%)" in label
    assert "kaldı" in label


def test_compute_task_progress_zero_total():
    percent, label = compute_task_progress(0, 0)
    assert percent == 0
    assert label == "0%"


def test_transfer_job_defaults_include_operational_features():
    job = TransferJob(name="demo")
    assert job.dry_run is False
    assert job.resume_from_checkpoint is False
    assert job.priority == 0
    assert job.delete_policy == "verified_only"
    assert job.require_preview_for_delete is True


def test_deletion_guard_fails_closed():
    base = TransferJob(name="demo", delete_after_transfer=True)
    assert _deletion_guard(base, preview_matches=True)[0]
    assert not _deletion_guard(base, preview_matches=False)[0]
    assert not _deletion_guard(TransferJob(
        name="demo", delete_after_transfer=True, delete_policy="never"), True)[0]
    assert not _deletion_guard(TransferJob(
        name="demo", delete_after_transfer=True, delete_policy="always"), True)[0]
    assert not _deletion_guard(TransferJob(
        name="demo", delete_after_transfer=True, verification_mode="None"), True)[0]
    assert not _deletion_guard(TransferJob(
        name="demo", delete_after_transfer=True, verification_mode="unknown"), True)[0]


def test_expired_preview_receipt_cannot_authorize_deletion(tmp_path):
    receipt = tmp_path / "preview.json"
    receipt.write_text('{"signature":"same-plan","created_at":0}', encoding="utf-8")

    assert not _preview_receipt_matches(receipt, "same-plan")


def test_sort_jobs_for_queue_prioritizes_highest_priority():
    jobs = [
        TransferJob(name="low", priority=1),
        TransferJob(name="high", priority=5),
        TransferJob(name="mid", priority=3),
    ]
    ordered = sort_jobs_for_queue(jobs)
    assert [job.name for job in ordered] == ["high", "mid", "low"]


def test_priority_queue_dispatches_highest_priority_first(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(str(tmp_path / "jobs.json"), str(tmp_path / "creds"), str(tmp_path / "app.exe"))
    jobs = [
        TransferJob(name="low", priority=1),
        TransferJob(name="high", priority=5),
        TransferJob(name="middle", priority=3),
    ]
    assert window.config_store.save(jobs)
    window.pending_jobs = {"low": 1, "high": 2, "middle": 3}
    window.pending_modes = {name: "normal" for name in window.pending_jobs}
    started = []
    monkeypatch.setattr(window, "_start_worker", lambda job, mode: started.append(job.name))

    window._dispatch_queue()

    assert started == ["high", "middle"]
    assert list(window.pending_jobs) == ["low"]
    window.close()
    del app


def test_robocopy_resume_uses_restartable_mode_only_when_requested():
    regular_args = _build_robocopy_args("src", "dst", "*.*", 30, 3, 8)
    resume_args = _build_robocopy_args("src", "dst", "*.*", 30, 3, 8, restartable=True)

    assert "/Z" not in regular_args
    assert "/Z" in resume_args


def test_build_transfer_plan_handles_preview_and_resume_skip():
    class FakeFile:
        def __init__(self, rel_path, size=1):
            self.rel_path = rel_path
            self.size = size
            self.path = rel_path

    files = [FakeFile("a.txt"), FakeFile("b.txt"), FakeFile("c.txt")]
    job = TransferJob(name="demo", dry_run=True, resume_from_checkpoint=True, delete_after_transfer=True)
    plan = build_transfer_plan(job, files, checkpoint_files={"b.txt"})
    assert plan["would_transfer"] == ["a.txt", "c.txt"]
    assert plan["dry_run"] is True
    assert plan["would_delete"] is True
    assert plan["skipped_resume"] == ["b.txt"]
    no_verify_job = TransferJob(name="demo", delete_after_transfer=True, verification_mode="None")
    assert build_transfer_plan(no_verify_job, files)["would_delete"] is False


def test_dry_run_does_not_create_missing_destination(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    source_file = source / "sample.txt"
    source_file.write_text("preview only", encoding="utf-8")
    os.utime(source_file, (1, 1))
    destination = tmp_path / "not-created"

    result = run_transfer(TransferJob(
        name="preview",
        source_path=str(source),
        destination_path=str(destination),
        older_than_days=1,
        file_filter="*.txt",
        dry_run=True,
        delete_after_transfer=True,
        log_dir=str(tmp_path / "logs"),
    ))

    assert result.overall_success
    assert result.dry_run
    assert not destination.exists()
    assert (tmp_path / "logs" / "Preview_preview.json").exists()


def _prepare_old_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    source_file = source / "sample.txt"
    source_file.write_text("transfer and verify", encoding="utf-8")
    os.utime(source_file, (1, 1))
    return source, source_file, tmp_path / "destination"


def test_transfer_keeps_source_without_matching_preview(monkeypatch, tmp_path):
    source, source_file, destination = _prepare_old_source(tmp_path)
    monkeypatch.setattr(
        transfer_module, "run_robocopy_with_progress",
        lambda *args, **kwargs: (1, timedelta(0)),
    )
    job = TransferJob(
        name="guarded", source_path=str(source), destination_path=str(destination),
        older_than_days=1, file_filter="*.txt", delete_after_transfer=True,
        log_dir=str(tmp_path / "logs"),
    )

    result = run_transfer(job, run_id="without-preview")

    assert result.overall_success
    assert source_file.exists()
    assert (destination / "sample.txt").exists()


def test_transfer_deletes_only_after_matching_preview_and_verification(monkeypatch, tmp_path):
    source, source_file, destination = _prepare_old_source(tmp_path)
    monkeypatch.setattr(
        transfer_module, "run_robocopy_with_progress",
        lambda *args, **kwargs: (1, timedelta(0)),
    )
    job = TransferJob(
        name="guarded", source_path=str(source), destination_path=str(destination),
        older_than_days=1, file_filter="*.txt", delete_after_transfer=True,
        dry_run=True, log_dir=str(tmp_path / "logs"),
    )

    preview = run_transfer(job, run_id="preview")
    job.dry_run = False
    result = run_transfer(job, run_id="transfer")

    assert preview.overall_success and preview.dry_run
    assert result.overall_success
    assert (destination / "sample.txt").read_text(encoding="utf-8") == "transfer and verify"
    assert not source_file.exists()


def test_transfer_keeps_source_if_inventory_changes_after_preview(monkeypatch, tmp_path):
    source, source_file, destination = _prepare_old_source(tmp_path)
    monkeypatch.setattr(
        transfer_module, "run_robocopy_with_progress",
        lambda *args, **kwargs: (1, timedelta(0)),
    )
    job = TransferJob(
        name="guarded", source_path=str(source), destination_path=str(destination),
        older_than_days=1, file_filter="*.txt", delete_after_transfer=True,
        dry_run=True, log_dir=str(tmp_path / "logs"),
    )

    preview = run_transfer(job, run_id="preview-changed")
    source_file.write_text("changed after preview", encoding="utf-8")
    os.utime(source_file, (1, 1))
    job.dry_run = False
    result = run_transfer(job, run_id="transfer-changed")

    assert preview.overall_success and preview.dry_run
    assert result.overall_success
    assert source_file.exists()
    assert (destination / "sample.txt").read_text(encoding="utf-8") == "changed after preview"


def test_resume_transfer_persists_verified_checkpoint(tmp_path, monkeypatch):
    source, source_file, destination = _prepare_old_source(tmp_path)
    monkeypatch.setattr(
        transfer_module, "run_robocopy_with_progress",
        lambda *args, **kwargs: (1, timedelta(0)),
    )
    job = TransferJob(
        name="checkpoint", source_path=str(source), destination_path=str(destination),
        older_than_days=1, file_filter="*.txt", resume_from_checkpoint=True,
        log_dir=str(tmp_path / "logs"),
    )

    result = run_transfer(job, run_id="checkpoint-run")

    journal_path = tmp_path / "logs" / "Checkpoint_checkpoint.jsonl"
    stat = source_file.stat()
    journal = CheckpointJournal(journal_path, checkpoint_signature(job), resume=True)
    assert result.overall_success
    assert journal.resumed
    assert journal.matching_count([
        SimpleNamespace(rel_path="sample.txt", size=stat.st_size, mtime=stat.st_mtime),
    ]) == 1
    journal.close()


def test_checkpoint_journal_ignores_torn_tail_and_resets_on_signature_change(tmp_path):
    journal_path = tmp_path / "checkpoint.jsonl"
    journal = CheckpointJournal(journal_path, "signature-a")
    journal.record_verified("sample.txt", 12, 3.5, "FullHash", "abc")
    journal.close()
    with journal_path.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"verified"')

    resumed = CheckpointJournal(journal_path, "signature-a", resume=True)
    assert resumed.resumed
    assert resumed.records["sample.txt"]["hash"] == "abc"
    resumed.close()

    incompatible = CheckpointJournal(journal_path, "signature-b", resume=True)
    assert not incompatible.resumed
    assert incompatible.records == {}
    incompatible.close()


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
