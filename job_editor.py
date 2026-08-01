"""
job_editor.py
-------------
Yeni job olusturma / mevcut job duzenleme dialog'u.

PySide6'nin QFormLayout + QScrollArea kombinasyonu, PowerShell WinForms
surumunde yasadigimiz AutoScroll guvenilmezligi sorununu ortadan kaldirir -
Qt'nin layout sistemi icerik boyutunu dogru hesaplar, manuel piksel
konumlandirmaya (Location/Point) hic gerek yoktur.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QScrollArea, QWidget,
    QLineEdit, QSpinBox, QDoubleSpinBox, QCheckBox, QComboBox, QPushButton,
    QMessageBox, QGroupBox, QDialogButtonBox, QInputDialog,
)

from engine.config import TransferJob
from engine.credentials import CredentialStore
from engine.logutil import get_disk_info, format_size


class JobEditorDialog(QDialog):
    def __init__(self, cred_store: CredentialStore, existing_job: Optional[TransferJob] = None,
                 existing_names: Optional[list[str]] = None, parent=None):
        super().__init__(parent)
        self.cred_store = cred_store
        self.existing_job = existing_job
        self.is_edit = existing_job is not None
        self.existing_names = set(existing_names or [])

        self.setWindowTitle(f"Duzenle: {existing_job.name}" if self.is_edit else "Yeni Job")
        self.setMinimumWidth(560)
        self.resize(600, 720)

        self._build_ui()
        if self.is_edit:
            self._populate_from_job(existing_job)
        self._refresh_credential_combo()

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        outer = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        form = QFormLayout(content)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # --- Temel alanlar ---
        self.name_edit = QLineEdit()
        self.name_edit.setToolTip("Benzersiz isim (kaydedince degistirilemez)")
        form.addRow("Job Adi:", self.name_edit)

        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText(r"D:\Klasor veya \\Sunucu\Paylasim\Klasor")
        form.addRow("Kaynak Yol:", self.source_edit)

        self.dest_edit = QLineEdit()
        self.dest_edit.setPlaceholderText(r"\\HedefSunucu\Paylasim\Klasor")
        form.addRow("Hedef Yol:", self.dest_edit)

        self.age_spin = QSpinBox()
        self.age_spin.setRange(0, 3650)
        self.age_spin.setValue(30)
        self.age_spin.setToolTip("Bu degerden DAHA ESKI dosyalar tasinir")
        form.addRow("Yas Filtresi (gun):", self.age_spin)

        self.filter_edit = QLineEdit("*.*")
        form.addRow("Dosya Filtresi:", self.filter_edit)

        self.delete_check = QCheckBox("Dogrulama basarili olursa kaynaktan sil")
        form.addRow("Kaynagi Sil:", self.delete_check)

        self.retries_spin = QSpinBox()
        self.retries_spin.setRange(0, 20)
        self.retries_spin.setValue(3)
        form.addRow("Tekrar Sayisi:", self.retries_spin)

        # --- Performans ---
        perf_group = QGroupBox("Performans")
        perf_form = QFormLayout(perf_group)

        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(1, 128)
        self.threads_spin.setValue(8)
        self.threads_spin.setToolTip("Kucuk/cok sayida dosyada artir (16-32). Buyuk dosyalarda dusuk tut.")
        perf_form.addRow("Robocopy Thread (/MT):", self.threads_spin)

        self.verify_combo = QComboBox()
        self.verify_combo.addItems(["FullHash", "SizeOnly", "None"])
        self.verify_combo.setToolTip(
            "FullHash=SHA256 (guvenli) | SizeOnly=boyut kontrolu (hizli) | None=dogrulama yok"
        )
        perf_form.addRow("Dogrulama Modu:", self.verify_combo)

        form.addRow(perf_group)

        # --- Disk ---
        disk_group = QGroupBox("Disk Kontrolu")
        disk_form = QFormLayout(disk_group)

        self.warn_spin = QSpinBox()
        self.warn_spin.setRange(1, 99)
        self.warn_spin.setValue(80)
        disk_form.addRow("Disk Uyari (%):", self.warn_spin)

        self.crit_spin = QSpinBox()
        self.crit_spin.setRange(1, 99)
        self.crit_spin.setValue(90)
        disk_form.addRow("Disk Kritik (%):", self.crit_spin)

        self.stop_crit_check = QCheckBox("Kritik esikte transferi baslamadan iptal et")
        disk_form.addRow("Kritikte Durdur:", self.stop_crit_check)

        self.min_free_spin = QDoubleSpinBox()
        self.min_free_spin.setRange(0, 99999)
        self.min_free_spin.setDecimals(1)
        self.min_free_spin.setToolTip("0 = kapali. Transfer sonrasi bu kadar alan kalmazsa durdurur/uyarir.")
        disk_form.addRow("Min. Bos Alan (GB):", self.min_free_spin)

        form.addRow(disk_group)

        # --- Genel ---
        self.logdir_edit = QLineEdit(r"C:\TransferLogs")
        form.addRow("Log Klasoru:", self.logdir_edit)

        cred_row = QHBoxLayout()
        self.cred_combo = QComboBox()
        self.cred_combo.setEditable(True)
        self.cred_combo.setToolTip("Kimlik Yoneticisi'nde kayitli bir alias secin veya yeni yazin. Bos='mevcut oturum'")
        cred_row.addWidget(self.cred_combo, 1)
        btn_new_cred = QPushButton("+ Yeni")
        btn_new_cred.clicked.connect(self._on_new_credential)
        cred_row.addWidget(btn_new_cred)
        btn_refresh_cred = QPushButton("Yenile")
        btn_refresh_cred.clicked.connect(self._refresh_credential_combo)
        cred_row.addWidget(btn_refresh_cred)
        form.addRow("Kimlik Aliasi:", cred_row)

        self.enabled_check = QCheckBox("Job aktif")
        self.enabled_check.setChecked(True)
        form.addRow("Job Aktif:", self.enabled_check)

        # --- Mail ---
        mail_group = QGroupBox("Mail Uyarilari (opsiyonel)")
        mail_form = QFormLayout(mail_group)
        self.smtp_edit = QLineEdit()
        self.smtp_edit.setToolTip("Bos birakilirsa mail gonderilmez")
        mail_form.addRow("SMTP Sunucu:", self.smtp_edit)
        self.mail_from_edit = QLineEdit()
        mail_form.addRow("Gonderen:", self.mail_from_edit)
        self.mail_to_edit = QLineEdit()
        self.mail_to_edit.setPlaceholderText("adres1@ornek.com, adres2@ornek.com")
        mail_form.addRow("Alici:", self.mail_to_edit)
        form.addRow(mail_group)

        # --- Zamanlama ---
        sched_group = QGroupBox("Zamanlama (Gorev Zamanlayici)")
        sched_form = QFormLayout(sched_group)

        self.sched_enabled_check = QCheckBox("Zamanlama aktif")
        sched_form.addRow("Zamanlama:", self.sched_enabled_check)

        self.sched_freq_combo = QComboBox()
        self.sched_freq_combo.addItems(["Daily", "Weekly", "Monthly"])
        sched_form.addRow("Siklik:", self.sched_freq_combo)

        self.sched_time_edit = QLineEdit("02:00")
        self.sched_time_edit.setToolTip("Format: HH:mm")
        sched_form.addRow("Saat:", self.sched_time_edit)

        self.sched_day_combo = QComboBox()
        self.sched_day_combo.addItems(
            ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        )
        sched_form.addRow("Haftalik Gun:", self.sched_day_combo)

        self.run_as_edit = QLineEdit("SYSTEM")
        self.run_as_edit.setToolTip("SYSTEM veya DOMAIN\\kullaniciadi")
        sched_form.addRow("Calistiran Hesap:", self.run_as_edit)

        form.addRow(sched_group)

        # --- Test Paths butonu ---
        test_btn = QPushButton("Yollari Test Et")
        test_btn.clicked.connect(self._on_test_paths)
        form.addRow(test_btn)

        # --- Alt butonlar ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        save_btn = buttons.button(QDialogButtonBox.StandardButton.Save)
        if save_btn:
            save_btn.setProperty("primary", True)
            save_btn.setText("Kaydet")
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_btn:
            cancel_btn.setText("Vazgec")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # -------------------------------------------------------- Yardimcilar

    def _refresh_credential_combo(self):
        current = self.cred_combo.currentText()
        self.cred_combo.clear()
        self.cred_combo.addItem("")  # bos = mevcut oturum
        for cred in self.cred_store.list_all():
            self.cred_combo.addItem(cred.alias)
        idx = self.cred_combo.findText(current)
        if idx >= 0:
            self.cred_combo.setCurrentIndex(idx)
        else:
            self.cred_combo.setCurrentText(current)

    def _on_new_credential(self):
        alias, ok = QInputDialog.getText(self, "Yeni Kimlik", "Alias adi (ornek: BackupServer01):")
        if not ok or not alias.strip():
            return
        alias = alias.strip()
        username, ok = QInputDialog.getText(self, "Kullanici Adi", "Kullanici adi (DOMAIN\\user):")
        if not ok or not username.strip():
            return
        password, ok = QInputDialog.getText(
            self, "Parola", "Parola:", QLineEdit.EchoMode.Password
        )
        if not ok:
            return
        self.cred_store.save(alias, username.strip(), password)
        self._refresh_credential_combo()
        self.cred_combo.setCurrentText(alias)
        QMessageBox.information(self, "Tamam", f"Kimlik kaydedildi: {alias}")

    def _on_test_paths(self):
        src, dst = self.source_edit.text().strip(), self.dest_edit.text().strip()
        src_ok = os.path.exists(src) if src else False
        dst_ok = os.path.exists(dst) if dst else False
        msg = f"Kaynak ({src}):\n  {'OK - erisilebilir' if src_ok else 'HATA - erisilemiyor'}\n\n"
        msg += f"Hedef ({dst}):\n  {'OK - erisilebilir' if dst_ok else 'Henuz yok - transfer sirasinda olusturulur'}"
        if dst_ok:
            di = get_disk_info(dst)
            if di:
                msg += f"\n\nHedef disk: {format_size(di.free_bytes)} bos / %{di.used_pct} dolu"
        QMessageBox.information(self, "Baglanti Testi", msg)

    def _populate_from_job(self, job: TransferJob):
        self.name_edit.setText(job.name)
        self.name_edit.setEnabled(False)  # isim kaydedince degistirilemez
        self.source_edit.setText(job.source_path)
        self.dest_edit.setText(job.destination_path)
        self.age_spin.setValue(job.older_than_days)
        self.filter_edit.setText(job.file_filter)
        self.delete_check.setChecked(job.delete_after_transfer)
        self.retries_spin.setValue(job.max_retries)
        self.threads_spin.setValue(job.robocopy_threads)
        idx = self.verify_combo.findText(job.verification_mode)
        if idx >= 0:
            self.verify_combo.setCurrentIndex(idx)
        self.warn_spin.setValue(job.disk_warn_threshold_pct)
        self.crit_spin.setValue(job.disk_critical_threshold_pct)
        self.stop_crit_check.setChecked(job.stop_on_critical_disk)
        self.min_free_spin.setValue(job.min_free_space_gb)
        self.logdir_edit.setText(job.log_dir)
        self.cred_combo.setCurrentText(job.credential_alias)
        self.enabled_check.setChecked(job.enabled)
        self.smtp_edit.setText(job.smtp_server)
        self.mail_from_edit.setText(job.mail_from)
        self.mail_to_edit.setText(", ".join(job.mail_to))
        self.sched_enabled_check.setChecked(job.schedule_enabled)
        idx = self.sched_freq_combo.findText(job.schedule_frequency)
        if idx >= 0:
            self.sched_freq_combo.setCurrentIndex(idx)
        self.sched_time_edit.setText(job.schedule_time)
        idx = self.sched_day_combo.findText(job.schedule_weekly_day)
        if idx >= 0:
            self.sched_day_combo.setCurrentIndex(idx)
        self.run_as_edit.setText(job.run_as_user)

    def _on_save(self):
        name = self.name_edit.text().strip()
        source = self.source_edit.text().strip()
        dest = self.dest_edit.text().strip()

        if not name:
            QMessageBox.warning(self, "Hata", "Job adi bos olamaz.")
            return
        if not source or not dest:
            QMessageBox.warning(self, "Hata", "Kaynak ve hedef yol zorunludur.")
            return
        if not self.is_edit and name.lower() in {n.lower() for n in self.existing_names}:
            QMessageBox.warning(self, "Hata", f"Bu isimde bir job zaten var: {name}")
            return

        # Ayni/ic-ice yollar robocopy'yi sonsuz/yikici bir kopyalama dongusune
        # sokabilir (ozellikle Kaynagi Sil aktifken veri kaybina yol acar) -
        # normalize edilmis yol karsilastirmasiyla erkenden engellenir.
        norm_source = os.path.normcase(os.path.normpath(source))
        norm_dest = os.path.normcase(os.path.normpath(dest))
        if norm_source == norm_dest:
            QMessageBox.warning(
                self, "Hata",
                "Kaynak ve hedef yol AYNI olamaz - bu, dosyalarin kendi uzerine "
                "kopyalanmasina ve (Kaynagi Sil aktifse) veri kaybina yol acar."
            )
            return
        sep = os.sep
        # normpath, surucu KOKU (orn. "D:\") icin sondaki ayraci KORUR - kosulsuz
        # "+ sep" eklemek bu durumda CIFT ayraca ("D:\\") yol acip startswith
        # kontrolunu HICBIR ZAMAN eslesmeyecek sekilde bozardi (tam da kok surucu
        # kaynak/hedef oldugunda yakalanmasi gereken tehlikeli durum).
        norm_source_sep = norm_source if norm_source.endswith(sep) else norm_source + sep
        norm_dest_sep = norm_dest if norm_dest.endswith(sep) else norm_dest + sep
        if norm_dest_sep.startswith(norm_source_sep):
            QMessageBox.warning(
                self, "Hata",
                "Hedef yol, kaynak yolun ICINDE olamaz - bu, robocopy'nin kopyaladigi "
                "dosyalari tekrar tekrar kopyalamaya calismasina yol acar."
            )
            return
        if norm_source_sep.startswith(norm_dest_sep):
            QMessageBox.warning(
                self, "Hata",
                "Kaynak yol, hedef yolun ICINDE olamaz - bu yapilandirma guvenli degildir."
            )
            return

        if self.warn_spin.value() >= self.crit_spin.value():
            QMessageBox.warning(
                self, "Hata",
                f"Disk Uyari esigi (%{self.warn_spin.value()}), Disk Kritik esiginden "
                f"(%{self.crit_spin.value()}) KUCUK olmalidir; aksi halde uyari hicbir "
                "zaman tetiklenmeden dogrudan kritik duruma gecilir."
            )
            return

        if self.sched_enabled_check.isChecked() and not re.match(
            r"^([01]\d|2[0-3]):([0-5]\d)$", self.sched_time_edit.text().strip()
        ):
            QMessageBox.warning(
                self, "Hata", "Zamanlama saati gecersiz. Format: HH:mm (ornek: 02:00, 23:45)."
            )
            return

        smtp = self.smtp_edit.text().strip()
        mail_from = self.mail_from_edit.text().strip()
        mail_to = [x.strip() for x in self.mail_to_edit.text().split(",") if x.strip()]
        if smtp or mail_from or mail_to:
            email_re = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
            if not smtp:
                QMessageBox.warning(self, "Hata", "Alici/Gonderen girildi ama SMTP Sunucu bos - mail hicbir zaman gonderilmez.")
                return
            if not mail_from:
                QMessageBox.warning(self, "Hata", "SMTP Sunucu girildiyse Gonderen adresi de zorunludur.")
                return
            if not mail_to:
                QMessageBox.warning(self, "Hata", "SMTP Sunucu girildiyse en az bir Alici adresi zorunludur.")
                return
            if not email_re.match(mail_from):
                QMessageBox.warning(self, "Hata", f"Gonderen adresi gecersiz: {mail_from}")
                return
            invalid_to = [addr for addr in mail_to if not email_re.match(addr)]
            if invalid_to:
                QMessageBox.warning(self, "Hata", "Gecersiz alici adres(ler)i: " + ", ".join(invalid_to))
                return

        # Robocopy, kaynak klasorun ICERIGINI hedefe kopyalar; kaynak klasor adiyla
        # otomatik bir alt klasor OLUSTURMAZ. Son klasor adlari uyusmuyorsa bu, dosyalarin
        # beklenmedik sekilde hedefin KOKUNE dagilmasina yol acan yaygin bir yapilandirma
        # hatasidir - kaydetmeden once kullaniciyi uyarip onay al.
        src_leaf = os.path.basename(source.rstrip("\\/"))
        dst_leaf = os.path.basename(dest.rstrip("\\/"))
        if src_leaf and dst_leaf and src_leaf.lower() != dst_leaf.lower():
            reply = QMessageBox.question(
                self, "Klasor adi uyusmuyor",
                f"Kaynak yolun son klasoru \"{src_leaf}\" iken hedef yolun son klasoru \"{dst_leaf}\".\n\n"
                f"Robocopy kaynak klasorun ICERIGINI dogrudan hedefe kopyalar; \"{src_leaf}\" adinda "
                "otomatik bir alt klasor OLUSTURMAZ. Eger hedefte de boyle bir alt klasor "
                f"istiyorsaniz, Hedef Yol'un sonuna \"{src_leaf}\" eklemelisiniz.\n\n"
                "Yine de bu sekilde kaydetmek istiyor musunuz?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self.accept()

    def get_job(self) -> TransferJob:
        """Dialog kabul edildikten sonra sonucu almak icin cagirilir."""
        mail_to = [x.strip() for x in self.mail_to_edit.text().split(",") if x.strip()]

        job = TransferJob(
            name=self.name_edit.text().strip() if not self.is_edit else self.existing_job.name,
            source_path=self.source_edit.text().strip(),
            destination_path=self.dest_edit.text().strip(),
            older_than_days=self.age_spin.value(),
            file_filter=self.filter_edit.text().strip() or "*.*",
            delete_after_transfer=self.delete_check.isChecked(),
            max_retries=self.retries_spin.value(),
            disk_warn_threshold_pct=self.warn_spin.value(),
            disk_critical_threshold_pct=self.crit_spin.value(),
            stop_on_critical_disk=self.stop_crit_check.isChecked(),
            min_free_space_gb=self.min_free_spin.value(),
            log_dir=self.logdir_edit.text().strip() or r"C:\TransferLogs",
            credential_alias=self.cred_combo.currentText().strip(),
            smtp_server=self.smtp_edit.text().strip(),
            mail_from=self.mail_from_edit.text().strip(),
            mail_to=mail_to,
            enabled=self.enabled_check.isChecked(),
            robocopy_threads=self.threads_spin.value(),
            verification_mode=self.verify_combo.currentText(),
            schedule_enabled=self.sched_enabled_check.isChecked(),
            schedule_frequency=self.sched_freq_combo.currentText(),
            schedule_time=self.sched_time_edit.text().strip() or "02:00",
            schedule_weekly_day=self.sched_day_combo.currentText(),
            run_as_user=self.run_as_edit.text().strip() or "SYSTEM",
        )
        if self.is_edit:
            job.last_run = self.existing_job.last_run
            job.last_status = self.existing_job.last_status
            job.last_message = self.existing_job.last_message
            job.last_log_file = self.existing_job.last_log_file
            job.last_hash_log = self.existing_job.last_hash_log
        return job
