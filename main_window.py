"""
main_window.py
---------------
Ana pencere: job listesi (grid), arac cubugu, canli log gorunumu.

Onemli mimari fark (PowerShell'e kiyasla):
- "Simdi Calistir" ayri bir surec (powershell.exe) baslatmiyor; ayni
  process icinde bir QThread'de calisiyor. Bu, GUI'yi dondurmadan
  calismasini saglar VE PowerShell surumundeki "iki ayri surec" (2 ayri
  powershell.exe) yaklasimindan cok daha az bellek/karmasiklik gerektirir.
- "Durdur" butonu, cancel_event (threading.Event) ile robocopy surecini
  temiz bir sekilde sonlandirir - taskkill /F /T gibi sert bir yontem
  gerekmez.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTableWidget,
    QTableWidgetItem, QPushButton, QSplitter, QPlainTextEdit, QMessageBox,
    QStatusBar, QHeaderView,
)

from engine.config import JobConfigStore, TransferJob
from engine.credentials import CredentialStore
from engine.transfer import run_transfer, TransferResult
from engine.scheduler import register_scheduled_task, unregister_scheduled_task
from gui.job_editor import JobEditorDialog
from gui.cred_manager import CredentialManagerDialog


COLUMNS = ["Job Adi", "Kaynak", "Hedef", "Yas(gun)", "Sil", "Aktif", "Zamanlama", "Son Calisma", "Durum"]


class TransferWorker(QThread):
    """Bir job'u arka planda (ayri thread'de) calistirir."""

    log_line = Signal(str)
    finished_result = Signal(object)  # TransferResult

    def __init__(self, job: TransferJob, cred_store: CredentialStore, run_id: str):
        super().__init__()
        self.job = job
        self.cred_store = cred_store
        self.run_id = run_id
        self.cancel_event = threading.Event()

    def run(self):
        result = run_transfer(
            self.job, run_id=self.run_id, credential_store=self.cred_store,
            on_log=self.log_line.emit, cancel_event=self.cancel_event,
        )
        self.finished_result.emit(result)

    def request_stop(self):
        self.cancel_event.set()


class MainWindow(QMainWindow):
    def __init__(self, config_path: str, cred_dir: str, exe_path: str):
        super().__init__()
        self.config_store = JobConfigStore(config_path)
        self.cred_store = CredentialStore(cred_dir)
        self.exe_path = exe_path
        self.config_path = config_path

        self.setWindowTitle(f"Veri Transfer Konsolu — {os.environ.get('COMPUTERNAME', 'localhost')}")
        self.resize(1100, 720)

        self.worker: Optional[TransferWorker] = None

        self._build_ui()
        self.refresh_grid()

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        # Arac cubugu
        toolbar = QHBoxLayout()
        self.btn_new = QPushButton("Yeni Job")
        self.btn_edit = QPushButton("Duzenle")
        self.btn_delete = QPushButton("Sil")
        self.btn_run = QPushButton("Simdi Calistir")
        self.btn_stop = QPushButton("Durdur")
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet("background-color:#dc3c3c; color:white;")
        self.btn_log = QPushButton("Log Goruntule")
        self.btn_hash_log = QPushButton("Hash Log")
        self.btn_schedule = QPushButton("Zamanla")
        self.btn_unschedule = QPushButton("Zamanlamayi Kaldir")
        self.btn_cred = QPushButton("Kimlik Yoneticisi")
        self.btn_refresh = QPushButton("Yenile")
        self.btn_open_folder = QPushButton("Klasoru Ac")

        for b in (self.btn_new, self.btn_edit, self.btn_delete, self.btn_run, self.btn_stop,
                  self.btn_log, self.btn_hash_log, self.btn_schedule, self.btn_unschedule,
                  self.btn_cred, self.btn_refresh, self.btn_open_folder):
            toolbar.addWidget(b)
        toolbar.addStretch(1)
        outer.addLayout(toolbar)

        # Splitter: ust=grid, alt=log
        splitter = QSplitter(Qt.Orientation.Vertical)
        outer.addWidget(splitter, 1)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.doubleClicked.connect(self._on_edit)
        splitter.addWidget(self.table)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet("background-color:#121212; color:#b4ffb4; font-family:Consolas,monospace;")
        self.log_view.setMaximumBlockCount(5000)  # sinirsiz buyumeyi engeller
        splitter.addWidget(self.log_view)
        splitter.setSizes([380, 300])

        self.status = QStatusBar()
        self.setStatusBar(self.status)

        # Sinyaller
        self.btn_new.clicked.connect(self._on_new)
        self.btn_edit.clicked.connect(self._on_edit)
        self.btn_delete.clicked.connect(self._on_delete)
        self.btn_run.clicked.connect(self._on_run)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_log.clicked.connect(self._on_view_log)
        self.btn_hash_log.clicked.connect(self._on_view_hash_log)
        self.btn_schedule.clicked.connect(self._on_schedule)
        self.btn_unschedule.clicked.connect(self._on_unschedule)
        self.btn_cred.clicked.connect(self._on_cred_manager)
        self.btn_refresh.clicked.connect(self.refresh_grid)
        self.btn_open_folder.clicked.connect(self._on_open_folder)

    # -------------------------------------------------------------- Grid

    def refresh_grid(self):
        jobs = self.config_store.load()
        self.table.setRowCount(len(jobs))
        for row, job in enumerate(jobs):
            sched = f"{job.schedule_frequency} {job.schedule_time}" if job.schedule_enabled else "-"
            last_run = job.last_run[:16].replace("T", " ") if job.last_run else "-"
            last_status = job.last_status or "-"

            values = [
                job.name, job.source_path, job.destination_path,
                str(job.older_than_days), str(job.delete_after_transfer),
                "Evet" if job.enabled else "Hayir", sched, last_run, last_status,
            ]
            for col, val in enumerate(values):
                item = QTableWidgetItem(val)
                if last_status == "Basarili":
                    item.setBackground(QColor(220, 255, 220))
                elif last_status == "Hatali":
                    item.setBackground(QColor(255, 220, 220))
                self.table.setItem(row, col, item)

        self.status.showMessage(f"{len(jobs)} job yuklendi.")

    def _selected_job(self) -> Optional[TransferJob]:
        row = self.table.currentRow()
        if row < 0:
            return None
        name = self.table.item(row, 0).text()
        return self.config_store.get_job(name)

    # ------------------------------------------------------------ Handlers

    def _on_new(self):
        jobs = self.config_store.load()
        existing_names = [j.name for j in jobs]
        dlg = JobEditorDialog(self.cred_store, existing_job=None, existing_names=existing_names, parent=self)
        if dlg.exec():
            new_job = dlg.get_job()
            ok = self.config_store.upsert_job(new_job)
            if not ok:
                QMessageBox.warning(self, "Hata", "Job kaydedilemedi! Mevcut veri korundu.")
            self.refresh_grid()

    def _on_edit(self):
        job = self._selected_job()
        if not job:
            QMessageBox.warning(self, "Uyari", "Once bir job secin.")
            return
        jobs = self.config_store.load()
        existing_names = [j.name for j in jobs if j.name != job.name]
        dlg = JobEditorDialog(self.cred_store, existing_job=job, existing_names=existing_names, parent=self)
        if dlg.exec():
            edited = dlg.get_job()
            ok = self.config_store.upsert_job(edited)
            if not ok:
                QMessageBox.warning(self, "Hata", "Job kaydedilemedi! Mevcut veri korundu.")
            self.refresh_grid()

    def _on_delete(self):
        job = self._selected_job()
        if not job:
            QMessageBox.warning(self, "Uyari", "Once bir job secin.")
            return
        reply = QMessageBox.question(
            self, "Onay", f"'{job.name}' silinsin mi? (Zamanlamasi da kaldirilir)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        unregister_scheduled_task(job.name)
        self.config_store.delete_job(job.name)
        self.refresh_grid()

    def _on_run(self):
        job = self._selected_job()
        if not job:
            QMessageBox.warning(self, "Uyari", "Once bir job secin.")
            return
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.warning(self, "Uyari", "Hali hazirda calisan bir job var.")
            return

        import datetime
        run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        self.log_view.clear()
        self.log_view.appendPlainText(f"Baslatiliyor: {job.name} [RunId: {run_id}]\n")

        self.worker = TransferWorker(job, self.cred_store, run_id)
        self.worker.log_line.connect(self.log_view.appendPlainText)
        self.worker.finished_result.connect(self._on_transfer_finished)
        self.worker.start()

        self.btn_run.setEnabled(False)
        self.btn_run.setText("Calisiyor...")
        self.btn_stop.setEnabled(True)
        self.status.showMessage(f"'{job.name}' calisiyor...")

    def _on_stop(self):
        if self.worker is None or not self.worker.isRunning():
            return
        reply = QMessageBox.question(
            self, "Durdur",
            "Job durdurulsun mu?\nYarim kalan dosyalar hedefte kalabilir.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.worker.request_stop()
            self.status.showMessage("Durdurma istegi gonderildi, bekleniyor...")

    def _on_transfer_finished(self, result: TransferResult):
        if result.overall_success:
            self.log_view.appendPlainText("\n=== JOB BASARILI ===")
            self.status.showMessage("Basariyla tamamlandi.")
        else:
            self.log_view.appendPlainText(f"\n=== JOB HATALI: {result.error_message} ===")
            self.status.showMessage(f"Hata: {result.error_message}")

        status_str = "Basarili" if result.overall_success else "Hatali"
        message = result.error_message or f"{result.verified_files} dosya dogrulandi"
        self.config_store.update_run_result(
            result.job_name, status_str, message, result.log_file, result.hash_log_file,
        )

        self.btn_run.setEnabled(True)
        self.btn_run.setText("Simdi Calistir")
        self.btn_stop.setEnabled(False)
        self.refresh_grid()

    def _on_view_log(self):
        job = self._selected_job()
        if not job:
            QMessageBox.warning(self, "Uyari", "Once bir job secin.")
            return
        if not job.last_log_file or not os.path.exists(job.last_log_file):
            QMessageBox.information(self, "Bilgi", "Henuz log bulunamadi.")
            return
        self._open_in_default_viewer(job.last_log_file)

    def _on_view_hash_log(self):
        job = self._selected_job()
        if not job:
            QMessageBox.warning(self, "Uyari", "Once bir job secin.")
            return
        if not job.last_hash_log or not os.path.exists(job.last_hash_log):
            QMessageBox.information(self, "Bilgi", "Henuz hash log bulunamadi.")
            return
        self._open_in_default_viewer(job.last_hash_log)

    def _open_in_default_viewer(self, path: str):
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            QMessageBox.information(self, "Log", Path(path).read_text(encoding="utf-8", errors="replace"))

    def _on_schedule(self):
        job = self._selected_job()
        if not job:
            QMessageBox.warning(self, "Uyari", "Once bir job secin.")
            return
        if not job.schedule_enabled:
            QMessageBox.information(
                self, "Bilgi", "Job'ta 'Zamanlama Aktif' isaretli degil. Once Duzenle'den aktif edin.",
            )
            return
        res = register_scheduled_task(
            job.name, self.exe_path, job.schedule_frequency, job.schedule_time,
            job.schedule_weekly_day, job.run_as_user, self.config_path,
        )
        if res.success:
            QMessageBox.information(self, "Basarili", res.message)
        else:
            QMessageBox.warning(self, "Hata", f"Gorev olusturulamadi:\n{res.message}")

    def _on_unschedule(self):
        job = self._selected_job()
        if not job:
            QMessageBox.warning(self, "Uyari", "Once bir job secin.")
            return
        res = unregister_scheduled_task(job.name)
        if res.success:
            QMessageBox.information(self, "Bilgi", res.message)
        else:
            QMessageBox.information(self, "Bilgi", "Gorev bulunamadi veya kaldirilamadi.")

    def _on_cred_manager(self):
        dlg = CredentialManagerDialog(self.cred_store, parent=self)
        dlg.exec()

    def _on_open_folder(self):
        folder = str(Path(self.config_path).parent)
        if sys.platform == "win32":
            os.startfile(folder)  # type: ignore[attr-defined]
        else:
            QMessageBox.information(self, "Klasor", folder)

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            reply = QMessageBox.question(
                self, "Cikis", "Bir job hala calisiyor. Yine de cikilsin mi?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.request_stop()
            self.worker.wait(3000)
        event.accept()
