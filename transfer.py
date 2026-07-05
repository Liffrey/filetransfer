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
import subprocess
import threading
import time
from dataclasses import dataclass
from email.mime.text import MIMEText
from pathlib import Path
from typing import Callable, Optional

try:
    from .config import TransferJob
    from .logutil import EngineLogger, DiskInfo, get_disk_info, format_size, build_hash_log, sanitize_filename
    from .credentials import CredentialStore, resolve_unc_server
except ImportError:  # PyInstaller / flat-module execution compatibility
    from config import TransferJob
    from logutil import EngineLogger, DiskInfo, get_disk_info, format_size, build_hash_log, sanitize_filename
    from credentials import CredentialStore, resolve_unc_server


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
#   8=KOPYALAMA HATASI, 16=FATAL HATA. 8 veya 16 set ise gercek hata.
ROBOCOPY_ERROR_MASK = 8 | 16


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


def run_robocopy(source: str, destination: str, file_filter: str,
                  older_than_days: int, max_retries: int, threads: int,
                  robocopy_log: str, cancel_event: Optional["threading.Event"] = None
                  ) -> tuple[int, "datetime.timedelta"]:
    """
    Robocopy'yi subprocess ile calistirir. Liste-tabanli argv kullanildigi
    icin Windows'un kendi quoting mekanizmasi devreye girer; "E:\\" gibi
    trailing-backslash yollarda PowerShell'de yasadigimiz kacis karakteri
    sorunu burada olusmaz (subprocess, argv'yi doğrudan CreateProcess'e
    Win32 API duzeyinde iletir, cmd.exe stringi ayristirma katmani devreye
    girmez).

    cancel_event verilirse, Popen+poll dongusu ile calistirilir ve event
    set edildiginde robocopy sureci terminate edilir (GUI'deki "Durdur"
    butonu icin). Exit code bu durumda -1 (kullanici tarafindan durduruldu)
    olarak doner.
    """
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
        f"/LOG+:{robocopy_log}",
        "/NP",
        "/BYTES",
        "/NDL",
    ]
    if older_than_days > 0:
        args.append(f"/MINAGE:{older_than_days}")

    start = datetime.datetime.now()

    if cancel_event is None:
        proc = subprocess.run(args, capture_output=True, text=True)
        duration = datetime.datetime.now() - start
        return proc.returncode, duration

    # Iptal edilebilir mod: Popen + poll dongusu
    popen = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    while popen.poll() is None:
        if cancel_event.is_set():
            popen.terminate()
            try:
                popen.wait(timeout=5)
            except subprocess.TimeoutExpired:
                popen.kill()
            duration = datetime.datetime.now() - start
            return -1, duration
        time.sleep(0.3)
    duration = datetime.datetime.now() - start
    return popen.returncode, duration


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
                  cancel_event: Optional[threading.Event] = None) -> TransferResult:
    """
    Bir job'u calistirir: yas filtreli robocopy + hash/boyut dogrulama +
    disk kontrolu + log/hashlog/JSON-ozet uretimi.

    on_log: her log satirinda cagrilan opsiyonel callback (GUI canli takip icin).
    cancel_event: set edildiginde robocopy calisiyorsa durdurulur, sonraki
                  asamalara (dogrulama, silme) gecilmez.
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

    def result(success: bool, error: str = "", **extra) -> TransferResult:
        return TransferResult(
            job_name=job.name, overall_success=success, error_message=error,
            log_file=str(log_file), robocopy_log=str(robocopy_log),
            hash_log_file=str(hash_log_file) if job.verification_mode != "None" else "",
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

    if not job.source_path or not job.destination_path:
        logger.error("Kaynak/Hedef tanimlanmamis!")
        return result(False, "Yol eksik")

    # ---- SMB pre-auth ----
    smb_targets: list[str] = []
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

    try:
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

        # ---- Yas filtreli dosya listesi ----
        logger.header("-" * 64)
        logger.info(f"Dosya listesi aliniyor (>{job.older_than_days} gun)...")
        cutoff = time.time() - job.older_than_days * 86400
        all_files: list[Path] = list(Path(job.source_path).rglob(job.file_filter))
        all_files = [f for f in all_files if f.is_file()]
        src_files = [f for f in all_files if f.stat().st_mtime < cutoff]
        skipped = len(all_files) - len(src_files)
        logger.info(f"Toplam={len(all_files)}  Tasinacak={len(src_files)}  Atlanacak(daha yeni)={skipped}")

        if not src_files:
            logger.success("Tasinacak dosya yok.")
            return result(True, total_files=0, skipped_files=skipped)

        src_total_bytes = sum(f.stat().st_size for f in src_files)
        logger.info(f"Transfer boyutu: {format_size(src_total_bytes)}")

        # ---- Disk kontrolu ----
        proceed, _ = check_disk_and_alert(job.destination_path, src_total_bytes, job, logger)
        if not proceed:
            return result(False, "Disk kontrolu basarisiz")

        # ---- Kaynak hash/boyut ----
        src_root = Path(job.source_path)
        hash_table: dict[str, dict] = {}

        if job.verification_mode != "None":
            logger.header("-" * 64)
            mode_label = "boyut karsilastirmasi" if job.verification_mode == "SizeOnly" else "SHA256 hash (paralel)"
            logger.info(f"Kaynak analizi basliyor — {mode_label} ({len(src_files)} dosya)...")
            t0 = time.time()

            if job.verification_mode == "FullHash":
                path_strs = [str(f) for f in src_files]
                hash_map = hash_files_parallel(path_strs)
                for f in src_files:
                    rel = str(f.relative_to(src_root))
                    h = hash_map.get(str(f))
                    if h:
                        hash_table[rel.lower()] = {"hash": h, "size": f.stat().st_size, "src": str(f), "rel": rel}
                    else:
                        logger.warn(f"Hash alinamadi: {f}")
            else:  # SizeOnly
                for f in src_files:
                    rel = str(f.relative_to(src_root))
                    sz = f.stat().st_size
                    hash_table[rel.lower()] = {"hash": f"SIZE:{sz}", "size": sz, "src": str(f), "rel": rel}

            logger.success(f"Kaynak analizi tamam: {len(hash_table)}/{len(src_files)}  ({time.time()-t0:.1f}sn)")
        else:
            for f in src_files:
                rel = str(f.relative_to(src_root))
                hash_table[rel.lower()] = {"hash": "", "size": f.stat().st_size, "src": str(f), "rel": rel}
            logger.warn("VerificationMode=None: dogrulama atlanacak.")

        # ---- Robocopy ----
        logger.header("-" * 64)
        logger.info(f"Robocopy basliyor (/MT:{job.robocopy_threads})...")
        exit_code, duration = run_robocopy(
            job.source_path, job.destination_path, job.file_filter,
            job.older_than_days, job.max_retries, job.robocopy_threads, str(robocopy_log),
            cancel_event=cancel_event,
        )

        if exit_code == -1:
            logger.warn("Robocopy kullanici tarafindan DURDURULDU.")
            return result(False, "Kullanici tarafindan durduruldu")

        desc = _robocopy_exit_description(exit_code)
        robo_ok = (exit_code & ROBOCOPY_ERROR_MASK) == 0
        level = "ERROR" if not robo_ok else ("WARN" if exit_code > 1 else "SUCCESS")
        logger.log(f"Robocopy bitti. Exit={exit_code} ({' | '.join(desc)})  Sure={duration}", level)

        if not robo_ok:
            logger.error(f"Robocopy hata! Detay: {robocopy_log}")
            send_alert_mail(job.smtp_server, job.mail_from, job.mail_to,
                             f"[HATA] {job.name} - Robocopy Exit={exit_code}", f"Log: {robocopy_log}", logger)
            return result(False, f"Robocopy hata exit={exit_code}")
        logger.success("Robocopy basarili.")

        # ---- Dogrulama ----
        verified: list[dict] = []
        failed: list[dict] = []
        missing: list[str] = []
        hash_entries: list[dict] = []

        if job.verification_mode == "None":
            total = len(src_files)
            verified_count = total
            failed_count = missing_count = 0
            transferred_bytes = src_total_bytes
            job_ok = True
        else:
            logger.header("-" * 64)
            logger.info(f"Dogrulama basliyor — mod={job.verification_mode} ({len(hash_table)} dosya)...")
            t0 = time.time()
            dst_root = Path(job.destination_path)

            if job.verification_mode == "FullHash":
                dst_paths = []
                rel_map = {}
                for rel_key, info in hash_table.items():
                    dst_file = dst_root / info["rel"]
                    if dst_file.exists():
                        dst_paths.append(str(dst_file))
                        rel_map[str(dst_file)] = rel_key
                    else:
                        logger.warn(f"EKSIK: {info['rel']}")
                        missing.append(info["rel"])
                        hash_entries.append({"rel": info["rel"], "src_hash": info["hash"], "dst_hash": "-", "size": 0, "result": "MISSING"})

                dst_hash_map = hash_files_parallel(dst_paths) if dst_paths else {}

                for dst_path in dst_paths:
                    rel_key = rel_map[dst_path]
                    info = hash_table[rel_key]
                    dh = dst_hash_map.get(dst_path)
                    try:
                        sz = os.path.getsize(dst_path)
                    except OSError:
                        sz = 0

                    if dh is None:
                        logger.error(f"HASH ALINAMADI: {info['rel']}")
                        failed.append({"rel": info["rel"], "src_hash": info["hash"], "dst_hash": "(okunamadi)"})
                        hash_entries.append({"rel": info["rel"], "src_hash": info["hash"], "dst_hash": "(okunamadi)", "size": sz, "result": "ERROR"})
                    elif dh == info["hash"]:
                        verified.append({"rel": info["rel"], "size": sz, "src_file": info["src"], "dst_file": dst_path})
                        hash_entries.append({"rel": info["rel"], "src_hash": info["hash"], "dst_hash": dh, "size": sz, "result": "OK"})
                    else:
                        logger.error(f"HASH UYUMSUZ: {info['rel']}  Kaynak={info['hash']}  Hedef={dh}")
                        failed.append({"rel": info["rel"], "src_hash": info["hash"], "dst_hash": dh})
                        hash_entries.append({"rel": info["rel"], "src_hash": info["hash"], "dst_hash": dh, "size": sz, "result": "MISMATCH"})
            else:  # SizeOnly
                for rel_key, info in hash_table.items():
                    dst_file = dst_root / info["rel"]
                    if not dst_file.exists():
                        logger.warn(f"EKSIK: {info['rel']}")
                        missing.append(info["rel"])
                        hash_entries.append({"rel": info["rel"], "src_hash": f"(boyut:{format_size(info['size'])})", "dst_hash": "-", "size": 0, "result": "MISSING"})
                        continue
                    sz = dst_file.stat().st_size
                    if sz == info["size"]:
                        verified.append({"rel": info["rel"], "size": sz, "src_file": info["src"], "dst_file": str(dst_file)})
                        hash_entries.append({"rel": info["rel"], "src_hash": f"(boyut:{format_size(info['size'])})", "dst_hash": f"(boyut:{format_size(sz)})", "size": sz, "result": "OK"})
                    else:
                        logger.error(f"BOYUT UYUMSUZ: {info['rel']}  Kaynak={format_size(info['size'])}  Hedef={format_size(sz)}")
                        failed.append({"rel": info["rel"], "src_hash": f"SIZE:{info['size']}", "dst_hash": f"SIZE:{sz}"})
                        hash_entries.append({"rel": info["rel"], "src_hash": f"(boyut:{format_size(info['size'])})", "dst_hash": f"(boyut:{format_size(sz)})", "size": sz, "result": "MISMATCH"})

            total = len(hash_table)
            verified_count = len(verified)
            failed_count = len(failed)
            missing_count = len(missing)
            transferred_bytes = sum(v["size"] for v in verified)
            job_ok = failed_count == 0 and missing_count == 0 and verified_count == total
            logger.success(f"Dogrulama tamam. Sure={time.time()-t0:.1f}sn")

            hash_log_text = build_hash_log(
                job.name, run_id, job.source_path, job.destination_path,
                job.verification_mode, hash_entries, str(duration),
            )
            hash_log_file.write_text(hash_log_text, encoding="utf-8")
            logger.success(f"Hash logu yazildi: {hash_log_file}")

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
                        os.remove(v["src_file"])
                    except OSError as e:
                        logger.error(f"Silinemedi: {v['src_file']} — {e}")
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
                "hash_log": str(hash_log_file) if job.verification_mode != "None" else "",
                "robocopy_log": str(robocopy_log),
            },
            "failed_files": failed,
            "missing_files": missing,
        }
        summary_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

        logger.header("=" * 64)
        logger.log("JOB TAMAMLANDI — BASARILI" if job_ok else "JOB TAMAMLANDI — HATALAR VAR",
                    "SUCCESS" if job_ok else "ERROR")

        return result(
            job_ok, total_files=total, verified_files=verified_count,
            failed_files=failed_count, missing_files=missing_count,
            skipped_files=skipped, transferred_bytes=transferred_bytes,
            duration=str(duration),
        )

    finally:
        if credential_store:
            for target in smb_targets:
                credential_store.unregister_smb_session(target)
                logger.info(f"SMB oturumu temizlendi: {target}")
