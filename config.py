"""
config.py
---------
Job konfigurasyonunun (jobs.json) guvenli okunmasi/yazilmasi.

Python'un json modulu PowerShell'in ConvertTo-Json'undaki "tek elemanli
dizi objeye donusur" hatasina sahip DEGIL - liste her zaman liste olarak
serialize edilir, eleman sayisindan bagimsiz. Yine de disk yazma sirasinda
kesinti (crash, guc kesintisi, ayni anda iki surecin yazmasi) ihtimaline
karsi atomic write + otomatik yedekleme uyguluyoruz.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import dataclasses
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


DEFAULT_LOG_DIR = "C:\\TransferLogs"


@dataclass
class TransferJob:
    """Tek bir transfer job'unun tum ayarlari."""

    name: str
    source_path: str = ""
    destination_path: str = ""
    older_than_days: int = 30
    file_filter: str = "*.*"
    delete_after_transfer: bool = False
    max_retries: int = 3
    disk_warn_threshold_pct: int = 80
    disk_critical_threshold_pct: int = 90
    stop_on_critical_disk: bool = False
    min_free_space_gb: float = 0.0
    log_dir: str = DEFAULT_LOG_DIR
    credential_alias: str = ""
    smtp_server: str = ""
    mail_from: str = ""
    mail_to: list[str] = field(default_factory=list)
    enabled: bool = True
    robocopy_threads: int = 8
    verification_mode: str = "FullHash"  # FullHash | SizeOnly | None

    # Zamanlama
    schedule_enabled: bool = False
    schedule_frequency: str = "Daily"  # Daily | Weekly | Monthly
    schedule_time: str = "02:00"
    schedule_weekly_day: str = "Sunday"
    run_as_user: str = "SYSTEM"

    # Calisma gecmisi (sadece calistirma sonrasi doldurulur)
    last_run: Optional[str] = None
    last_status: Optional[str] = None
    last_message: Optional[str] = None
    last_log_file: Optional[str] = None
    last_hash_log: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TransferJob":
        """
        Eksik/bilinmeyen alanlara karsi toleransli: eski surumlerden kalma
        job kayitlarinda yeni eklenen alanlar olmayabilir (schema evrimi),
        bunlar icin dataclass varsayilanlari kullanilir. Bilinmeyen fazladan
        anahtarlar sessizce yok sayilir (ileri uyumluluk).
        """
        valid_fields = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        if "mail_to" in filtered and filtered["mail_to"] is None:
            filtered["mail_to"] = []
        return cls(**filtered)


class ConfigError(Exception):
    pass


class JobConfigStore:
    """
    jobs.json dosyasini yonetir. Atomic write + .bak yedekleme + bozuk
    dosyadan otomatik kurtarma (self-healing) icerir.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write_raw(self.path, [])

    # ---------- Dahili yardimcilar ----------

    @staticmethod
    def _write_raw(path: Path, data: list[dict]) -> None:
        """Gecici dosyaya yazip atomic rename ile yerine koyar."""
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.stem + "_", suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            # os.replace atomic'tir (ayni dosya sistemi icinde)
            os.replace(tmp_name, str(path))
        except Exception:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
            raise

    @staticmethod
    def _read_raw(path: Path, retries: int = 5) -> list[dict]:
        """Gecici dosya kilidi durumlarina karsi kisa retry uygular."""
        last_err: Optional[Exception] = None
        for attempt in range(retries):
            try:
                if not path.exists():
                    return []
                text = path.read_text(encoding="utf-8")
                if not text.strip():
                    return []
                data = json.loads(text)
                if data is None:
                    return []
                if isinstance(data, dict):
                    # Tek bir obje kaydedilmis olabilir (eski/bozuk veri) - listeye sar
                    return [data]
                if not isinstance(data, list):
                    return []
                return data
            except (OSError, json.JSONDecodeError) as e:
                last_err = e
                time.sleep(0.15 * (attempt + 1))
        if last_err:
            raise ConfigError(f"'{path}' okunamadi ({retries} deneme): {last_err}")
        return []

    def _backup_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".bak")

    # ---------- Genel API ----------

    def load(self) -> list[TransferJob]:
        """
        jobs.json'u okur. Ana dosya bozuk/parse-edilemez ise .bak yedeginden
        otomatik kurtarir ve ana dosyayi kurtarilan veriyle onarir.
        """
        try:
            raw = self._read_raw(self.path)
        except ConfigError:
            raw = []

        raw_text = ""
        if self.path.exists():
            try:
                raw_text = self.path.read_text(encoding="utf-8")
            except OSError:
                pass

        looks_corrupted = (
            len(raw) == 0
            and self.path.exists()
            and raw_text.strip() not in ("", "[]")
        )

        if looks_corrupted:
            bak = self._backup_path()
            if bak.exists():
                try:
                    bak_data = self._read_raw(bak)
                except ConfigError:
                    bak_data = []
                if bak_data:
                    self._write_raw(self.path, bak_data)
                    raw = bak_data

        jobs = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("Name")
            if not name or not str(name).strip():
                continue
            # PowerShell'den kalma PascalCase anahtarlari da destekle (gecis kolayligi)
            normalized = _normalize_keys(item)
            try:
                jobs.append(TransferJob.from_dict(normalized))
            except TypeError:
                continue
        return jobs

    def save(self, jobs: list[TransferJob], allow_empty_overwrite: bool = True) -> bool:
        """
        Job listesini kaydeder. allow_empty_overwrite=False ise, bos bir
        listenin halihazirda DOLU olan bir dosyanin uzerine yazilmasini
        engeller (otomatik/arka plan yazma yollari icin guvenlik onlemi).
        """
        data = [j.to_dict() for j in jobs]

        if not data and not allow_empty_overwrite:
            existing = self._read_raw(self.path) if self.path.exists() else []
            if existing:
                return False

        try:
            self._write_raw(self.path, data)
        except OSError:
            return False

        # Yazdiktan sonra dogrula
        verify = self._read_raw(self.path)
        if len(verify) != len(data):
            return False

        # Basarili yazimdan sonra yedek guncelle
        try:
            shutil.copy2(self.path, self._backup_path())
        except OSError:
            pass  # yedekleme basarisiz olsa da asil yazim basarili sayilir

        return True

    def get_job(self, name: str) -> Optional[TransferJob]:
        for j in self.load():
            if j.name == name:
                return j
        return None

    def upsert_job(self, job: TransferJob) -> bool:
        jobs = self.load()
        jobs = [j for j in jobs if j.name != job.name]
        jobs.append(job)
        return self.save(jobs, allow_empty_overwrite=True)

    def delete_job(self, name: str) -> bool:
        jobs = self.load()
        jobs = [j for j in jobs if j.name != name]
        return self.save(jobs, allow_empty_overwrite=True)

    def update_run_result(
        self,
        name: str,
        status: str,
        message: str,
        log_file: str = "",
        hash_log: str = "",
    ) -> bool:
        """Arka plan calistirma sonrasi durum bilgisini gunceller (bos liste ile ezmeyi engeller)."""
        import datetime

        jobs = self.load()
        if not jobs:
            return False
        found = False
        for j in jobs:
            if j.name == name:
                j.last_run = datetime.datetime.now().isoformat()
                j.last_status = status
                j.last_message = message
                j.last_log_file = log_file
                j.last_hash_log = hash_log
                found = True
        if not found:
            return False
        return self.save(jobs, allow_empty_overwrite=False)


def _normalize_keys(item: dict) -> dict:
    """PowerShell surumunden kalma PascalCase anahtarlari snake_case'e cevirir."""
    mapping = {
        "Name": "name",
        "SourcePath": "source_path",
        "DestinationPath": "destination_path",
        "OlderThanDays": "older_than_days",
        "FileFilter": "file_filter",
        "DeleteAfterTransfer": "delete_after_transfer",
        "MaxRetries": "max_retries",
        "DiskWarnThresholdPct": "disk_warn_threshold_pct",
        "DiskCriticalThresholdPct": "disk_critical_threshold_pct",
        "StopOnCriticalDisk": "stop_on_critical_disk",
        "MinFreeSpaceGB": "min_free_space_gb",
        "LogDir": "log_dir",
        "CredentialAlias": "credential_alias",
        "SmtpServer": "smtp_server",
        "MailFrom": "mail_from",
        "MailTo": "mail_to",
        "Enabled": "enabled",
        "RobocopyThreads": "robocopy_threads",
        "VerificationMode": "verification_mode",
        "ScheduleEnabled": "schedule_enabled",
        "ScheduleFrequency": "schedule_frequency",
        "ScheduleTime": "schedule_time",
        "ScheduleWeeklyDay": "schedule_weekly_day",
        "RunAsUser": "run_as_user",
        "LastRun": "last_run",
        "LastStatus": "last_status",
        "LastMessage": "last_message",
        "LastLogFile": "last_log_file",
        "LastHashLog": "last_hash_log",
    }
    out = {}
    for k, v in item.items():
        out[mapping.get(k, k)] = v
    return out
