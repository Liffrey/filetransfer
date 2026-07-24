"""
transfer.py
-----------
Ana transfer motoru: yas filtreli robocopy transferi + SHA256/boyut
dogrulamasi + disk kontrolu + mail uyarisi + log/JSON ozet uretimi.

PowerShell surumunden farklar (kasitli iyilestirmeler):
- subprocess.run(["robocopy", kaynak, hedef, ...]) LISTE olarak cagrilir;
  Windows'un kendi argv olusturma mekanizmasi kullanilir, bu yuzden
  "E:\\" gibi tek backslash ile biten yollarda PowerShell'de yasadigimiz
  kacis-karakteri/tirnak sorunu burada YASANMAZ.
- Hash hesaplama concurrent.futures.ThreadPoolExecutor ile paralel yapilir
  (PowerShell runspace pool'a kiyasla çok daha basit ve az hataya acik).
- Tum dizi/koleksiyon islemleri Python list'i - PowerShell'in pipeline
  "unroll" tuzagi burada yoktur.
"""
from __future__ import annotations

import concurrent.futures
import datetime
import hashlib
import json
import os
import smtplib
import threading
import time
from dataclasses import dataclass
from email.mime.text import MIMEText
from pathlib import Path
from typing import Callable, Optional

from engine.config import TransferJob
from engine.logutil import EngineLogger, DiskInfo, get_disk_info, format_size, HashLogWriter, sanitize_filename
from engine.credentials import CredentialStore, resolve_unc_server
from engine.scanner import scan_directory, build_rel_index
from engine.joblock import JobLock, JobLockError
from engine.robocopy_runner import run_robocopy_with_progress, RobocopyProgress


@dataclass(slots=True)
class SourceFileInfo:
    """hash_table degeri - eski dict-of-dict yapisina kiyasla ~%25 daha
    az bellek kullanir (400bin+ dosyada onemli bir fark)."""
    hash: str
    size: int
    src: str
    rel: str


@dataclass(slots=True)
class VerifiedFileInfo:
    rel: str
    size: int
    src_file: str
    dst_file: str


@dataclass(slots=True)
class FailedFileInfo:
    rel: str
    src_hash: str
    dst_hash: str


@dataclass
class TransferResult:
    job_name: str
    overall_success: bool
    error_message: str = ""
    log_file: str = ""
    robocopy_log: str = ""
    hash_log_file: str = ""
    summary_json: str = ""
    run_id: str = ""
    total_files: int = 0
    verified_files: int = 0
    failed_files: int = 0
    missing_files: int = 0
    skipped_files: int = 0
    transferred_bytes: int = 0
    duration: str = ""


# Robocopy exit kodu bit anlami:
#   0=degisiklik yok, 1=kopyalandi, 2=ekstra dosya, 4=uyumsuzluk,
#   8=KOPYALAMA HATASI (bazi dosyalar basarisiz), 16=FATAL HATA (robocopy
#   hic calisamadi - gecersiz yol/parametre gibi).
#
# ONEMLI: Sadece FATAL (16) gercek bir "hic calismadi" durumudur ve
# dogrulama asamasini ATLAMAYI gerektirir. KOPYALAMA HATASI (8) tek basina
# robocopy'nin transferi TAMAMEN durdurdugu anlamina gelmez - kaynak
# klasorde kopyalama sirasinda YENI bir dosya belirmesi, gecici bir
# paylasim ihlali (sharing violation) gibi TEK TEK dosya sorunlarinda da
# bu bit set edilir, cogu zaman dosyalarin BUYUK COGUNLUGU basariyla
# kopyalanmis olur. Bu durumda transferi tumden basarisiz saymak yerine
# DOGRULAMA asamasina gecilir - hangi dosyalarin EKSIK/UYUMSUZ oldugunu
# TEK TEK ve DOGRU sekilde bu asama tespit eder (robocopy'nin kaba exit
# kodundan cok daha faydali bir sonuc).
ROBOCOPY_FATAL_MASK = 16
ROBOCOPY_FILE_ERROR_BIT = 8


def _robocopy_exit_description(code: int) -> list[str]:
    parts = []
    if code == 0:
        parts.append("Degisiklik yok")
    if code & 1:
        parts.append("Kopyalandi")
    if code & 2:
        parts.append("Ekstra dosya")
    if code & 4:
        parts.append("Uyumsuzluk")
    if code & 8:
        parts.append("KOPYALAMA HATASI")
    if code & 16:
        parts.append("FATAL HATA")
    return parts


def send_alert_mail(smtp_server: str, mail_from: str, mail_to: list[str],
                     subject: str, body: str, logger: Optional[EngineLogger] = None) -> None:
    if not smtp_server or not mail_to:
        return
    try:
        msg = MIMEText(body, _charset="utf-8")
        msg["Subject"] = subject
        msg["From"] = mail_from
        msg["To"] = ", ".join(mail_to)
        with smtplib.SMTP(smtp_server, timeout=15) as server:
            server.sendmail(mail_from, mail_to, msg.as_string())
        if logger:
            logger.success(f"Mail gonderildi: {', '.join(mail_to)}")
    except Exception as e:
        if logger:
            logger.warn(f"Mail gonderilemedi: {e}")


def _sha256_of_file(path: str, chunk_size: int = 1024 * 1024) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest().upper()
    except OSError:
        return None


def hash_files_parallel(paths: list[str], max_workers: Optional[int] = None,
                          min_parallel_count: int = 30) -> dict[str, Optional[str]]:
    """
    Dosyalarin SHA256 hashini paralel hesaplar. Az dosyada thread-pool
    overhead'i fazla oldugu icin sequential'a duser.
    """
    if not paths:
        return {}
    if max_workers is None:
        max_workers = min(32, (os.cpu_count() or 4) * 2)

    if len(paths) < min_parallel_count or max_workers <= 1:
        return {p: _sha256_of_file(p) for p in paths}

    results: dict[str, Optional[str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_path = {executor.submit(_sha256_of_file, p): p for p in paths}
        for future in concurrent.futures.as_completed(future_to_path):
            path = future_to_path[future]
            try:
                results[path] = future.result()
            except Exception:
                results[path] = None
    return results


def check_disk_and_alert(destination: str, projected_extra_bytes: int, job: TransferJob,
                          logger: EngineLogger) -> tuple[bool, Optional[DiskInfo]]:
    """
    Hedef disk kontrolu yapar, uyari/kritik esiklerde mail atar.
    Donus: (devam_edilsin_mi, disk_info)
    """
    logger.header("-" * 64)
    logger.info("Hedef disk kontrolu...")
    di = get_disk_info(destination)
    if di is None:
        logger.warn("Disk bilgisi alinamadi, kontrol atlandi.")
        return True, None

    logger.info(
        f"Disk: Toplam={format_size(di.total_bytes)}  "
        f"Bos={format_size(di.free_bytes)}  Dolu=%{di.used_pct}"
    )

    if job.min_free_space_gb > 0:
        min_bytes = job.min_free_space_gb * 1024**3
        if di.free_bytes < min_bytes:
            msg = f"Mevcut bos alan ({format_size(di.free_bytes)}) < minimum ({job.min_free_space_gb} GB)"
            logger.error(msg)
            send_alert_mail(job.smtp_server, job.mail_from, job.mail_to,
                             f"[DURDU] {job.name} - Yetersiz disk", msg, logger)
            return False, di

    projected_pct = round(((di.used_bytes + projected_extra_bytes) / di.total_bytes) * 100, 1) if di.total_bytes else 0
    logger.info(f"Transfer sonrasi tahmini doluluk: %{projected_pct}")

    if di.used_pct >= job.disk_critical_threshold_pct:
        msg = f"KRITIK: Hedef disk %{di.used_pct} dolu (esik %{job.disk_critical_threshold_pct})"
        logger.error(msg)
        send_alert_mail(job.smtp_server, job.mail_from, job.mail_to,
                         f"[KRITIK] {job.name} - Disk", msg, logger)
        if job.stop_on_critical_disk:
            logger.error("StopOnCriticalDisk aktif - iptal.")
            return False, di
        logger.warn("StopOnCriticalDisk pasif - devam.")
    elif di.used_pct >= job.disk_warn_threshold_pct:
        msg = f"UYARI: Hedef disk %{di.used_pct} dolu (esik %{job.disk_warn_threshold_pct})"
        logger.warn(msg)
        send_alert_mail(job.smtp_server, job.mail_from, job.mail_to,
                         f"[UYARI] {job.name} - Disk", msg, logger)
    else:
        logger.success("Disk durumu normal.")

    return True, di


ProgressCallback = Callable[[str], None]


def run_transfer(job: TransferJob, run_id: Optional[str] = None,
                  credential_store: Optional[CredentialStore] = None,
                  on_log: Optional[ProgressCallback] = None,
                  cancel_event: Optional[threading.Event] = None,
                  lock_dir: Optional[str] = None,
                  on_progress: Optional[Callable[[RobocopyProgress], None]] = None) -> TransferResult:
    """
    Bir job'u calistirir: yas filtreli robocopy + hash/boyut dogrulama +
    disk kontrolu + log/hashlog/JSON-ozet uretimi.

    on_log: her log satirinda cagrilan opsiyonel callback (GUI canli takip icin).
    cancel_event: set edildiginde robocopy calisiyorsa durdurulur, sonraki
                  asamalara (dogrulama, silme) gecilmez.
    lock_dir: verilirse, ayni job'un ayni anda (baska bir surecten - ornegin
              GUI'den elle "Simdi Calistir" derken Gorev Zamanlayici'nin da
              tetiklemesi gibi) TEKRAR calistirilmasi engellenir. Verilmezse
              kilit kontrolu YAPILMAZ (geriye donuk uyumluluk icin opsiyonel).
    on_progress: robocopy calisirken canli olarak (dosya adi, yuzde) bilgisini
                 ileten opsiyonel callback. NOT: bu, o an kopyalanan TEK
                 DOSYANIN yuzdesidir - robocopy'nin dogasi geregi tum job'un
                 toplam ilerlemesini DEGIL, mevcut dosyanin ilerlemesini verir.
    """
    if run_id is None:
        run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    safe_name = sanitize_filename(job.name)
    log_dir = Path(job.log_dir or "C:\\TransferLogs")
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / f"Transfer_{safe_name}_{run_id}.log"
    robocopy_log = log_dir / f"Robocopy_{safe_name}_{run_id}.log"
    hash_log_file = log_dir / f"HashLog_{safe_name}_{run_id}.log"
    summary_json = log_dir / f"Summary_{safe_name}_{run_id}.json"

    logger = EngineLogger(log_file)
    if on_log:
        original_log = logger.log
        def hooked_log(message, level="INFO"):
            original_log(message, level)
            on_log(f"[{level}] {message}")
        logger.log = hooked_log  # type: ignore

    hash_log_created = False  # HashLogWriter GERCEKTEN tamamlanip dosyayi kapattiginda True olur

    def result(success: bool, error: str = "", **extra) -> TransferResult:
        return TransferResult(
            job_name=job.name, overall_success=success, error_message=error,
            log_file=str(log_file), robocopy_log=str(robocopy_log),
            hash_log_file=str(hash_log_file) if hash_log_created else "",
            summary_json=str(summary_json), run_id=run_id, **extra,
        )

    logger.header("=" * 64)
    logger.header(f" TRANSFER JOB: {job.name}  (RunId: {run_id})")
    logger.header("=" * 64)
    logger.info(f"Kaynak    : {job.source_path}")
    logger.info(f"Hedef     : {job.destination_path}")
    logger.info(f"Filtre    : {job.file_filter}  |  Yas: >{job.older_than_days} gun")
    logger.info(f"Dogrulama : {job.verification_mode}  |  Robocopy /MT: {job.robocopy_threads}")
    logger.header("-" * 64)

    smb_targets: list[str] = []
    job_lock = JobLock(lock_dir, job.name) if lock_dir else None
    try:
        if job_lock:
            try:
                job_lock.acquire()
            except JobLockError as e:
                logger.error(str(e))
                return result(False, str(e))

        if not job.source_path or not job.destination_path:
            logger.error("Kaynak/Hedef tanimlanmamis!")
            return result(False, "Yol eksik")

        # ---- SMB pre-auth ----
        if credential_store and job.credential_alias:
            cred = credential_store.get(job.credential_alias)
            if cred:
                for p in (job.source_path, job.destination_path):
                    srv = resolve_unc_server(p)
                    if srv and srv not in smb_targets:
                        logger.info(f"SMB pre-auth: {srv} ({cred.username})")
                        if credential_store.register_smb_session(srv, cred):
                            smb_targets.append(srv)
                            logger.success(f"SMB auth OK: {srv}")
                        else:
                            logger.warn(f"SMB auth basarisiz: {srv} (devam edilecek)")
            else:
                logger.warn(f"Kimlik aliasi bulunamadi: {job.credential_alias}")
        # ---- Kaynak / hedef kontrolu ----
        if not os.path.exists(job.source_path):
            logger.error(f"Kaynak erisilemiyor: {job.source_path}")
            send_alert_mail(job.smtp_server, job.mail_from, job.mail_to,
                             f"[HATA] {job.name} - Kaynak erisilemiyor", f"Kaynak: {job.source_path}", logger)
            return result(False, "Kaynak erisilemiyor")

        if not os.path.exists(job.destination_path):
            logger.warn("Hedef yok, olusturuluyor...")
            try:
                os.makedirs(job.destination_path, exist_ok=True)
            except OSError as e:
                logger.error(f"Hedef olusturulamadi: {e}")
                return result(False, "Hedef olusturulamadi")

        # ---- Yas filtreli dosya listesi (hizli paralel tarama) ----
        logger.header("-" * 64)
        logger.info(f"Dosya listesi taraniyor (>{job.older_than_days} gun)...")
        # Gece yarisi (00:00) hizali kesim - saniye-hassas hesap YERINE.
        # Iki sebep: (1) robocopy'nin /MINAGE:N parametresi gun-bazli
        # calisir (resmi belgeler "N gunden yeni dosyalari haric tut" der,
        # saat/saniye hassasiyeti belirtmez) - gece yarisi hizalamasi bizim
        # ON-TARAMAMIZ ile robocopy'nin GERCEKTE kopyaladigi dosya kumesini
        # daha tutarli hale getirir, boylece dogrulama asamasinda sinir
        # farklarindan kaynaklanan sahte "EKSIK" sonuclari onlenir.
        # (2) Ayni zamanlanmis job saat 02:00'de de 23:00'te de calissa,
        # "30 gunden eski" ayni takvim gunune karsilik gelir - saat-bagimli
        # tutarsizlik olmaz.
        today_midnight = datetime.datetime.combine(datetime.date.today(), datetime.time.min)
        cutoff = (today_midnight - datetime.timedelta(days=job.older_than_days)).timestamp()

        def scan_progress(scanned: int, matched: int) -> None:
            logger.info(f"  Taraniyor... {scanned:,} dosya kontrol edildi, {matched:,} eslesti")

        scan_result = scan_directory(
            job.source_path, file_filter=job.file_filter, mtime_cutoff=cutoff,
            max_workers=job.robocopy_threads, progress_callback=scan_progress,
            cancel_event=cancel_event,
        )
        if scan_result.cancelled:
            logger.warn("Tarama kullanici tarafindan DURDURULDU.")
            return result(False, "Kullanici tarafindan durduruldu")
        if scan_result.error_count:
            logger.warn(f"Tarama sirasinda {scan_result.error_count} dosya/dizine erisilemedi (atlandi).")

        src_files = scan_result.matched
        skipped = scan_result.skipped_count
        logger.info(f"Tarama tamam: Tasinacak={len(src_files)}  Atlanacak(daha yeni)={skipped}")

        if not src_files:
            logger.success("Tasinacak dosya yok.")
            return result(True, total_files=0, skipped_files=skipped)

        src_total_bytes = sum(f.size for f in src_files)
        logger.info(f"Transfer boyutu: {format_size(src_total_bytes)}")

        # ---- Disk kontrolu ----
        proceed, _ = check_disk_and_alert(job.destination_path, src_total_bytes, job, logger)
        if not proceed:
            return result(False, "Disk kontrolu basarisiz")

        # ---- Kaynak hash/boyut (tarama sirasinda alinan boyut/mtime tekrar KULLANILIR, yeniden stat YOK) ----
        hash_table: dict[str, SourceFileInfo] = {}

        if job.verification_mode != "None":
            logger.header("-" * 64)
            mode_label = "boyut karsilastirmasi" if job.verification_mode == "SizeOnly" else "SHA256 hash (paralel)"
            logger.info(f"Kaynak analizi basliyor — {mode_label} ({len(src_files)} dosya)...")
            t0 = time.time()

            if job.verification_mode == "FullHash":
                path_strs = [f.path for f in src_files]
                hash_map = hash_files_parallel(path_strs)
                for f in src_files:
                    h = hash_map.get(f.path)
                    if h:
                        hash_table[f.rel_path.lower()] = SourceFileInfo(hash=h, size=f.size, src=f.path, rel=f.rel_path)
                    else:
                        logger.warn(f"Hash alinamadi: {f.path}")
            else:  # SizeOnly - boyut zaten tarama sirasinda alindi, ekstra islem gerekmez
                for f in src_files:
                    hash_table[f.rel_path.lower()] = SourceFileInfo(hash=f"SIZE:{f.size}", size=f.size, src=f.path, rel=f.rel_path)

            logger.success(f"Kaynak analizi tamam: {len(hash_table)}/{len(src_files)}  ({time.time()-t0:.1f}sn)")
        else:
            # VerificationMode=None: hash_table hicbir yerde kullanilmiyor,
            # doldurmak sadece bellek/CPU israfi - atlaniyor.
            logger.warn("VerificationMode=None: dogrulama atlanacak.")

        # ---- Robocopy ----
        logger.header("-" * 64)
        logger.info(f"Robocopy basliyor (/MT:{job.robocopy_threads})...")
        exit_code, duration = run_robocopy_with_progress(
            job.source_path, job.destination_path, job.file_filter,
            job.older_than_days, job.max_retries, job.robocopy_threads, str(robocopy_log),
            on_progress=on_progress, cancel_event=cancel_event,
        )

        if exit_code == -1:
            logger.warn("Robocopy kullanici tarafindan DURDURULDU.")
            return result(False, "Kullanici tarafindan durduruldu")

        desc = _robocopy_exit_description(exit_code)
        robo_fatal = (exit_code & ROBOCOPY_FATAL_MASK) != 0
        robo_had_file_errors = (exit_code & ROBOCOPY_FILE_ERROR_BIT) != 0
        level = "ERROR" if robo_fatal else ("WARN" if exit_code > 1 else "SUCCESS")
        logger.log(f"Robocopy bitti. Exit={exit_code} ({' | '.join(desc)})  Sure={duration}", level)

        if robo_fatal:
            # ONEMLI: ESKIDEN burada erken donup dogrulamayi TAMAMEN
            # atliyorduk. Ancak /MT (coklu is parcacigi) ile calisirken,
            # BAZI thread'ler dosyalari basariyla kopyalamisken BASKA bir
            # thread'de ciddi bir hata olusup genel exit code'u FATAL (16)
            # yapabiliyor - yani "hic dosya kopyalanmadi" aciklamasi
            # coklu-thread senaryolarda HER ZAMAN dogru olmuyor. Erken
            # donup hash log HIC uretmemek, kismen (belki cogunlukla)
            # basarili olmus bir transferde kullaniciya HICBIR BILGI
            # vermemek anlamina geliyordu - bu asil sikayet edilen sorundu.
            #
            # Simdi: FATAL durumu kaydedip/mail atip DOGRULAMAYA DEVAM
            # ediyoruz. Dogrulama, hedefte GERCEKTE ne var ne yok tek tek
            # tespit edip hash log uretecek. Is sonucu asla "tam basarili"
            # RAPORLANMAZ (asagida job_ok zorla False yapiliyor), ama
            # kullanici en azindan HANGI dosyalarin gectigini gorebilecek.
            logger.error(f"Robocopy FATAL bildirdi (Exit={exit_code}) - dogrulama yine de yapilacak, hedefte gercekte ne oldugu tespit edilecek. Detay: {robocopy_log}")
            send_alert_mail(job.smtp_server, job.mail_from, job.mail_to,
                             f"[HATA] {job.name} - Robocopy FATAL Exit={exit_code}", f"Log: {robocopy_log}", logger)
        elif robo_had_file_errors:
            # Bazi dosyalarda kopyalama hatasi olustu (ornegin kaynakta transfer
            # sirasinda yeni/degisen bir dosya, gecici paylasim ihlali gibi) ama
            # robocopy calismaya devam etti. Transferi TUMDEN basarisiz saymak
            # yerine dogrulama asamasina geciyoruz - hangi dosyalarin gercekten
            # eksik/uyumsuz oldugunu ORADA tek tek ve dogru tespit edecegiz.
            logger.warn(f"Robocopy bazi dosyalarda hata bildirdi (Exit={exit_code}). Dogrulama, etkilenen dosyalari tek tek tespit edecek. Detay: {robocopy_log}")
        else:
            logger.success("Robocopy basarili.")

        # ---- Dogrulama ----
        verified: list[VerifiedFileInfo] = []
        failed: list[FailedFileInfo] = []
        missing: list[str] = []

        if job.verification_mode == "None":
            total = len(src_files)
            verified_count = total
            failed_count = missing_count = 0
            transferred_bytes = src_total_bytes
            # FATAL bayragi varsa, dogrulama YAPILMADIGI icin (bu mod zaten
            # dogrulama atliyor) gercekte ne kadarinin basarili oldugunu
            # BILEMIYORUZ - guvenli tarafta kalip basarisiz raporlaniyor.
            job_ok = not robo_fatal
        else:
            logger.header("-" * 64)
            logger.info(f"Dogrulama basliyor — mod={job.verification_mode} ({len(hash_table)} dosya)...")
            t0 = time.time()

            # Hedefi TEK GECISTE tara (dosya basina ayri exists()/stat() cagrisi YOK).
            # Bu, kaynak taramasindaki ayni performans duzeltmesinin hedefe uygulanmis hali.
            logger.info("Hedef dizin taraniyor (dogrulama icin)...")
            dst_scan = scan_directory(
                job.destination_path, file_filter=job.file_filter, mtime_cutoff=None,
                max_workers=job.robocopy_threads, cancel_event=cancel_event,
            )
            dst_index = build_rel_index(dst_scan.matched)
            logger.info(f"Hedef tarama tamam: {len(dst_index)} dosya bulundu.")

            # HashLogWriter: her girdi HEMEN diske yazilir, bellekte TUM girdileri
            # tutan bir liste (eski hash_entries) ARTIK YOK. 400bin+ dosyada bu,
            # sadece hash log uretimi icin gereken bellegi dosya sayisindan
            # BAGIMSIZ (sabit) hale getirir.
            with HashLogWriter(hash_log_file, job.name, run_id, job.source_path,
                                job.destination_path, job.verification_mode) as hlw:
                if job.verification_mode == "FullHash":
                    dst_paths = []
                    rel_map = {}
                    for rel_key, info in hash_table.items():
                        dst_entry = dst_index.get(rel_key)
                        if dst_entry is not None:
                            dst_paths.append(dst_entry.path)
                            rel_map[dst_entry.path] = rel_key
                        else:
                            logger.warn(f"EKSIK: {info.rel}")
                            missing.append(info.rel)
                            hlw.add_entry(info.rel, info.hash, "-", 0, "MISSING")

                    dst_hash_map = hash_files_parallel(dst_paths) if dst_paths else {}

                    for dst_path in dst_paths:
                        rel_key = rel_map[dst_path]
                        info = hash_table[rel_key]
                        dh = dst_hash_map.get(dst_path)
                        sz = dst_index[rel_key].size  # taramadan geldi, ekstra stat() YOK

                        if dh is None:
                            logger.error(f"HASH ALINAMADI: {info.rel}")
                            failed.append(FailedFileInfo(rel=info.rel, src_hash=info.hash, dst_hash="(okunamadi)"))
                            hlw.add_entry(info.rel, info.hash, "(okunamadi)", sz, "ERROR")
                        elif dh == info.hash:
                            verified.append(VerifiedFileInfo(rel=info.rel, size=sz, src_file=info.src, dst_file=dst_path))
                            hlw.add_entry(info.rel, info.hash, dh, sz, "OK")
                        else:
                            logger.error(f"HASH UYUMSUZ: {info.rel}  Kaynak={info.hash}  Hedef={dh}")
                            failed.append(FailedFileInfo(rel=info.rel, src_hash=info.hash, dst_hash=dh))
                            hlw.add_entry(info.rel, info.hash, dh, sz, "MISMATCH")
                else:  # SizeOnly - boyut karsilastirmasi tamamen bellek-ici, sifir ekstra I/O
                    for rel_key, info in hash_table.items():
                        dst_entry = dst_index.get(rel_key)
                        src_hash_label = f"(boyut:{format_size(info.size)})"
                        if dst_entry is None:
                            logger.warn(f"EKSIK: {info.rel}")
                            missing.append(info.rel)
                            hlw.add_entry(info.rel, src_hash_label, "-", 0, "MISSING")
                            continue
                        sz = dst_entry.size
                        if sz == info.size:
                            verified.append(VerifiedFileInfo(rel=info.rel, size=sz, src_file=info.src, dst_file=dst_entry.path))
                            hlw.add_entry(info.rel, src_hash_label, f"(boyut:{format_size(sz)})", sz, "OK")
                        else:
                            logger.error(f"BOYUT UYUMSUZ: {info.rel}  Kaynak={format_size(info.size)}  Hedef={format_size(sz)}")
                            failed.append(FailedFileInfo(rel=info.rel, src_hash=f"SIZE:{info.size}", dst_hash=f"SIZE:{sz}"))
                            hlw.add_entry(info.rel, src_hash_label, f"(boyut:{format_size(sz)})", sz, "MISMATCH")

                total = hlw.total_entries
                verified_count = hlw.ok_count
                failed_count = hlw.mismatch_count + hlw.error_count
                missing_count = hlw.missing_count
                transferred_bytes = hlw.total_bytes
                verification_ok = hlw.finish(str(duration))
                # Robocopy FATAL bildirmisse, dogrulama TEK TEK her seyi
                # gecerli bulsa bile is sonucu "tam basarili" RAPORLANMAZ -
                # FATAL, ciddi bir sorunun isareti oldugu icin guvenli
                # tarafta kaliniyor (kullanici log'da GERCEK detaylari gorur).
                job_ok = verification_ok and not robo_fatal
            hash_log_created = True  # with blogu basariyla tamamlandi, dosya gercekten diskte

            logger.success(f"Hash logu yazildi: {hash_log_file}")
            logger.success(f"Dogrulama tamam. Sure={time.time()-t0:.1f}sn")

        logger.header("-" * 64)
        logger.header(f"Toplam={total}  Dogrulanan={verified_count}  Basarisiz={failed_count}  Eksik={missing_count}")
        logger.info(f"Boyut={format_size(transferred_bytes)}  Robocopy={duration}")

        di_after = get_disk_info(job.destination_path)
        if di_after:
            logger.info(f"Hedef disk (sonra): Bos={format_size(di_after.free_bytes)}  Dolu=%{di_after.used_pct}")
            if di_after.used_pct >= job.disk_critical_threshold_pct:
                send_alert_mail(job.smtp_server, job.mail_from, job.mail_to,
                                 f"[KRITIK] {job.name} - Transfer sonrasi disk", f"Hedef: {job.destination_path}", logger)

        if job_ok:
            logger.success("TUM DOSYALAR DOGRULANDI.")
        elif robo_fatal and failed_count == 0 and missing_count == 0:
            # Dogrulama TEK TEK her seyi gecerli buldu (0 basarisiz, 0 eksik)
            # ama robocopy FATAL bildirdigi icin is yine de basarisiz
            # raporlaniyor - bunu ayri ve NET bir sekilde belirtiyoruz,
            # yoksa "DOGRULAMA HATASI" mesaji yaniltici olurdu (dogrulamanin
            # KENDISI aslinda sorun bulmadi).
            logger.error(f"Robocopy FATAL bildirdigi icin is BASARISIZ sayildi, ancak dogrulanan {verified_count} dosyanin hepsi TUTARLI cikti - kismi bir basari olabilir, log'u inceleyin.")
        else:
            logger.error(f"DOGRULAMA HATASI! Basarisiz={failed_count} Eksik={missing_count}")
            send_alert_mail(job.smtp_server, job.mail_from, job.mail_to,
                             f"[HATA] {job.name} - Dogrulama", f"Basarisiz={failed_count} Eksik={missing_count}", logger)

        # ---- Silme ----
        if job.delete_after_transfer:
            if job_ok:
                logger.warn("Kaynak dosyalar siliniyor...")
                del_err = 0
                for v in verified:
                    try:
                        os.remove(v.src_file)
                    except OSError as e:
                        logger.error(f"Silinemedi: {v.src_file} — {e}")
                        del_err += 1
                if del_err == 0:
                    logger.success("Tum kaynak dosyalar silindi.")
                else:
                    logger.warn(f"{del_err} dosya silinemedi.")
            else:
                logger.warn("Dogrulama basarisiz — kaynak SILINMEDI.")

        # ---- JSON ozet ----
        summary = {
            "job_name": job.name, "run_id": run_id,
            "run_info": {
                "source": job.source_path, "destination": job.destination_path,
                "age_days": job.older_than_days, "delete_after": job.delete_after_transfer,
                "verify_mode": job.verification_mode, "robocopy_mt": job.robocopy_threads,
            },
            "result": {
                "success": job_ok, "robocopy_exit": exit_code,
                "total": total, "verified": verified_count, "failed": failed_count,
                "missing": missing_count, "skipped": skipped,
                "transferred_bytes": transferred_bytes, "duration": str(duration),
            },
            "log_files": {
                "main_log": str(log_file),
                "hash_log": str(hash_log_file) if hash_log_created else "",
                "robocopy_log": str(robocopy_log),
            },
            "failed_files": [{"rel": f.rel, "src_hash": f.src_hash, "dst_hash": f.dst_hash} for f in failed],
            "missing_files": missing,
        }
        summary_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

        logger.header("=" * 64)
        logger.log("JOB TAMAMLANDI — BASARILI" if job_ok else "JOB TAMAMLANDI — HATALAR VAR",
                    "SUCCESS" if job_ok else "ERROR")

        final_error = ""
        if not job_ok:
            if robo_fatal and failed_count == 0 and missing_count == 0:
                final_error = f"Robocopy FATAL bildirdi (Exit={exit_code}), ancak dogrulanan {verified_count} dosya tutarli"
            elif robo_fatal:
                final_error = f"Robocopy FATAL bildirdi (Exit={exit_code}) + dogrulamada Basarisiz={failed_count} Eksik={missing_count}"
            else:
                final_error = f"Dogrulama: Basarisiz={failed_count} Eksik={missing_count}"

        return result(
            job_ok, final_error, total_files=total, verified_files=verified_count,
            failed_files=failed_count, missing_files=missing_count,
            skipped_files=skipped, transferred_bytes=transferred_bytes,
            duration=str(duration),
        )

    finally:
        if credential_store:
            for target in smb_targets:
                credential_store.unregister_smb_session(target)
                logger.info(f"SMB oturumu temizlendi: {target}")
        if job_lock:
            job_lock.release()
        logger.close()  # EngineLogger artik dosya tanitcisini surekli acik tutuyor - burada kapatiliyor
