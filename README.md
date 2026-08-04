# File Transfer Console

File Transfer Console is a PySide6-based Windows file transfer utility that provides a GUI for managing transfer jobs, while also supporting a headless CLI mode for scheduled execution.

## Features

- GUI and CLI execution modes
- Run multiple transfer jobs concurrently from the GUI, tracked in a live "Gorevler" (Tasks) panel showing each job's stage, progress and duration, plus per-row Log/Hash Log buttons in the main grid and a separate live log view per job
- Windows Task Scheduler integration, including scheduling jobs that run as a non-SYSTEM account
- Robocopy-based transfer engine
- Hash (SHA256) or size-only verification with detailed step-by-step output written to the log file while the console/GUI log view stays high-level
- Partial-failure-safe: if a handful of files fail verification out of a large batch, only those files are retried (re-copied and re-verified) - a run is never all-or-nothing, and source deletion proceeds for every file that is actually verified good, even if a few files remain permanently failed (those are kept in the source and reported separately)
- Parallel hashing and source deletion (thread pool, sized from the job's own thread setting) instead of one-file-at-a-time loops, so verifying/deleting hundreds of thousands of files over a network share takes minutes instead of hours
- Credential storage with Windows DPAPI-backed persistence, decryptable by any account on the machine (so SYSTEM-scheduled jobs can use credentials saved from the interactive GUI)
- Job editor validation to catch dangerous configurations before saving (duplicate/blank names, identical or nested source-destination paths, inverted disk thresholds, malformed schedule times, incomplete mail settings)
- `jobs.json` writes are resilient to transient read/IO failures - a failed read never gets misread as "zero jobs" and silently wipes existing job configuration
- Single-file EXE packaging with PyInstaller, with CLI mode working correctly even though the EXE is built with the GUI (`--windowed`) subsystem
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

> Note: credentials are now encrypted with a machine-scoped DPAPI flag so that
> SYSTEM-scheduled jobs can decrypt them. Credentials saved before this change
> were encrypted per-user and must be re-entered once via the Credential
> Manager for scheduled (non-interactive) runs to pick them up.

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

