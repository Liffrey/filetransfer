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
from functools import partial
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QCoreApplication, QSettings, Qt, QThread, Signal
from PySide6.QtGui import QColor, QTextCursor, QTextCharFormat
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTableWidget,
    QTableWidgetItem, QPushButton, QSplitter, QPlainTextEdit, QMessageBox,
    QStatusBar, QHeaderView, QFrame, QLabel, QProgressBar,
)

from engine.config import JobConfigStore, TransferJob
from engine.credentials import CredentialStore
from engine.transfer import run_transfer, TransferResult
from engine.scheduler import register_scheduled_task, unregister_scheduled_task
from gui.job_editor import JobEditorDialog
from gui.cred_manager import CredentialManagerDialog
from gui.style import (
    LOG_LEVEL_COLORS,
    THEME_DARK,
    THEME_LIGHT,
    build_app_palette,
    build_app_stylesheet,
    empty_label_stylesheet,
    log_view_stylesheet,
    progress_label_stylesheet,
    status_error_bg,
    status_ok_bg,
)


COLUMNS = ["Job Adi", "Kaynak", "Hedef", "Yas(gun)", "Sil", "Aktif", "Zamanlama", "Son Calisma", "Durum", "Ilerleme"]
PROGRESS_COL = COLUMNS.index("Ilerleme")


class TransferWorker(QThread):
    """Bir job'u arka planda (ayri thread'de) calistirir."""

    log_line = Signal(str)
    progress_update = Signal(str, float)  # (dosya adi, yuzde)
    finished_result = Signal(object)  # TransferResult

    def __init__(self, job: TransferJob, cred_store: CredentialStore, run_id: str, lock_dir: str):
        super().__init__()
        self.job = job
        self.cred_store = cred_store
        self.run_id = run_id
        self.lock_dir = lock_dir
        self.cancel_event = threading.Event()

    def _on_progress(self, p) -> None:
        # RobocopyProgress nesnesini Qt sinyaline uygun basit (str, float)
        # ciftine cevirir - worker thread'den emit edilir, Qt'nin queued
        # connection mekanizmasi GUI thread'ine guvenli sekilde tasir.
        self.progress_update.emit(p.current_file, p.percent)

    def run(self):
        result = run_transfer(
            self.job, run_id=self.run_id, credential_store=self.cred_store,
            on_log=self.log_line.emit, cancel_event=self.cancel_event,
            lock_dir=self.lock_dir, on_progress=self._on_progress,
        )
        self.finished_result.emit(result)

    def request_stop(self):
        self.cancel_event.set()


class MainWindow(QMainWindow):
    def __init__(self, config_path: str, cred_dir: str, exe_path: str,
                 settings: Optional[QSettings] = None, initial_theme: str = THEME_LIGHT):
        super().__init__()
        self.config_store = JobConfigStore(config_path)
        self.cred_store = CredentialStore(cred_dir)
        self.exe_path = exe_path
        self.config_path = config_path
        self.lock_dir = str(Path(config_path).parent / "locks")
        self.settings = settings or QSettings("DataTransferTool", "TransferConsole")
        self.theme = self._normalize_theme(initial_theme)

        self.setWindowTitle(f"Veri Transfer Konsolu — {os.environ.get('COMPUTERNAME', 'localhost')}")
        self.resize(1100, 720)

        # Birden fazla FARKLI job'un GERCEKTEN paralel calisabilmesi icin tek
        # bir self.worker yerine job adina gore bir workers sozlugu tutulur.
        # Ayni job'un iki kez calistirilmasi burada (GUI seviyesinde) ve
        # ayrica run_transfer icindeki JobLock ile (surecler-arasi, ornegin
        # Gorev Zamanlayici ile cakisma icin) engellenir.
        self.workers: dict[str, TransferWorker] = {}
        # Her calisan job icin en son bilinen (dosya adi, yuzde) - hem tablo
        # satirini hem de (seciliyse) alttaki ozet ilerleme cubugunu doldurmak icin.
        self.active_progress: dict[str, tuple[str, float]] = {}

        self._build_ui()
        self.refresh_grid()

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        # Arac cubugu - mantiksal gruplar halinde, ayirici cizgilerle
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)

        self.btn_new = QPushButton("+ Yeni Job")
        self.btn_new.setProperty("primary", True)
        self.btn_edit = QPushButton("Duzenle")
        self.btn_delete = QPushButton("Sil")

        self.btn_run = QPushButton("▶ Simdi Calistir")
        self.btn_run.setProperty("primary", True)
        self.btn_stop = QPushButton("■ Durdur")
        self.btn_stop.setProperty("danger", True)
        self.btn_stop.setEnabled(False)

        self.btn_log = QPushButton("Log")
        self.btn_hash_log = QPushButton("Hash Log")

        self.btn_schedule = QPushButton("Zamanla")
        self.btn_unschedule = QPushButton("Zamanlamayi Kaldir")

        self.btn_theme = QPushButton()
        self.btn_theme.setCheckable(True)

        self.btn_cred = QPushButton("Kimlik Yoneticisi")
        self.btn_refresh = QPushButton("⟳ Yenile")
        self.btn_open_folder = QPushButton("Klasoru Ac")

        def add_separator():
            line = QFrame()
            line.setFrameShape(QFrame.Shape.VLine)
            line.setFrameShadow(QFrame.Shadow.Sunken)
            toolbar.addWidget(line)

        for b in (self.btn_new, self.btn_edit, self.btn_delete):
            toolbar.addWidget(b)
        add_separator()
        for b in (self.btn_run, self.btn_stop):
            toolbar.addWidget(b)
        add_separator()
        for b in (self.btn_log, self.btn_hash_log):
            toolbar.addWidget(b)
        add_separator()
        for b in (self.btn_schedule, self.btn_unschedule):
            toolbar.addWidget(b)
        add_separator()
        for b in (self.btn_cred, self.btn_refresh, self.btn_open_folder):
            toolbar.addWidget(b)

        toolbar.addWidget(self.btn_theme)

        toolbar.addStretch(1)
        outer.addLayout(toolbar)

        # Splitter: ust=grid, alt=log
        splitter = QSplitter(Qt.Orientation.Vertical)
        outer.addWidget(splitter, 1)

        table_container = QWidget()
        table_layout = QVBoxLayout(table_container)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(4)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.doubleClicked.connect(self._on_edit)
        self.table.itemSelectionChanged.connect(self._update_run_stop_buttons)
        table_layout.addWidget(self.table)

        self.empty_label = QLabel("Henuz job yok. Baslamak icin '+ Yeni Job' butonuna tiklayin.")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setVisible(False)
        table_layout.addWidget(self.empty_label)

        splitter.addWidget(table_container)

        log_container = QWidget()
        log_layout = QVBoxLayout(log_container)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(4)

        progress_row = QHBoxLayout()
        self.progress_file_label = QLabel("")
        progress_row.addWidget(self.progress_file_label, 1)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFixedWidth(220)
        self.progress_bar.setVisible(False)  # sadece bir job calisirken gorunur
        progress_row.addWidget(self.progress_bar)
        log_layout.addLayout(progress_row)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)  # sinirsiz buyumeyi engeller
        log_layout.addWidget(self.log_view)

        splitter.addWidget(log_container)
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
        self.btn_theme.toggled.connect(self._on_theme_toggled)

        self._apply_theme(self.theme, persist=False)

    # -------------------------------------------------------------- Grid

    def refresh_grid(self):
        # ONEMLI: tablo yeniden dolduruldugunda TUM QTableWidgetItem'lar
        # sifirdan olusturulur (asagida). Siralama (sorting) acikken bu
        # yeniden doldurma sonrasi satirlar yer degistirebilir - Qt'nin
        # secim modeli bu durumda ESKI SATIR NUMARASINI korur, ESKI JOB'U
        # DEGIL. Bu yuzden secili job'u ISME GORE hatirlayip yeniden
        # doldurduktan sonra AYNI ISME GORE tekrar seciyoruz - aksi halde
        # (ornegin bir job calistirilip "Son Calisma" sutununa gore
        # siralama aktifken) secim sessizce BASKA BIR JOB'A kayabilir ve
        # "Log Goruntule"/"Hash Log Goruntule" o job'un (bir onceki
        # islemin) log dosyasini acar.
        selected_name = self._selected_job_name()

        jobs = self.config_store.load()

        self.table.setSortingEnabled(False)  # doldururken siralama satirlari kaydirmasin
        self.table.setRowCount(len(jobs))
        for row, job in enumerate(jobs):
            sched = f"{job.schedule_frequency} {job.schedule_time}" if job.schedule_enabled else "-"
            last_run = job.last_run[:16].replace("T", " ") if job.last_run else "-"

            # Job su an bu pencereden calistiriliyorsa, Durum/Ilerleme
            # sutunlarinda jobs.json'daki KALICI son sonuc yerine CANLI
            # durumu gosteririz - birden fazla job ayni anda calisirken
            # her birinin kendi satirinda gercek zamanli ilerleme gorunur.
            running = job.name in self.workers and self.workers[job.name].isRunning()
            if running:
                last_status = "Calisiyor..."
                file_name, percent = self.active_progress.get(job.name, ("", 0.0))
                progress_text = f"%{percent:5.1f}  {file_name}".strip() if file_name else f"%{percent:5.1f}"
            else:
                last_status = job.last_status or "-"
                progress_text = "-"

            values = [
                job.name, job.source_path, job.destination_path,
                str(job.older_than_days), str(job.delete_after_transfer),
                "Evet" if job.enabled else "Hayir", sched, last_run, last_status, progress_text,
            ]
            for col, val in enumerate(values):
                item = QTableWidgetItem(val)
                if col in (1, 2):  # Kaynak/Hedef - uzun yollar icin tooltip
                    item.setToolTip(val)
                if last_status == "Basarili":
                    item.setBackground(QColor(status_ok_bg(self.theme)))
                elif last_status == "Hatali":
                    item.setBackground(QColor(status_error_bg(self.theme)))
                self.table.setItem(row, col, item)
        self.table.setSortingEnabled(True)

        self.empty_label.setVisible(len(jobs) == 0)
        self.table.setVisible(len(jobs) > 0)
        running_count = len(self.workers)
        suffix = f" ({running_count} job calisiyor)" if running_count else ""
        self.status.showMessage(f"{len(jobs)} job yuklendi.{suffix}")

        if selected_name is not None:
            self._select_job_by_name(selected_name)

        self._update_run_stop_buttons()

    def _selected_job_name(self) -> Optional[str]:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.text() if item else None

    def _select_job_by_name(self, name: str) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.text() == name:
                self.table.setCurrentCell(row, 0)
                return

    def _selected_job(self) -> Optional[TransferJob]:
        name = self._selected_job_name()
        if name is None:
            return None
        return self.config_store.get_job(name)

    def _row_for_job_name(self, name: str) -> Optional[int]:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.text() == name:
                return row
        return None

    def _update_run_stop_buttons(self) -> None:
        """Secili job'a gore Calistir/Durdur butonlarini ve alttaki ozet
        ilerleme cubugunu gunceller. Birden fazla job ayni anda calisiyor
        olabilir - bu cubuk sadece SU AN SECILI olan job'u yansitir, diger
        calisan job'larin ilerlemesi tablodaki kendi satirlarinda gorulur."""
        job = self._selected_job()
        running = bool(job) and job.name in self.workers and self.workers[job.name].isRunning()

        self.btn_run.setEnabled(job is not None and not running)
        self.btn_run.setText("Calisiyor..." if running else "▶ Simdi Calistir")
        self.btn_stop.setEnabled(running)

        if running:
            file_name, percent = self.active_progress.get(job.name, ("", 0.0))
            self.progress_bar.setVisible(True)
            self.progress_bar.setValue(int(percent))
            self.progress_file_label.setText(f"[{job.name}] Kopyalaniyor: {file_name}" if file_name else f"[{job.name}] Hazirlaniyor...")
        else:
            self.progress_bar.setVisible(False)
            self.progress_file_label.setText("")

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
        if job.name in self.workers and self.workers[job.name].isRunning():
            # Calisan bir job'un adi degistirilirse, worker'in self.workers
            # sozlugundeki (eski isimle kayitli) anahtari ile config'deki
            # yeni isim uyusmaz - calisma bitince update_run_result() yeni
            # ismi bulamaz ve sonuc SESSIZCE kaydedilmez, ilerleme de tabloda
            # gorunmez olur. Bu yuzden calisirken duzenlemeye izin verilmez.
            QMessageBox.warning(self, "Uyari", f"'{job.name}' su an calisiyor, once durdurun.")
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
        if job.name in self.workers and self.workers[job.name].isRunning():
            QMessageBox.warning(self, "Uyari", f"'{job.name}' su an calisiyor, once durdurun.")
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

    def _append_colored_log(self, line: str, level_override: Optional[str] = None) -> None:
        """Log satirini seviyesine gore renklendirerek ekler. Motordan gelen
        satirlar zaten "[SEVIYE  ]" onekini icerir (bkz. EngineLogger), bu
        onek aranarak renk secilir. level_override verilirse (ozet mesajlari
        icin, ornegin '=== JOB BASARILI ===') dogrudan o renk kullanilir ve
        metne herhangi bir etiket EKLENMEZ."""
        if level_override and level_override in LOG_LEVEL_COLORS:
            color_hex = LOG_LEVEL_COLORS[level_override]
        else:
            color_hex = LOG_LEVEL_COLORS["INFO"]
            for level, hexcolor in LOG_LEVEL_COLORS.items():
                if f"[{level}" in line:
                    color_hex = hexcolor
                    break
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color_hex))
        cursor.setCharFormat(fmt)
        cursor.insertText(line + "\n")
        self.log_view.setTextCursor(cursor)
        self.log_view.ensureCursorVisible()

    @staticmethod
    def _normalize_theme(theme: str) -> str:
        return THEME_DARK if str(theme).lower() == THEME_DARK else THEME_LIGHT

    def _apply_theme(self, theme: str, persist: bool = True) -> None:
        self.theme = self._normalize_theme(theme)
        app = QCoreApplication.instance()
        if app is not None:
            app.setPalette(build_app_palette(self.theme))
            app.setStyleSheet(build_app_stylesheet(self.theme))

        self.log_view.setStyleSheet(log_view_stylesheet(self.theme))
        self.empty_label.setStyleSheet(empty_label_stylesheet(self.theme))
        self.progress_file_label.setStyleSheet(progress_label_stylesheet(self.theme))

        self.btn_theme.blockSignals(True)
        self.btn_theme.setChecked(self.theme == THEME_DARK)
        self.btn_theme.setText("Koyu Tema" if self.theme == THEME_LIGHT else "Açık Tema")
        self.btn_theme.blockSignals(False)

        if persist:
            self.settings.setValue("ui/theme", self.theme)

        self.refresh_grid()

    def _on_theme_toggled(self, checked: bool) -> None:
        self._apply_theme(THEME_DARK if checked else THEME_LIGHT)

    def _on_worker_log(self, job_name: str, line: str) -> None:
        # Birden fazla job ayni anda calisirken satirlarin birbirine
        # karismamasi icin her satirin basina hangi job'a ait oldugu eklenir.
        self._append_colored_log(f"[{job_name}] {line}")

    def _on_worker_progress(self, job_name: str, filename: str, percent: float) -> None:
        self.active_progress[job_name] = (filename, percent)

        row = self._row_for_job_name(job_name)
        if row is not None:
            progress_item = self.table.item(row, PROGRESS_COL)
            if progress_item is not None:
                text = f"%{percent:5.1f}  {filename}".strip() if filename else f"%{percent:5.1f}"
                progress_item.setText(text)

        if self._selected_job_name() == job_name:
            self.progress_bar.setVisible(True)
            self.progress_bar.setValue(int(percent))
            self.progress_file_label.setText(f"[{job_name}] Kopyalaniyor: {filename}" if filename else f"[{job_name}] Hazirlaniyor...")

    def _on_run(self):
        job = self._selected_job()
        if not job:
            QMessageBox.warning(self, "Uyari", "Once bir job secin.")
            return
        existing = self.workers.get(job.name)
        if existing is not None and existing.isRunning():
            QMessageBox.warning(self, "Uyari", f"'{job.name}' zaten calisiyor.")
            return

        import datetime
        run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        # NOT: log_view TUM job'lar icin PAYLASILIR (temizlenmez) - baska bir
        # job zaten calisiyor olabilir, onun gecmis ciktisini silmemek icin.
        self._append_colored_log(f"=== Baslatiliyor: {job.name} [RunId: {run_id}] ===")
        self.active_progress[job.name] = ("", 0.0)

        worker = TransferWorker(job, self.cred_store, run_id, self.lock_dir)
        worker.log_line.connect(partial(self._on_worker_log, job.name))
        worker.progress_update.connect(partial(self._on_worker_progress, job.name))
        worker.finished_result.connect(partial(self._on_transfer_finished, job.name))
        self.workers[job.name] = worker
        worker.start()

        self.status.showMessage(f"'{job.name}' calisiyor... ({len(self.workers)} job aktif)")
        self.refresh_grid()

    def _on_stop(self):
        job = self._selected_job()
        worker = self.workers.get(job.name) if job else None
        if worker is None or not worker.isRunning():
            return
        reply = QMessageBox.question(
            self, "Durdur",
            f"'{job.name}' durdurulsun mu?\nYarim kalan dosyalar hedefte kalabilir.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            worker.request_stop()
            self.status.showMessage(f"'{job.name}' icin durdurma istegi gonderildi, bekleniyor...")

    def _on_transfer_finished(self, job_name: str, result: TransferResult):
        self.workers.pop(job_name, None)
        self.active_progress.pop(job_name, None)

        if result.overall_success:
            self._append_colored_log(f"=== [{job_name}] JOB BASARILI ===", level_override="SUCCESS")
            self.status.showMessage(f"'{job_name}' basariyla tamamlandi.")
        else:
            self._append_colored_log(f"=== [{job_name}] JOB HATALI: {result.error_message} ===", level_override="ERROR")
            self.status.showMessage(f"'{job_name}' hata: {result.error_message}")

        status_str = "Basarili" if result.overall_success else "Hatali"
        message = result.error_message or f"{result.verified_files} dosya dogrulandi"
        self.config_store.update_run_result(
            result.job_name, status_str, message, result.log_file, result.hash_log_file,
        )

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
        running = [w for w in self.workers.values() if w.isRunning()]
        if running:
            reply = QMessageBox.question(
                self, "Cikis", f"{len(running)} job hala calisiyor. Yine de cikilsin mi?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            for worker in running:
                worker.request_stop()
            for worker in running:
                worker.wait(3000)
        event.accept()
