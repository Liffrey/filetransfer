from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from joblock import JobLock, JobLockError, is_job_locked

__all__ = [
    "JobLock",
    "JobLockError",
    "is_job_locked",
]
