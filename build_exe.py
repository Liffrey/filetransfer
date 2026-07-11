"""
build_exe.py
------------
Projeyi tek bir TransferConsole.exe dosyasina derler (PyInstaller).

Kullanim:
    pip install -r requirements.txt
    python build_exe.py

Cikti: dist/TransferConsole.exe  (TEK DOSYA - baska hicbir dosyaya
gerek yoktur, PowerShell surumunun aksine TransferEngine.psm1 veya
Run-TransferJob.ps1 gibi ayri dosyalar tasima geregi YOKTUR).

Gorev Zamanlayici, ayni exe'yi asagidaki gibi cagirir:
    TransferConsole.exe --run-job "JobAdi" --config "C:\\ProgramData\\DataTransferTool\\jobs.json"
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _python_executable() -> str:
    """Use the workspace venv interpreter when present; otherwise fall back to the current interpreter."""
    candidates = [
        HERE / ".venv" / "Scripts" / "python.exe",
        HERE / ".venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def main():
    python_exe = _python_executable()
    args = [
        python_exe, "-m", "PyInstaller",
        "--name", "TransferConsole",
        "--onefile",
        "--windowed",              # konsol penceresi acmaz (GUI modunda)
        "--noconfirm",
        "--clean",
        "--distpath", str(HERE / "dist"),
        "--workpath", str(HERE / "build"),
        "--specpath", str(HERE),
        "--hidden-import", "config",
        "--hidden-import", "credentials",
        "--hidden-import", "transfer",
        "--hidden-import", "scheduler",
        "--hidden-import", "logutil",
        "--hidden-import", "main_window",
        "--hidden-import", "job_editor",
        "--hidden-import", "cred_manager",
        "--hidden-import", "engine",
        "--hidden-import", "gui",
        str(HERE / "main.py"),
    ]
    print("Calistiriliyor:", " ".join(args))
    result = subprocess.run(args)
    if result.returncode != 0:
        print("\nDERLEME BASARISIZ.", file=sys.stderr)
        sys.exit(1)

    exe_path = HERE / "dist" / "TransferConsole.exe"
    print(f"\n=== BASARILI ===")
    print(f"EXE: {exe_path}")
    print(f"\nBu TEK dosyayi istediginiz sunucuya kopyalayabilirsiniz.")
    print(f"Gorev Zamanlayici icin komut satiri ornegi:")
    print(f'  "{exe_path}" --run-job "JobAdi" --config "C:\\ProgramData\\DataTransferTool\\jobs.json"')


if __name__ == "__main__":
    main()
