"""
logutil.py
----------
Log dosyasi yazimi, disk bilgisi sorgusu, dosya boyutu formatlama.

BELLEK OPTIMIZASYONU (400bin+ dosya olceginde):
- EngineLogger artik dosya tanitcisini surekli ACIK tutuyor (her log
  satirinda ayri open/close yapmiyor) - hem hizli hem de cok sayida
  hata/uyari olustugunda dosya tanitici acma/kapama maliyetini onler.
- HashLogWriter, tum girdileri bellekte biriktirip TEK SEFERDE yazmak
  yerine HER GIRDIYI HEMEN diske yazar (streaming). Eski build_hash_log()
  fonksiyonu 400bin dosyada yuzlerce MB'lik bir liste+string olusturuyordu;
  bu siniftaki bellek kullanimi artik dosya sayisindan BAGIMSIZ (sabit).
"""
from __future__ import annotations

import datetime
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, TextIO


LEVELS = ("INFO", "WARN", "ERROR", "SUCCESS", "HEADER")


def format_size(num_bytes: float) -> str:
    if num_bytes >= 1024**3:
        return f"{num_bytes / 1024**3:.2f} GB"
    if num_bytes >= 1024**2:
        return f"{num_bytes / 1024**2:.2f} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.2f} KB"
    return f"{int(num_bytes)} B"


class EngineLogger:
    """
    Tek bir calisma (RunId) icin log dosyasina yazan logger.
    Dosya tanitcisi __init__'te acilir ve close()/__exit__ ile kapatilir -
    her log() cagrisinda ayri acma/kapama YAPMAZ (eski davranistan farkli).
    """

    def __init__(self, log_file: str | Path, echo: bool = False):
        self.log_file = Path(log_file)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.echo = echo
        self._fh: TextIO = open(self.log_file, "a", encoding="utf-8", buffering=1)  # satir-tamponlu

    def log(self, message: str, level: str = "INFO", console: bool = True) -> None:
        """
        console=False: satir DAIMA dosyaya yazilir, ancak CLI/GUI canli log
        akisina (on_log callback - bkz. transfer.py run_transfer) YANSITILMAZ.
        Boylece dosya-basina tekrarlanan ayrintilar (tarama ilerlemesi,
        dosya-basina EKSIK/UYUMSUZ vb.) sadece log dosyasinda birikir, konsol/
        GUI paneli ise genel/ozet satirlarla sinirli kalir.
        """
        if level not in LEVELS:
            level = "INFO"
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} [{level:<7}] {message}"
        self._fh.write(line + "\n")
        if self.echo:
            print(line)

    def info(self, msg, console: bool = True): self.log(msg, "INFO", console)
    def warn(self, msg, console: bool = True): self.log(msg, "WARN", console)
    def error(self, msg, console: bool = True): self.log(msg, "ERROR", console)
    def success(self, msg, console: bool = True): self.log(msg, "SUCCESS", console)
    def header(self, msg, console: bool = True): self.log(msg, "HEADER", console)

    def close(self) -> None:
        if self._fh and not self._fh.closed:
            self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def __del__(self):
        # Guvenlik agi: close() unutulursa bile dosya tanitici sizmasin.
        try:
            self.close()
        except Exception:
            pass


@dataclass
class DiskInfo:
    total_bytes: int
    free_bytes: int

    @property
    def used_bytes(self) -> int:
        return self.total_bytes - self.free_bytes

    @property
    def used_pct(self) -> float:
        if self.total_bytes == 0:
            return 0.0
        return round((self.used_bytes / self.total_bytes) * 100, 1)

    @property
    def free_pct(self) -> float:
        if self.total_bytes == 0:
            return 0.0
        return round((self.free_bytes / self.total_bytes) * 100, 1)


def get_disk_info(path: str) -> Optional[DiskInfo]:
    """
    Hem yerel (D:\\...) hem UNC (\\\\Sunucu\\Pay\\Klasor) yollar icin
    disk doluluk bilgisi doner. shutil.disk_usage Windows'ta UNC yollari
    da destekler (win32 GetDiskFreeSpaceEx'i dahili olarak kullanir),
    ekstra bir Win32 API cagrisina gerek yok.
    """
    try:
        if not Path(path).exists():
            return None
        usage = shutil.disk_usage(path)
        return DiskInfo(total_bytes=usage.total, free_bytes=usage.free)
    except OSError:
        return None


_BADGE_MAP = {
    "OK": "[  OK   ]",
    "MISMATCH": "[UYUMSUZ]",
    "MISSING": "[ EKSIK ]",
    "ERROR": "[ HATA  ]",
}
_NOTE_MAP = {
    "OK": "Eslesti — dosya butunlugu dogrulandi",
    "MISMATCH": "*** ESLESMEDI — kopyalama hatali veya dosya degisti ***",
    "MISSING": "*** Hedefte bulunamadi ***",
    "ERROR": "*** Hedef hash/boyut alinamadi ***",
}
_SEP1 = "=" * 160
_SEP2 = "-" * 160


class HashLogWriter:
    """
    Hash/dogrulama log dosyasini AKIS HALINDE (streaming) yazar.

    Eski build_hash_log() fonksiyonu TUM girdileri bir listede biriktirip
    sonunda tek bir dev string olusturuyordu - 400bin+ dosyada bu, sadece
    bu is icin yuzlerce MB ekstra bellek demekti. HashLogWriter, her
    add_entry() cagrisinda ilgili satirlari DOGRUDAN diske yazar; bellekte
    sadece birkac sayac (ok_count, mismatch_count, vb.) tutulur - bellek
    kullanimi dosya sayisindan tamamen BAGIMSIZDIR.

    Kullanim:
        with HashLogWriter(path, job_name, run_id, source, dest, mode) as w:
            for ... :
                w.add_entry(rel, src_hash, dst_hash, size, result)
        # __exit__ otomatik olarak ozet altbilgiyi yazar ve dosyayi kapatir
    """

    def __init__(self, path: str | Path, job_name: str, run_id: str,
                 source: str, destination: str, verify_mode: str):
        self.path = Path(path)
        self._fh = open(self.path, "w", encoding="utf-8")
        self.ok_count = 0
        self.mismatch_count = 0
        self.missing_count = 0
        self.error_count = 0
        self.total_bytes = 0
        self.total_entries = 0

        header = [
            _SEP1,
            f"  HASH / DOGRULAMA LOGU — {job_name}",
            _SEP1,
            f"  Job          : {job_name}",
            f"  RunId        : {run_id}",
            f"  Kaynak       : {source}",
            f"  Hedef        : {destination}",
            f"  Algoritma    : {'SHA256' if verify_mode == 'FullHash' else 'Boyut Karsilastirmasi'}",
            f"  Tarih        : {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            _SEP1,
            "",
        ]
        self._fh.write("\n".join(header) + "\n")

    def add_entry(self, rel: str, src_hash: str, dst_hash: str, size: int, result: str) -> None:
        """Tek bir dosyanin dogrulama sonucunu HEMEN diske yazar (bellekte tutmaz)."""
        badge = _BADGE_MAP.get(result, "[ HATA  ]")
        note = _NOTE_MAP.get(result, "-")
        self._fh.write(
            f"{badge}  {rel}\n"
            f"         Boyut         : {format_size(size)}\n"
            f"         Kaynak Hash   : {src_hash}\n"
            f"         Hedef Hash    : {dst_hash}\n"
            f"         Karsilastirma : {note}\n\n"
        )
        self.total_entries += 1
        if result == "OK":
            self.ok_count += 1
            self.total_bytes += size
        elif result == "MISMATCH":
            self.mismatch_count += 1
        elif result == "MISSING":
            self.missing_count += 1
        else:
            self.error_count += 1

    def finish(self, duration_str: str) -> bool:
        """Ozet altbilgiyi yazar ve dosyayi kapatir. Genel basari durumunu doner."""
        overall_ok = self.mismatch_count == 0 and self.missing_count == 0 and self.error_count == 0
        footer = [
            _SEP1,
            "  OZET",
            _SEP2,
            f"  Toplam Dosya     : {self.total_entries}",
            f"  OK  (Eslesti)    : {self.ok_count}",
            f"  Uyumsuz          : {self.mismatch_count}",
            f"  Eksik            : {self.missing_count}",
            f"  Hata             : {self.error_count}",
            f"  Transfer Boyutu  : {format_size(self.total_bytes)}",
            f"  Sure             : {duration_str}",
            f"  Genel Sonuc      : {'BASARILI — Tum dosyalar dogrulandi' if overall_ok else 'BASARISIZ — Hata var, yukaridaki satirlari inceleyin'}",
            _SEP1,
        ]
        self._fh.write("\n".join(footer) + "\n")
        self._fh.close()
        return overall_ok

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self._fh.closed:
            self._fh.close()
        return False


def sanitize_filename(name: str) -> str:
    """Dosya adinda kullanilamayacak karakterleri temizler."""
    cleaned = re.sub(r'[\\/:*?"<>|]', "_", name)
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned
