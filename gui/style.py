"""Compatibility wrapper for the package-based GUI imports.

The shared style constants live in the top-level ``style.py`` module.
This shim re-exports those symbols under ``gui.style`` and keeps a few
legacy helper names available for older call sites.
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
    THEME_DARK,
    THEME_LIGHT,
    build_app_palette,
    build_app_stylesheet,
    empty_label_stylesheet,
    log_view_stylesheet,
    progress_label_stylesheet,
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
    "THEME_DARK",
    "THEME_LIGHT",
    "build_app_palette",
    "build_app_stylesheet",
    "empty_label_stylesheet",
    "log_view_stylesheet",
    "progress_label_stylesheet",
    "status_error_bg",
    "status_ok_bg",
]
