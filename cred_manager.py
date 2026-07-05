"""
cred_manager.py
----------------
Kimlik bilgisi yoneticisi dialog'u: kayitli alias'lari listeler, ekler, siler.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QMessageBox, QInputDialog, QLineEdit, QHeaderView,
)

from engine.credentials import CredentialStore


class CredentialManagerDialog(QDialog):
    def __init__(self, cred_store: CredentialStore, parent=None):
        super().__init__(parent)
        self.cred_store = cred_store
        self.setWindowTitle("Kimlik Bilgisi Yoneticisi")
        self.resize(480, 360)

        layout = QVBoxLayout(self)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Alias", "Kullanici Adi"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Ekle")
        add_btn.clicked.connect(self._on_add)
        btn_row.addWidget(add_btn)

        remove_btn = QPushButton("Sil")
        remove_btn.clicked.connect(self._on_remove)
        btn_row.addWidget(remove_btn)

        btn_row.addStretch(1)

        close_btn = QPushButton("Kapat")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)

        layout.addLayout(btn_row)

        self.refresh()

    def refresh(self):
        creds = self.cred_store.list_all()
        self.table.setRowCount(len(creds))
        for row, cred in enumerate(creds):
            self.table.setItem(row, 0, QTableWidgetItem(cred.alias))
            self.table.setItem(row, 1, QTableWidgetItem(cred.username))

    def _on_add(self):
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
        self.refresh()
        QMessageBox.information(self, "Tamam", f"Kimlik kaydedildi: {alias}")

    def _on_remove(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.warning(self, "Uyari", "Once bir kimlik secin.")
            return
        alias = self.table.item(row, 0).text()
        reply = QMessageBox.question(
            self, "Onay", f"'{alias}' silinsin mi?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.cred_store.remove(alias)
            self.refresh()
