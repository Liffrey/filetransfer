"""
style.py
--------
Uygulama genelinde kullanilan QSS (Qt Style Sheet) tanimi ve yardimci
renk sabitleri. Tek bir noktadan uygulanip tum pencerelere/dialoglara
(QApplication seviyesinde) otomatik yayilir.

Tasarim tercihi: acik/temiz bir ana arayuz + koyu (terminal tarzi) canli
log paneli. Artik istege bagli koyu tema da desteklenir.
"""

from PySide6.QtGui import QColor, QPalette

THEME_LIGHT = "light"
THEME_DARK = "dark"

ACCENT = "#3a7bd5"
ACCENT_DARK = "#2f66b3"
ACCENT_LIGHT = "#eaf2fc"

STATUS_OK_BG = "#dff5e1"
STATUS_ERROR_BG = "#fbe3e3"
STATUS_OK_FG = "#1c7a2e"
STATUS_ERROR_FG = "#b3261e"

LOG_LEVEL_COLORS = {
    "ERROR": "#ff6b6b",
    "WARN": "#ffb454",
    "SUCCESS": "#7ee787",
    "HEADER": "#9ecbff",
    "INFO": "#d6d6d6",
}


def _normalize_theme(theme: str | None) -> str:
    return THEME_DARK if str(theme).lower() == THEME_DARK else THEME_LIGHT


def _theme_tokens(theme: str | None) -> dict[str, str]:
    theme = _normalize_theme(theme)
    if theme == THEME_DARK:
        return {
            "window_bg": "#1f232a",
            "window_text": "#e6e8ec",
            "base_bg": "#262b33",
            "alt_bg": "#2b313a",
            "text": "#e6e8ec",
            "button_bg": "#2d333d",
            "button_text": "#e6e8ec",
            "button_border": "#3c4451",
            "button_hover_bg": "#323947",
            "button_pressed_bg": "#394251",
            "button_disabled_bg": "#252a31",
            "button_disabled_text": "#7f8793",
            "button_disabled_border": "#323842",
            "primary_disabled_bg": "#2f4868",
            "primary_disabled_border": "#2f4868",
            "danger_bg": "#b93f3f",
            "danger_border": "#8d2f2f",
            "danger_hover_bg": "#9f3434",
            "danger_disabled_bg": "#6f3535",
            "danger_disabled_border": "#6f3535",
            "table_bg": "#262b33",
            "table_alt_bg": "#2a303a",
            "table_grid": "#3a404b",
            "table_border": "#3a404b",
            "table_text": "#e6e8ec",
            "table_selection_bg": "#35557a",
            "table_selection_text": "#ffffff",
            "header_bg": "#2f3641",
            "header_text": "#e6e8ec",
            "group_border": "#3a404b",
            "group_text": "#e6e8ec",
            "group_title": "#8db6ff",
            "statusbar_bg": "#242931",
            "statusbar_border": "#3a404b",
            "statusbar_text": "#c9d0d9",
            "field_bg": "#2a3038",
            "field_border": "#3b4350",
            "field_text": "#e6e8ec",
            "field_placeholder": "#8b93a0",
            "field_focus_border": ACCENT,
            "splitter": "#3a404b",
            "splitter_hover": ACCENT,
            "label_text": "#e6e8ec",
            "dialog_bg": "#1f232a",
            "tooltip_bg": "#2f3641",
            "tooltip_text": "#f3f6fb",
            "log_bg": "#11151b",
            "log_text": "#d6d6d6",
            "log_border": "#333a45",
            "empty_text": "#aeb5bf",
            "progress_text": "#c0c7d0",
            "status_ok_bg": "#285d35",
            "status_error_bg": "#6f2e2e",
        }

    return {
        "window_bg": "#f4f5f7",
        "window_text": "#2b2f36",
        "base_bg": "#ffffff",
        "alt_bg": "#f7f9fb",
        "text": "#2b2f36",
        "button_bg": "#ffffff",
        "button_text": "#2b2f36",
        "button_border": "#c9ccd1",
        "button_hover_bg": ACCENT_LIGHT,
        "button_pressed_bg": "#d7e7fa",
        "button_disabled_bg": "#eef0f2",
        "button_disabled_text": "#a4a9b0",
        "button_disabled_border": "#dde0e4",
        "primary_disabled_bg": "#b9cde8",
        "primary_disabled_border": "#b9cde8",
        "danger_bg": "#dc3c3c",
        "danger_border": "#b32d2d",
        "danger_hover_bg": "#b32d2d",
        "danger_disabled_bg": "#e8a3a3",
        "danger_disabled_border": "#e8a3a3",
        "table_bg": "#ffffff",
        "table_alt_bg": "#f7f9fb",
        "table_grid": "#e3e6ea",
        "table_border": "#d8dbe0",
        "table_text": "#1a1a1a",
        "table_selection_bg": ACCENT_LIGHT,
        "table_selection_text": "#1a1a1a",
        "header_bg": "#eef1f5",
        "header_text": "#2b2f36",
        "group_border": "#d8dbe0",
        "group_text": "#2b2f36",
        "group_title": ACCENT_DARK,
        "statusbar_bg": "#eef1f5",
        "statusbar_border": "#d8dbe0",
        "statusbar_text": "#444",
        "field_bg": "#ffffff",
        "field_border": "#c9ccd1",
        "field_text": "#2b2f36",
        "field_placeholder": "#9aa0a8",
        "field_focus_border": ACCENT,
        "splitter": "#d8dbe0",
        "splitter_hover": ACCENT,
        "label_text": "#2b2f36",
        "dialog_bg": "#f4f5f7",
        "tooltip_bg": "#2b2f36",
        "tooltip_text": "#f4f5f7",
        "log_bg": "#1a1a1a",
        "log_text": "#d6d6d6",
        "log_border": "#333",
        "empty_text": "#8a8f98",
        "progress_text": "#5a5f68",
        "status_ok_bg": STATUS_OK_BG,
        "status_error_bg": STATUS_ERROR_BG,
    }


def build_app_palette(theme: str = THEME_LIGHT) -> QPalette:
    tokens = _theme_tokens(theme)
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(tokens["window_bg"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(tokens["window_text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(tokens["base_bg"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(tokens["alt_bg"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(tokens["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(tokens["button_bg"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(tokens["button_text"]))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(tokens["tooltip_bg"]))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(tokens["tooltip_text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(tokens["field_placeholder"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(tokens["button_disabled_text"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(tokens["button_disabled_text"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(tokens["button_disabled_text"]))
    return palette


def build_app_stylesheet(theme: str = THEME_LIGHT) -> str:
    tokens = _theme_tokens(theme)
    return f"""
QMainWindow, QDialog {{
    background-color: {tokens['window_bg']};
}}

QWidget {{
    font-size: 13px;
    color: {tokens['text']};
}}

QPushButton {{
    background-color: {tokens['button_bg']};
    border: 1px solid {tokens['button_border']};
    border-radius: 5px;
    padding: 6px 14px;
    color: {tokens['button_text']};
}}
QPushButton:hover {{
    background-color: {tokens['button_hover_bg']};
    border-color: {ACCENT};
}}
QPushButton:pressed {{
    background-color: {tokens['button_pressed_bg']};
}}
QPushButton:disabled {{
    color: {tokens['button_disabled_text']};
    background-color: {tokens['button_disabled_bg']};
    border-color: {tokens['button_disabled_border']};
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
    background-color: {tokens['primary_disabled_bg']};
    border-color: {tokens['primary_disabled_border']};
    color: #eef2f8;
}}

QPushButton[danger="true"] {{
    background-color: {tokens['danger_bg']};
    border: 1px solid {tokens['danger_border']};
    color: white;
    font-weight: 600;
}}
QPushButton[danger="true"]:hover {{
    background-color: {tokens['danger_hover_bg']};
}}
QPushButton[danger="true"]:disabled {{
    background-color: {tokens['danger_disabled_bg']};
    border-color: {tokens['danger_disabled_border']};
    color: #fbe3e3;
}}

QTableWidget {{
    background-color: {tokens['table_bg']};
    alternate-background-color: {tokens['table_alt_bg']};
    gridline-color: {tokens['table_grid']};
    border: 1px solid {tokens['table_border']};
    border-radius: 5px;
    selection-background-color: {tokens['table_selection_bg']};
    selection-color: {tokens['table_selection_text']};
}}
QHeaderView::section {{
    background-color: {tokens['header_bg']};
    padding: 6px;
    border: none;
    border-bottom: 2px solid {ACCENT};
    font-weight: 600;
    color: {tokens['header_text']};
}}

QGroupBox {{
    font-weight: 600;
    border: 1px solid {tokens['group_border']};
    border-radius: 6px;
    margin-top: 12px;
    padding-top: 12px;
    color: {tokens['group_text']};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
    color: {tokens['group_title']};
}}

QStatusBar {{
    background-color: {tokens['statusbar_bg']};
    border-top: 1px solid {tokens['statusbar_border']};
    color: {tokens['statusbar_text']};
}}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    border: 1px solid {tokens['field_border']};
    border-radius: 4px;
    padding: 4px 6px;
    background-color: {tokens['field_bg']};
    color: {tokens['field_text']};
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {tokens['field_focus_border']};
}}
QLineEdit:disabled {{
    background-color: {tokens['button_disabled_bg']};
    color: {tokens['field_placeholder']};
}}

QSplitter::handle {{
    background-color: {tokens['splitter']};
}}
QSplitter::handle:hover {{
    background-color: {tokens['splitter_hover']};
}}

QLabel {{
    color: {tokens['label_text']};
    background-color: transparent;
}}

QMessageBox, QInputDialog {{
    background-color: {tokens['dialog_bg']};
}}
QMessageBox QLabel, QInputDialog QLabel {{
    color: {tokens['label_text']};
}}

QCheckBox, QRadioButton {{
    color: {tokens['label_text']};
    background-color: transparent;
}}

QToolTip {{
    background-color: {tokens['tooltip_bg']};
    color: {tokens['tooltip_text']};
    border: 1px solid {ACCENT};
    padding: 4px;
}}
"""


def log_view_stylesheet(theme: str = THEME_LIGHT) -> str:
    tokens = _theme_tokens(theme)
    return (
        f"background-color:{tokens['log_bg']}; color:{tokens['log_text']}; "
        "font-family:Consolas,'Courier New',monospace; font-size:12px; "
        f"border:1px solid {tokens['log_border']}; border-radius:5px;"
    )


def empty_label_stylesheet(theme: str = THEME_LIGHT) -> str:
    tokens = _theme_tokens(theme)
    return f"color:{tokens['empty_text']}; font-size:14px; padding:24px;"


def progress_label_stylesheet(theme: str = THEME_LIGHT) -> str:
    tokens = _theme_tokens(theme)
    return f"color:{tokens['progress_text']}; font-size:11px;"


def status_ok_bg(theme: str = THEME_LIGHT) -> str:
    return _theme_tokens(theme)["status_ok_bg"]


def status_error_bg(theme: str = THEME_LIGHT) -> str:
    return _theme_tokens(theme)["status_error_bg"]


APP_STYLESHEET = build_app_stylesheet(THEME_LIGHT)
