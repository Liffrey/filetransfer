from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scanner import ScanResult, ScannedFile, build_rel_index, scan_directory

__all__ = [
    "ScanResult",
    "ScannedFile",
    "scan_directory",
    "build_rel_index",
]
