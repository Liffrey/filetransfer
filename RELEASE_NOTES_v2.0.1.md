# File Transfer Console v2.0.1

Release date: 2026-10-22

## Improvements

- Bound parallel hashing, retry, and deletion work queues to reduce memory use on large transfers.
- Added progress updates during source analysis and destination scanning.
- Re-check source metadata and the verified destination immediately before deleting a source file.
- Preserve pending file counts on cancellation and report file/directory deletion failures as job failures.
- Reject overlapping source/destination paths and invalid transfer settings.
- Improve toolbar layout, button sizing, and Windows Segoe UI font fallback.

## Validation

- `pytest`: 39 tests passed.
- PyInstaller build completed successfully.