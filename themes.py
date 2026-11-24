"""Dark and Light themes for YT Downloader (PySide6 / Fusion)."""

from pathlib import Path
import sys
import tempfile

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap

from config import load_settings, remember_setting

VALID_THEMES = ("dark", "light")

PALETTES = {
    "dark": {
        "bg": "#0F1117",
        "surface": "#181B23",
        "surface_alt": "#222633",
        "border": "#2A2F3D",
        "border_focus": "#3B82F6",
        "divider": "#262B38",
        "text": "#F3F4F6",
        "text_muted": "#9CA3AF",
        "accent": "#E11D48",
        "accent_hover": "#F43F5E",
        "accent_pressed": "#BE123C",
        "btn_bg": "#232735",
        "btn_hover": "#2D3244",
        "btn_border": "#343A4D",
        "ghost_hover": "#1E222D",
        "danger": "#EF4444",
        "danger_hover": "#DC2626",
        "input_bg": "#12141B",
        "selection": "#28354D",
        "segment_bg": "#12141B",
        "segment_active": "#272C3B",
        "scroll_handle": "#343A4D",
        "check_bg": "#12141B",
        "check_border": "#4B5563",
    },
    "light": {
        "bg": "#F3F5F9",
        "surface": "#FFFFFF",
        "surface_alt": "#EBEEF4",
        "border": "#DCE1EB",
        "border_focus": "#2563EB",
        "divider": "#E5E9F0",
        "text": "#111827",
        "text_muted": "#6B7280",
        "accent": "#DC2626",
        "accent_hover": "#EF4444",
        "accent_pressed": "#B91C1C",
        "btn_bg": "#FFFFFF",
        "btn_hover": "#F3F4F6",
        "btn_border": "#D1D5DB",
        "ghost_hover": "#E5E9F0",
        "danger": "#DC2626",
        "danger_hover": "#B91C1C",
        "input_bg": "#F9FAFB",
        "selection": "#DBEAFE",
        "segment_bg": "#EBEEF4",
        "segment_active": "#FFFFFF",
        "scroll_handle": "#CBD5E1",
        "check_bg": "#FFFFFF",
        "check_border": "#9CA3AF",
    },
}

THEME_STATUS_COLORS = {
    "dark": {"error": "#F87171", "warning": "#FBBF24", "success": "#4ADE80"},
    "light": {"error": "#DC2626", "warning": "#D97706", "success": "#16A34A"},
}


def _checkmark_icon_url() -> str:
    """Create a crisp 16x16 white checkmark PNG once and return its QSS url path."""
    icon_path = Path(tempfile.gettempdir()) / "ytdownloader_check_white.png"
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
    """Return a complete Qt Style Sheet for 'dark' or 'light'."""
    p = PALETTES[theme if theme in VALID_THEMES else "dark"]
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
        border-radius: 12px;
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
        font-size: 15pt;
        font-weight: 700;
        background-color: transparent;
    }}
    QLabel[badge="true"] {{
        background-color: {p["surface_alt"]};
        color: {p["text_muted"]};
        border: 1px solid {p["border"]};
        border-radius: 6px;
        padding: 2px 8px;
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
        border-radius: 8px;
        padding: 7px 12px;
        selection-background-color: {p["selection"]};
        selection-color: {p["text"]};
    }}
    QLineEdit:focus, QComboBox:focus, QListWidget:focus {{
        border: 1px solid {p["border_focus"]};
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
        selection-background-color: {p["selection"]};
        selection-color: {p["text"]};
        outline: none;
        padding: 4px;
    }}

    /* Segmented toggle (Video / Audio) */
    QFrame[segmentContainer="true"] {{
        background-color: {p["segment_bg"]};
        border: 1px solid {p["border"]};
        border-radius: 9px;
    }}
    QPushButton[segment="true"] {{
        background-color: transparent;
        color: {p["text_muted"]};
        border: none;
        border-radius: 7px;
        padding: 7px 14px;
        font-weight: 600;
    }}
    QPushButton[segment="true"]:hover:!checked {{
        color: {p["text"]};
    }}
    QPushButton[segment="true"]:checked {{
        background-color: {p["segment_active"]};
        color: {p["text"]};
        border: 1px solid {p["border"]};
    }}

    /* Buttons */
    QPushButton {{
        background-color: {p["btn_bg"]};
        color: {p["text"]};
        border: 1px solid {p["btn_border"]};
        border-radius: 8px;
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
        border-radius: 8px;
        padding: 6px 10px;
        font-size: 9pt;
        font-weight: 500;
    }}
    QPushButton[role="ghost"]:hover {{
        background-color: {p["ghost_hover"]};
        color: {p["text"]};
        border: 1px solid {p["border"]};
    }}

    QPushButton[role="primary"] {{
        background-color: {p["accent"]};
        color: #FFFFFF;
        border: none;
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
        border-radius: 10px;
        padding: 11px 20px;
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
        background-color: {p["accent"]};
        border-radius: 4px;
    }}

    /* Playlist checklist & crisp checkboxes in both Dark and Light mode */
    QListWidget {{
        padding: 6px;
        outline: none;
    }}
    QListWidget::item {{
        padding: 6px 8px;
        border-radius: 6px;
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
        border-radius: 4px;
        border: 1.5px solid {p["check_border"]};
        background-color: {p["check_bg"]};
    }}
    QListWidget::indicator:hover, QCheckBox::indicator:hover {{
        border-color: {p["accent"]};
    }}
    QListWidget::indicator:checked, QCheckBox::indicator:checked {{
        background-color: {p["accent"]};
        border-color: {p["accent"]};
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
            if app.styleHints().colorScheme() == color_scheme.Dark:
                return "dark"
            if app.styleHints().colorScheme() == color_scheme.Light:
                return "light"
        except Exception:
            pass
    return "dark"


def get_saved_theme(app) -> str:
    """Return the user's saved theme from settings.json, or the OS default."""
    saved = load_settings().get("theme")
    if saved in VALID_THEMES:
        return saved
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
    """Apply 'dark' or 'light' theme to the app and optionally save it."""
    chosen = theme if theme in VALID_THEMES else "dark"
    app.setStyleSheet(build_stylesheet(chosen))
    set_windows_titlebar_theme(window, dark=(chosen == "dark"))
    if save:
        remember_setting("theme", chosen)
    return chosen