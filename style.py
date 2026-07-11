"""
style.py
--------
Uygulama genelinde kullanilan QSS (Qt Style Sheet) tanimi ve yardimci
renk sabitleri. Tek bir noktadan uygulanip tum pencerelere/dialoglara
(QApplication seviyesinde) otomatik yayilir.

Tasarim tercihi: acik/temiz bir ana arayuz + koyu (terminal tarzi) canli
log paneli - VS Code'un varsayilan acik temasi + entegre koyu terminali
gibi, sysadmin araclari icin yaygin ve goze yorucu olmayan bir kontrast.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

ACCENT = "#3a7bd5"
ACCENT_DARK = "#2f66b3"
ACCENT_LIGHT = "#eaf2fc"

STATUS_OK_BG = "#dff5e1"
STATUS_ERROR_BG = "#fbe3e3"
STATUS_OK_FG = "#1c7a2e"
STATUS_ERROR_FG = "#b3261e"

STATUS_OK_BG_DARK = "#24412d"
STATUS_ERROR_BG_DARK = "#5b2b35"

LOG_LEVEL_COLORS = {
    "ERROR": "#ff6b6b",
    "WARN": "#ffb454",
    "SUCCESS": "#7ee787",
    "HEADER": "#9ecbff",
    "INFO": "#d6d6d6",
}


def _is_dark_theme() -> bool:
    app = QApplication.instance()
    if app is None:
        return False

    try:
        color_scheme = app.styleHints().colorScheme()
        if color_scheme == Qt.ColorScheme.Dark:
            return True
        if color_scheme == Qt.ColorScheme.Light:
            return False
    except Exception:
        pass

    window_color = app.palette().color(QPalette.ColorRole.Window)
    return window_color.lightness() < 128


def _theme_palette(dark_mode: bool) -> dict[str, str]:
    if dark_mode:
        return {
            "window_bg": "#202124",
            "panel_bg": "#2b2d31",
            "panel_alt": "#24262a",
            "text": "#f1f3f4",
            "muted": "#8a8f98",
            "border": "#525866",
            "button_bg": "#2b2d31",
            "button_fg": "#f1f3f4",
            "button_hover": "#3a4250",
            "button_pressed": "#2f3746",
            "table_alt": "#23252b",
            "table_grid": "#454b56",
            "selection": "#2f4a67",
            "tooltip_bg": "#1f1f1f",
            "tooltip_fg": "#f1f3f4",
        }

    return {
        "window_bg": "#f4f5f7",
        "panel_bg": "#ffffff",
        "panel_alt": "#f7f9fb",
        "text": "#2b2f36",
        "muted": "#8a8f98",
        "border": "#c9ccd1",
        "button_bg": "#ffffff",
        "button_fg": "#2b2f36",
        "button_hover": "#eaf2fc",
        "button_pressed": "#d7e7fa",
        "table_alt": "#f7f9fb",
        "table_grid": "#e3e6ea",
        "selection": "#eaf2fc",
        "tooltip_bg": "#2b2f36",
        "tooltip_fg": "#f4f5f7",
    }


def build_app_stylesheet(dark_mode: bool | None = None) -> str:
    use_dark = _is_dark_theme() if dark_mode is None else dark_mode
    palette = _theme_palette(use_dark)

    return f"""
QMainWindow, QDialog {{
    background-color: {palette['window_bg']};
    color: {palette['text']};
}}

QWidget {{
    font-size: 13px;
    color: {palette['text']};
}}

QPushButton {{
    background-color: {palette['button_bg']};
    border: 1px solid {palette['border']};
    border-radius: 5px;
    padding: 6px 14px;
    color: {palette['button_fg']};
}}
QPushButton:hover {{
    background-color: {palette['button_hover']};
    border-color: {ACCENT};
}}
QPushButton:pressed {{
    background-color: {palette['button_pressed']};
}}
QPushButton:disabled {{
    color: {palette['muted']};
    background-color: {palette['panel_alt']};
    border-color: {palette['border']};
}}

QPushButton[primary="true"] {{
    background-color: {ACCENT};
    border: 1px solid {ACCENT_DARK};
    color: white;
    font-weight: 600;
}}
QPushButton[primary="true"]:hover {{
    background-color: {ACCENT_DARK};
}}
QPushButton[primary="true"]:disabled {{
    background-color: #b9cde8;
    border-color: #b9cde8;
    color: #eef2f8;
}}

QPushButton[danger="true"] {{
    background-color: #dc3c3c;
    border: 1px solid #b32d2d;
    color: white;
    font-weight: 600;
}}
QPushButton[danger="true"]:hover {{
    background-color: #b32d2d;
}}
QPushButton[danger="true"]:disabled {{
    background-color: #e8a3a3;
    border-color: #e8a3a3;
    color: #fbe3e3;
}}

QTableWidget {{
    background-color: {palette['panel_bg']};
    alternate-background-color: {palette['table_alt']};
    gridline-color: {palette['table_grid']};
    border: 1px solid {palette['border']};
    border-radius: 5px;
    selection-background-color: {palette['selection']};
    selection-color: {palette['text']};
}}
QHeaderView::section {{
    background-color: {palette['panel_alt']};
    padding: 6px;
    border: none;
    border-bottom: 2px solid {ACCENT};
    font-weight: 600;
    color: {palette['text']};
}}

QGroupBox {{
    font-weight: 600;
    border: 1px solid {palette['border']};
    border-radius: 6px;
    margin-top: 12px;
    padding-top: 12px;
    color: {palette['text']};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
    color: {ACCENT_DARK};
}}

QStatusBar {{
    background-color: {palette['panel_alt']};
    border-top: 1px solid {palette['border']};
    color: {palette['text']};
}}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    border: 1px solid {palette['border']};
    border-radius: 4px;
    padding: 4px 6px;
    background-color: {palette['panel_bg']};
    color: {palette['text']};
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {ACCENT};
}}
QLineEdit:disabled {{
    background-color: {palette['panel_alt']};
    color: {palette['muted']};
}}

QSplitter::handle {{
    background-color: {palette['border']};
}}
QSplitter::handle:hover {{
    background-color: {ACCENT};
}}

QToolTip {{
    background-color: {palette['tooltip_bg']};
    color: {palette['tooltip_fg']};
    border: 1px solid {ACCENT};
    padding: 4px;
}}
"""


APP_STYLESHEET = build_app_stylesheet()


def empty_label_stylesheet(dark_mode: bool | None = None) -> str:
    use_dark = _is_dark_theme() if dark_mode is None else dark_mode
    muted = "#8a8f98" if use_dark else "#8a8f98"
    return f"color:{muted}; font-size:14px; padding:24px;"


def status_ok_bg(dark_mode: bool | None = None) -> str:
    use_dark = _is_dark_theme() if dark_mode is None else dark_mode
    return STATUS_OK_BG_DARK if use_dark else STATUS_OK_BG


def status_error_bg(dark_mode: bool | None = None) -> str:
    use_dark = _is_dark_theme() if dark_mode is None else dark_mode
    return STATUS_ERROR_BG_DARK if use_dark else STATUS_ERROR_BG


def log_view_stylesheet() -> str:
    return (
        "background-color:#1a1a1a; color:#d6d6d6; "
        "font-family:Consolas,'Courier New',monospace; font-size:12px; "
        "border:1px solid #333; border-radius:5px;"
    )
