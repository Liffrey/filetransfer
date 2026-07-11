"""Compatibility wrapper for the package-based GUI imports.

This project keeps the shared styles in the top-level ``style.py`` file for
historical reasons, but the GUI package imports them as ``gui.style``.
Providing this shim keeps both import styles working.
"""

from style import (  # noqa: F401
    ACCENT,
    ACCENT_DARK,
    ACCENT_LIGHT,
    APP_STYLESHEET,
    LOG_LEVEL_COLORS,
    STATUS_ERROR_BG,
    STATUS_ERROR_FG,
    STATUS_OK_BG,
    STATUS_OK_FG,
    build_app_stylesheet,
    empty_label_stylesheet,
    log_view_stylesheet,
    status_error_bg,
    status_ok_bg,
)

__all__ = [
    "ACCENT",
    "ACCENT_DARK",
    "ACCENT_LIGHT",
    "APP_STYLESHEET",
    "LOG_LEVEL_COLORS",
    "STATUS_ERROR_BG",
    "STATUS_ERROR_FG",
    "STATUS_OK_BG",
    "STATUS_OK_FG",
    "build_app_stylesheet",
    "empty_label_stylesheet",
    "log_view_stylesheet",
    "status_error_bg",
    "status_ok_bg",
]
