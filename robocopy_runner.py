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


@dataclass
class RobocopyProgress:
    current_file: str = ""
    percent: float = 0.0


ProgressCallback = Callable[[RobocopyProgress], None]


def _reader_thread(pipe, line_queue: "list", stop_flag: list) -> None:
    """Popen.stdout'tan satir satir okuyup paylasilan listeye ekler.
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
) -> tuple[int, "__import__('datetime').timedelta"]:
    """
    Robocopy'yi calistirir, ilerleme yuzdesini CANLI olarak on_progress'e
    bildirir, ANLAMLI satirlari (yuzde spam'i HARIC) robocopy_log dosyasina
    yazar. Donus degeri run_robocopy ile AYNI (exit_code, duration) - mevcut
    cagiran kod (transfer.py) minimal degisiklikle gecis yapabilir.
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

    line_queue: list = []
    stop_flag = [False]
    reader = threading.Thread(target=_reader_thread, args=(proc.stdout, line_queue, stop_flag), daemon=True)
    reader.start()

    current_file = ""
    was_cancelled = False

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

            line = line_queue.pop(0).rstrip("\n").rstrip("\r")
            if not line.strip():
                continue

            pct_match = _PERCENT_RE.match(line)
            if pct_match:
                # Yuzde satiri: SADECE ilerleme bildirimi icin kullanilir,
                # log dosyasina YAZILMAZ (spam onleme).
                if on_progress:
                    try:
                        on_progress(RobocopyProgress(current_file=current_file, percent=float(pct_match.group(1))))
                    except Exception:
                        pass
                continue

            file_match = _FILE_LINE_RE.match(line)
            if file_match:
                current_file = file_match.group(1).strip()

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

    return proc.returncode, duration
