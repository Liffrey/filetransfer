"""
robocopy_runner.py
-------------------
Robocopy'yi canli ilerleme yuzdesi TAKIP EDEREK calistirir.

NEDEN AYRI MODUL / OZEL YAKLASIM:
Robocopy'nin ilerleme yuzdesi (/NP KALDIRILDIGINDA gorunur) tek bir satiri
"\r" (satir basi, YENI SATIR DEGIL) ile surekli guncelleyerek konsola yazilir.
Robocopy'nin KENDI /LOG+ mekanizmasi kullanilirsa, bu yuzde satirlari log
DOSYASINA da (konsoldakiyle AYNI sekilde) yaziliyor - yani /NP kaldirilinca
hem konsolda ilerleme gorunuyor HEM DE log dosyasi yuzlerce/binlerce
"%45.2" turu satirla sisiyor (bu, resmi kaynaklarca da dogrulanmis bilinen
bir robocopy davranisi).

Bu yuzden robocopy'nin KENDI /LOG+ ozelligini HIC KULLANMIYORUZ. Bunun
yerine:
  1. robocopy /LOG+ OLMADAN calistirilir (varsayilan: TUM cikti stdout'a gider)
  2. Cikti SATIR SATIR bu modul tarafindan okunur (ayri bir thread'de, ana
     dongu cancel_event'i sik sik kontrol edebilsin diye)
  3. Yuzde satirlari AYRISTIRILIP ilerleme callback'ine iletilir - LOG
     DOSYASINA YAZILMAZ (boylece log sismesi sorunu hic olusmaz)
  4. Diger TUM satirlar (dosya adlari, basliklar, hatalar, ozet) kendi
     yonettigimiz log dosyasina yazilir - icerik robocopy'nin /LOG+'i ile
     ayni ama yuzde spam'i FILTRELENMIS halde
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


# Robocopy'nin ilerleme satiri: sadece bosluk + sayi + '%' + bosluk icerir.
# Ornekler: " 45.2%", "100%", "  0%"
_PERCENT_RE = re.compile(r'^\s*(\d{1,3}(?:\.\d+)?)\s*%\s*$')

# "Yeni dosya kopyalaniyor" satiri (best-effort - robocopy surumune gore
# bicimi hafif degisebilir, bu yuzden esnek/genis bir kalip kullanilir).
_FILE_LINE_RE = re.compile(
    r'^\s*(?:New File|Newer|Changed|\*EXTRA File|Older|Tweaked)\s+\S+\s+(.+?)\s*$',
    re.IGNORECASE,
)

# Robocopy hata satiri: "<tarih> <saat> ERROR <kod> (0xNNNNNNNN) <islem+yol>"
# Ornek: "2024/06/10 10:15:23 ERROR 32 (0x00000020) Copying File D:\src\a.txt"
_ERROR_LINE_RE = re.compile(
    r'^\s*\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}\s+ERROR\s+\d+\s+\(0x[0-9A-Fa-f]+\)\s+(.+?)\s*$',
    re.IGNORECASE,
)
# Robocopy'nin /W bekleme satiri - bir sonraki tekrar denemeden ONCE basilir.
_WAITING_LINE_RE = re.compile(r'Waiting\s+(\d+)\s+seconds', re.IGNORECASE)


@dataclass
class RobocopyProgress:
    current_file: str = ""
    percent: float = 0.0  # o an kopyalanan TEK dosyanin yuzdesi (job'un tamami DEGIL)
    files_done: int = 0
    files_total: int = 0
    percent_overall: float = 0.0  # files_done/files_total - TUM job'un gercek ilerlemesi


ProgressCallback = Callable[[RobocopyProgress], None]


def _reader_thread(pipe, line_queue: "deque[str]", stop_flag: list) -> None:
    """Popen.stdout'tan satir satir okuyup paylasilan kuyruga ekler.
    Ayri thread'de calisir ki ana dongu readline()'da bloke olmadan
    cancel_event'i sik sik kontrol edebilsin."""
    try:
        for line in iter(pipe.readline, ''):
            line_queue.append(line)
            if stop_flag[0]:
                break
    except (ValueError, OSError):
        pass
    finally:
        stop_flag[0] = True


def run_robocopy_with_progress(
    source: str,
    destination: str,
    file_filter: str,
    older_than_days: int,
    max_retries: int,
    threads: int,
    robocopy_log: str,
    on_progress: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
    total_files: int = 0,
    on_log: Optional[Callable[[str], None]] = None,
) -> tuple[int, "__import__('datetime').timedelta"]:
    """
    Robocopy'yi calistirir, ilerleme yuzdesini CANLI olarak on_progress'e
    bildirir, ANLAMLI satirlari (yuzde spam'i HARIC) robocopy_log dosyasina
    yazar. Donus degeri run_robocopy ile AYNI (exit_code, duration) - mevcut
    cagiran kod (transfer.py) minimal degisiklikle gecis yapabilir.

    total_files: cagiranin (transfer.py) kendi on-taramasindan bildigi toplam
                 dosya sayisi - job'un GERCEK toplam ilerlemesini (files_done/
                 total_files) hesaplamak icin kullanilir. Robocopy zaten
                 GUNCEL/ayni dosyalar icin hicbir satir BASMADIGINDAN
                 (sadece New/Newer/Changed/Tweaked/*EXTRA icin satir basar),
                 bu bir YAKLASIMDIR - kesin degildir.

    on_log: robocopy KENDI ICINDE (/R:{max_retries} /W:5 ile) bir dosyayi
            tekrar denerken (ornegin dosya baska bir islem tarafindan
            kullanimdaysa) bunu CANLI bildiren opsiyonel callback. Bu
            olmadan, robocopy dosya kilitliyken sessizce N kez bekleyip
            tekrar deniyor ve kullanici bu sirada arka planda ne oldugunu
            goremiyordu (sadece ham robocopy_log dosyasinda gorunuyordu).
    """
    import datetime

    args = [
        "robocopy",
        source,
        destination,
        file_filter,
        "/E",
        "/COPY:DAT",
        f"/MT:{max(1, min(128, threads))}",
        f"/R:{max_retries}",
        "/W:5",
        "/BYTES",
        "/NDL",  # dizin listesini yine de bastir - satir sayisini makul tutar
        # DIKKAT: /NP ve /LOG+ KASITLI OLARAK KULLANILMIYOR (yukaridaki aciklamaya bakin)
    ]
    if older_than_days > 0:
        args.append(f"/MINAGE:{older_than_days}")


    Path(robocopy_log).parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(robocopy_log, "w", encoding="utf-8", errors="replace")

    start = datetime.datetime.now()
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, universal_newlines=True,
    )

    # deque.popleft() O(1)'dir; duz list.pop(0) her cagirista kalan tum
    # elemanlari kaydirdigi icin robocopy binlerce/yuz binlerce dosya satiri
    # urettiginde O(n^2) toplam maliyete (ciddi yavaslama) yol aciyordu.
    line_queue: deque = deque()
    stop_flag = [False]
    reader = threading.Thread(target=_reader_thread, args=(proc.stdout, line_queue, stop_flag), daemon=True)
    reader.start()

    current_file = ""
    was_cancelled = False
    files_seen = 0  # su ana kadar goruntlenen "yeni dosya" satiri sayisi

    # Robocopy'nin KENDI /R+/W tekrar-deneme mekanizmasini canli takip etmek
    # icin: bir ERROR satiri gorulunce hangi islem/dosyanin basarisiz oldugu
    # (pending_error_detail) ve varsa aciklama satiri (pending_error_reason)
    # bir sonraki "Waiting N seconds" satirina kadar biriktirilir - o satir
    # geldiginde robocopy'nin GERCEKTEN tekrar denemeye gectigi anlasilir.
    pending_error_detail: Optional[str] = None
    pending_error_reason: str = ""
    retry_counts: dict[str, int] = {}
    max_attempts_total = max(1, max_retries) + 1  # ilk deneme + /R kadar tekrar

    def emit_retry_log(detail: str, reason: str, wait_seconds: str) -> None:
        if on_log is None:
            return
        attempt = retry_counts.get(detail, 0) + 1
        retry_counts[detail] = attempt
        reason_part = f" - Sebep: {reason}" if reason else ""
        msg = (
            f"ROBOCOPY TEKRAR DENEME {attempt}/{max_attempts_total}: {detail}"
            f"{reason_part} - {wait_seconds}sn beklenip tekrar denenecek."
        )
        try:
            on_log(msg)
        except Exception:
            pass

    def emit_progress(percent: float) -> None:
        if on_progress is None:
            return
        files_done = max(0, files_seen - 1)
        percent_overall = round(files_done / total_files * 100, 1) if total_files else 0.0
        try:
            on_progress(RobocopyProgress(
                current_file=current_file, percent=percent,
                files_done=files_done, files_total=total_files, percent_overall=percent_overall,
            ))
        except Exception:
            pass

    try:
        while True:
            if proc.poll() is not None and not line_queue:
                # Surec bitti VE bekleyen satir kalmadi
                break

            if cancel_event is not None and cancel_event.is_set():
                was_cancelled = True
                stop_flag[0] = True
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break

            if not line_queue:
                time.sleep(0.05)
                continue

            line = line_queue.popleft().rstrip("\n").rstrip("\r")
            if not line.strip():
                continue

            pct_match = _PERCENT_RE.match(line)
            if pct_match:
                # Yuzde satiri: SADECE ilerleme bildirimi icin kullanilir,
                # log dosyasina YAZILMAZ (spam onleme).
                emit_progress(float(pct_match.group(1)))
                continue

            file_match = _FILE_LINE_RE.match(line)
            if file_match:
                # Yeni bir dosya satiri = BIR ONCEKI dosya tamamlandi demektir.
                files_seen += 1
                current_file = file_match.group(1).strip()
                emit_progress(0.0)

            err_match = _ERROR_LINE_RE.match(line)
            if err_match:
                pending_error_detail = err_match.group(1).strip()
                pending_error_reason = ""
                log_fh.write(line + "\n")
                continue

            wait_match = _WAITING_LINE_RE.search(line)
            if wait_match and pending_error_detail:
                # Robocopy tam olarak SIMDI tekrar denemeye geciyor.
                emit_retry_log(pending_error_detail, pending_error_reason, wait_match.group(1))
                log_fh.write(line + "\n")
                pending_error_detail = None
                pending_error_reason = ""
                continue

            if pending_error_detail and not pending_error_reason:
                # ERROR satirindan sonraki insan-okunur aciklama satiri
                # (ornegin "The process cannot access the file...").
                pending_error_reason = line.strip()

            # Yuzde-disi TUM satirlar (basliklar, dosya adlari, hatalar,
            # ozet) kendi yonettigimiz log dosyasina yazilir.
            log_fh.write(line + "\n")

    finally:
        log_fh.close()
        stop_flag[0] = True
        try:
            proc.stdout.close()
        except Exception:
            pass

    duration = datetime.datetime.now() - start

    if was_cancelled:
        return -1, duration

    # Surec bitti - son dosya da tamamlanmis sayilir (basarili/hatali farketmez,
    # robocopy bir sonraki dosyaya GECMISTIR artik).
    files_seen += 1
    emit_progress(100.0)

    return proc.returncode, duration
