"""
scheduler.py
------------
Windows Gorev Zamanlayici (Task Scheduler) entegrasyonu.

PowerShell'in ScheduledTask cmdlet'leri yerine `schtasks.exe` kullanilir:
- Hicbir ekstra modul/PSSnapin gerektirmez
- Cikti/hata kodu net ve script'ten kolayca kontrol edilir
- PyInstaller ile paketlenen exe'den subprocess olarak sorunsuz cagrilir
  (pywin32/COM API'sine kiyasla cok daha az "paketleme sorunu" riski)
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Optional

try:
    from .logutil import sanitize_filename
except ImportError:  # PyInstaller / flat-module execution compatibility
    from logutil import sanitize_filename


WEEKDAY_MAP = {
    "Monday": "MON", "Tuesday": "TUE", "Wednesday": "WED", "Thursday": "THU",
    "Friday": "FRI", "Saturday": "SAT", "Sunday": "SUN",
}


def task_name_for_job(job_name: str) -> str:
    return f"DataTransfer_{sanitize_filename(job_name)}"


@dataclass
class ScheduleResult:
    success: bool
    message: str = ""


def register_scheduled_task(
    job_name: str,
    exe_path: str,
    schedule_frequency: str,
    schedule_time: str,
    schedule_weekly_day: str,
    run_as_user: str,
    config_path: str,
    run_as_password: Optional[str] = None,
) -> ScheduleResult:
    """
    Job'u Windows Gorev Zamanlayici'ya kaydeder. exe_path, --run-job
    argumaniyla cagrilacak calistirilabilir dosyanin (bu programin kendi
    exe'si veya python.exe + script) tam yoludur.
    """
    task_name = task_name_for_job(job_name)
    task_run = f'"{exe_path}" --run-job "{job_name}" --config "{config_path}"'

    schedule_map = {"Daily": "DAILY", "Weekly": "WEEKLY", "Monthly": "MONTHLY"}
    sc = schedule_map.get(schedule_frequency, "DAILY")

    args = [
        "schtasks", "/Create", "/F",
        "/TN", task_name,
        "/TR", task_run,
        "/SC", sc,
        "/ST", schedule_time,
    ]

    if sc == "WEEKLY":
        day_code = WEEKDAY_MAP.get(schedule_weekly_day, "SUN")
        args += ["/D", day_code]
    elif sc == "MONTHLY":
        args += ["/D", "1"]

    if run_as_user.upper() == "SYSTEM":
        args += ["/RU", "SYSTEM"]
    else:
        # /RP '*' schtasks'a parolayi ETKILESIMLI olarak sormasini soyler -
        # bu surec subprocess.run ile stdin'i etkilesimli olmayan bir
        # baglamdan cagrildigi icin ya hemen hata verir ya da timeout'a
        # kadar askida kalirdi. Gercek parola olmadan gorev olusturmayi
        # tamamen REDDEDIYORUZ (kullaniciya net bir hata donuyoruz).
        if not run_as_password:
            return ScheduleResult(
                False,
                f"'{run_as_user}' hesabiyla calistirmak icin parola gerekli. "
                "SYSTEM disinda bir hesap secildiyse parola saglanmalidir.",
            )
        args += ["/RU", run_as_user, "/RP", run_as_password]

    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            return ScheduleResult(True, f"Gorev olusturuldu: {task_name}")
        return ScheduleResult(False, proc.stderr.strip() or proc.stdout.strip())
    except (OSError, subprocess.SubprocessError) as e:
        return ScheduleResult(False, str(e))


def unregister_scheduled_task(job_name: str) -> ScheduleResult:
    task_name = task_name_for_job(job_name)
    try:
        proc = subprocess.run(
            ["schtasks", "/Delete", "/TN", task_name, "/F"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0:
            return ScheduleResult(True, f"Gorev kaldirildi: {task_name}")
        return ScheduleResult(False, proc.stderr.strip() or proc.stdout.strip())
    except (OSError, subprocess.SubprocessError) as e:
        return ScheduleResult(False, str(e))


def task_exists(job_name: str) -> bool:
    task_name = task_name_for_job(job_name)
    try:
        proc = subprocess.run(
            ["schtasks", "/Query", "/TN", task_name],
            capture_output=True, text=True, timeout=15,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
