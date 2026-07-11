"""
joblock.py
----------
Ayni job'un ayni anda birden fazla kez calismasini engeller.

Neden gerekli: Gorev Zamanlayici bir job'u tetiklerken, kullanici da GUI'den
"Simdi Calistir" derse, ayni kaynak/hedefe IKI robocopy sureci birden yazmaya
calisabilir - bu, dosya bütünlügünü bozabilir (yarim yazilmis dosyalarin
uzerine ikinci surecin de yazmaya calismasi gibi durumlar).

Kilit dosyasi icine PID + baslangic zamani yazilir. Eger onceki kilit
"bayat" ise (islem artik calismiyor VEYA cok uzun suredir - crash sonrasi
temizlenmemis kilit ihtimaline karsi) otomatik olarak temizlenip devam
edilir; gercekten calisan baska bir surec varsa kilit reddedilir.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional


STALE_LOCK_HOURS = 12  # Bu sureden eski kilitler, sahibi olmasa bile bayat sayilir


class JobLockError(Exception):
    """Job zaten calisirken tekrar baslatilmaya calisildiginda firlatilir."""
    pass


def _pid_is_running(pid: int) -> bool:
    """Verilen PID'nin hala calisip calismadigini kontrol eder (cross-platform, best-effort)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    else:
        # os.kill(pid, 0) sinyal GONDERMEZ, sadece surecin varligini kontrol eder.
        # ProcessLookupError (ESRCH) ve PermissionError (EPERM) ikisi de OSError
        # alt siniflaridir ama TAM TERSI anlamlara gelir - ayri ayri yakalanmali:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False  # Surec kesinlikle yok
        except PermissionError:
            return True   # Surec var, sadece sinyal gonderme iznimiz yok
        except OSError:
            return False  # Beklenmeyen durum - guvenli tarafta kal (bayat say)


class JobLock:
    """
    Kullanim:
        with JobLock(lock_dir, job_name):
            ... transferi calistir ...

    Kilit alinamazsa JobLockError firlatilir (baska bir calisma zaten surüyor).
    """

    def __init__(self, lock_dir: str | Path, job_name: str):
        from engine.logutil import sanitize_filename
        self.lock_dir = Path(lock_dir)
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.lock_dir / f"{sanitize_filename(job_name)}.lock"
        self.job_name = job_name
        self._acquired = False

    def _read_lock_info(self) -> Optional[dict]:
        try:
            return json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def _is_stale(self, info: dict) -> bool:
        pid = info.get("pid", 0)
        started = info.get("started_ts", 0)
        age_hours = (time.time() - started) / 3600
        if age_hours > STALE_LOCK_HOURS:
            return True
        if not _pid_is_running(pid):
            return True
        return False

    def acquire(self) -> None:
        if self.lock_path.exists():
            info = self._read_lock_info()
            if info is not None and not self._is_stale(info):
                raise JobLockError(
                    f"Job '{self.job_name}' zaten calisiyor "
                    f"(PID={info.get('pid')}, baslangic={info.get('started_at')})."
                )
            # Bayat kilit (veya okunamayan/bozuk kilit dosyasi) - once fiziksel
            # olarak silinmeli, aksi halde asagidaki O_CREAT|O_EXCL zaten var
            # olan dosyayla catisir ve yanlislikla "yaris durumu" hatasi verir.
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass  # Bu arada baska bir surec zaten temizlemis olabilir, sorun degil

        # Atomic olusturma: dosya zaten varsa (yarisdan bir surecle gercek race durumu) hata verir
        try:
            fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # Cok nadir bir race: iki surec ayni anda kilidi almaya calisti.
            # Kaybeden taraf net bir hata alsin.
            raise JobLockError(f"Job '{self.job_name}' icin kilit alinamadi (yaris durumu).")

        info = {
            "pid": os.getpid(),
            "started_ts": time.time(),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(info))
        self._acquired = True

    def release(self) -> None:
        if self._acquired and self.lock_path.exists():
            try:
                self.lock_path.unlink()
            except OSError:
                pass
        self._acquired = False

    def __enter__(self) -> "JobLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


def is_job_locked(lock_dir: str | Path, job_name: str) -> bool:
    """GUI'de "calisiyor mu?" gostergesi icin - kilit almadan sadece kontrol eder."""
    from engine.logutil import sanitize_filename
    lock_path = Path(lock_dir) / f"{sanitize_filename(job_name)}.lock"
    if not lock_path.exists():
        return False
    try:
        info = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    age_hours = (time.time() - info.get("started_ts", 0)) / 3600
    if age_hours > STALE_LOCK_HOURS:
        return False
    return _pid_is_running(info.get("pid", 0))
