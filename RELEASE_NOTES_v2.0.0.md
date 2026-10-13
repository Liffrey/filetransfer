# File Transfer Console v2.0.0

Release date: 2026-10-14

## Highlights

- Added persistent per-job checkpoint journals with configuration signatures and verified-file records.
- Resume mode reloads compatible checkpoints, uses Robocopy restartable mode, and re-verifies destination files before marking them complete.
- Added a priority-based GUI queue with FIFO ordering for equal priorities and up to two concurrent jobs.
- Added dry-run previews and a 24-hour inventory-bound receipt for source deletion authorization.
- Source deletion now fails closed unless policy, preview, and per-file verification requirements pass; unsupported `always` deletion is rejected.
- Added explicit confirmation before GUI runs that may delete source files.
- Added the checkpoint module to the PyInstaller release build.

## Validation

- `pytest`: 17 tests passed.
- Python compilation and offscreen Qt GUI smoke test passed.