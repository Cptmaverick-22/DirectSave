"""Minimal, soothing themes for DirectSave (PySide6 / Fusion)."""

from pathlib import Path
import sys
import tempfile

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap

from config import load_settings, remember_setting

VALID_THEMES = ("midnight", "lavender", "ocean", "obsidian")

THEME_META = {
    "midnight": {"name": "Midnight Indigo", "badge": "✦ Midnight", "dark": True},
    "lavender": {"name": "Lavender Mist", "badge": "☀ Lavender", "dark": False},
    "ocean": {"name": "Nordic Ocean", "badge": "🌊 Ocean", "dark": True},
    "obsidian": {"name": "Obsidian Crimson", "badge": "◆ Obsidian", "dark": True},
}

_LEGACY_THEME_MAP = {
    "dark": "midnight",
    "light": "lavender",
}

PALETTES = {
    "midnight": {
        "bg": "#0E1019",
        "surface": "#171A27",
        "surface_alt": "#1F2335",
        "border": "#262A3E",
        "border_focus": "#7464FF",
        "divider": "#222638",
        "text": "#F3F4FA",
        "text_muted": "#8E93B2",
        "accent": "#7464FF",
        "accent_hover": "#8779FF",
        "accent_pressed": "#5E4EE6",
        "progress": "#10B981",
        "btn_bg": "#1F2335",
        "btn_hover": "#282D44",
        "btn_border": "#303652",
        "ghost_hover": "#1B1F30",
        "danger": "#F87171",
        "danger_hover": "#EF4444",
        "input_bg": "#121420",
        "selection": "#28264D",
        "segment_bg": "#121420",
        "segment_active": "#7464FF",
        "segment_active_text": "#FFFFFF",
        "scroll_handle": "#303652",
        "check_bg": "#121420",
        "check_border": "#464D70",
        "check_fill": "#10B981",
    },
    "lavender": {
        "bg": "#F3F1FA",
        "surface": "#FFFFFF",
        "surface_alt": "#EAE6F8",
        "border": "#E2DDF5",
        "border_focus": "#6D4AFF",
        "divider": "#EBE7F8",
        "text": "#171625",
        "text_muted": "#6E6B8A",
        "accent": "#6D4AFF",
        "accent_hover": "#7F5EFF",
        "accent_pressed": "#5734E8",
        "progress": "#059669",
        "btn_bg": "#F7F5FD",
        "btn_hover": "#ECE8FA",
        "btn_border": "#DCD6F2",
        "ghost_hover": "#E8E4F7",
        "danger": "#E11D48",
        "danger_hover": "#BE123C",
        "input_bg": "#FAF9FE",
        "selection": "#E5DFFF",
        "segment_bg": "#EAE6F8",
        "segment_active": "#6D4AFF",
        "segment_active_text": "#FFFFFF",
        "scroll_handle": "#CBC3EB",
        "check_bg": "#FFFFFF",
        "check_border": "#B8B0DC",
        "check_fill": "#059669",
    },
    "ocean": {
        "bg": "#0B192C",
        "surface": "#132640",
        "surface_alt": "#1A3252",
        "border": "#223C5E",
        "border_focus": "#3898FF",
        "divider": "#1D3554",
        "text": "#F0F6FC",
        "text_muted": "#8BA6C7",
        "accent": "#3898FF",
        "accent_hover": "#57AAFF",
        "accent_pressed": "#227FE6",
        "progress": "#10B981",
        "btn_bg": "#1A3252",
        "btn_hover": "#223F66",
        "btn_border": "#2B4B75",
        "ghost_hover": "#162C4A",
        "danger": "#F87171",
        "danger_hover": "#EF4444",
        "input_bg": "#0E1F36",
        "selection": "#1E3E66",
        "segment_bg": "#0E1F36",
        "segment_active": "#3898FF",
        "segment_active_text": "#FFFFFF",
        "scroll_handle": "#2B4B75",
        "check_bg": "#0E1F36",
        "check_border": "#3E628F",
        "check_fill": "#10B981",
    },
    "obsidian": {
        "bg": "#0B0B0E",
        "surface": "#15161C",
        "surface_alt": "#1E2029",
        "border": "#272935",
        "border_focus": "#E11D48",
        "divider": "#222430",
        "text": "#F4F4F6",
        "text_muted": "#989AA6",
        "accent": "#E11D48",
        "accent_hover": "#F43F5E",
        "accent_pressed": "#BE123C",
        "progress": "#E11D48",
        "btn_bg": "#1E2029",
        "btn_hover": "#282B37",
        "btn_border": "#323645",
        "ghost_hover": "#1A1C24",
        "danger": "#F87171",
        "danger_hover": "#DC2626",
        "input_bg": "#101116",
        "selection": "#381D29",
        "segment_bg": "#101116",
        "segment_active": "#E11D48",
        "segment_active_text": "#FFFFFF",
        "scroll_handle": "#323645",
        "check_bg": "#101116",
        "check_border": "#484C5E",
        "check_fill": "#E11D48",
    },
}

THEME_STATUS_COLORS = {
    "midnight": {"error": "#F87171", "warning": "#FBBF24", "success": "#34D399"},
    "lavender": {"error": "#DC2626", "warning": "#D97706", "success": "#059669"},
    "ocean": {"error": "#F87171", "warning": "#FBBF24", "success": "#34D399"},
    "obsidian": {"error": "#F87171", "warning": "#FBBF24", "success": "#4ADE80"},
}


def normalize_theme(theme: str | None) -> str:
    """Resolve current or legacy theme names to a valid theme key."""
    if not theme:
        return "midnight"
    mapped = _LEGACY_THEME_MAP.get(theme, theme)
    return mapped if mapped in VALID_THEMES else "midnight"


def _checkmark_icon_url() -> str:
    """Create a crisp 16x16 white checkmark PNG once and return its QSS url path."""
    icon_path = Path(tempfile.gettempdir()) / "directsave_check_white.png"
    if not icon_path.is_file():
        pm = QPixmap(16, 16)
        pm.fill(Qt.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor("#FFFFFF"), 2.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(QPointF(3.5, 8.2), QPointF(6.6, 11.4))
        painter.drawLine(QPointF(6.6, 11.4), QPointF(12.6, 4.6))
        painter.end()
        pm.save(str(icon_path), "PNG")
    return icon_path.as_posix()


def build_stylesheet(theme: str) -> str:
    """Return a complete Qt Style Sheet for the selected theme."""
    chosen = normalize_theme(theme)
    p = PALETTES[chosen]
    check_url = _checkmark_icon_url()
    return f"""
    QWidget {{
        background-color: {p["bg"]};
        color: {p["text"]};
        font-family: "Segoe UI", "Inter", "Cantarell", sans-serif;
        font-size: 10pt;
    }}

    /* Scroll area */
    QScrollArea, QScrollArea > QWidget > QWidget {{
        background-color: {p["bg"]};
        border: none;
    }}

    /* Cards */
    QFrame[card="true"] {{
        background-color: {p["surface"]};
        border: 1px solid {p["border"]};
        border-radius: 14px;
    }}
    QFrame[card="true"] QLabel,
    QFrame[card="true"] QCheckBox {{
        background-color: transparent;
    }}

    QFrame[divider="true"] {{
        background-color: {p["divider"]};
        border: none;
        max-height: 1px;
    }}

    /* Typography */
    QLabel[heading="true"] {{
        font-size: 15.5pt;
        font-weight: 700;
        background-color: transparent;
    }}
    QLabel[badge="true"] {{
        background-color: {p["surface_alt"]};
        color: {p["text_muted"]};
        border: 1px solid {p["border"]};
        border-radius: 8px;
        padding: 2px 9px;
        font-size: 8.5pt;
        font-weight: 600;
    }}
    QLabel[muted="true"] {{
        color: {p["text_muted"]};
        font-size: 9pt;
    }}
    QLabel[formLabel="true"] {{
        color: {p["text_muted"]};
        font-size: 9.5pt;
        font-weight: 500;
    }}
    QLabel[cardTitle="true"] {{
        font-size: 12pt;
        font-weight: 700;
    }}

    /* Inputs */
    QLineEdit, QComboBox, QListWidget {{
        background-color: {p["input_bg"]};
        color: {p["text"]};
        border: 1px solid {p["border"]};
        border-radius: 10px;
        padding: 7px 13px;
        selection-background-color: {p["selection"]};
        selection-color: {p["text"]};
    }}
    QLineEdit:focus, QComboBox:focus, QListWidget:focus {{
        border: 1.5px solid {p["border_focus"]};
    }}
    QLineEdit:disabled, QComboBox:disabled, QListWidget:disabled {{
        color: {p["text_muted"]};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 28px;
    }}
    QComboBox QAbstractItemView {{
        background-color: {p["surface"]};
        color: {p["text"]};
        border: 1px solid {p["border"]};
        border-radius: 10px;
        selection-background-color: {p["selection"]};
        selection-color: {p["text"]};
        outline: none;
        padding: 4px;
    }}

    /* Menus */
    QMenu {{
        background-color: {p["surface"]};
        color: {p["text"]};
        border: 1px solid {p["border"]};
        border-radius: 10px;
        padding: 6px;
    }}
    QMenu::item {{
        padding: 7px 16px;
        border-radius: 6px;
    }}
    QMenu::item:selected {{
        background-color: {p["selection"]};
        color: {p["text"]};
    }}

    /* Segmented pill switcher */
    QFrame[segmentContainer="true"] {{
        background-color: {p["segment_bg"]};
        border: 1px solid {p["border"]};
        border-radius: 12px;
    }}
    QPushButton[segment="true"] {{
        background-color: transparent;
        color: {p["text_muted"]};
        border: none;
        border-radius: 9px;
        padding: 7px 16px;
        font-weight: 600;
    }}
    QPushButton[segment="true"]:hover:!checked {{
        color: {p["text"]};
    }}
    QPushButton[segment="true"]:checked {{
        background-color: {p["segment_active"]};
        color: {p["segment_active_text"]};
        border: none;
    }}

    /* Buttons */
    QPushButton {{
        background-color: {p["btn_bg"]};
        color: {p["text"]};
        border: 1px solid {p["btn_border"]};
        border-radius: 10px;
        padding: 7px 14px;
        font-weight: 500;
    }}
    QPushButton:hover {{
        background-color: {p["btn_hover"]};
    }}
    QPushButton:disabled {{
        color: {p["text_muted"]};
    }}

    QPushButton[role="ghost"] {{
        background-color: transparent;
        color: {p["text_muted"]};
        border: 1px solid transparent;
        border-radius: 9px;
        padding: 6px 11px;
        font-size: 9pt;
        font-weight: 500;
    }}
    QPushButton[role="ghost"]:hover {{
        background-color: {p["ghost_hover"]};
        color: {p["text"]};
        border: 1px solid {p["border"]};
    }}

    QPushButton[role="pill"] {{
        background-color: {p["surface_alt"]};
        color: {p["text"]};
        border: 1px solid {p["border"]};
        border-radius: 999px;
        padding: 5px 14px;
        font-size: 9pt;
        font-weight: 600;
    }}
    QPushButton[role="pill"]:hover {{
        border-color: {p["border_focus"]};
        background-color: {p["btn_hover"]};
    }}

    QPushButton[role="primary"] {{
        background-color: {p["accent"]};
        color: #FFFFFF;
        border: none;
        border-radius: 10px;
        font-weight: 600;
    }}
    QPushButton[role="primary"]:hover {{
        background-color: {p["accent_hover"]};
    }}
    QPushButton[role="primary"]:pressed {{
        background-color: {p["accent_pressed"]};
    }}
    QPushButton[role="primary"]:disabled {{
        background-color: {p["border"]};
        color: {p["text_muted"]};
    }}

    QPushButton[role="cta"] {{
        background-color: {p["accent"]};
        color: #FFFFFF;
        border: none;
        border-radius: 12px;
        padding: 12px 22px;
        font-size: 10.5pt;
        font-weight: 700;
    }}
    QPushButton[role="cta"]:hover {{
        background-color: {p["accent_hover"]};
    }}
    QPushButton[role="cta"]:pressed {{
        background-color: {p["accent_pressed"]};
    }}
    QPushButton[role="cta"]:disabled {{
        background-color: {p["border"]};
        color: {p["text_muted"]};
    }}

    QPushButton[role="danger"] {{
        background-color: transparent;
        color: {p["danger"]};
        border: 1px solid {p["danger"]};
        border-radius: 10px;
        font-weight: 600;
        padding: 6px 14px;
    }}
    QPushButton[role="danger"]:hover {{
        background-color: {p["danger_hover"]};
        color: #FFFFFF;
    }}
    QPushButton[role="danger"]:disabled {{
        border-color: {p["border"]};
        color: {p["text_muted"]};
    }}

    /* Progress bar */
    QProgressBar {{
        background-color: {p["surface_alt"]};
        border: none;
        border-radius: 4px;
        height: 8px;
        text-align: center;
    }}
    QProgressBar::chunk {{
        background-color: {p["progress"]};
        border-radius: 4px;
    }}

    /* Playlist checklist & crisp emerald checkboxes */
    QListWidget {{
        padding: 6px;
        outline: none;
    }}
    QListWidget::item {{
        padding: 7px 10px;
        border-radius: 8px;
        color: {p["text"]};
    }}
    QListWidget::item:hover {{
        background-color: {p["surface_alt"]};
    }}
    QListWidget::item:selected {{
        background-color: {p["selection"]};
        color: {p["text"]};
    }}
    QListWidget::indicator, QCheckBox::indicator {{
        width: 16px;
        height: 16px;
        border-radius: 5px;
        border: 1.5px solid {p["check_border"]};
        background-color: {p["check_bg"]};
    }}
    QListWidget::indicator:hover, QCheckBox::indicator:hover {{
        border-color: {p["check_fill"]};
    }}
    QListWidget::indicator:checked, QCheckBox::indicator:checked {{
        background-color: {p["check_fill"]};
        border-color: {p["check_fill"]};
        image: url("{check_url}");
    }}

    /* Scrollbars */
    QScrollBar:vertical {{
        background: transparent;
        width: 8px;
        margin: 2px;
    }}
    QScrollBar::handle:vertical {{
        background: {p["scroll_handle"]};
        border-radius: 4px;
        min-height: 24px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
    }}

    QMessageBox, QDialog {{
        background-color: {p["surface"]};
    }}
    """


def detect_system_theme(app) -> str:
    """Detect OS dark/light preference via Qt 6.5+ style hints."""
    color_scheme = getattr(Qt, "ColorScheme", None)
    if color_scheme is not None:
        try:
            if app.styleHints().colorScheme() == color_scheme.Light:
                return "lavender"
        except Exception:
            pass
    return "midnight"


def get_saved_theme(app) -> str:
    """Return the user's saved theme from settings.json, or the OS default."""
    saved = load_settings().get("theme")
    if saved:
        return normalize_theme(saved)
    return detect_system_theme(app)


def set_windows_titlebar_theme(window, dark: bool) -> None:
    """Switch the native Windows 10/11 title bar between dark and light."""
    if sys.platform != "win32" or window is None:
        return
    try:
        import ctypes

        hwnd = int(window.winId())
        value = ctypes.c_int(1 if dark else 0)
        dwmapi = ctypes.windll.dwmapi
        for attr in (20, 19):
            res = dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)
            )
            if res == 0:
                break
    except Exception:
        pass


def apply_theme(app, theme: str, window=None, save: bool = True) -> str:
    """Apply one of the themes to the app and optionally save it."""
    chosen = normalize_theme(theme)
    app.setStyleSheet(build_stylesheet(chosen))
    is_dark = THEME_META[chosen]["dark"]
    set_windows_titlebar_theme(window, dark=is_dark)
    if save:
        remember_setting("theme", chosen)
    return chosen