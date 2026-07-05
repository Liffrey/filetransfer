# File Transfer Console

A Python implementation of the PowerShell/WinForms file transfer tool using PySide6.

## Features

- GUI and CLI modes
- Scheduled task support via Windows Task Scheduler
- Robocopy-based transfer engine
- Hash verification and logging
- Credential storage support
- Single-file EXE packaging with PyInstaller

## Development and Run

```bash
pip install -r requirements.txt
python main.py
```

## Build to a Single EXE

```bash
python build_exe.py
```

Output: dist/TransferConsole.exe

## Scheduled Task Usage

The GUI "Schedule" action creates a scheduled task similar to:

```powershell
TransferConsole.exe --run-job "JobName" --config "C:\ProgramData\DataTransferTool\jobs.json"
```

## Project Structure

```text
main.py                  Entry point (GUI + CLI)
engine/
  config.py              Job configuration (jobs.json) with atomic write + backup
  transfer.py            Transfer engine with Robocopy, hashing, disk checks
  credentials.py         Credential storage with DPAPI (Windows) + SMB pre-auth
  scheduler.py           Windows Task Scheduler integration
  logutil.py             Logging, disk info, hash log generation
gui/
  main_window.py         Main window UI
  job_editor.py          Add/Edit job dialog
  cred_manager.py        Credential manager dialog
build_exe.py             PyInstaller build script
requirements.txt
```

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

