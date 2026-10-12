from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional


def resource_path(*parts: str) -> Path:
    base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base_path.joinpath(*parts)


def get_app_icon_path() -> Optional[Path]:
    icon_path = resource_path("assets", "app.ico")
    return icon_path if icon_path.exists() else None