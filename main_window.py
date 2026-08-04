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

import datetime
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QCoreApplication, QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QTextCursor, QTextCharFormat
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTableWidget,
    QTableWidgetItem, QPushButton, QSplitter, QPlainTextEdit, QMessageBox,
    QStatusBar, QHeaderView, QFrame, QLabel, QProgressBar, QInputDialog, QLineEdit,
)

from engine.config import JobConfigStore, TransferJob, DEFAULT_LOG_DIR
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
    status_cancelled_bg,
    status_error_bg,
    status_ok_bg,
    status_running_bg,
)
from resources import get_app_icon_path


COLUMNS = ["Job Adi", "Kaynak", "Hedef", "Yas(gun)", "Sil", "Aktif", "Zamanlama", "Son Calisma", "Durum", "Log", "Hash Log"]
LOG_COL = COLUMNS.index("Log")
HASH_LOG_COL = COLUMNS.index("Hash Log")

TASK_COLUMNS = ["Job", "Durum", "Asama", "Ilerleme", "Sure", ""]
TASK_JOB_COL, TASK_STATUS_COL, TASK_STAGE_COL, TASK_PROGRESS_COL, TASK_DURATION_COL, TASK_CLOSE_COL = range(len(TASK_COLUMNS))


@dataclass
class JobTaskState:
    """Bir job'un CANLI calisma durumu - Gorevler panelini VE ana tablonun
    Ilerleme sutununu beslemek icin tek bir yerde tutulur (Veeam/vCenter
    tarzi gorev listesi). status: 'running' | 'success' | 'error' | 'cancelled'.
    Satir job bitince OTOMATIK silinmez - kullanici '✕' ile elle kapatana
    kadar panelde kalir (finished_at, tamamlanma zamanini gostermek icin tutulur)."""
    stage: str = "Hazirlaniyor..."
    files_done: int = 0
    files_total: int = 0
    current_file: str = ""
    status: str = "running"
    start_time: float = field(default_factory=time.time)
    finished_at: Optional[float] = None


class TransferWorker(QThread):
    """Bir job'u arka planda (ayri thread'de) calistirir."""

    log_line = Signal(str)
    file_update = Signal(int, int, str)  # files_done, files_total, o an islenen dosya adi
    stage_update = Signal(str)  # job'un su anki asamasi (Taraniyor/Robocopy/Dogrulama vb.)
    finished_result = Signal(object)  # TransferResult

    def __init__(self, job: TransferJob, cred_store: CredentialStore, run_id: str, lock_dir: str):
        super().__init__()
        self.job = job
        self.cred_store = cred_store
        self.run_id = run_id
        self.lock_dir = lock_dir
        self.cancel_event = threading.Event()

    def _on_progress(self, p) -> None:
        # RobocopyProgress'in tek-dosya yuzdesi (p.percent) YOK SAYILIR - tum
        # job'un GERCEK ilerlemesini yansitan files_done/files_total GUI'ye tasinir.
        self.file_update.emit(p.files_done, p.files_total, p.current_file)

    def run(self):
        result = run_transfer(
            self.job, run_id=self.run_id, credential_store=self.cred_store,
            on_log=self.log_line.emit, cancel_event=self.cancel_event,
            lock_dir=self.lock_dir, on_progress=self._on_progress,
            on_stage=self.stage_update.emit,
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

        icon_path = get_app_icon_path()
        if icon_path is not None:
            self.setWindowIcon(QIcon(str(icon_path)))

        # Birden fazla FARKLI job'un GERCEKTEN paralel calisabilmesi icin tek
        # bir self.worker yerine job adina gore bir workers sozlugu tutulur.
        # Ayni job'un iki kez calistirilmasi burada (GUI seviyesinde) ve
        # ayrica run_transfer icindeki JobLock ile (surecler-arasi, ornegin
        # Gorev Zamanlayici ile cakisma icin) engellenir.
        self.workers: dict[str, TransferWorker] = {}
        # Her calisan/az once biten job icin CANLI durum - hem ana tablonun
        # Ilerleme sutununu hem de asagidaki ayri "Gorevler" panelini besler
        # (Veeam/vCenter tarzi gorev listesi - bkz. JobTaskState). Satirlar
        # OTOMATIK silinmez, kullanici '✕' ile elle kapatir.
        self.tasks: dict[str, JobTaskState] = {}
        self._tasks_panel_hidden = False

        # Her job'un KENDI CLI logu ayri tutulur (job'lar arasi karismasin
        # diye) - tabloda hangi job SECILIYSE onun logu gosterilir.
        self.job_logs: dict[str, deque] = {}
        self._displayed_log_job: Optional[str] = None

        self._build_ui()
        self.refresh_grid()

        # Calisan gorevlerin "Sure" sutununu canli tutmak icin - hic calisan
        # gorev yokken CPU harcamamasi icin durdurulur/baslatilir (bkz. _tick_task_durations).
        self._task_timer = QTimer(self)
        self._task_timer.setInterval(1000)
        self._task_timer.timeout.connect(self._tick_task_durations)

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

        self.btn_schedule = QPushButton("Zamanla")
        self.btn_unschedule = QPushButton("Zamanlamayi Kaldir")

        self.btn_theme = QPushButton()
        self.btn_theme.setCheckable(True)

        self.btn_cred = QPushButton("Kimlik Yoneticisi")
        self.btn_refresh = QPushButton("⟳ Yenile")
        self.btn_open_config = QPushButton("Config Ac")
        self.btn_open_logs_folder = QPushButton("Log Klasoru")

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
        for b in (self.btn_schedule, self.btn_unschedule):
            toolbar.addWidget(b)
        add_separator()
        for b in (self.btn_cred, self.btn_refresh, self.btn_open_config, self.btn_open_logs_folder):
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
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(LOG_COL, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(HASH_LOG_COL, QHeaderView.ResizeMode.ResizeToContents)
        self.table.doubleClicked.connect(self._on_edit)
        self.table.itemSelectionChanged.connect(self._on_table_selection_changed)
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

        # Gorevler paneli: calisan/az once biten job'lari SATIR SATIR, kendi
        # asama/ilerleme/sure bilgisiyle gosterir - Veeam'in is oturumu
        # listesine benzer. Log panelinden AYRI tutulur ki cok sayida log
        # satiri arasinda kaybolmadan tek bakista hangi job'un nerede
        # oldugu gorulebilsin. Satirlar KALICIDIR (otomatik silinmez) -
        # kullanici '✕' ile tek tek, veya basliktaki Gizle/Goster butonuyla
        # tum paneli elle acip kapatabilir.
        tasks_header = QHBoxLayout()
        tasks_header.setContentsMargins(0, 0, 0, 0)
        tasks_title = QLabel("Gorevler")
        tasks_title.setStyleSheet("font-weight: bold;")
        tasks_header.addWidget(tasks_title)
        tasks_header.addStretch(1)
        self.btn_toggle_tasks = QPushButton("Gizle")
        self.btn_toggle_tasks.setCheckable(True)
        self.btn_toggle_tasks.setMaximumWidth(90)
        self.btn_toggle_tasks.toggled.connect(self._on_toggle_tasks_panel)
        tasks_header.addWidget(self.btn_toggle_tasks)
        log_layout.addLayout(tasks_header)

        self.tasks_table = QTableWidget(0, len(TASK_COLUMNS))
        self.tasks_table.setHorizontalHeaderLabels(TASK_COLUMNS)
        self.tasks_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tasks_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.tasks_table.setSortingEnabled(False)
        self.tasks_table.verticalHeader().setVisible(False)
        self.tasks_table.verticalHeader().setDefaultSectionSize(26)
        self.tasks_table.horizontalHeader().setSectionResizeMode(TASK_JOB_COL, QHeaderView.ResizeMode.ResizeToContents)
        self.tasks_table.horizontalHeader().setSectionResizeMode(TASK_STAGE_COL, QHeaderView.ResizeMode.Stretch)
        self.tasks_table.horizontalHeader().setSectionResizeMode(TASK_CLOSE_COL, QHeaderView.ResizeMode.ResizeToContents)
        self.tasks_table.setMaximumHeight(160)
        self.tasks_table.setVisible(False)  # hic gorev yokken yer kaplamasin
        log_layout.addWidget(self.tasks_table)

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
        self.btn_schedule.clicked.connect(self._on_schedule)
        self.btn_unschedule.clicked.connect(self._on_unschedule)
        self.btn_cred.clicked.connect(self._on_cred_manager)
        self.btn_refresh.clicked.connect(self.refresh_grid)
        self.btn_open_config.clicked.connect(self._on_open_config)
        self.btn_open_logs_folder.clicked.connect(self._on_open_logs_folder)
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

            # Job su an bu pencereden calistiriliyorsa, Durum sutununda
            # jobs.json'daki KALICI son sonuc yerine CANLI durumu gosteririz -
            # detayli asama/dosya/ilerleme bilgisi artik asagidaki Gorevler
            # panelindedir (bkz. JobTaskState/_update_task_row).
            running = job.name in self.workers and self.workers[job.name].isRunning()
            last_status = "Calisiyor..." if running else (job.last_status or "-")

            values = [
                job.name, job.source_path, job.destination_path,
                str(job.older_than_days), str(job.delete_after_transfer),
                "Evet" if job.enabled else "Hayir", sched, last_run, last_status,
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

            self.table.setCellWidget(row, LOG_COL, self._make_log_button(job.name, job.last_log_file))
            self.table.setCellWidget(row, HASH_LOG_COL, self._make_log_button(job.name, job.last_hash_log, hash_log=True))
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

    def _make_log_button(self, job_name: str, log_path: Optional[str], hash_log: bool = False) -> QPushButton:
        """Tablo satirindaki Log/Hash Log butonu - ilgili SATIRIN job'una
        ait dosyayi acar, tablodaki SECIME bagli degildir."""
        btn = QPushButton("Hash Log" if hash_log else "Log")
        btn.setStyleSheet("padding: 3px 10px;")
        exists = bool(log_path) and os.path.exists(log_path)
        btn.setEnabled(exists)
        btn.setToolTip(log_path if exists else "Henuz olusturulmadi")
        handler = self._on_view_hash_log if hash_log else self._on_view_log
        btn.clicked.connect(partial(handler, job_name))
        return btn

    def _update_run_stop_buttons(self) -> None:
        """Secili job'a gore Calistir/Durdur butonlarini gunceller. Birden
        fazla job ayni anda calisiyor olabilir - her job'un kendi asamasi
        asagidaki Gorevler panelinde gorulur."""
        job = self._selected_job()
        running = bool(job) and job.name in self.workers and self.workers[job.name].isRunning()

        self.btn_run.setEnabled(job is not None and not running)
        self.btn_run.setText("Calisiyor..." if running else "▶ Simdi Calistir")
        self.btn_stop.setEnabled(running)

    # -------------------------------------------------------- Gorevler paneli

    @staticmethod
    def _task_status_visual(theme: str, status: str) -> tuple[str, str]:
        """(etiket metni, arkaplan rengi) dondurur - Veeam'deki is oturumu
        simgelerine benzer sekilde her durum icin ayri renk/ikon kullanilir."""
        if status == "success":
            return "✅ Basarili", status_ok_bg(theme)
        if status == "error":
            return "❌ Hatali", status_error_bg(theme)
        if status == "cancelled":
            return "⏹ Durduruldu", status_cancelled_bg(theme)
        return "⏳ Calisiyor", status_running_bg(theme)

    def _next_task_token(self) -> int:
        self._task_token_counter += 1
        return self._task_token_counter

    def _task_row_for(self, job_name: str) -> Optional[int]:
        for row in range(self.tasks_table.rowCount()):
            item = self.tasks_table.item(row, TASK_JOB_COL)
            if item is not None and item.text() == job_name:
                return row
        return None

    def _ensure_task_row(self, job_name: str) -> int:
        row = self._task_row_for(job_name)
        if row is not None:
            return row
        row = self.tasks_table.rowCount()
        self.tasks_table.insertRow(row)
        self.tasks_table.setItem(row, TASK_JOB_COL, QTableWidgetItem(job_name))
        self.tasks_table.setItem(row, TASK_STATUS_COL, QTableWidgetItem(""))
        self.tasks_table.setItem(row, TASK_STAGE_COL, QTableWidgetItem(""))
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setTextVisible(True)
        self.tasks_table.setCellWidget(row, TASK_PROGRESS_COL, bar)
        self.tasks_table.setItem(row, TASK_DURATION_COL, QTableWidgetItem(""))
        close_btn = QPushButton("✕")
        close_btn.setToolTip("Bu gorev satirini kapat")
        close_btn.setMaximumWidth(28)
        close_btn.clicked.connect(partial(self._on_close_task_clicked, job_name))
        self.tasks_table.setCellWidget(row, TASK_CLOSE_COL, close_btn)
        if not self._task_timer.isActive():
            self._task_timer.start()
        self._update_tasks_panel_visibility()
        return row

    def _update_task_row(self, job_name: str) -> None:
        t = self.tasks.get(job_name)
        if t is None:
            return
        row = self._ensure_task_row(job_name)

        status_text, bg_hex = self._task_status_visual(self.theme, t.status)
        bg = QColor(bg_hex)
        for col in (TASK_JOB_COL, TASK_STATUS_COL, TASK_STAGE_COL, TASK_DURATION_COL):
            item = self.tasks_table.item(row, col)
            if item is not None:
                item.setBackground(bg)
        self.tasks_table.item(row, TASK_STATUS_COL).setText(status_text)

        stage_text = t.stage
        if t.current_file:
            stage_text = f"{stage_text} — {t.current_file}"
        self.tasks_table.item(row, TASK_STAGE_COL).setText(stage_text)

        bar = self.tasks_table.cellWidget(row, TASK_PROGRESS_COL)
        if isinstance(bar, QProgressBar):
            percent = round(t.files_done / t.files_total * 100) if t.files_total else 0
            bar.setValue(min(100, percent))
            bar.setFormat(f"{t.files_done}/{t.files_total} (%p)" if t.files_total else "%p%")

        if t.status == "running":
            elapsed = time.time() - t.start_time
            duration_text = self._format_duration(elapsed)
        else:
            finished_at = t.finished_at or time.time()
            elapsed = finished_at - t.start_time
            finished_clock = datetime.datetime.fromtimestamp(finished_at).strftime("%H:%M:%S")
            duration_text = f"{self._format_duration(elapsed)} (tamamlandi: {finished_clock})"
        self.tasks_table.item(row, TASK_DURATION_COL).setText(duration_text)

        close_btn = self.tasks_table.cellWidget(row, TASK_CLOSE_COL)
        if close_btn is not None:
            close_btn.setVisible(t.status != "running")

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total = max(0, int(seconds))
        minutes, secs = divmod(total, 60)
        return f"{minutes:02d}:{secs:02d}"

    def _tick_task_durations(self) -> None:
        running_names = [name for name, t in self.tasks.items() if t.status == "running"]
        if not running_names:
            self._task_timer.stop()
            return
        for name in running_names:
            self._update_task_row(name)

    def _on_close_task_clicked(self, job_name: str) -> None:
        """Kullanici bir gorev satirini elle kapattiginda cagrilir - calisan
        bir job'un satiri kapatilamaz (buton zaten gizlenir, bkz. _update_task_row)."""
        t = self.tasks.get(job_name)
        if t is not None and t.status == "running":
            return
        self.tasks.pop(job_name, None)
        row = self._task_row_for(job_name)
        if row is not None:
            self.tasks_table.removeRow(row)
        self._update_tasks_panel_visibility()

    def _on_toggle_tasks_panel(self, checked: bool) -> None:
        self._tasks_panel_hidden = checked
        self.btn_toggle_tasks.setText("Goster" if checked else "Gizle")
        self._update_tasks_panel_visibility()

    def _update_tasks_panel_visibility(self) -> None:
        self.tasks_table.setVisible(not self._tasks_panel_hidden and self.tasks_table.rowCount() > 0)

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

    def _log_job(self, job_name: str, line: str, level_override: Optional[str] = None) -> None:
        """Bir job'a ait log satirini o job'un KENDI tamponunda saklar; ekranda
        sadece o an tabloda SECILI olan job'un logu gosterildigi icin, bu
        satir gorunur log'a ANCAK job_name su an gosterilen job ise eklenir."""
        buf = self.job_logs.setdefault(job_name, deque(maxlen=5000))
        buf.append((line, level_override))
        if job_name == self._displayed_log_job:
            self._render_log_line(line, level_override)

    def _render_log_line(self, line: str, level_override: Optional[str] = None) -> None:
        """Log satirini seviyesine gore renklendirerek log_view'a ekler.
        Motordan gelen satirlar zaten "[SEVIYE  ]" onekini icerir (bkz.
        EngineLogger), bu onek aranarak renk secilir. level_override
        verilirse (ozet mesajlari icin, ornegin '=== JOB BASARILI ===')
        dogrudan o renk kullanilir."""
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

    def _show_job_log(self, job_name: Optional[str]) -> None:
        """Log panelini SECILI job'un kendi loguyla doldurur - baska bir
        job'un satirlariyla KARISMAZ. Ayni job zaten gosterilirken tekrar
        cagrilirsa (ornegin refresh_grid ayni secimi geri yukleyince)
        gereksiz yeniden cizimi atlamak icin no-op kisayolu vardir."""
        if job_name == self._displayed_log_job:
            return
        self._displayed_log_job = job_name
        self.log_view.clear()
        if job_name is None:
            return
        for line, level_override in self.job_logs.get(job_name, []):
            self._render_log_line(line, level_override)

    def _on_table_selection_changed(self) -> None:
        self._update_run_stop_buttons()
        self._show_job_log(self._selected_job_name())

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
        self._log_job(job_name, line)

    def _on_worker_progress(self, job_name: str, files_done: int, files_total: int, filename: str) -> None:
        t = self.tasks.get(job_name)
        if t is None:
            return
        t.files_done = files_done
        t.files_total = files_total
        t.current_file = filename
        self._update_task_row(job_name)

    def _on_worker_stage(self, job_name: str, stage_text: str) -> None:
        t = self.tasks.get(job_name)
        if t is None:
            return
        t.stage = stage_text
        self._update_task_row(job_name)

    def _on_run(self):
        job = self._selected_job()
        if not job:
            QMessageBox.warning(self, "Uyari", "Once bir job secin.")
            return
        existing = self.workers.get(job.name)
        if existing is not None and existing.isRunning():
            QMessageBox.warning(self, "Uyari", f"'{job.name}' zaten calisiyor.")
            return

        run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        # NOT: her job'un KENDI log tamponu ayri tutulur (bkz. _log_job) -
        # baska bir job'un ciktisiyla KARISMAZ.
        self._log_job(job.name, f"=== Baslatiliyor [RunId: {run_id}] ===")
        self.tasks[job.name] = JobTaskState(status="running")
        self._update_task_row(job.name)

        worker = TransferWorker(job, self.cred_store, run_id, self.lock_dir)
        worker.log_line.connect(partial(self._on_worker_log, job.name))
        worker.file_update.connect(partial(self._on_worker_progress, job.name))
        worker.stage_update.connect(partial(self._on_worker_stage, job.name))
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

        if result.overall_success:
            self._log_job(job_name, "=== JOB BASARILI ===", level_override="SUCCESS")
            self.status.showMessage(f"'{job_name}' basariyla tamamlandi.")
        elif result.error_message == "Kullanici tarafindan durduruldu":
            self._log_job(job_name, "=== JOB DURDURULDU ===", level_override="WARN")
            self.status.showMessage(f"'{job_name}' durduruldu.")
        else:
            self._log_job(job_name, f"=== JOB HATALI: {result.error_message} ===", level_override="ERROR")
            self.status.showMessage(f"'{job_name}' hata: {result.error_message}")

        status_str = "Basarili" if result.overall_success else "Hatali"
        message = result.error_message or f"{result.verified_files} dosya dogrulandi"
        self.config_store.update_run_result(
            result.job_name, status_str, message, result.log_file, result.hash_log_file,
        )

        # Gorevler panelindeki satir KALICI kalir - kullanici '✕' ile elle
        # kapatana kadar son durumuyla (basarili/hatali/durduruldu + ne
        # zaman tamamlandigi) gorunmeye devam eder (bkz. _update_task_row).
        t = self.tasks.get(job_name)
        if t is not None:
            if result.overall_success:
                t.status = "success"
            elif result.error_message == "Kullanici tarafindan durduruldu":
                t.status = "cancelled"
            else:
                t.status = "error"
            t.stage = "Tamamlandi"
            t.current_file = ""
            t.finished_at = time.time()
            self._update_task_row(job_name)

        self.refresh_grid()

    def _on_view_log(self, job_name: str) -> None:
        job = self.config_store.get_job(job_name)
        if not job or not job.last_log_file or not os.path.exists(job.last_log_file):
            QMessageBox.information(self, "Bilgi", "Henuz log bulunamadi.")
            return
        self._open_in_default_viewer(job.last_log_file)

    def _on_view_hash_log(self, job_name: str) -> None:
        job = self.config_store.get_job(job_name)
        if not job or not job.last_hash_log or not os.path.exists(job.last_hash_log):
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
        run_as_password = None
        if job.run_as_user.upper() != "SYSTEM":
            # SYSTEM disinda bir hesapla schtasks /RP icin gercek bir parola
            # sart - yoksa schtasks etkilesimli parola istemine kilitlenir.
            password, ok = QInputDialog.getText(
                self, "Hesap Parolasi",
                f"'{job.run_as_user}' hesabinin parolasini girin:",
                QLineEdit.EchoMode.Password,
            )
            if not ok or not password:
                QMessageBox.warning(self, "Iptal", "Parola girilmeden gorev olusturulamaz.")
                return
            run_as_password = password
        res = register_scheduled_task(
            job.name, self.exe_path, job.schedule_frequency, job.schedule_time,
            job.schedule_weekly_day, job.run_as_user, self.config_path, run_as_password,
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

    def _on_open_config(self) -> None:
        folder = Path(self.config_path).parent
        if not folder.exists():
            QMessageBox.information(self, "Bilgi", f"Config klasoru bulunamadi: {folder}")
            return
        if sys.platform == "win32":
            os.startfile(str(folder))  # type: ignore[attr-defined]
        else:
            QMessageBox.information(self, "Config Klasoru", str(folder))

    def _on_open_logs_folder(self) -> None:
        # NOT: her job KENDI log_dir'ini kullanabilir (job_editor.py'de
        # ozellestirilebilir) - bu buton VARSAYILAN/ORTAK log klasorunu acar.
        folder = Path(DEFAULT_LOG_DIR)
        if not folder.exists():
            QMessageBox.information(self, "Bilgi", f"Log klasoru henuz olusturulmamis: {folder}")
            return
        if sys.platform == "win32":
            os.startfile(str(folder))  # type: ignore[attr-defined]
        else:
            QMessageBox.information(self, "Log Klasoru", str(folder))

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
            # Qt, hala CALISAN bir QThread yok edilirse ("QThread: Destroyed
            # while thread is still running") CRASH/tanimsiz davranisa yol
            # acar - sabit kisa bir bekleme sonrasi kosulsuz kapatmak yerine
            # thread'ler GERCEKTEN bitene kadar (UI donmasin diye
            # processEvents ile) beklenir; makul surede bitmezlerse kapanis
            # IPTAL edilir (veri bozulmasi/crash riskini almaktansa).
            still_running = list(running)
            deadline = time.monotonic() + 30
            while still_running and time.monotonic() < deadline:
                still_running = [w for w in still_running if not w.wait(200)]
                QCoreApplication.processEvents()
            if still_running:
                QMessageBox.warning(
                    self, "Bekleniyor",
                    f"{len(still_running)} job durduruluyor, henuz tamamlanmadi. "
                    "Birkac saniye sonra tekrar kapatmayi deneyin.",
                )
                event.ignore()
                return
        event.accept()
