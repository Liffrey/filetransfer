"""
main.py
-------
Giris noktasi. Iki modda calisir:

1) GUI modu (varsayilan):           python main.py
2) CLI modu (Gorev Zamanlayici icin): python main.py --run-job "JobAdi" [--config yol] [--run-id id]

PyInstaller ile derlendiginde TEK BIR exe hem GUI hem CLI modunu destekler -
Gorev Zamanlayici, ayni exe'yi --run-job argumaniyla cagirir. Bu, PowerShell
surumunde ayri TransferGUI.ps1/Run-TransferJob.ps1/TransferEngine.psm1
dosyalarina ihtiyac duyulmasinin aksine, TEK DOSYA dagitimini mumkun kilar.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def get_default_data_dir() -> Path:
    """Windows'ta %ProgramData%\\DataTransferTool, diger platformlarda ~/.datatransfertool."""
    if sys.platform == "win32":
        base = os.environ.get("ProgramData", r"C:\ProgramData")
        return Path(base) / "DataTransferTool"
    return Path.home() / ".datatransfertool"


def run_cli(job_name: str, config_path: str, run_id: str | None) -> int:
    """Tek bir job'u headless calistirir (Gorev Zamanlayici / arka plan icin)."""
    from engine.config import JobConfigStore
    from engine.credentials import CredentialStore
    from engine.transfer import run_transfer

    data_dir = Path(config_path).parent
    cred_store = CredentialStore(data_dir / "Credentials")
    config_store = JobConfigStore(config_path)

    job = config_store.get_job(job_name)
    if job is None:
        print(f"HATA: Job bulunamadi: {job_name}", file=sys.stderr)
        return 1

    if not job.enabled:
        print(f"Job '{job_name}' devre disi, atlaniyor.")
        return 0

    result = run_transfer(job, run_id=run_id, credential_store=cred_store,
                           on_log=lambda line: print(line), lock_dir=str(data_dir / "locks"))

    status = "Basarili" if result.overall_success else "Hatali"
    message = result.error_message or f"{result.verified_files} dosya dogrulandi"
    ok = config_store.update_run_result(
        job_name, status, message, result.log_file, result.hash_log_file,
    )
    if not ok:
        print("UYARI: Job durumu config'e yazilamadi (guvenlik engeli veya IO hatasi).", file=sys.stderr)

    return 0 if result.overall_success else 1


def run_gui(config_path: str, cred_dir: str) -> int:
    from PySide6.QtWidgets import QApplication
    from gui.main_window import MainWindow
    from gui.style import build_app_stylesheet

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(build_app_stylesheet())

    exe_path = sys.executable if not getattr(sys, "frozen", False) else sys.argv[0]
    window = MainWindow(config_path=config_path, cred_dir=cred_dir, exe_path=exe_path)
    window.show()
    return app.exec()


def main() -> int:
    parser = argparse.ArgumentParser(description="Veri Transfer Konsolu")
    parser.add_argument("--run-job", metavar="JOB_ADI", help="Belirtilen job'u headless calistirir (GUI acmaz)")
    parser.add_argument("--config", metavar="YOL", help="jobs.json tam yolu (varsayilan: %%ProgramData%%\\DataTransferTool\\jobs.json)")
    parser.add_argument("--run-id", metavar="ID", help="Log dosyasi adlandirmasi icin calisma kimligi")
    args = parser.parse_args()

    data_dir = get_default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = args.config or str(data_dir / "jobs.json")
    cred_dir = str(data_dir / "Credentials")

    # Proje kok dizinini import yoluna ekle (PyInstaller ile derlenmis exe'de
    # gerekmez ama gelistirme/dogrudan script calistirmada gerekli olabilir)
    project_root = str(Path(__file__).resolve().parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    if args.run_job:
        return run_cli(args.run_job, config_path, args.run_id)
    return run_gui(config_path, cred_dir)


if __name__ == "__main__":
    sys.exit(main())
