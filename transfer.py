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
import html
import hashlib
import json
import os
import shutil
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
from checkpoint import CheckpointJournal, checkpoint_signature


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


@dataclass(slots=True)
class _PendingVerify:
    """Ilk denemede basarisiz/eksik cikan, tekrar deneme kuyruguna alinan bir dosya.
    dst_path None ise hedefte HIC yok (rel'den yeniden hesaplanir)."""
    rel: str
    src_hash: str
    dst_hash: str
    size: int
    result: str  # MISSING | MISMATCH | ERROR (basarili olursa OK'e cevrilir)
    dst_path: Optional[str]


@dataclass
class TransferResult:
    job_name: str
    overall_success: bool
    error_message: str = ""
    log_file: str = ""
    robocopy_log: str = ""
    hash_log_file: str = ""
    summary_json: str = ""
    report_file: str = ""
    run_id: str = ""
    total_files: int = 0
    verified_files: int = 0
    failed_files: int = 0
    missing_files: int = 0
    skipped_files: int = 0
    transferred_bytes: int = 0
    duration: str = ""
    dry_run: bool = False


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
PREVIEW_RECEIPT_MAX_AGE_SECONDS = 24 * 60 * 60


def build_transfer_plan(job: TransferJob, files: list, checkpoint_files: Optional[set[str]] = None) -> dict:
    """Ilk 4 urun adimi icin makul bir plan uretir: dry run, resume skip, queue order, delete policy."""
    checkpoint_files = checkpoint_files or set()
    rel_names = [getattr(f, "rel_path", getattr(f, "path", "")) for f in files]
    transfer_candidates = []
    skipped_resume = []
    for rel in rel_names:
        if rel in checkpoint_files:
            skipped_resume.append(rel)
            continue
        transfer_candidates.append(rel)
    return {
        "dry_run": bool(getattr(job, "dry_run", False)),
        "resume_from_checkpoint": bool(getattr(job, "resume_from_checkpoint", False)),
        "would_transfer": transfer_candidates,
        "skipped_resume": skipped_resume,
        "would_delete": bool(
            getattr(job, "delete_after_transfer", False)
            and getattr(job, "delete_policy", "verified_only") == "verified_only"
            and getattr(job, "verification_mode", "FullHash") in ("FullHash", "SizeOnly")
        ),
        "delete_policy": getattr(job, "delete_policy", "verified_only"),
        "priority": int(getattr(job, "priority", 0)),
        "require_preview_for_delete": bool(getattr(job, "require_preview_for_delete", True)),
    }


def _deletion_guard(job: TransferJob, preview_matches: bool) -> tuple[bool, str]:
    """Return whether source deletion is permitted; every ambiguous state fails closed."""
    if not job.delete_after_transfer:
        return False, "Kaynak silme job ayarlarinda kapali."
    if job.delete_policy == "never":
        return False, "Silme politikasi 'never'."
    if job.delete_policy != "verified_only":
        return False, f"Desteklenmeyen silme politikasi: {job.delete_policy}."
    if job.verification_mode not in ("FullHash", "SizeOnly"):
        return False, f"Dosya-bazli dogrulama modu desteklenmiyor: {job.verification_mode}."
    if job.require_preview_for_delete and not preview_matches:
        return False, "Guncel dry-run onizlemesi yok veya kaynak envanteri onizlemeden sonra degisti."
    return True, ""


def _preview_signature(job: TransferJob, files: list) -> str:
    file_inventory = sorted(
        (str(item.rel_path), int(item.size), float(item.mtime))
        for item in files
    )
    payload = {
        "job": job.name,
        "source": os.path.normcase(os.path.abspath(job.source_path)),
        "destination": os.path.normcase(os.path.abspath(job.destination_path)),
        "file_filter": job.file_filter,
        "older_than_days": job.older_than_days,
        "verification_mode": job.verification_mode,
        "delete_after_transfer": job.delete_after_transfer,
        "delete_policy": job.delete_policy,
        "files": file_inventory,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _write_preview_receipt(path: Path, signature: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"signature": signature, "created_at": time.time()}),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _preview_receipt_matches(path: Path, signature: str) -> bool:
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(receipt, dict) or receipt.get("signature") != signature:
        return False
    created_at = receipt.get("created_at")
    if not isinstance(created_at, (int, float)):
        return False
    age_seconds = time.time() - created_at
    return 0 <= age_seconds <= PREVIEW_RECEIPT_MAX_AGE_SECONDS


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


def _render_html_report(summary: dict) -> str:
    """Calisma ozetini dis bagimliligi olmayan, tarayicida acilabilir HTML'e cevirir."""
    run_info = summary.get("run_info", {})
    result = summary.get("result", {})
    successful = bool(result.get("success"))
    cancelled = result.get("error_message") == "Kullanici tarafindan durduruldu"
    status_text = "BASARILI" if successful else ("DURDURULDU" if cancelled else "HATALI")
    status_color = "#18794e" if successful else ("#8a5a00" if cancelled else "#b42318")

    def esc(value) -> str:
        return html.escape(str(value if value not in (None, "") else "-"))

    elapsed = float(result.get("elapsed_seconds") or 0)
    transferred_bytes = int(result.get("transferred_bytes") or 0)
    speed = f"{transferred_bytes / elapsed / 1024 / 1024:.2f} MB/sn" if elapsed > 0 else "-"
    metrics = [
        ("Toplam dosya", result.get("total", 0)),
        ("Dogrulanan", result.get("verified", 0)),
        ("Basarisiz", result.get("failed", 0)),
        ("Eksik", result.get("missing", 0)),
        ("Atlanan", result.get("skipped", 0)),
        ("Aktarilan", format_size(transferred_bytes)),
        ("Ortalama hiz", speed),
        ("Toplam sure", f"{elapsed:.1f} sn"),
    ]
    metric_html = "".join(
        f'<div class="metric"><span>{esc(label)}</span><strong>{esc(value)}</strong></div>'
        for label, value in metrics
    )

    def file_link(label: str, path: str) -> str:
        if not path:
            return ""
        href = Path(path).resolve().as_uri()
        return f'<a href="{esc(href)}">{esc(label)}</a>'

    links = summary.get("log_files", {})
    link_html = " · ".join(
        link for label, key in (("Transfer logu", "main_log"), ("Robocopy logu", "robocopy_log"), ("Hash logu", "hash_log"))
        if (link := file_link(label, links.get(key, "")))
    ) or "Log dosyasi yok"

    failed_files = summary.get("failed_files", [])
    failed_rows = "".join(
        f"<tr><td>{esc(item.get('rel'))}</td><td>{esc(item.get('src_hash'))}</td>"
        f"<td>{esc(item.get('dst_hash'))}</td></tr>"
        for item in failed_files[:500]
    )
    if len(failed_files) > 500:
        failed_rows += f"<tr><td colspan=\"3\">Ilk 500 kayit gosteriliyor; tam liste hash/JSON raporunda ({len(failed_files)} dosya).</td></tr>"
    missing_files = summary.get("missing_files", [])
    missing_rows = "".join(
        f"<tr><td>{esc(item.get('rel') if isinstance(item, dict) else item)}</td></tr>"
        for item in missing_files[:500]
    )
    if len(missing_files) > 500:
        missing_rows += f"<tr><td>Ilk 500 kayit gosteriliyor; tam liste JSON raporunda ({len(missing_files)} dosya).</td></tr>"
    error_message = result.get("error_message", "")
    error_html = f'<p class="error">{esc(error_message)}</p>' if error_message else ""

    return f"""<!doctype html>
<html lang="tr">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(summary.get('job_name'))} - Transfer raporu</title>
<style>
body {{ margin: 0; background: #f3f5f7; color: #17212b; font: 15px/1.5 "Segoe UI", sans-serif; }}
main {{ max-width: 1080px; margin: 32px auto; padding: 0 20px; }}
header {{ display: flex; justify-content: space-between; align-items: start; gap: 20px; border-bottom: 1px solid #d7dee5; padding-bottom: 18px; }}
h1 {{ margin: 0; font-size: 26px; }} h2 {{ margin: 28px 0 10px; font-size: 18px; }}
.status {{ color: {status_color}; border: 1px solid currentColor; padding: 4px 10px; font-weight: 700; }}
.muted {{ color: #536273; }} .error {{ padding: 10px 12px; color: #8f1d16; background: #fdecea; }}
.metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 1px; background: #d7dee5; border: 1px solid #d7dee5; }}
.metric {{ background: white; padding: 12px 14px; }} .metric span {{ display: block; color: #536273; font-size: 13px; }}
.metric strong {{ display: block; margin-top: 4px; font-size: 18px; overflow-wrap: anywhere; }}
table {{ width: 100%; border-collapse: collapse; background: white; }} th, td {{ padding: 9px 10px; text-align: left; border-bottom: 1px solid #e1e6eb; overflow-wrap: anywhere; }}
th {{ background: #e9eef2; }} .details {{ display: grid; grid-template-columns: 160px minmax(0, 1fr); gap: 6px 14px; }}
.details strong {{ overflow-wrap: anywhere; }} a {{ color: #075e9c; }}
@media (max-width: 600px) {{ main {{ margin: 18px auto; }} header {{ display: block; }} .status {{ display: inline-block; margin-top: 12px; }} .details {{ grid-template-columns: 1fr; gap: 0; }} }}
</style>
<main>
<header><div><h1>{esc(summary.get('job_name'))}</h1><div class="muted">Run ID: {esc(summary.get('run_id'))}</div></div><span class="status">{status_text}</span></header>
{error_html}
<h2>Ozet</h2><section class="metrics">{metric_html}</section>
<h2>Calisma bilgileri</h2><div class="details">
<span>Baslangic</span><strong>{esc(summary.get('started_at'))}</strong>
<span>Bitis</span><strong>{esc(summary.get('finished_at'))}</strong>
<span>Kaynak</span><strong>{esc(run_info.get('source'))}</strong>
<span>Hedef</span><strong>{esc(run_info.get('destination'))}</strong>
<span>Dosya filtresi</span><strong>{esc(run_info.get('file_filter'))}</strong>
<span>Yas siniri</span><strong>{esc(run_info.get('age_days'))} gun</strong>
<span>Dogrulama</span><strong>{esc(run_info.get('verify_mode'))}</strong>
<span>Transfer sonrasi sil</span><strong>{esc(run_info.get('delete_after'))}</strong>
<span>Robocopy cikis kodu</span><strong>{esc(result.get('robocopy_exit'))}</strong>
<span>Robocopy suresi</span><strong>{esc(result.get('duration'))}</strong>
</div>
<h2>Basarisiz dosyalar</h2><table><thead><tr><th>Dosya</th><th>Kaynak hash</th><th>Hedef sonucu</th></tr></thead><tbody>{failed_rows or '<tr><td colspan="3">Basarisiz dosya yok</td></tr>'}</tbody></table>
<h2>Eksik dosyalar</h2><table><thead><tr><th>Dosya</th></tr></thead><tbody>{missing_rows or '<tr><td>Eksik dosya yok</td></tr>'}</tbody></table>
<h2>Loglar</h2><p>{link_html}</p>
</main></html>"""


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


def delete_files_parallel(paths: list[str], max_workers: Optional[int] = None,
                           min_parallel_count: int = 20,
                           progress_callback: Optional[Callable[[int, int], None]] = None,
                           cancel_event: Optional[threading.Event] = None,
                           max_retries: int = 1,
                           logger: Optional[EngineLogger] = None,
                           ) -> tuple[int, list[tuple[str, str]]]:
    """
    Kaynak dosyalari PARALEL siler. Eski sequential `for v in verified:
    os.remove(...)` dongusu, ozellikle UNC/ag yollarinda dosya basina ayri
    bir round-trip gecikmesi biriktirip 200bin+ dosyada SAATLERCE surebiliyor
    ve bazen tamamlanamiyordu (agir latency x cok dosya = pratikte "silemiyor"
    gibi gorunuyor). hash_files_parallel ile AYNI ThreadPoolExecutor deseni
    burada da gecikmeyi bircok istegi ayni anda ucusturarak ortuyor.

    Kilitli dosyalar (paylasim ihlali) veya gecici ag kesintileri yuzunden
    basarisiz olan silmeler, sadece basarisiz kalanlar uzerinde `max_retries`
    kadar (artan bekleme suresiyle) tekrar denenir - dosyalar bu asamaya
    gelmeden once zaten TAM olarak kopyalanip dogrulanmis oldugu icin, iş
    kalici bir hataya (ornegin dosya gercekten silinemez durumda) carpmadikca
    pes etmeden denemeye devam eder.

    Donus: (basarili_sayisi, [(yol, hata_mesaji), ...]) - hata listesinde
    sadece TUM denemeler tukendikten sonra HALA basarisiz olan dosyalar kalir.
    """
    if not paths:
        return 0, []
    if max_workers is None:
        max_workers = min(32, (os.cpu_count() or 4) * 4)

    def _delete_one(path: str) -> Optional[OSError]:
        try:
            os.remove(path)
            return None
        except FileNotFoundError:
            return None  # zaten yok - basarili sayilir
        except OSError as e:
            return e

    def _delete_batch(batch: list[str]) -> tuple[int, list[tuple[str, OSError]]]:
        batch_errors: list[tuple[str, OSError]] = []
        batch_success = 0
        if len(batch) < min_parallel_count or max_workers <= 1:
            for p in batch:
                if cancel_event is not None and cancel_event.is_set():
                    break
                err = _delete_one(p)
                if err is None:
                    batch_success += 1
                else:
                    batch_errors.append((p, err))
            return batch_success, batch_errors

        done = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_delete_one, p): p for p in batch}
            for future in concurrent.futures.as_completed(futures):
                path = futures[future]
                err = future.result()
                if err is None:
                    batch_success += 1
                else:
                    batch_errors.append((path, err))
                done += 1
                if progress_callback and done % 5000 == 0:
                    progress_callback(done, len(batch))
                if cancel_event is not None and cancel_event.is_set():
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
        return batch_success, batch_errors

    total_success = 0
    remaining = list(paths)
    last_errors: list[tuple[str, OSError]] = []
    attempts = max(1, max_retries)

    for attempt in range(1, attempts + 1):
        if not remaining or (cancel_event is not None and cancel_event.is_set()):
            break
        succ, errs = _delete_batch(remaining)
        total_success += succ
        last_errors = errs
        if not errs:
            remaining = []
            break
        remaining = [p for p, _ in errs]
        if attempt < attempts:
            wait_s = min(2.0 * attempt, 8.0)
            if logger is not None:
                in_use = sum(1 for _, e in errs if _is_file_in_use_error(e))
                logger.warn(
                    f"Silme basarisiz: {len(errs)} dosya (kilitli/kullanimda: {in_use}) - "
                    f"{wait_s:.0f}sn beklenip tekrar denenecek (deneme {attempt}/{attempts}).",
                    console=True,
                )
            time.sleep(wait_s)

    return total_success, [(p, str(e)) for p, e in last_errors]


def remove_empty_dirs(root: str) -> tuple[int, list[tuple[str, str]]]:
    """Kaynak koku haricindeki tum bos klasorleri alttan uste dogru siler."""
    removed = 0
    errors: list[tuple[str, str]] = []

    def on_walk_error(error: OSError) -> None:
        errors.append((error.filename or root, str(error)))

    for current_root, dirnames, _ in os.walk(root, topdown=False, onerror=on_walk_error):
        for dirname in dirnames:
            directory = os.path.join(current_root, dirname)
            if os.path.islink(directory):
                continue
            try:
                if not os.listdir(directory):
                    os.rmdir(directory)
                    removed += 1
            except FileNotFoundError:
                pass
            except OSError as error:
                errors.append((directory, str(error)))
    return removed, errors



# WinError 32 = ERROR_SHARING_VIOLATION ("dosya baska bir islem tarafindan
# kullaniliyor"). errno 13 (Permission denied) bazen ayni durumda POSIX
# tarafinda da gorulur, o yuzden ikisi de kontrol edilir.
_SHARING_VIOLATION_WINERROR = 32


def _is_file_in_use_error(e: OSError) -> bool:
    """Hata, hedef/kaynak dosyanin BASKA BIR ISLEM tarafindan acik/kilitli
    oldugunu (paylasim ihlali) mi gosteriyor tespit eder."""
    if getattr(e, "winerror", None) == _SHARING_VIOLATION_WINERROR:
        return True
    return isinstance(e, PermissionError)


def _retry_and_reverify_files(
    job: TransferJob,
    hash_table: dict[str, SourceFileInfo],
    pending: list[_PendingVerify],
    logger: EngineLogger,
    cancel_event: Optional[threading.Event],
) -> tuple[list[_PendingVerify], list[_PendingVerify]]:
    """
    Ilk denemede basarisiz/eksik cikan dosyalari (genellikle robocopy /MT
    coklu-thread calisirken kaynakta gecici bir paylasim ihlali, ya da tek
    tek dosyalarda olusan agir/gecici ag hatalari yuzunden) TEK TEK yeniden
    kopyalar ve yeniden dogrular. 200bin dosyadan sadece 1-2'sinin basarisiz
    oldugu (cok yaygin) senaryoda, TUM job'u basarisiz/silinmez saymak yerine
    bu birkac dosyayi otomatik onarmayi dener.

    Donus: (fixed, still_failed) - ikisi de _PendingVerify listesi; fixed
    olanlarin dst_hash/size/dst_path alanlari YENIDEN dogrulanmis degerlerle
    guncellenmis olarak, result="OK" ile doner.
    """
    if not pending:
        return [], []

    max_attempts = max(1, job.max_retries)
    # job.robocopy_threads TUM job-genelinde tek paralellik ayari (scan/robocopy/
    # hash/silme hepsi bunu kullanir) - burada da AYNI ayara uyulur, aksi halde
    # kullanici hassas/yavas bir ag icin dusuk tutsa bile tekrar deneme asamasi
    # sabit bir CPU-bazli deger ile ag'i yine de yuklerdi.
    max_workers = max(1, min(128, job.robocopy_threads))

    def _retry_one(item: _PendingVerify) -> tuple[_PendingVerify, bool]:
        info = hash_table.get(item.rel.lower())
        if info is None:
            return item, False
        dst_path = item.dst_path or os.path.join(job.destination_path, item.rel)
        last_err = ""
        in_use = False  # son hatanin "baska bir islem tarafindan kullaniliyor" olup olmadigi
        for attempt in range(1, max_attempts + 1):
            if cancel_event is not None and cancel_event.is_set():
                last_err = "Kullanici tarafindan durduruldu"
                break
            try:
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                shutil.copy2(info.src, dst_path)
            except OSError as e:
                last_err = str(e)
                in_use = _is_file_in_use_error(e)
                wait_s = min(2.0 * attempt, 8.0)
                if in_use:
                    logger.warn(
                        f"KULLANIMDA: {item.rel} baska bir islem/uygulama tarafindan kullaniliyor "
                        f"(deneme {attempt}/{max_attempts}) - {wait_s:.0f}sn beklenip tekrar denenecek.",
                        console=True,
                    )
                time.sleep(wait_s)
                continue

            try:
                sz = os.path.getsize(dst_path)
            except OSError as e:
                last_err = str(e)
                in_use = _is_file_in_use_error(e)
                time.sleep(min(2.0 * attempt, 8.0))
                continue
            item.size = sz  # her denemede hedefte GERCEKTE ne var onu yansitir (MISSING'den duzelen dosyada 0'da KALMASIN)

            if job.verification_mode == "FullHash":
                dh = _sha256_of_file(dst_path)
                ok = dh is not None and dh == info.hash
                dst_hash_final = dh or "(okunamadi)"
            else:
                ok = sz == info.size
                dst_hash_final = f"(boyut:{format_size(sz)})"

            if ok:
                item.dst_hash = dst_hash_final
                item.dst_path = dst_path
                item.result = "OK"
                return item, True
            last_err = "dogrulama basarisiz (hash/boyut hala uyusmuyor)"
            in_use = False

        if last_err:
            if in_use:
                item.dst_hash = f"(tekrar denendi, hala baska bir islem tarafindan kullaniliyor: {last_err})"
            else:
                item.dst_hash = f"(tekrar denendi, basarisiz: {last_err})"
        return item, False

    fixed: list[_PendingVerify] = []
    still_failed: list[_PendingVerify] = []

    if len(pending) < 4 or max_workers <= 1:
        for item in pending:
            result_item, ok = _retry_one(item)
            (fixed if ok else still_failed).append(result_item)
            logger.log(f"TEKRAR DENEME {'BASARILI' if ok else 'BASARISIZ'}: {result_item.rel}",
                       "SUCCESS" if ok else "ERROR", console=False)
        return fixed, still_failed

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_retry_one, item): item for item in pending}
        for future in concurrent.futures.as_completed(futures):
            result_item, ok = future.result()
            (fixed if ok else still_failed).append(result_item)
            logger.log(f"TEKRAR DENEME {'BASARILI' if ok else 'BASARISIZ'}: {result_item.rel}",
                       "SUCCESS" if ok else "ERROR", console=False)
    return fixed, still_failed


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
                  on_progress: Optional[Callable[[RobocopyProgress], None]] = None,
                  on_stage: Optional[Callable[[str], None]] = None,
                  checkpoint_files: Optional[set[str]] = None) -> TransferResult:
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
    on_stage: job'un hangi ASAMADA oldugunu ("Taraniyor", "Robocopy
              calisiyor", "Dogrulaniyor" vb.) bildiren opsiyonel callback.
              Birden fazla job ayni anda calisirken GUI'nin her satirda
              (dosya yuzdesi yerine) anlamli bir asama gostermesi icindir -
              bkz. main_window.py'deki gorev listesi (vCenter tarzi).
    """
    if run_id is None:
        run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    safe_name = sanitize_filename(job.name)
    log_dir = Path(job.log_dir or "C:\\TransferLogs")
    log_dir.mkdir(parents=True, exist_ok=True)
    run_started_at = datetime.datetime.now().astimezone()
    run_started_clock = time.monotonic()

    log_file = log_dir / f"Transfer_{safe_name}_{run_id}.log"
    robocopy_log = log_dir / f"Robocopy_{safe_name}_{run_id}.log"
    hash_log_file = log_dir / f"HashLog_{safe_name}_{run_id}.log"
    summary_json = log_dir / f"Summary_{safe_name}_{run_id}.json"
    report_html = log_dir / f"Report_{safe_name}_{run_id}.html"
    preview_receipt_path = log_dir / f"Preview_{safe_name}.json"
    checkpoint_path = log_dir / f"Checkpoint_{safe_name}.jsonl"

    logger = EngineLogger(log_file)
    checkpoint_journal: Optional[CheckpointJournal] = None
    if on_log:
        original_log = logger.log
        def hooked_log(message, level="INFO", console=True):
            original_log(message, level, console)
            # console=False olan satirlar (dosya-basina ayrintilar) sadece
            # log dosyasina yazilir, CLI/GUI canli akisina YANSITILMAZ.
            if console:
                on_log(f"[{level}] {message}")
        logger.log = hooked_log  # type: ignore

    def stage(text: str) -> None:
        if checkpoint_journal is not None:
            try:
                checkpoint_journal.record_stage(text)
            except OSError as e:
                logger.warn(f"Checkpoint asama bilgisi yazilamadi: {e}")
        if on_stage:
            on_stage(text)

    def record_verified_checkpoint(
        rel_path: str,
        size: int,
        mtime: float,
        verification_mode: str,
        verified_hash: str = "",
    ) -> None:
        if checkpoint_journal is None:
            return
        try:
            checkpoint_journal.record_verified(rel_path, size, mtime, verification_mode, verified_hash)
        except OSError as e:
            logger.warn(f"Checkpoint dosya kaydi yazilamadi ({rel_path}): {e}")

    hash_log_created = False  # HashLogWriter GERCEKTEN tamamlanip dosyayi kapattiginda True olur
    report_data: dict = {}

    def result(success: bool, error: str = "", **extra) -> TransferResult:
        if checkpoint_journal is not None:
            checkpoint_status = (
                "success" if success else
                "cancelled" if error == "Kullanici tarafindan durduruldu" else
                "failed"
            )
            try:
                checkpoint_journal.finish(
                    checkpoint_status,
                    extra.get("verified_files", 0),
                    extra.get("total_files", 0),
                    error,
                )
            except OSError as e:
                logger.warn(f"Checkpoint sonucu yazilamadi: {e}")
        finished_at = datetime.datetime.now().astimezone()
        elapsed_seconds = round(time.monotonic() - run_started_clock, 2)
        report_summary = {
            "job_name": job.name,
            "run_id": run_id,
            "started_at": run_started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds"),
            "report_file": str(report_html),
            "run_info": {
                "source": job.source_path,
                "destination": job.destination_path,
                "file_filter": job.file_filter,
                "age_days": job.older_than_days,
                "delete_after": job.delete_after_transfer,
                "verify_mode": job.verification_mode,
                "robocopy_mt": job.robocopy_threads,
            },
            "result": {
                "success": success,
                "error_message": error,
                "dry_run": bool(extra.get("dry_run", job.dry_run)),
                "robocopy_exit": extra.get("robocopy_exit"),
                "total": extra.get("total_files", 0),
                "verified": extra.get("verified_files", 0),
                "failed": extra.get("failed_files", 0),
                "missing": extra.get("missing_files", 0),
                "skipped": extra.get("skipped_files", 0),
                "transferred_bytes": extra.get("transferred_bytes", 0),
                "duration": extra.get("duration", ""),
                "elapsed_seconds": elapsed_seconds,
            },
            "log_files": {
                "main_log": str(log_file),
                "hash_log": str(hash_log_file) if hash_log_created else "",
                "robocopy_log": str(robocopy_log),
            },
            "failed_files": [],
            "missing_files": [],
        }
        if report_data:
            report_summary.update(report_data)
            report_summary["run_info"] = {**report_summary["run_info"], **report_data.get("run_info", {})}
            report_summary["result"] = {**report_summary["result"], **report_data.get("result", {})}
            report_summary["log_files"] = {**report_summary["log_files"], **report_data.get("log_files", {})}
        report_summary["started_at"] = run_started_at.isoformat(timespec="seconds")
        report_summary["finished_at"] = finished_at.isoformat(timespec="seconds")
        report_summary["report_file"] = str(report_html)
        report_summary["result"].update(
            success=success, error_message=error, elapsed_seconds=elapsed_seconds,
        )
        summary_json.write_text(json.dumps(report_summary, indent=2, ensure_ascii=False), encoding="utf-8")
        report_html.write_text(_render_html_report(report_summary), encoding="utf-8")
        return TransferResult(
            job_name=job.name, overall_success=success, error_message=error,
            log_file=str(log_file), robocopy_log=str(robocopy_log),
            hash_log_file=str(hash_log_file) if hash_log_created else "",
            summary_json=str(summary_json), report_file=str(report_html), run_id=run_id, **extra,
        )

    logger.header("=" * 64)
    logger.header(f" TRANSFER JOB: {job.name}  (RunId: {run_id})")
    logger.header("=" * 64)
    logger.info(f"Kaynak    : {job.source_path}")
    logger.info(f"Hedef     : {job.destination_path}")
    logger.info(f"Filtre    : {job.file_filter}  |  Yas: >{job.older_than_days} gun")
    logger.info(f"Dogrulama : {job.verification_mode}  |  Robocopy /MT: {job.robocopy_threads}")
    if job.dry_run:
        logger.warn("Dry-run modu aktif: gercek transfer yapilmadan sadece onizleme uretilecek.")
    if job.resume_from_checkpoint:
        logger.warn("Robocopy restartable modu aktif (/Z): yarim kalan dosya kopyalari surdurulebilir.")
    logger.header("-" * 64)

    stage("Baslatiliyor")
    smb_targets: list[str] = []
    job_lock = JobLock(lock_dir, job.name) if lock_dir else None
    try:
        if job_lock:
            try:
                job_lock.acquire()
            except JobLockError as e:
                logger.error(str(e))
                return result(False, str(e))

        try:
            checkpoint_journal = CheckpointJournal(
                checkpoint_path,
                checkpoint_signature(job),
                resume=job.resume_from_checkpoint,
            )
        except OSError as e:
            logger.error(f"Checkpoint acilamadi: {e}")
            if job.resume_from_checkpoint:
                return result(False, f"Checkpoint acilamadi: {e}")
        if job.resume_from_checkpoint:
            if checkpoint_journal is not None and checkpoint_journal.resumed:
                logger.info(f"Checkpoint yuklendi: {len(checkpoint_journal.records):,} onceki dogrulanmis dosya kaydi.")
            else:
                logger.info("Uyumlu checkpoint bulunamadi; yeni checkpoint baslatiliyor.")

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
            if job.dry_run:
                logger.info("Hedef henuz yok; dry-run sirasinda olusturulmayacak.")
            else:
                logger.warn("Hedef yok, olusturuluyor...")
                try:
                    os.makedirs(job.destination_path, exist_ok=True)
                except OSError as e:
                    logger.error(f"Hedef olusturulamadi: {e}")
                    return result(False, "Hedef olusturulamadi")

        # ---- Yas filtreli dosya listesi (hizli paralel tarama) ----
        stage("Kaynak taraniyor")
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
        source_mtime_by_rel = {item.rel_path.casefold(): item.mtime for item in src_files}
        if checkpoint_journal is not None and checkpoint_journal.resumed:
            matching_checkpoint_files = checkpoint_journal.matching_count(src_files)
            logger.info(
                f"Checkpoint'te kaynak metadata'si eslesen {matching_checkpoint_files:,} dosya var. "
                "Robocopy mevcut dosyalari kontrol edecek ve tum dosyalar yeniden dogrulanacak."
            )

        checkpoint_set = set(checkpoint_files or set())
        all_src_files_for_plan = list(src_files)
        current_preview_signature = _preview_signature(job, all_src_files_for_plan)
        preview_matches = (
            scan_result.error_count == 0
            and _preview_receipt_matches(preview_receipt_path, current_preview_signature)
        )
        if job.resume_from_checkpoint and checkpoint_set:
            plan = build_transfer_plan(job, all_src_files_for_plan, checkpoint_set)
            if plan["skipped_resume"]:
                logger.warn(f"Resume/checkpoint nedeniyle atlanacak dosya sayisi: {len(plan['skipped_resume'])}")
                for rel in plan["skipped_resume"][:10]:
                    logger.info(f"  Atlandi: {rel}")
            src_files = [f for f in src_files if getattr(f, "rel_path", getattr(f, "path", "")) not in checkpoint_set]

        if job.dry_run:
            stage("Dry-run onizleme")
            preview_plan = build_transfer_plan(job, all_src_files_for_plan, checkpoint_set)
            logger.success(f"Dry-run tamamlandi. {len(preview_plan['would_transfer'])} dosya tasinacak; {len(preview_plan['skipped_resume'])} dosya atlandi.")
            if job.delete_after_transfer:
                if job.delete_policy == "verified_only" and job.verification_mode != "None" and scan_result.error_count == 0:
                    try:
                        _write_preview_receipt(preview_receipt_path, current_preview_signature)
                        logger.success("Silme onizlemesi kaydedildi; gercek calistirmada envanter ayniysa dogrulanmis dosyalar silinebilir.")
                    except OSError as e:
                        logger.error(f"Silme onizlemesi kaydedilemedi; kaynak silme engellenecek: {e}")
                else:
                    logger.warn("Silme onizlemesi onaylanamadi: politika, dogrulama modu veya tarama hatalarini kontrol edin.")
            if preview_plan["would_transfer"]:
                for rel in preview_plan["would_transfer"][:20]:
                    logger.info(f"  Preview: {rel}")
            return result(
                True,
                total_files=len(preview_plan["would_transfer"]),
                skipped_files=len(preview_plan["skipped_resume"]) + skipped,
                transferred_bytes=0,
                dry_run=True,
            )

        if job.delete_after_transfer and job.require_preview_for_delete:
            if preview_matches:
                logger.info("Silme onizlemesi mevcut ve kaynak envanteriyle eslesiyor.")
            else:
                logger.warn("Eslesen, hatasiz bir dry-run onizlemesi yok; transfer devam edebilir ancak kaynak silme engellenecek.")

        if not src_files:
            logger.success("Tasinacak dosya yok.")
            return result(True, total_files=0, skipped_files=skipped)

        src_total_bytes = sum(f.size for f in src_files)
        logger.info(f"Transfer boyutu: {format_size(src_total_bytes)}")

        # ---- Disk kontrolu ----
        stage("Disk kontrol ediliyor")
        proceed, _ = check_disk_and_alert(job.destination_path, src_total_bytes, job, logger)
        if not proceed:
            return result(False, "Disk kontrolu basarisiz")

        # ---- Kaynak hash/boyut (tarama sirasinda alinan boyut/mtime tekrar KULLANILIR, yeniden stat YOK) ----
        hash_table: dict[str, SourceFileInfo] = {}

        if job.verification_mode != "None":
            logger.header("-" * 64)
            mode_label = "boyut karsilastirmasi" if job.verification_mode == "SizeOnly" else "SHA256 hash (paralel)"
            stage(f"Kaynak analizi ({mode_label})")
            logger.info(f"Kaynak analizi basliyor — {mode_label} ({len(src_files)} dosya)...")
            t0 = time.time()

            if job.verification_mode == "FullHash":
                path_strs = [f.path for f in src_files]
                hash_map = hash_files_parallel(path_strs, max_workers=job.robocopy_threads)
                for f in src_files:
                    h = hash_map.get(f.path)
                    if h:
                        hash_table[f.rel_path.lower()] = SourceFileInfo(hash=h, size=f.size, src=f.path, rel=f.rel_path)
                    else:
                        logger.warn(f"Hash alinamadi: {f.path}", console=False)
            else:  # SizeOnly - boyut zaten tarama sirasinda alindi, ekstra islem gerekmez
                for f in src_files:
                    hash_table[f.rel_path.lower()] = SourceFileInfo(hash=f"SIZE:{f.size}", size=f.size, src=f.path, rel=f.rel_path)

            logger.success(f"Kaynak analizi tamam: {len(hash_table)}/{len(src_files)}  ({time.time()-t0:.1f}sn)")
        else:
            # VerificationMode=None: hash_table hicbir yerde kullanilmiyor,
            # doldurmak sadece bellek/CPU israfi - atlaniyor.
            logger.warn("VerificationMode=None: dogrulama atlanacak.")

        # ---- Robocopy ----
        stage("Robocopy calisiyor")
        logger.header("-" * 64)
        logger.info(f"Robocopy basliyor (/MT:{job.robocopy_threads})...")
        exit_code, duration = run_robocopy_with_progress(
            job.source_path, job.destination_path, job.file_filter,
            job.older_than_days, job.max_retries, job.robocopy_threads, str(robocopy_log),
            on_progress=on_progress, cancel_event=cancel_event, total_files=len(src_files),
            restartable=job.resume_from_checkpoint,
            on_log=lambda msg: logger.warn(msg, console=True),
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
            stage("Hedef taraniyor (dogrulama)")
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
            stage("Dogrulama yapiliyor")

            # HashLogWriter: her girdi HEMEN diske yazilir, bellekte TUM girdileri
            # tutan bir liste (eski hash_entries) ARTIK YOK. 400bin+ dosyada bu,
            # sadece hash log uretimi icin gereken bellegi dosya sayisindan
            # BAGIMSIZ (sabit) hale getirir.
            with HashLogWriter(hash_log_file, job.name, run_id, job.source_path,
                                job.destination_path, job.verification_mode) as hlw:
                # Basarisiz/eksik cikan dosyalarin hlw.add_entry() cagrisi
                # BILEREK burada YAPILMAZ - asagidaki tekrar-deneme asamasi
                # bitene kadar ERTELENIR, boylece hash logunda ayni dosya
                # icin cift satir (once MISMATCH, sonra tekrar OK) OLUSMAZ;
                # her dosya icin TEK ve NIHAI sonuc yazilir.
                pending_negative: list[_PendingVerify] = []
                if job.verification_mode == "FullHash":
                    dst_paths = []
                    rel_map = {}
                    for rel_key, info in hash_table.items():
                        dst_entry = dst_index.get(rel_key)
                        if dst_entry is not None:
                            dst_paths.append(dst_entry.path)
                            rel_map[dst_entry.path] = rel_key
                        else:
                            logger.warn(f"EKSIK: {info.rel}", console=False)
                            missing.append(info.rel)
                            pending_negative.append(_PendingVerify(info.rel, info.hash, "-", 0, "MISSING", None))

                    dst_hash_map = hash_files_parallel(dst_paths, max_workers=job.robocopy_threads) if dst_paths else {}

                    for dst_path in dst_paths:
                        rel_key = rel_map[dst_path]
                        info = hash_table[rel_key]
                        dh = dst_hash_map.get(dst_path)
                        sz = dst_index[rel_key].size  # taramadan geldi, ekstra stat() YOK

                        if dh is None:
                            logger.error(f"HASH ALINAMADI: {info.rel}", console=False)
                            failed.append(FailedFileInfo(rel=info.rel, src_hash=info.hash, dst_hash="(okunamadi)"))
                            pending_negative.append(_PendingVerify(info.rel, info.hash, "(okunamadi)", sz, "ERROR", dst_path))
                        elif dh == info.hash:
                            verified.append(VerifiedFileInfo(rel=info.rel, size=sz, src_file=info.src, dst_file=dst_path))
                            hlw.add_entry(info.rel, info.hash, dh, sz, "OK")
                            record_verified_checkpoint(
                                info.rel, info.size, source_mtime_by_rel.get(rel_key, 0.0),
                                "FullHash", info.hash,
                            )
                        else:
                            logger.error(f"HASH UYUMSUZ: {info.rel}  Kaynak={info.hash}  Hedef={dh}", console=False)
                            failed.append(FailedFileInfo(rel=info.rel, src_hash=info.hash, dst_hash=dh))
                            pending_negative.append(_PendingVerify(info.rel, info.hash, dh, sz, "MISMATCH", dst_path))
                else:  # SizeOnly - boyut karsilastirmasi tamamen bellek-ici, sifir ekstra I/O
                    for rel_key, info in hash_table.items():
                        dst_entry = dst_index.get(rel_key)
                        src_hash_label = f"(boyut:{format_size(info.size)})"
                        if dst_entry is None:
                            logger.warn(f"EKSIK: {info.rel}", console=False)
                            missing.append(info.rel)
                            pending_negative.append(_PendingVerify(info.rel, src_hash_label, "-", 0, "MISSING", None))
                            continue
                        sz = dst_entry.size
                        if sz == info.size:
                            verified.append(VerifiedFileInfo(rel=info.rel, size=sz, src_file=info.src, dst_file=dst_entry.path))
                            hlw.add_entry(info.rel, src_hash_label, f"(boyut:{format_size(sz)})", sz, "OK")
                            record_verified_checkpoint(
                                info.rel, info.size, source_mtime_by_rel.get(rel_key, 0.0),
                                "SizeOnly",
                            )
                        else:
                            logger.error(f"BOYUT UYUMSUZ: {info.rel}  Kaynak={format_size(info.size)}  Hedef={format_size(sz)}", console=False)
                            failed.append(FailedFileInfo(rel=info.rel, src_hash=f"SIZE:{info.size}", dst_hash=f"SIZE:{sz}"))
                            pending_negative.append(_PendingVerify(info.rel, src_hash_label, f"(boyut:{format_size(sz)})", sz, "MISMATCH", dst_entry.path))

                # ---- Basarisiz/eksik dosyalari tekrar dene ----
                # Buyuk transferlerde (ornegin 200bin dosya) genellikle
                # sadece BIRKAC dosya (gecici paylasim ihlali, kaynakta
                # ayni anda degisen dosya vb.) basarisiz olur - bunlari
                # otomatik olarak tekrar kopyalayip dogrulamak, TUM job'u
                # "basarisiz + silinmedi" durumuna dusurmek yerine sadece
                # gercekten sorunlu olanlari ayirt etmemizi saglar.
                if pending_negative:
                    stage("Hatali/eksik dosyalar tekrar deneniyor")
                    logger.warn(f"{len(pending_negative)} dosya ilk denemede basarisiz/eksik cikti - kopyalama tekrar denenip yeniden dogrulanacak...")
                    fixed, still_bad = _retry_and_reverify_files(job, hash_table, pending_negative, logger, cancel_event)
                    fixed_rels = {f.rel for f in fixed}
                    failed = [f for f in failed if f.rel not in fixed_rels]
                    missing = [m for m in missing if m not in fixed_rels]
                    for f in fixed:
                        src_info = hash_table[f.rel.lower()]
                        verified.append(VerifiedFileInfo(rel=f.rel, size=f.size, src_file=src_info.src, dst_file=f.dst_path))
                        hlw.add_entry(f.rel, f.src_hash, f.dst_hash, f.size, "OK")
                        record_verified_checkpoint(
                            f.rel, src_info.size, source_mtime_by_rel.get(f.rel.casefold(), 0.0),
                            job.verification_mode, src_info.hash if job.verification_mode == "FullHash" else "",
                        )
                    for f in still_bad:
                        hlw.add_entry(f.rel, f.src_hash, f.dst_hash, f.size, f.result)
                    if fixed:
                        logger.success(f"Tekrar deneme sonucu {len(fixed)} dosya duzeltildi ve dogrulandi - bu dosyalar da kaynaktan silinecek.")
                    if still_bad:
                        logger.error(f"Tekrar denemeye ragmen {len(still_bad)} dosya hala basarisiz/eksik kaldi - bu dosyalar kaynaktan SILINMEYECEK.")

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
            logger.error(f"Robocopy FATAL bildirdigi icin is BASARISIZ sayildi, ancak dogrulanan {verified_count} dosya tutarli cikti - kismi bir basari olabilir, log'u inceleyin.")
        else:
            logger.error(f"DOGRULAMA HATASI! Basarisiz={failed_count} Eksik={missing_count}")
            send_alert_mail(job.smtp_server, job.mail_from, job.mail_to,
                             f"[HATA] {job.name} - Dogrulama", f"Basarisiz={failed_count} Eksik={missing_count}", logger)

        def emit_delete_progress(done: int, total: int, current_file: str = "") -> None:
            if on_progress is None:
                return
            if total <= 0:
                return
            percent = round((done / total) * 100, 1)
            try:
                on_progress(RobocopyProgress(
                    current_file=current_file or "Siliniyor...",
                    percent=percent,
                    files_done=min(done, total),
                    files_total=total,
                    percent_overall=percent,
                ))
            except Exception:
                pass

        # ---- Silme ----
        if job.delete_after_transfer:
            deletion_allowed, deletion_reason = _deletion_guard(job, preview_matches)
            if not deletion_allowed:
                logger.warn(f"Kaynak dosyalar SILINMEDI: {deletion_reason}")
            elif not verified:
                logger.warn("Silinecek dogrulanmis dosya yok — kaynak SILINMEDI.")
            else:
                stage("Kaynak dosyalar siliniyor")
                logger.warn(f"Kaynak dosyalar siliniyor... ({len(verified)} dogrulanmis dosya)")
                verified_paths = [item.src_file for item in verified]

                def on_delete_progress(done, tot):
                    emit_delete_progress(done, tot, "Kaynak dosyalar siliniyor")
                    logger.info(f"  Siliniyor... {done:,}/{tot:,} dosya")

                del_ok, del_errors = delete_files_parallel(
                    verified_paths,
                    max_workers=job.robocopy_threads,
                    progress_callback=on_delete_progress,
                    cancel_event=cancel_event,
                    max_retries=job.max_retries,
                    logger=logger,
                )
                for path, error in del_errors:
                    logger.error(f"Silinemedi: {path} — {error}", console=False)
                if not del_errors:
                    logger.success(f"Dogrulanan {del_ok} kaynak dosya silindi.")
                else:
                    logger.warn(f"{del_ok} dosya silindi, {len(del_errors)} dosya silinemedi (detay: log dosyasi).")
                dirs_removed, dir_errors = remove_empty_dirs(job.source_path)
                if dirs_removed:
                    logger.info(f"Bos kalan {dirs_removed} kaynak klasoru de silindi.")
                for directory, error in dir_errors:
                    logger.warn(f"Bos klasor silinemedi: {directory} — {error}", console=False)

            if failed or missing:
                logger.error(
                    f"{len(failed) + len(missing)} dosya tekrar denemeye ragmen basarisiz/eksik kaldi — "
                    f"BU DOSYALAR KAYNAKTA KORUNDU (silinmedi). Detaylar hash logunda: {hash_log_file}"
                )

        # ---- JSON ve HTML rapor ozeti ----
        summary = {
            "job_name": job.name, "run_id": run_id,
            "run_info": {
                "source": job.source_path, "destination": job.destination_path,
            "file_filter": job.file_filter,
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

        stage("Tamamlandi")
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

        summary["result"]["error_message"] = final_error
        report_data.update(summary)
        return result(
            job_ok, final_error, total_files=total, verified_files=verified_count,
            failed_files=failed_count, missing_files=missing_count,
            skipped_files=skipped, transferred_bytes=transferred_bytes,
            duration=str(duration),
        )

    finally:
        if checkpoint_journal is not None:
            try:
                checkpoint_journal.close()
            except OSError as e:
                logger.warn(f"Checkpoint kapatilamadi: {e}")
        if credential_store:
            for target in smb_targets:
                credential_store.unregister_smb_session(target)
                logger.info(f"SMB oturumu temizlendi: {target}", console=False)
        if job_lock:
            job_lock.release()
        logger.close()  # EngineLogger artik dosya tanitcisini surekli acik tutuyor - burada kapatiliyor
