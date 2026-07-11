"""
scanner.py
----------
Yuksek performansli dosya tarama motoru.

KOK SORUN (12 saat surme):
Path.rglob() + ayri is_file()/stat() cagrilari, dosya basina 3-4 AYRI
stat() islemi yapiyordu. Windows'ta dizin listeleme API'si (FindFirstFile/
FindNextFile) zaten dosya boyutu+tarihini TEK seferde donduruyor (SMB
uzerinden bile), ama pathlib.Path bu onbellegi ATIYOR - her .stat() cagrisi
YENI bir round-trip. Milyonlarca dosyada, ozellikle UNC/network yollarda,
bu 3-4x gereksiz ag trafigi = saatlerce surme demek.

COZUM:
- os.scandir() DirEntry nesneleri kullanilir - entry.stat() Windows'ta
  dizin listelemesi sirasinda ZATEN alinan veriyi yeniden kullanir,
  EK bir sistem cagrisi/ag round-trip'i YAPMAZ (Python resmi belgeleri:
  "On Windows, no extra system call is needed").
- Alt dizinler bir kuyruga (queue) alinip COKLU THREAD ile paralel
  taranir - ag gecikmesi (latency) bircok istegin AYNI ANDA ucuste
  olmasiyla ortulur (robocopy'nin /MT mantiginin taramaya uygulanmis hali).
- Ilerleme periyodik olarak raporlanir - kullanici "donmus mu?" diye
  saatlerce beklemek zorunda kalmaz.
"""
from __future__ import annotations

import fnmatch
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class ScannedFile:
    path: str        # tam yol
    rel_path: str     # kok dizine gore relatif yol
    mtime: float
    size: int


@dataclass
class ScanResult:
    matched: list[ScannedFile] = field(default_factory=list)
    skipped_count: int = 0       # yas filtresini gecemeyen (mtime_cutoff verildiyse)
    error_count: int = 0         # erisilemeyen dosya/dizin sayisi
    total_scanned: int = 0
    cancelled: bool = False


ScanProgressCallback = Callable[[int, int], None]  # (total_scanned, matched_count)


def scan_directory(
    root_path: str,
    file_filter: str = "*.*",
    mtime_cutoff: Optional[float] = None,
    max_workers: int = 16,
    progress_callback: Optional[ScanProgressCallback] = None,
    progress_interval: int = 25000,
    cancel_event: Optional[threading.Event] = None,
) -> ScanResult:
    """
    root_path altini TEK GECIS + PARALEL olarak tarar.

    mtime_cutoff verilirse: sadece bu epoch-saniyeden ESKI dosyalar
        'matched' listesine girer, digerleri skipped_count'a eklenir
        (kaynak tarafinda yas filtresi icin).
    mtime_cutoff None ise: TUM eslesen dosyalar 'matched' listesine girer
        (hedef dizin taramasi icin - yas filtresi anlamsizdir).

    Windows'ta entry.stat() cagrisi EK sistem cagrisi yapmadigi icin
    (DirEntry'nin FindFirstFile/FindNextFile'dan gelen onbellegini
    kullanir), bu fonksiyon dosya basina SADECE 1 dizin-listeleme
    round-trip'i gerektirir - eski rglob+stat zincirindeki 3-4'e kiyasla.
    """
    root_path = os.path.normpath(root_path)
    root_len = len(root_path.rstrip(os.sep)) + 1  # relpath icin on-hesap

    dir_queue: "queue.Queue[str]" = queue.Queue()
    dir_queue.put(root_path)

    result = ScanResult()
    lock = threading.Lock()
    last_reported = [0]
    done_event = threading.Event()

    def compute_rel(full_path: str) -> str:
        if len(full_path) > root_len:
            return full_path[root_len:]
        return os.path.basename(full_path)

    def worker():
        while not done_event.is_set():
            if cancel_event is not None and cancel_event.is_set():
                result.cancelled = True
                _drain_queue(dir_queue)
                return
            try:
                current_dir = dir_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            local_matched: list[ScannedFile] = []
            local_skipped = 0
            local_errors = 0
            local_scanned = 0
            local_new_dirs: list[str] = []

            try:
                with os.scandir(current_dir) as it:
                    for entry in it:
                        if cancel_event is not None and cancel_event.is_set():
                            break
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                local_new_dirs.append(entry.path)
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                        except OSError:
                            local_errors += 1
                            continue

                        if not fnmatch.fnmatch(entry.name, file_filter):
                            continue

                        local_scanned += 1
                        try:
                            # Windows'ta bu cagri EK round-trip yapmaz -
                            # DirEntry'nin scandir sirasinda zaten aldigi
                            # onbellegi kullanir.
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            local_errors += 1
                            continue

                        if mtime_cutoff is not None and st.st_mtime >= mtime_cutoff:
                            local_skipped += 1
                            continue

                        local_matched.append(ScannedFile(
                            path=entry.path,
                            rel_path=compute_rel(entry.path),
                            mtime=st.st_mtime,
                            size=st.st_size,
                        ))
            except OSError:
                local_errors += 1

            for d in local_new_dirs:
                dir_queue.put(d)

            with lock:
                result.matched.extend(local_matched)
                result.skipped_count += local_skipped
                result.error_count += local_errors
                result.total_scanned += local_scanned
                if progress_callback and (result.total_scanned - last_reported[0]) >= progress_interval:
                    last_reported[0] = result.total_scanned
                    progress_callback(result.total_scanned, len(result.matched))

            dir_queue.task_done()

    threads = [threading.Thread(target=worker, daemon=True, name=f"scan-{i}") for i in range(max_workers)]
    for t in threads:
        t.start()

    dir_queue.join()
    done_event.set()
    for t in threads:
        t.join(timeout=2)

    if progress_callback:
        progress_callback(result.total_scanned, len(result.matched))

    return result


def _drain_queue(q: "queue.Queue") -> None:
    """Iptal durumunda kuyrugu bosaltir ki join() kilitlenmesin."""
    try:
        while True:
            q.get_nowait()
            q.task_done()
    except queue.Empty:
        pass


def build_rel_index(files: list[ScannedFile]) -> dict[str, ScannedFile]:
    """rel_path (kucuk harfe cevrilmis) -> ScannedFile eslemesi olusturur.
    Hedef dizin taramasi sonrasi O(1) bellek-ici arama icin kullanilir -
    dosya basina ayri bir os.path.exists()/stat() cagrisi GEREKTIRMEZ."""
    return {f.rel_path.lower(): f for f in files}
