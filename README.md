# File Transfer Console

File Transfer Console is a PySide6-based Windows file transfer utility that provides a GUI for managing transfer jobs, while also supporting a headless CLI mode for scheduled execution.

## Features

- GUI and CLI execution modes
- Run multiple transfer jobs concurrently from the GUI, each with its own live per-file progress shown in its grid row
- Windows Task Scheduler integration
- Robocopy-based transfer engine
- Hash verification and log generation
- Credential storage with Windows DPAPI-backed persistence
- Single-file EXE packaging with PyInstaller
- Adaptive light/dark styling based on the active system theme
- Import compatibility for packaged and source-based execution paths

## Requirements

- Python 3.10+
- Windows 10/11 for the full scheduler and credential flow
- The project dependencies in `requirements.txt`

## Development and Run

Recommended workflow:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe main.py
```

Run the GUI version by default:

```powershell
.venv\Scripts\python.exe main.py
```

Run a single job headlessly:

```powershell
.venv\Scripts\python.exe main.py --run-job "JobName" --config "C:\ProgramData\DataTransferTool\jobs.json"
```

## Build to a Single EXE

```powershell
python build_exe.py
```

This generates a self-contained executable:

```text
dist\TransferConsole.exe
```

## Deployment Notes

The packaged EXE is designed to be copied as a single binary. For scheduled execution, the default data directory is:

```text
C:\ProgramData\DataTransferTool
```

That directory typically contains:

- `jobs.json`
- `Credentials/`

## Scheduled Task Usage

The GUI schedule action creates a scheduled task that launches the same EXE in headless mode, for example:

```powershell
TransferConsole.exe --run-job "JobName" --config "C:\ProgramData\DataTransferTool\jobs.json"
```

## Project Structure

The root `.py` files are the real implementation. The `engine/` and `gui/` packages
are thin `from <module> import *` re-export shims kept only so PyInstaller can
resolve every import path when the project is packaged - edit the root files,
not the shims.

```text
main.py                  Entry point (GUI + CLI)
main_window.py           Main GUI window implementation
job_editor.py            Add/Edit job dialog
cred_manager.py          Credential manager dialog
style.py                 Shared UI stylesheet and theme helpers
config.py                Job configuration (jobs.json) with atomic write + backup
transfer.py              Transfer engine with Robocopy, hashing, disk checks
robocopy_runner.py       Robocopy process runner with live progress parsing
credentials.py           Credential storage with DPAPI (Windows) + SMB pre-auth
scheduler.py             Windows Task Scheduler integration
joblock.py               Per-job file lock to prevent duplicate concurrent runs
logutil.py               Logging, disk info, hash log generation
engine/, gui/            Re-export shims over the root modules (PyInstaller bundling only)
build_exe.py             PyInstaller build script
requirements.txt         Python dependencies
```

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

