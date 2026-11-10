import importlib
import io
import os
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from checkpoint import CheckpointJournal, checkpoint_signature
from config import TransferJob, sort_jobs_for_queue
from credentials import CredentialStore
from gui.job_editor import JobEditorDialog
import main_window as main_window_module
from main import configure_ui_font
from main_window import (
    COLUMNS,
    PRIORITY_COL,
    TASK_PROGRESS_COL,
    JobTaskState,
    MainWindow,
    TransferWorker,
    compute_task_progress,
)
import robocopy_runner
from robocopy_runner import _FILE_LINE_RE, _build_robocopy_args, run_robocopy_with_progress
import transfer as transfer_module
from transfer import _deletion_guard, _preview_receipt_matches, build_transfer_plan, run_transfer


def test_compute_task_progress_basic():
    percent, label = compute_task_progress(3, 10, elapsed_seconds=10)
    assert percent == 30
    assert label == "3 / 10 dosya"
    assert "%" not in label


def test_compute_task_progress_zero_total():
    percent, label = compute_task_progress(0, 0)
    assert percent == 0
    assert label == "0 / 0 dosya"


def test_compute_task_progress_formats_large_file_counts_without_percent():
    _, label = compute_task_progress(12345, 400000)
    assert label == "12.345 / 400.000 dosya"


def test_task_panel_shows_file_counts_instead_of_a_progress_bar(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(str(tmp_path / "jobs.json"), str(tmp_path / "creds"), str(tmp_path / "app.exe"))
    window.tasks["bulk"] = JobTaskState(files_done=12345, files_total=400000)

    window._update_task_row("bulk")
    row = window._task_row_for("bulk")

    assert row is not None
    assert window.tasks_table.item(row, TASK_PROGRESS_COL).text() == "12.345 / 400.000 dosya"
    assert window.tasks_table.cellWidget(row, TASK_PROGRESS_COL) is None
    window.close()
    del app


def test_parallel_hash_iterator_keeps_a_bounded_input_window(monkeypatch):
    submitted = []

    class Future:
        def __init__(self, path):
            self.path = path

        def result(self):
            return self.path.upper()

    class Executor:
        def __init__(self, max_workers):
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def submit(self, _function, path):
            submitted.append(path)
            return Future(path)

    monkeypatch.setattr(transfer_module.concurrent.futures, "ThreadPoolExecutor", Executor)
    results = transfer_module._iter_hashes_parallel((str(i) for i in range(10)), max_workers=2)

    assert next(results) == "0"
    assert len(submitted) == 4
    assert list(results) == [str(i) for i in range(1, 10)]


def test_robocopy_progress_only_matches_copying_file_states():
    assert _FILE_LINE_RE.match("New File 10 new.txt")
    assert _FILE_LINE_RE.match("Changed 10 changed.txt")
    assert _FILE_LINE_RE.match("Newer 10 newer.txt")
    assert not _FILE_LINE_RE.match("Older 10 existing.txt")
    assert not _FILE_LINE_RE.match("*EXTRA File 10 extra.txt")
    assert not _FILE_LINE_RE.match("Tweaked 10 tweaked.txt")


def test_robocopy_progress_reports_copy_count_without_overcounting(monkeypatch, tmp_path):
    class FakeProcess:
        def __init__(self, output):
            self.stdout = io.StringIO(output)
            self.returncode = 1

        def poll(self):
            return self.returncode

    monkeypatch.setattr(
        robocopy_runner.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(
            "New File 10 first.txt\nOlder 10 existing.txt\n*EXTRA File 2 extra.txt\nChanged 5 second.txt\n"
        ),
    )
    progress = []
    run_robocopy_with_progress(
        "src", "dst", "*.*", 0, 1, 2, str(tmp_path / "robocopy.log"),
        on_progress=progress.append, total_files=4,
    )

    assert progress[-1].files_done == 2
    assert progress[-1].files_total == 4

    monkeypatch.setattr(robocopy_runner.subprocess, "Popen", lambda *args, **kwargs: FakeProcess(""))
    no_copy_progress = []
    run_robocopy_with_progress(
        "src", "dst", "*.*", 0, 1, 2, str(tmp_path / "empty.log"),
        on_progress=no_copy_progress.append, total_files=4,
    )
    assert no_copy_progress[-1].files_done == 0


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


def test_delete_checkbox_replaces_policy_and_migrates_legacy_jobs_safely(tmp_path):
    app = QApplication.instance() or QApplication([])
    credentials = CredentialStore(str(tmp_path / "credentials"))
    new_job_dialog = JobEditorDialog(credentials)
    assert "Doğrulanmış dosyaları" in new_job_dialog.delete_check.text()
    assert not hasattr(new_job_dialog, "delete_policy_combo")
    new_job_dialog.close()

    legacy_always = JobEditorDialog(
        credentials,
        TransferJob(name="legacy-always", delete_after_transfer=True, delete_policy="always"),
    )
    assert not legacy_always.delete_check.isChecked()
    assert "desteklenmeyen 'always'" in legacy_always.delete_legacy_notice.text()
    assert legacy_always.get_job().delete_policy == "verified_only"
    legacy_always.close()

    legacy_never = JobEditorDialog(
        credentials,
        TransferJob(name="legacy-never", delete_after_transfer=True, delete_policy="never"),
    )
    assert not legacy_never.delete_check.isChecked()
    assert "'never'" in legacy_never.delete_legacy_notice.text()
    legacy_never.close()
    del app


def test_run_modes_are_grouped_in_one_menu(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(str(tmp_path / "jobs.json"), str(tmp_path / "creds"), str(tmp_path / "app.exe"))
    action_labels = [action.text() for action in window.run_menu.actions() if not action.isSeparator()]
    selected_modes = []
    window._on_run = lambda mode="normal": selected_modes.append(mode)
    window.action_run.setEnabled(True)
    window.action_dry_run.setEnabled(True)
    window.action_resume.setEnabled(True)

    assert window.btn_run.defaultAction() is window.action_run
    toolbar = window.toolbar_container.layout()
    assert toolbar is not None and toolbar.__class__.__name__ == "QVBoxLayout"
    assert window.minimumWidth() <= window.width()
    assert action_labels == ["Başlat", "Önizleme", "Checkpoint'ten Devam"]
    assert not hasattr(window, "btn_dry_run")
    assert not hasattr(window, "btn_resume")
    assert not hasattr(window, "btn_priority")
    for button in (window.btn_new, window.btn_edit, window.btn_delete, window.btn_run, window.btn_stop):
        assert button.minimumWidth() >= button.fontMetrics().horizontalAdvance(button.text())
    window.action_run.trigger()
    window.action_dry_run.trigger()
    window.action_resume.trigger()
    assert selected_modes == ["normal", "dry_run", "resume"]
    window.close()
    del app


def test_windows_system_font_is_registered_for_qt():
    app = QApplication.instance() or QApplication([])
    font_path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "segoeui.ttf"

    if font_path.is_file():
        assert configure_ui_font(app)
        assert app.font().family() == "Segoe UI"
    else:
        assert not configure_ui_font(app)
    del app


def test_job_grid_shows_priority_and_actual_deletion_state(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(str(tmp_path / "jobs.json"), str(tmp_path / "creds"), str(tmp_path / "app.exe"))
    jobs = [
        TransferJob(name="higher", priority=10, delete_after_transfer=True),
        TransferJob(name="lower", priority=2, delete_policy="never", delete_after_transfer=True),
    ]
    assert window.config_store.save(jobs)
    window.refresh_grid()
    window.show()
    app.processEvents()

    assert PRIORITY_COL == 0
    assert window.table.horizontalHeaderItem(0).text() == "Öncelik"
    job_col = COLUMNS.index("İş")
    row_by_name = {
        window.table.item(row, job_col).text(): row
        for row in range(window.table.rowCount())
    }
    delete_col = COLUMNS.index("Kaynak Silme")
    assert window.table.item(row_by_name["higher"], delete_col).text() == "Doğrulanınca"
    assert window.table.item(row_by_name["lower"], delete_col).text() == "Kapalı"

    window.table.sortItems(PRIORITY_COL, Qt.SortOrder.AscendingOrder)
    assert window.table.item(0, job_col).text() == "lower"
    window.resize(640, window.height())
    app.processEvents()
    assert window.toolbar_container.isVisible()
    assert not hasattr(window, "btn_priority")
    for button in (window.btn_new, window.btn_edit, window.btn_delete, window.btn_run, window.btn_stop):
        assert button.width() >= button.minimumWidth()
    assert window.main_splitter.sizes()[0] < 300
    assert window.width() <= window.screen().availableGeometry().width()
    window.close()
    del app


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


def test_transfer_reports_source_hash_failures_and_keeps_source(monkeypatch, tmp_path):
    source, source_file, destination = _prepare_old_source(tmp_path)
    monkeypatch.setattr(transfer_module, "_sha256_of_file", lambda _path: None)
    monkeypatch.setattr(
        transfer_module, "run_robocopy_with_progress",
        lambda *args, **kwargs: (1, timedelta(0)),
    )
    result = run_transfer(TransferJob(
        name="hash-failure", source_path=str(source), destination_path=str(destination),
        older_than_days=1, file_filter="*.txt", log_dir=str(tmp_path / "logs"),
    ))

    assert not result.overall_success
    assert result.failed_files == 1
    assert source_file.exists()
    assert "sample.txt" in Path(result.hash_log_file).read_text(encoding="utf-8")


def test_transfer_rejects_overlapping_source_and_destination(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    nested_destination = source / "destination"
    result = run_transfer(TransferJob(
        name="overlap", source_path=str(source), destination_path=str(nested_destination),
        log_dir=str(tmp_path / "logs"),
    ))

    assert not result.overall_success
    assert "ic ice" in result.error_message
    assert not nested_destination.exists()


def test_delete_failure_marks_transfer_failed_and_preserves_source(monkeypatch, tmp_path):
    source, source_file, destination = _prepare_old_source(tmp_path)
    job = TransferJob(
        name="delete-failure", source_path=str(source), destination_path=str(destination),
        older_than_days=1, file_filter="*.txt", delete_after_transfer=True,
        dry_run=True, log_dir=str(tmp_path / "logs"),
    )
    assert run_transfer(job, run_id="preview-delete-failure").overall_success
    job.dry_run = False

    def fail_delete(paths, **_kwargs):
        return 0, [(paths[0], "access denied")]

    monkeypatch.setattr(transfer_module, "delete_files_parallel", fail_delete)
    result = run_transfer(job, run_id="delete-failure")

    assert not result.overall_success
    assert result.delete_failed_files == 1
    assert "Kaynak dosyalari silinemedi: 1" in result.error_message
    assert source_file.exists()


def test_transfer_keeps_source_changed_after_verification(monkeypatch, tmp_path):
    source, source_file, destination = _prepare_old_source(tmp_path)
    job = TransferJob(
        name="changed-before-delete", source_path=str(source), destination_path=str(destination),
        older_than_days=1, file_filter="*.txt", delete_after_transfer=True,
        dry_run=True, max_retries=1, log_dir=str(tmp_path / "logs"),
    )
    assert run_transfer(job, run_id="preview-changed-before-delete").overall_success
    job.dry_run = False
    original_delete = transfer_module.delete_files_parallel

    def change_source_then_delete(paths, **kwargs):
        source_file.write_text("new content", encoding="utf-8")
        os.utime(source_file, (2, 2))
        return original_delete(paths, **kwargs)

    monkeypatch.setattr(transfer_module, "delete_files_parallel", change_source_then_delete)
    result = run_transfer(job, run_id="changed-before-delete")

    assert not result.overall_success
    assert source_file.read_text(encoding="utf-8") == "new content"
    assert result.delete_failed_files == 1


def test_transfer_removes_empty_subdirectories_after_verified_delete(tmp_path):
    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    source_file = nested / "sample.txt"
    source_file.write_text("transfer and verify", encoding="utf-8")
    os.utime(source_file, (1, 1))
    destination = tmp_path / "destination"
    job = TransferJob(
        name="remove-empty-folders", source_path=str(source), destination_path=str(destination),
        older_than_days=1, file_filter="*.txt", delete_after_transfer=True,
        dry_run=True, log_dir=str(tmp_path / "logs"),
    )
    assert run_transfer(job, run_id="preview-folders").overall_success
    job.dry_run = False

    result = run_transfer(job, run_id="remove-folders")

    assert result.overall_success
    assert not source_file.exists()
    assert not nested.exists()
    assert source.exists()


def test_directory_delete_failure_marks_transfer_failed(monkeypatch, tmp_path):
    source, source_file, destination = _prepare_old_source(tmp_path)
    nested = source / "empty-after-delete"
    nested.mkdir()
    job = TransferJob(
        name="directory-delete-failure", source_path=str(source), destination_path=str(destination),
        older_than_days=1, file_filter="*.txt", delete_after_transfer=True,
        dry_run=True, max_retries=1, log_dir=str(tmp_path / "logs"),
    )
    assert run_transfer(job, run_id="preview-directory-failure").overall_success
    job.dry_run = False
    original_rmdir = os.rmdir

    def fail_nested_rmdir(path):
        if os.path.normcase(path) == os.path.normcase(str(nested)):
            raise PermissionError("directory locked")
        return original_rmdir(path)

    monkeypatch.setattr(transfer_module.os, "rmdir", fail_nested_rmdir)
    result = run_transfer(job, run_id="directory-failure")

    assert not result.overall_success
    assert result.delete_directory_failed == 1
    assert "Kaynak klasorleri silinemedi: 1" in result.error_message
    assert not source_file.exists()
    assert nested.exists()


def test_unverified_transfer_failure_never_deletes_source(monkeypatch, tmp_path):
    source, source_file, destination = _prepare_old_source(tmp_path)
    monkeypatch.setattr(
        transfer_module, "run_robocopy_with_progress",
        lambda *args, **kwargs: (8, timedelta(0)),
    )
    result = run_transfer(TransferJob(
        name="unverified-failure", source_path=str(source), destination_path=str(destination),
        older_than_days=1, file_filter="*.txt", verification_mode="None",
        delete_after_transfer=True, require_preview_for_delete=False,
        log_dir=str(tmp_path / "logs"),
    ))

    assert not result.overall_success
    assert result.verified_files == 0
    assert source_file.exists()


def test_transfer_converts_unexpected_engine_error_to_failed_result(monkeypatch, tmp_path):
    source, source_file, destination = _prepare_old_source(tmp_path)

    def raise_runner_error(*_args, **_kwargs):
        raise RuntimeError("simulated engine failure")

    monkeypatch.setattr(transfer_module, "run_robocopy_with_progress", raise_runner_error)
    result = run_transfer(TransferJob(
        name="unexpected-error", source_path=str(source), destination_path=str(destination),
        older_than_days=1, file_filter="*.txt", delete_after_transfer=True,
        require_preview_for_delete=False, log_dir=str(tmp_path / "logs"),
    ))

    assert not result.overall_success
    assert "RuntimeError: simulated engine failure" in result.error_message
    assert source_file.exists()


def test_transfer_worker_emits_failed_result_on_startup_exception(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])

    def raise_startup_error(*_args, **_kwargs):
        raise OSError("cannot create log")

    monkeypatch.setattr(main_window_module, "run_transfer", raise_startup_error)
    worker = TransferWorker(
        TransferJob(name="worker-error"),
        CredentialStore(str(tmp_path / "creds")),
        "worker-run",
        str(tmp_path / "locks"),
    )
    results = []
    logs = []
    worker.finished_result.connect(results.append)
    worker.log_line.connect(logs.append)

    worker.run()

    assert len(results) == 1
    assert not results[0].overall_success
    assert "OSError: cannot create log" in results[0].error_message
    assert any("Beklenmeyen aktarim hatasi" in line for line in logs)
    del app


def test_delete_files_parallel_retries_transient_error(monkeypatch, tmp_path):
    path = tmp_path / "retry.txt"
    path.write_text("retry", encoding="utf-8")
    original_remove = os.remove
    attempts = 0

    def fail_once(candidate):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("temporary lock")
        original_remove(candidate)

    monkeypatch.setattr(transfer_module.os, "remove", fail_once)
    monkeypatch.setattr(transfer_module.time, "sleep", lambda _seconds: None)

    deleted, errors = transfer_module.delete_files_parallel(
        [str(path)], max_workers=1, max_retries=2,
    )

    assert deleted == 1
    assert errors == []
    assert attempts == 2
    assert not path.exists()


def test_transfer_keeps_source_when_destination_disappears_before_delete(monkeypatch, tmp_path):
    source, source_file, destination = _prepare_old_source(tmp_path)
    job = TransferJob(
        name="destination-disappears", source_path=str(source), destination_path=str(destination),
        older_than_days=1, file_filter="*.txt", delete_after_transfer=True,
        dry_run=True, max_retries=1, log_dir=str(tmp_path / "logs"),
    )
    assert run_transfer(job, run_id="preview-destination-disappears").overall_success
    job.dry_run = False
    original_delete = transfer_module.delete_files_parallel

    def remove_destination_then_delete(paths, **kwargs):
        (destination / "sample.txt").unlink()
        return original_delete(paths, **kwargs)

    monkeypatch.setattr(transfer_module, "delete_files_parallel", remove_destination_then_delete)
    result = run_transfer(job, run_id="destination-disappears")

    assert not result.overall_success
    assert result.delete_failed_files == 1
    assert source_file.exists()


def test_delete_files_parallel_pre_cancel_preserves_all_files(tmp_path):
    paths = []
    for index in range(3):
        path = tmp_path / f"item-{index}.txt"
        path.write_text("keep", encoding="utf-8")
        paths.append(str(path))
    cancel_event = threading.Event()
    cancel_event.set()

    deleted, errors = transfer_module.delete_files_parallel(
        paths, max_workers=2, cancel_event=cancel_event,
    )

    assert deleted == 0
    assert len(errors) == 1
    assert errors[0][0] == ""
    assert "3 dosya bekliyor" in errors[0][1]
    assert all(Path(path).exists() for path in paths)


def test_delete_files_parallel_stops_submitting_after_cancel(tmp_path, monkeypatch):
    paths = []
    for index in range(25):
        path = tmp_path / f"item-{index}.txt"
        path.write_text("keep", encoding="utf-8")
        paths.append(str(path))
    cancel_event = threading.Event()
    original_remove = os.remove
    removed = []
    removed_lock = threading.Lock()

    def cancel_on_first_remove(path):
        with removed_lock:
            removed.append(path)
            cancel_event.set()
        original_remove(path)

    monkeypatch.setattr(transfer_module.os, "remove", cancel_on_first_remove)
    deleted, errors = transfer_module.delete_files_parallel(
        paths, max_workers=2, cancel_event=cancel_event,
    )

    assert deleted == len(removed)
    assert len(removed) <= 4
    assert len(errors) == 1
    assert errors[0][0] == ""
    assert "dosya bekliyor" in errors[0][1]


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
