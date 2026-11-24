"""YT Downloader - graphical interface (Qt)."""

from datetime import datetime
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request



def _configure_runtime():
    """Prepare streams and bundled tool paths when running as a packaged app."""

    # In Windows --windowed builds (or pythonw.exe), stdout/stderr are None.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    # Expose bundled binaries (ffmpeg, ffprobe, deno) on PATH.
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        bundle_dir = getattr(sys, "_MEIPASS", exe_dir)
        extra_paths = [p for p in dict.fromkeys((bundle_dir, exe_dir)) if p]
        current_path = os.environ.get("PATH", "")
        os.environ["PATH"] = os.pathsep.join(extra_paths + [current_path])

        # Prevent yt-dlp's child tools (ffmpeg, ffprobe, deno) from flashing
        # black console windows in a --windowed Windows GUI app.
        if sys.platform == "win32":
            _orig_popen = subprocess.Popen

            class _SilentPopen(_orig_popen):
                def __init__(self, *args, **kwargs):
                    if not kwargs.get("creationflags"):
                        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                    super().__init__(*args, **kwargs)

            subprocess.Popen = _SilentPopen


_configure_runtime()


from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QDesktopServices,
    QIcon,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

import updater

updater.activate()  # Must run before anything imports yt-dlp.

from updater import UpdateError

from config import (
    APP_NAME,
    APP_VERSION,
    AUDIO_CHOICES,
    CONFIG_DIR,
    ORIGINAL_AUDIO,
    PLAYLIST_QUALITIES,
    audio_label,
    get_download_dir,
    load_settings,
    remember_setting,
    save_download_dir,
)

from desktop import notify

from downloader import (
    DownloadError,
    PlaylistLinkError,
    Reporter,
    download_audio,
    download_playlist,
    download_video,
    fetch_playlist,
    fetch_video_info,
    format_duration,
    format_file_size,
    friendly_error,
    has_playlist_and_video,
    parse_range,
    quality_options,
    summarize_video,
)

from themes import (
    THEME_META,
    THEME_STATUS_COLORS,
    VALID_THEMES,
    apply_theme,
    get_saved_theme,
)
from logs import diagnostics_text, setup_logging

THUMBNAIL_MAX_BYTES = 5 * 1024 * 1024
CLOSE_WAIT_MS = 30000
LABEL_COL_WIDTH = 88
HISTORY_FILE = CONFIG_DIR / "history.json"
MAX_HISTORY_ITEMS = 150


# --------------------------------------------------------------------------
# Background work
# --------------------------------------------------------------------------


class TaskThread(QThread):
    """Runs a function off the window thread and reports back via signals."""

    done = Signal(object)
    failed = Signal(str)
    message = Signal(str, str)
    stage = Signal(str)
    progress = Signal(object, object, object, object)
    progress_done = Signal()
    playlist_item = Signal(int, int, str)

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function

    def run(self):
        try:
            self.done.emit(self.function(self))

        except (DownloadError, UpdateError) as error:
            self.failed.emit(str(error))

        except Exception as error:
            logging.getLogger("ytdl").error(
                "Unexpected error in a background task", exc_info=True
            )
            self.failed.emit(friendly_error(error))

    def detach(self):
        """Stop delivering results to the window (used while closing)."""

        for signal in (
            self.done,
            self.failed,
            self.message,
            self.stage,
            self.progress,
            self.progress_done,
            self.playlist_item,
        ):
            try:
                signal.disconnect()
            except (RuntimeError, TypeError):
                pass


class QtReporter(Reporter):
    """Forwards download events to the window through thread signals."""

    def __init__(self, thread):
        self.thread = thread
        self.last_progress = 0.0

    def message(self, text, level="info"):
        self.thread.message.emit(text, level)

    def stage(self, text):
        self.thread.stage.emit(text)

    def playlist_item(self, number, total, title):
        self.thread.playlist_item.emit(number, total, title)

    def progress(self, downloaded, total, speed, eta):
        # Limit updates to about ten per second.
        now = time.monotonic()

        if now - self.last_progress < 0.1:
            return

        self.last_progress = now
        self.thread.progress.emit(downloaded, total, speed, eta)

    def progress_done(self):
        self.last_progress = 0.0
        self.thread.progress_done.emit()


class UpdateNotice(QObject):
    """Looks for a newer yt-dlp in the background and reports what it finds.

    A plain daemon thread is used, so it can never keep the app from closing.
    """

    found = Signal(str)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        version = updater.background_check()

        if version:
            try:
                self.found.emit(version)
            except RuntimeError:
                pass  # The window was closed in the meantime.


def load_thumbnail(url):
    """Download thumbnail bytes safely, or return None if unavailable."""

    if not url:
        return None

    try:
        parsed = urllib.parse.urlparse(url)

        # HTTPS only.
        if parsed.scheme != "https" or not parsed.hostname:
            return None

        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})

        with urllib.request.urlopen(request, timeout=10) as response:
            # Check the final address after redirects.
            if urllib.parse.urlparse(response.geturl()).scheme != "https":
                return None

            length = response.headers.get("Content-Length")

            if length and int(length) > THUMBNAIL_MAX_BYTES:
                return None

            data = response.read(THUMBNAIL_MAX_BYTES + 1)

            if len(data) > THUMBNAIL_MAX_BYTES:
                return None

            return data

    except Exception:
        return None


def playlist_thumbnail(playlist):
    """Thumbnail bytes for a playlist (its first video), or None."""

    if not playlist.entries:
        return None

    first = playlist.entries[0]

    return load_thumbnail(
        first.get("thumbnail")
        or f"https://i.ytimg.com/vi/{first.get('id')}/hqdefault.jpg"
    )


def fetch_everything(url, prefer_playlist=False):
    """Fetch a single video or a playlist (runs in a thread)."""

    if not prefer_playlist:
        try:
            info = fetch_video_info(url)

        except PlaylistLinkError:
            pass  # A playlist link: fetch it as a playlist below.

        else:
            summary = summarize_video(info)

            return (
                "video",
                info,
                summary,
                quality_options(info),
                load_thumbnail(summary.thumbnail),
            )

    playlist = fetch_playlist(url)

    return ("playlist", playlist, playlist_thumbnail(playlist))


def _find_downloaded_file(folder, video_id, preferred_name=None):
    """Locate the finished media file in folder using filename or [video_id]."""
    if folder is None:
        return None

    folder_path = Path(folder)
    if not folder_path.is_dir():
        return None

    if preferred_name:
        candidate = folder_path / preferred_name
        if candidate.is_file():
            return candidate

    if not video_id:
        return None

    marker = f"[{video_id}]"
    matches = []
    try:
        for entry in folder_path.iterdir():
            if not entry.is_file():
                continue
            name = entry.name
            if marker in name and not name.endswith((".part", ".ytdl", ".tmp")):
                matches.append(entry)
    except OSError:
        return None

    if not matches:
        return None

    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


def _quality_badge(kind, setting):
    """Readable quality/bitrate badge text for a history entry."""
    if kind == "audio":
        if setting == ORIGINAL_AUDIO:
            return "Original Audio"
        return f"{setting} kbps MP3"
    return f"{setting}p MP4"


# --------------------------------------------------------------------------
# Small UI helpers
# --------------------------------------------------------------------------


def plain_label(text="", bold=False, muted=False, wrap=True):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(wrap)

    if bold:
        label.setStyleSheet("font-weight: 600;")
    if muted:
        label.setProperty("muted", True)

    return label


def form_label(text):
    """Fixed-width left label so every form input aligns vertically."""
    label = QLabel(text)
    label.setProperty("formLabel", True)
    label.setFixedWidth(LABEL_COL_WIDTH)
    return label


def horizontal_divider():
    line = QFrame()
    line.setProperty("divider", True)
    line.setFixedHeight(1)
    return line


def rounded_pixmap(pixmap, radius=8):
    """Return a copy of pixmap clipped to smooth rounded corners."""

    result = QPixmap(pixmap.size())
    result.fill(Qt.GlobalColor.transparent)

    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, pixmap.width(), pixmap.height(), radius, radius)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, pixmap)
    painter.end()

    return result


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()

        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.setMinimumSize(680, 520)

        self.current_theme = get_saved_theme(QApplication.instance())
        self._status_level = "info"

        self.active_tab = "downloader"  # "downloader" or "history"
        self.history_items = self._load_history()

        self.state = "idle"  # idle, fetching, ready, downloading, done
        self.mode = "video"  # "video" or "playlist"
        self.task = None
        self.cancel_event = None
        self.url = ""
        self.info = None
        self.summary = None
        self.video_options = []
        self.playlist = None
        self._pending_playlist_entries = []
        self.failed_items = []
        self.last_folder = None
        self.download_kind = "video"
        self.download_setting = None
        self._syncing_checks = False
        self.tray_icon = None

        self.build_ui()
        self.setup_tray()
        self.set_state("idle")
        self._update_history_tab_label()
        self.url_input.setFocus()

        self.update_notice = UpdateNotice(self)
        self.update_notice.found.connect(self.on_update_available)
        self.update_notice.start()

        if not shutil.which("ffmpeg"):
            self.show_status(
                "FFmpeg was not found. Downloads will not work until "
                "it is installed.",
                "warning",
            )

    # ----- history persistence --------------------------------------------

    @staticmethod
    def _load_history():
        try:
            if HISTORY_FILE.is_file():
                data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data[:MAX_HISTORY_ITEMS]
        except (OSError, ValueError):
            pass
        return []

    def _save_history(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            tmp = HISTORY_FILE.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self.history_items[:MAX_HISTORY_ITEMS], indent=2),
                encoding="utf-8",
            )
            tmp.replace(HISTORY_FILE)
        except OSError:
            pass

    def _record_history(self, title, kind, quality_str, file_path):
        path_str = str(file_path)
        entry = {
            "id": str(int(time.time() * 1000)),
            "title": title,
            "kind": kind,
            "quality": quality_str,
            "path": path_str,
            "timestamp": datetime.now().strftime("%b %d, %I:%M %p"),
        }
        self.history_items = [
            item for item in self.history_items if item.get("path") != path_str
        ]
        self.history_items.insert(0, entry)
        self.history_items = self.history_items[:MAX_HISTORY_ITEMS]
        self._save_history()
        self._update_history_tab_label()
        if self.active_tab == "history":
            self._render_history_list()

    def _update_history_tab_label(self):
        count = len(self.history_items)
        self.nav_history_btn.setText(f"History ({count})" if count else "History")

    def _switch_tab(self, tab_name):
        if self.active_tab == tab_name:
            return
        self.active_tab = tab_name
        is_dl = tab_name == "downloader"
        self.downloader_page.setVisible(is_dl)
        self.history_page.setVisible(not is_dl)
        if not is_dl:
            self._render_history_list()
        self.scroll_area.verticalScrollBar().setValue(0)
        self.fit_window()

    def _render_history_list(self):
        while self.history_list_layout.count():
            child = self.history_list_layout.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.deleteLater()

        query = self.history_search_input.text().strip().lower()
        items = [
            item
            for item in self.history_items
            if not query or query in (item.get("title") or "").lower()
        ]

        self.clear_history_btn.setEnabled(bool(self.history_items))

        if not items:
            empty_card = QFrame()
            empty_card.setProperty("card", True)
            empty_layout = QVBoxLayout(empty_card)
            empty_layout.setContentsMargins(20, 24, 20, 24)
            msg = (
                "No matching downloads found."
                if query
                else "No downloads yet. Completed videos and audio will appear here."
            )
            empty_lbl = plain_label(msg, muted=True)
            empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_layout.addWidget(empty_lbl)
            self.history_list_layout.addWidget(empty_card)
            return

        for item in items:
            kind = item.get("kind", "video")
            quality = item.get("quality", "")
            ts = item.get("timestamp", "")
            path_str = item.get("path", "")
            title = item.get("title", "Untitled")

            card = QFrame()
            card.setProperty("card", True)
            row = QHBoxLayout(card)
            row.setContentsMargins(16, 14, 16, 14)
            row.setSpacing(14)

            info_col = QVBoxLayout()
            info_col.setSpacing(4)

            prefix = "AUDIO" if kind == "audio" else "VIDEO"
            badge_lbl = plain_label(f"{prefix}  ·  {quality}  ·  {ts}", muted=True)
            title_lbl = plain_label(title, bold=True)
            path_lbl = plain_label(Path(path_str).name if path_str else "", muted=True)

            info_col.addWidget(badge_lbl)
            info_col.addWidget(title_lbl)
            info_col.addWidget(path_lbl)
            row.addLayout(info_col, 1)

            play_text = "▶ Play Audio" if kind == "audio" else "▶ Play Video"
            play_btn = QPushButton(play_text)
            play_btn.setProperty("role", "primary")
            play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            play_btn.setFixedHeight(36)
            play_btn.clicked.connect(
                lambda _checked=False, p=path_str: self._play_history_item(p)
            )

            folder_btn = QPushButton("Folder")
            folder_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            folder_btn.setFixedHeight(36)
            folder_btn.clicked.connect(
                lambda _checked=False, p=path_str: self._open_history_folder(p)
            )

            remove_btn = QPushButton("Remove")
            remove_btn.setProperty("role", "ghost")
            remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            remove_btn.setFixedHeight(36)
            remove_btn.clicked.connect(
                lambda _checked=False, p=path_str: self._remove_history_item(p)
            )

            row.addWidget(play_btn)
            row.addWidget(folder_btn)
            row.addWidget(remove_btn)
            self.history_list_layout.addWidget(card)

    def _play_history_item(self, path_str):
        file_path = Path(path_str)
        if not file_path.is_file():
            QMessageBox.warning(
                self,
                "File Not Found",
                "This file could not be found on disk.\n"
                "It may have been moved, renamed, or deleted.",
            )
            return

        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(file_path))):
            QMessageBox.warning(
                self,
                "Playback Failed",
                f"Could not open the default media player for:\n{file_path.name}",
            )

    def _open_history_folder(self, path_str):
        folder = Path(path_str).parent
        if folder.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        else:
            QMessageBox.warning(
                self,
                "Folder Not Found",
                "The download folder for this item no longer exists.",
            )

    def _remove_history_item(self, path_str):
        self.history_items = [
            item for item in self.history_items if item.get("path") != path_str
        ]
        self._save_history()
        self._update_history_tab_label()
        self._render_history_list()

    def _clear_history(self):
        if not self.history_items:
            return
        answer = QMessageBox.question(
            self,
            "Clear History",
            "Clear all items from Download History?\n\n"
            "(Your downloaded video and audio files will not be deleted.)",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.history_items = []
            self._save_history()
            self._update_history_tab_label()
            self._render_history_list()

    # ----- theme ----------------------------------------------------------

    def apply_current_theme(self, save=True):
        self.current_theme = apply_theme(
            QApplication.instance(), self.current_theme, window=self, save=save
        )
        meta = THEME_META[self.current_theme]
        self.theme_btn.setText(f"{meta['badge']} ▾")
        self.theme_btn.setToolTip(f"Theme: {meta['name']} (click to switch)")

        for key, action in self._theme_actions.items():
            action.setChecked(key == self.current_theme)

        if self.status_label.text():
            self.show_status(self.status_label.text(), self._status_level)

    def set_theme(self, theme_key):
        self.current_theme = theme_key
        self.apply_current_theme(save=True)

    # ----- desktop notifications ------------------------------------------

    def setup_tray(self):
        """Set up a system tray icon for Windows/macOS toast notifications.

        On Linux (such as Fedora Workstation / GNOME), native notify-send is
        used directly so no unnecessary tray icon is created.
        """
        if sys.platform.startswith("linux") and shutil.which("notify-send"):
            return
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        icon = QApplication.instance().windowIcon()
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)

        tray = QSystemTrayIcon(icon, self)
        tray.setToolTip(APP_NAME)

        menu = QMenu(self)
        show_action = QAction(f"Show {APP_NAME}", self)
        show_action.triggered.connect(self.bring_to_front)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.close)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(quit_action)

        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.messageClicked.connect(self._on_notification_clicked)
        tray.show()
        self.tray_icon = tray

    def bring_to_front(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.bring_to_front()

    def _on_notification_clicked(self):
        if self.last_folder is not None:
            self.open_folder()
        else:
            self.bring_to_front()

    def send_notification(self, title, message):
        """Show a native desktop notification on Linux, Windows, and macOS."""
        text = str(message)
        if len(text) > 200:
            text = text[:199] + "…"

        if self.tray_icon is not None:
            self.tray_icon.showMessage(
                title,
                text,
                self.tray_icon.icon(),
                5000,
            )
        else:
            # Run notify-send off the GUI thread so Fedora/GNOME stays snappy.
            threading.Thread(target=notify, args=(title, text), daemon=True).start()

    # ----- layout ---------------------------------------------------------

    def build_ui(self):
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        outer_layout.addWidget(self.scroll_area)

        self._content_widget = QWidget()
        self._content_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.scroll_area.setWidget(self._content_widget)

        root_layout = QVBoxLayout(self._content_widget)
        root_layout.setContentsMargins(24, 20, 24, 24)
        root_layout.setSpacing(14)
        self._content_layout = root_layout

        # 1. Top header bar (brand left, utility actions right)
        header_row = QHBoxLayout()
        header_row.setSpacing(8)

        heading = QLabel(APP_NAME)
        heading.setProperty("heading", True)

        version_badge = QLabel(f"v{APP_VERSION}")
        version_badge.setProperty("badge", True)

        self.update_button = QPushButton("Update yt-dlp")
        self.update_button.setProperty("role", "ghost")
        self.update_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_button.setToolTip(
            "Download the newest yt-dlp. This fixes most sudden download failures."
        )
        self.update_button.clicked.connect(self.update_ytdlp)

        self.theme_btn = QPushButton()
        self.theme_btn.setProperty("role", "pill")
        self.theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        theme_menu = QMenu(self)
        self._theme_actions = {}
        for key in VALID_THEMES:
            act = QAction(THEME_META[key]["name"], self)
            act.setCheckable(True)
            act.triggered.connect(lambda _checked=False, k=key: self.set_theme(k))
            theme_menu.addAction(act)
            self._theme_actions[key] = act
        self.theme_btn.setMenu(theme_menu)

        header_row.addWidget(heading)
        header_row.addWidget(version_badge)
        header_row.addStretch(1)
        header_row.addWidget(self.update_button)
        header_row.addWidget(self.theme_btn)
        root_layout.addLayout(header_row)

        # 2. Top navigation tabs (Downloader | History)
        nav_frame = QFrame()
        nav_frame.setProperty("segmentContainer", True)
        nav_layout = QHBoxLayout(nav_frame)
        nav_layout.setContentsMargins(4, 4, 4, 4)
        nav_layout.setSpacing(4)

        self.nav_downloader_btn = QPushButton("Downloader")
        self.nav_downloader_btn.setCheckable(True)
        self.nav_downloader_btn.setChecked(True)
        self.nav_downloader_btn.setProperty("segment", True)
        self.nav_downloader_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.nav_downloader_btn.clicked.connect(lambda: self._switch_tab("downloader"))

        self.nav_history_btn = QPushButton("History")
        self.nav_history_btn.setCheckable(True)
        self.nav_history_btn.setProperty("segment", True)
        self.nav_history_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.nav_history_btn.clicked.connect(lambda: self._switch_tab("history"))

        nav_group = QButtonGroup(self)
        nav_group.setExclusive(True)
        nav_group.addButton(self.nav_downloader_btn)
        nav_group.addButton(self.nav_history_btn)

        nav_layout.addWidget(self.nav_downloader_btn, 1)
        nav_layout.addWidget(self.nav_history_btn, 1)
        root_layout.addWidget(nav_frame)

        # ── Downloader Page ──────────────────────────────────────────────
        self.downloader_page = QWidget()
        layout = QVBoxLayout(self.downloader_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        # 3. URL input card
        url_card = QFrame()
        url_card.setProperty("card", True)
        url_layout = QHBoxLayout(url_card)
        url_layout.setContentsMargins(12, 12, 12, 12)
        url_layout.setSpacing(10)

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText(
            "Paste a video or playlist link here…"
        )
        self.url_input.setFixedHeight(40)
        self.url_input.setClearButtonEnabled(True)
        self.url_input.returnPressed.connect(self.fetch)
        self.url_input.textEdited.connect(self.on_url_edited)

        self.paste_button = QPushButton("Paste")
        self.paste_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.paste_button.setFixedHeight(40)
        self.paste_button.setToolTip("Paste link from clipboard and fetch")
        self.paste_button.clicked.connect(self.paste_from_clipboard)

        self.fetch_button = QPushButton("Fetch")
        self.fetch_button.setProperty("role", "primary")
        self.fetch_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fetch_button.setFixedHeight(40)
        self.fetch_button.setMinimumWidth(104)
        self.fetch_button.clicked.connect(self.fetch)

        url_layout.addWidget(self.url_input, 1)
        url_layout.addWidget(self.paste_button)
        url_layout.addWidget(self.fetch_button)
        layout.addWidget(url_card)

        # 4. Video / playlist preview card
        self.card = QFrame()
        self.card.setProperty("card", True)
        card_layout = QHBoxLayout(self.card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(16)

        self.thumb = QLabel()
        self.thumb.setFixedSize(192, 108)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)

        text_column = QVBoxLayout()
        text_column.setSpacing(6)
        self.title_label = plain_label()
        self.title_label.setProperty("cardTitle", True)
        self.meta_label = plain_label(muted=True)
        text_column.addWidget(self.title_label)
        text_column.addWidget(self.meta_label)
        text_column.addStretch(1)

        card_layout.addWidget(self.thumb)
        card_layout.addLayout(text_column, 1)
        layout.addWidget(self.card)

        # 5. Format, quality, and playlist options card
        self.options = QFrame()
        self.options.setProperty("card", True)
        self.options.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        options_layout = QVBoxLayout(self.options)
        options_layout.setContentsMargins(16, 16, 16, 16)
        options_layout.setSpacing(12)

        # Segmented Video / Audio switcher
        segment_frame = QFrame()
        segment_frame.setProperty("segmentContainer", True)
        segment_layout = QHBoxLayout(segment_frame)
        segment_layout.setContentsMargins(4, 4, 4, 4)
        segment_layout.setSpacing(4)

        self.video_radio = QPushButton("Video (MP4)")
        self.video_radio.setCheckable(True)
        self.video_radio.setChecked(True)
        self.video_radio.setProperty("segment", True)
        self.video_radio.setCursor(Qt.CursorShape.PointingHandCursor)

        self.audio_radio = QPushButton("Audio (MP3 / Original)")
        self.audio_radio.setCheckable(True)
        self.audio_radio.setProperty("segment", True)
        self.audio_radio.setCursor(Qt.CursorShape.PointingHandCursor)

        group = QButtonGroup(self)
        group.setExclusive(True)
        group.addButton(self.video_radio)
        group.addButton(self.audio_radio)
        self.video_radio.toggled.connect(self.populate_quality)

        segment_layout.addWidget(self.video_radio, 1)
        segment_layout.addWidget(self.audio_radio, 1)
        options_layout.addWidget(segment_frame)

        # Quality row
        quality_row = QHBoxLayout()
        quality_row.setSpacing(12)
        self.quality_label = form_label("Quality")
        self.quality_box = QComboBox()
        self.quality_box.setFixedHeight(38)
        quality_row.addWidget(self.quality_label)
        quality_row.addWidget(self.quality_box, 1)
        options_layout.addLayout(quality_row)

        # Playlist-only options
        self.playlist_options = QWidget()
        self.playlist_options.setStyleSheet("background-color: transparent;")
        self.playlist_options.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        playlist_layout = QVBoxLayout(self.playlist_options)
        playlist_layout.setContentsMargins(0, 4, 0, 0)
        playlist_layout.setSpacing(10)

        playlist_layout.addWidget(horizontal_divider())

        range_row = QHBoxLayout()
        range_row.setSpacing(10)
        range_row.addWidget(form_label("Range"))

        self.range_input = QLineEdit()
        self.range_input.setFixedHeight(36)
        self.range_input.setClearButtonEnabled(True)
        self.range_input.textEdited.connect(self.on_range_edited)

        self.select_all_button = QPushButton("All")
        self.select_all_button.setFixedHeight(36)
        self.select_all_button.setMinimumWidth(58)
        self.select_all_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.select_all_button.setToolTip(
            "Check all videos (or all shown videos when filtered)"
        )
        self.select_all_button.clicked.connect(self.select_all_videos)

        self.select_none_button = QPushButton("None")
        self.select_none_button.setFixedHeight(36)
        self.select_none_button.setMinimumWidth(64)
        self.select_none_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.select_none_button.setToolTip(
            "Uncheck all videos (or all shown videos when filtered)"
        )
        self.select_none_button.clicked.connect(self.select_no_videos)

        range_row.addWidget(self.range_input, 1)
        range_row.addWidget(self.select_all_button)
        range_row.addWidget(self.select_none_button)
        playlist_layout.addLayout(range_row)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(10)
        filter_row.addWidget(form_label("Filter"))

        self.search_input = QLineEdit()
        self.search_input.setFixedHeight(36)
        self.search_input.setPlaceholderText("Filter videos by title…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self.on_search_changed)

        filter_row.addWidget(self.search_input, 1)
        playlist_layout.addLayout(filter_row)

        self.selection_count_label = plain_label(muted=True)
        self.selection_count_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        playlist_layout.addWidget(self.selection_count_label)

        self.playlist_list = QListWidget()
        self.playlist_list.setFixedHeight(220)
        self.playlist_list.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.playlist_list.itemChanged.connect(self.on_checklist_item_changed)
        playlist_layout.addWidget(self.playlist_list)

        subfolder_row = QHBoxLayout()
        subfolder_row.setSpacing(10)
        subfolder_row.addWidget(form_label("Subfolder"))
        self.subfolder_input = QLineEdit()
        self.subfolder_input.setFixedHeight(36)
        self.subfolder_input.setClearButtonEnabled(True)
        subfolder_row.addWidget(self.subfolder_input, 1)
        playlist_layout.addLayout(subfolder_row)

        options_layout.addWidget(self.playlist_options)
        layout.addWidget(self.options)

        # 6. Save location card (always visible)
        folder_card = QFrame()
        folder_card.setProperty("card", True)
        folder_row = QHBoxLayout(folder_card)
        folder_row.setContentsMargins(16, 12, 16, 12)
        folder_row.setSpacing(12)

        self.folder_label = plain_label(str(get_download_dir()), muted=True)
        self.folder_button = QPushButton("Change…")
        self.folder_button.setFixedHeight(34)
        self.folder_button.setMinimumWidth(92)
        self.folder_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.folder_button.clicked.connect(self.choose_folder)

        folder_row.addWidget(form_label("Save to"))
        folder_row.addWidget(self.folder_label, 1)
        folder_row.addWidget(self.folder_button)
        layout.addWidget(folder_card)

        # 7. Full-width primary Download CTA
        self.download_button = QPushButton("Download")
        self.download_button.setProperty("role", "cta")
        self.download_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.download_button.setFixedHeight(44)
        self.download_button.clicked.connect(self.start_download)
        layout.addWidget(self.download_button)

        # 8. Progress card (visible while fetching or downloading)
        self.progress_box = QFrame()
        self.progress_box.setProperty("card", True)
        progress_layout = QVBoxLayout(self.progress_box)
        progress_layout.setContentsMargins(16, 14, 16, 14)
        progress_layout.setSpacing(8)

        progress_header = QHBoxLayout()
        progress_header.setSpacing(10)

        self.item_label = plain_label(bold=True)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setProperty("role", "danger")
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button.clicked.connect(self.cancel_download)

        progress_header.addWidget(self.item_label, 1)
        progress_header.addWidget(self.cancel_button)
        progress_layout.addLayout(progress_header)

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(8)
        self.progress_label = plain_label(muted=True)

        progress_layout.addWidget(self.progress_bar)
        progress_layout.addWidget(self.progress_label)
        layout.addWidget(self.progress_box)

        # 9. Contextual status & result bar (right-aligned action buttons)
        self.status_box = QFrame()
        self.status_box.setProperty("card", True)
        status_row = QHBoxLayout(self.status_box)
        status_row.setContentsMargins(16, 12, 16, 12)
        status_row.setSpacing(10)

        self.status_label = plain_label()

        self.open_button = QPushButton("Open folder")
        self.open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_button.clicked.connect(self.open_folder)

        self.failures_button = QPushButton("View failed")
        self.failures_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.failures_button.clicked.connect(self.show_failures)

        self.diagnostics_button = QPushButton("Copy diagnostics")
        self.diagnostics_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.diagnostics_button.setToolTip(
            "Copy technical details to share when reporting a problem"
        )
        self.diagnostics_button.clicked.connect(self.copy_diagnostics)
        self.diagnostics_button.hide()

        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(self.open_button)
        status_row.addWidget(self.failures_button)
        status_row.addWidget(self.diagnostics_button)
        layout.addWidget(self.status_box)

        root_layout.addWidget(self.downloader_page)

        # ── History Page ─────────────────────────────────────────────────
        self.history_page = QWidget()
        history_layout = QVBoxLayout(self.history_page)
        history_layout.setContentsMargins(0, 0, 0, 0)
        history_layout.setSpacing(12)

        history_toolbar = QHBoxLayout()
        history_toolbar.setSpacing(10)

        self.history_search_input = QLineEdit()
        self.history_search_input.setFixedHeight(38)
        self.history_search_input.setPlaceholderText("Filter history by title…")
        self.history_search_input.setClearButtonEnabled(True)
        self.history_search_input.textChanged.connect(self._render_history_list)

        self.clear_history_btn = QPushButton("Clear All")
        self.clear_history_btn.setProperty("role", "danger")
        self.clear_history_btn.setFixedHeight(38)
        self.clear_history_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_history_btn.clicked.connect(self._clear_history)

        history_toolbar.addWidget(self.history_search_input, 1)
        history_toolbar.addWidget(self.clear_history_btn)
        history_layout.addLayout(history_toolbar)

        self.history_list_layout = QVBoxLayout()
        self.history_list_layout.setSpacing(10)
        history_layout.addLayout(self.history_list_layout)

        self.history_page.setVisible(False)
        root_layout.addWidget(self.history_page)

        root_layout.addStretch(1)

    # ----- state ----------------------------------------------------------

    def _update_status_visibility(self):
        has_content = (
            bool(self.status_label.text())
            or self.open_button.isVisibleTo(self.status_box)
            or self.failures_button.isVisibleTo(self.status_box)
            or self.diagnostics_button.isVisibleTo(self.status_box)
        )
        self.status_box.setVisible(has_content)

    def set_state(self, state):
        self.state = state

        busy = state in ("fetching", "downloading")
        has_media = state in ("ready", "downloading", "done")
        editable = state in ("ready", "done")
        is_playlist = self.mode == "playlist"

        self.url_input.setEnabled(not busy)
        self.paste_button.setEnabled(not busy)
        self.fetch_button.setEnabled(not busy)
        self.fetch_button.setText("Fetching…" if state == "fetching" else "Fetch")
        self.folder_button.setEnabled(not busy)
        self.update_button.setEnabled(not busy)

        self.card.setVisible(has_media)
        self.options.setVisible(has_media)
        self.playlist_options.setVisible(has_media and is_playlist)

        self.video_radio.setEnabled(editable)
        self.audio_radio.setEnabled(editable)
        self.quality_box.setEnabled(editable)
        self.range_input.setEnabled(editable)
        self.search_input.setEnabled(editable)
        self.select_all_button.setEnabled(editable)
        self.select_none_button.setEnabled(editable)
        self.playlist_list.setEnabled(editable)
        self.subfolder_input.setEnabled(editable)

        self.download_button.setVisible(editable)
        self.cancel_button.setVisible(state == "downloading")
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("Cancel")
        self.open_button.setVisible(state == "done" and self.last_folder is not None)
        self.failures_button.setVisible(state == "done" and bool(self.failed_items))

        self.progress_box.setVisible(busy)
        self.item_label.setVisible(
            (state == "fetching")
            or (state == "downloading" and bool(self.item_label.text()))
        )

        self.update_download_button()
        self._update_status_visibility()
        self.fit_window(shrink=(state == "idle"))

    def fit_window(self, shrink=False):
        """Grow the window up to screen height when needed; never fight maximize."""
        if self.isMaximized() or self.isFullScreen():
            return

        self._content_layout.activate()
        needed = self._content_widget.sizeHint().height() + 12

        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            max_height = max(560, screen.availableGeometry().height() - 80)
            needed = min(needed, max_height)

        target_height = max(self.minimumHeight(), needed)
        if shrink or self.height() < target_height:
            self.resize(self.width(), target_height)

    def update_download_button(self):
        if self.mode == "playlist" and self.playlist is not None:
            count = self.checked_count()
            self.download_button.setText(
                f"Download {count} item{'s' if count != 1 else ''}"
            )
            self.download_button.setEnabled(count > 0)
        else:
            self.download_button.setText("Download")
            self.download_button.setEnabled(True)

    def show_status(self, text, level="info"):
        self._status_level = level
        palette = THEME_STATUS_COLORS.get(
            self.current_theme, THEME_STATUS_COLORS["midnight"]
        )
        color = palette.get(level)
        self.status_label.setStyleSheet(
            f"color: {color}; font-weight: 500;" if color else ""
        )
        self.status_label.setText(text)
        self.diagnostics_button.setVisible(level == "error" and bool(text))
        self._update_status_visibility()

    def copy_diagnostics(self):
        QApplication.clipboard().setText(diagnostics_text())
        self.show_status(
            "Diagnostics copied to the clipboard. "
            "Paste them when you report the problem."
        )

    # ----- background tasks -----------------------------------------------

    def start_task(self, function):
        """Create a background thread that cleans itself up when finished."""

        task = TaskThread(function, self)
        task.finished.connect(self._on_task_finished)
        task.finished.connect(task.deleteLater)

        self.task = task

        return task

    def _on_task_finished(self):
        if self.sender() is self.task:
            self.task = None

    # ----- thumbnails -----------------------------------------------------

    def show_thumbnail(self, data, placeholder=""):
        try:
            pixmap = QPixmap()
            self.thumb.setStyleSheet("")

            if data and pixmap.loadFromData(data):
                scaled = pixmap.scaled(
                    self.thumb.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self.thumb.setPixmap(rounded_pixmap(scaled, radius=8))
                return

            self.thumb.clear()

            if placeholder:
                self.thumb.setText(placeholder)
                self.thumb.setStyleSheet(
                    "background-color: rgba(128, 128, 128, 0.15); "
                    "border-radius: 8px; font-weight: bold; "
                    "letter-spacing: 2px;"
                )

        except Exception:
            logging.getLogger("ytdl").warning(
                "Could not show the thumbnail", exc_info=True
            )
            self.thumb.clear()

    # ----- playlist checklist ---------------------------------------------

    def populate_playlist_checklist(self):
        """Fill the checklist with all videos from the playlist."""

        self._syncing_checks = True
        self.playlist_list.clear()

        if self.playlist:
            for index, entry in enumerate(self.playlist.entries, start=1):
                title = entry.get("title") or entry.get("id") or "Untitled"
                item = QListWidgetItem(f"{index}.  {title}")
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked)
                self.playlist_list.addItem(item)

        self._syncing_checks = False
        self.update_selection_count()

    def checked_count(self):
        return sum(
            self.playlist_list.item(i).checkState() == Qt.CheckState.Checked
            for i in range(self.playlist_list.count())
        )

    def on_search_changed(self, text):
        """Hide or show checklist items based on the title filter."""

        query = text.strip().lower()

        for i in range(self.playlist_list.count()):
            item = self.playlist_list.item(i)
            item.setHidden(bool(query) and query not in item.text().lower())

        self.update_selection_count()

    def update_selection_count(self):
        total = self.playlist_list.count()
        filtering = bool(self.search_input.text().strip())

        checked = shown = hidden_checked = 0

        for i in range(total):
            item = self.playlist_list.item(i)
            is_checked = item.checkState() == Qt.CheckState.Checked

            checked += is_checked
            shown += not item.isHidden()
            hidden_checked += is_checked and item.isHidden()

        text = f"{checked} of {total} selected"

        if filtering:
            text += f"  ·  {shown} shown"

            if hidden_checked:
                text += f"  ·  {hidden_checked} selected but hidden by the filter"

        self.selection_count_label.setText(text)

        if checked == 0:
            hint = "Nothing selected"
        elif checked == total:
            hint = f"All (1-{total}), or type e.g. 1-3, 5, 8-10"
        else:
            hint = ""

        self.range_input.setPlaceholderText(hint)
        self.update_download_button()

    @staticmethod
    def format_ranges(indices):
        """Format numbers like [1, 2, 3, 5] as '1-3, 5'."""

        if not indices:
            return ""

        numbers = sorted(indices)
        parts = []
        start = end = numbers[0]

        for number in numbers[1:]:
            if number == end + 1:
                end = number
            else:
                parts.append(str(start) if start == end else f"{start}-{end}")
                start = end = number

        parts.append(str(start) if start == end else f"{start}-{end}")

        return ", ".join(parts)

    def sync_range_from_checks(self):
        """Make the range box describe the currently checked items."""

        total = self.playlist_list.count()
        checked = [
            i + 1
            for i in range(total)
            if self.playlist_list.item(i).checkState() == Qt.CheckState.Checked
        ]

        if not checked or len(checked) == total:
            self.range_input.clear()
        else:
            self.range_input.setText(self.format_ranges(checked))

    def on_range_edited(self, text):
        """Update the checkboxes to match the typed range."""

        if not self.playlist:
            return

        total = len(self.playlist.entries)
        indices = parse_range(text, total)

        if indices is None:
            return

        self._syncing_checks = True

        for index in range(total):
            self.playlist_list.item(index).setCheckState(
                Qt.CheckState.Checked
                if (index + 1) in indices
                else Qt.CheckState.Unchecked
            )

        self._syncing_checks = False
        self.update_selection_count()

    def on_checklist_item_changed(self, _item):
        if self._syncing_checks or not self.playlist:
            return

        self.sync_range_from_checks()
        self.update_selection_count()

    def set_shown_checks(self, state):
        """Check or uncheck every item that is currently shown."""

        self._syncing_checks = True

        for i in range(self.playlist_list.count()):
            item = self.playlist_list.item(i)

            if not item.isHidden():
                item.setCheckState(state)

        self._syncing_checks = False

        self.sync_range_from_checks()
        self.update_selection_count()

    def select_all_videos(self):
        self.set_shown_checks(Qt.CheckState.Checked)

    def select_no_videos(self):
        self.set_shown_checks(Qt.CheckState.Unchecked)

    def get_checked_entries(self):
        """The playlist entries whose checkboxes are checked."""

        if not self.playlist:
            return []

        return [
            entry
            for index, entry in enumerate(self.playlist.entries)
            if self.playlist_list.item(index).checkState() == Qt.CheckState.Checked
        ]

    # ----- fetching -------------------------------------------------------

    def on_url_edited(self):
        if self.state in ("ready", "done"):
            self.info = None
            self.playlist = None
            self.failed_items = []
            self.set_state("idle")
            self.show_status("")

    def ask_mixed_url_choice(self):
        """Ask whether a video-inside-a-playlist link means one or all.

        Returns "video", "playlist", or None if cancelled.
        """

        dialog = QMessageBox(self)
        dialog.setWindowTitle(APP_NAME)
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setText("This link is a video inside a playlist.")
        dialog.setInformativeText(
            "Would you like just this video, or the whole playlist?"
        )

        video_button = dialog.addButton(
            "Single Video", QMessageBox.ButtonRole.AcceptRole
        )
        playlist_button = dialog.addButton(
            "Whole Playlist", QMessageBox.ButtonRole.ActionRole
        )
        dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(video_button)

        dialog.exec()
        clicked = dialog.clickedButton()

        if clicked == video_button:
            return "video"

        if clicked == playlist_button:
            return "playlist"

        return None
    
    def paste_from_clipboard(self):
        if self.state in ("fetching", "downloading"):
            return

        text = QApplication.clipboard().text().strip()
        if not text:
            self.show_status("Clipboard is empty.", "warning")
            return

        self.url_input.setText(text)
        self.on_url_edited()
        self.fetch()

    def fetch(self):
        if self.state in ("fetching", "downloading"):
            return

        url = self.url_input.text().strip()

        if not url:
            self.show_status("Paste a YouTube link first.", "error")
            return

        prefer_playlist = False

        if has_playlist_and_video(url):
            choice = self.ask_mixed_url_choice()

            if choice is None:
                return

            prefer_playlist = choice == "playlist"

        self.url = url
        self.failed_items = []
        self.show_status("")
        self.item_label.setText(
            "Fetching playlist information…"
            if prefer_playlist
            else "Fetching video information…"
        )
        self.progress_bar.setRange(0, 0)
        self.progress_label.setText("")
        self.set_state("fetching")

        task = self.start_task(lambda thread: fetch_everything(url, prefer_playlist))
        task.done.connect(self.on_fetched)
        task.failed.connect(self.on_fetch_failed)
        task.start()

    def on_fetched(self, result):
        self.mode = result[0]

        if self.mode == "video":
            _, self.info, self.summary, self.video_options, thumbnail = result
            self.playlist = None
            self.search_input.clear()
            self.playlist_list.clear()

            self.title_label.setText(self.summary.title)
            self.meta_label.setText(
                f"{self.summary.channel}  ·  "
                f"{format_duration(self.summary.duration)}  ·  "
                f"{self.summary.upload_date}"
            )
            self.show_thumbnail(thumbnail)

        else:
            _, self.playlist, thumbnail = result
            self.info = None
            self.summary = None
            self.video_options = []

            count = len(self.playlist.entries)
            self.title_label.setText(self.playlist.title)
            self.meta_label.setText(
                f"Playlist  ·  {count} {'video' if count == 1 else 'videos'}"
            )
            self.show_thumbnail(thumbnail, placeholder="PLAYLIST")

            self.range_input.clear()
            self.search_input.clear()
            self.subfolder_input.setText(self.playlist.title)
            self.subfolder_input.setPlaceholderText(self.playlist.title)
            self.populate_playlist_checklist()

        self.populate_quality()
        self.show_status("")
        self.set_state("ready")

    def on_fetch_failed(self, message):
        self.show_status(message, "error")
        self.set_state("idle")

    def populate_quality(self):
        if self.mode == "video" and self.info is None:
            return

        if self.mode == "playlist" and self.playlist is None:
            return

        settings = load_settings()
        self.quality_box.clear()

        if self.video_radio.isChecked():
            if self.mode == "video":
                self.quality_label.setText("Quality")

                for height, size in self.video_options:
                    text = f"{height}p"

                    if size:
                        text += f"    ·    ~{format_file_size(size)}"

                    self.quality_box.addItem(text, height)
            else:
                self.quality_label.setText("Max quality")

                for height in PLAYLIST_QUALITIES:
                    self.quality_box.addItem(f"Up to {height}p", height)

            saved = settings.get("video_quality")

        else:
            self.quality_label.setText("Bitrate")

            for choice in AUDIO_CHOICES:
                self.quality_box.addItem(audio_label(choice), choice)

            saved = settings.get("audio_bitrate")

        index = self.quality_box.findData(saved)

        if index >= 0:
            self.quality_box.setCurrentIndex(index)

    # ----- downloading ----------------------------------------------------

    def start_download(self):
        setting = self.quality_box.currentData()

        if setting is None:
            self.show_status(
                "No supported MP4 resolutions for this video. Try the Audio option.",
                "warning",
            )
            return

        kind = "video" if self.video_radio.isChecked() else "audio"
        self.cancel_event = threading.Event()
        cancel = self.cancel_event

        if self.mode == "playlist":
            total = len(self.playlist.entries)
            range_text = self.range_input.text().strip()

            if range_text and parse_range(range_text, total) is None:
                self.show_status(
                    f"Invalid range. Use numbers between 1 and {total}, "
                    "for example 1-3, 5, 8-10, or use the checkboxes.",
                    "error",
                )
                return

            selected = self.get_checked_entries()

            if not selected:
                self.show_status("Select at least one video to download.", "error")
                return

            self._pending_playlist_entries = list(selected)
            folder_name = self.subfolder_input.text().strip() or self.playlist.title

            def job(thread):
                return download_playlist(
                    selected,
                    kind,
                    setting,
                    folder_name,
                    reporter=QtReporter(thread),
                    cancel=cancel,
                )

            done_handler = self.on_playlist_done

        else:
            self._pending_playlist_entries = []
            function = download_video if kind == "video" else download_audio
            url, info = self.url, self.info

            def job(thread):
                return function(
                    url,
                    setting,
                    info=info,
                    reporter=QtReporter(thread),
                    cancel=cancel,
                )

            done_handler = self.on_download_done

        self.download_kind = kind
        self.download_setting = setting
        self.failed_items = []

        self.show_status("")
        self.item_label.setText("Starting download…")
        self.progress_bar.setRange(0, 0)
        self.progress_label.setText("")
        self.set_state("downloading")

        task = self.start_task(job)
        task.message.connect(self.show_status)
        task.stage.connect(self.on_stage)
        task.playlist_item.connect(self.on_playlist_item)
        task.progress.connect(self.on_progress)
        task.progress_done.connect(self.on_progress_done)
        task.done.connect(done_handler)
        task.failed.connect(self.on_download_failed)
        task.start()

    def cancel_download(self):
        if self.cancel_event is not None:
            self.cancel_event.set()

        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Cancelling…")

    def on_playlist_item(self, number, total, title):
        self.item_label.setText(f"({number}/{total})  {title}")
        self.item_label.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.progress_label.setText("")
        self.cancel_button.setEnabled(
            self.cancel_event is None or not self.cancel_event.is_set()
        )
        self.fit_window()

    def on_stage(self, text):
        # Merging and converting can't be interrupted.
        self.item_label.setText(text)
        self.item_label.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.progress_label.setText("")
        self.cancel_button.setEnabled(False)

    def on_progress(self, downloaded, total, speed, eta):
        if self.mode == "video":
            self.item_label.setText("Downloading…")
            self.item_label.setVisible(True)

        if total:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(min(1000, int(downloaded * 1000 / total)))
            parts = [f"{format_file_size(downloaded)} / {format_file_size(total)}"]
        else:
            self.progress_bar.setRange(0, 0)
            parts = [format_file_size(downloaded)]

        if speed:
            parts.append(f"{format_file_size(speed)}/s")

        if eta is not None:
            parts.append(f"ETA {format_duration(eta)}")

        self.progress_label.setText("    ·    ".join(parts))

    def on_progress_done(self):
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_label.clear()

    def remember_choice(self):
        key = "video_quality" if self.download_kind == "video" else "audio_bitrate"
        remember_setting(key, self.download_setting)

    def on_download_done(self, result):
        self.last_folder = result.folder

        if result.status == "completed":
            title = self.summary.title if self.summary else "Downloaded media"
            vid = (self.info or {}).get("id")
            saved_file = _find_downloaded_file(result.folder, vid, result.filename)
            if saved_file is not None:
                self._record_history(
                    title,
                    self.download_kind,
                    _quality_badge(self.download_kind, self.download_setting),
                    saved_file,
                )

            self.show_status("✔ Download complete · Saved to History", "success")
            self.send_notification("Download complete", title)
            self.remember_choice()

        elif result.status == "exists":
            self.show_status(f"Already downloaded: {result.filename}", "warning")

        else:
            self.show_status("Download cancelled.", "warning")

        self.set_state("done")

    def on_playlist_done(self, result):
        self.last_folder = result.folder
        self.failed_items = result.failed

        if result.folder is not None and self._pending_playlist_entries:
            failed_titles = {title for title, _ in result.failed}
            quality_str = _quality_badge(self.download_kind, self.download_setting)
            for entry in self._pending_playlist_entries:
                entry_title = entry.get("title") or entry.get("id") or "Untitled"
                if entry_title in failed_titles:
                    continue
                saved_file = _find_downloaded_file(result.folder, entry.get("id"))
                if saved_file is not None:
                    self._record_history(
                        entry_title, self.download_kind, quality_str, saved_file
                    )
        self._pending_playlist_entries = []

        summary_text = (
            f"{result.completed} completed, "
            f"{result.skipped} already downloaded, "
            f"{len(result.failed)} failed."
        )

        if result.cancelled:
            self.show_status(f"Playlist cancelled — {summary_text}", "warning")
        elif result.failed and result.completed == 0:
            self.show_status(f"Playlist failed — {summary_text}", "error")
        elif result.failed:
            self.show_status(
                f"Playlist finished with errors — {summary_text}", "warning"
            )
        else:
            self.show_status(f"✔ Playlist complete — {summary_text}", "success")

        if result.completed:
            self.send_notification(
                "Playlist finished",
                f"{result.completed} of {result.total} downloaded",
            )
            self.remember_choice()

        if result.failed:
            self.diagnostics_button.show()

        self.set_state("done")

    def on_download_failed(self, message):
        self._pending_playlist_entries = []
        self.show_status(message, "error")
        self.set_state("ready")

    def show_failures(self):
        if not self.failed_items:
            return

        lines = [f"• {title}\n   {reason}" for title, reason in self.failed_items]

        QMessageBox.warning(self, "Failed Playlist Items", "\n\n".join(lines))

    # ----- folder ---------------------------------------------------------

    def choose_folder(self):
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose download folder", str(get_download_dir())
        )

        if not chosen:
            return

        try:
            folder = save_download_dir(chosen)
        except (OSError, ValueError) as error:
            self.show_status(f"Could not use that folder: {error}", "error")
            return

        self.folder_label.setText(str(folder))

    def open_folder(self):
        if self.last_folder is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_folder)))

    # ----- yt-dlp updates -------------------------------------------------

    def on_update_available(self, version):
        self.update_button.setText(f"Update yt-dlp ({version})")
        self.update_button.setToolTip(
            f"A newer yt-dlp, version {version}, is available."
        )

    def open_progress(self, text):
        dialog = QProgressDialog(text, "", 0, 0, self)
        dialog.setWindowTitle("Update yt-dlp")
        dialog.setCancelButton(None)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.show()

        return dialog

    def update_ytdlp(self):
        if self.state in ("fetching", "downloading"):
            return

        progress = self.open_progress("Checking for updates…")

        task = self.start_task(lambda thread: updater.check_for_update())
        task.done.connect(lambda info: self.on_update_checked(progress, info))
        task.failed.connect(lambda message: self.on_update_failed(progress, message))
        task.start()

    def on_update_failed(self, progress, message):
        progress.close()
        self.show_status(message, "error")

    def on_update_checked(self, progress, info):
        progress.close()

        if not info.available:
            self.update_button.setText("Update yt-dlp")
            self.update_button.setToolTip("")

        box = QMessageBox(self)
        box.setWindowTitle("Update yt-dlp")

        daily = QCheckBox("Check for updates once a day")
        daily.setChecked(updater.auto_check_enabled())
        box.setCheckBox(daily)

        install_button = restart_button = builtin_button = None

        if info.available:
            box.setIcon(QMessageBox.Icon.Question)
            box.setText(f"yt-dlp {info.latest} is available.")
            box.setInformativeText(
                f"You have {info.current}. Download and install the update?"
            )
            install_button = box.addButton("Update", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)

        elif info.pending_restart:
            box.setIcon(QMessageBox.Icon.Information)
            box.setText("An update is already installed.")
            box.setInformativeText(
                f"yt-dlp {info.installed} will be used after the app restarts."
            )
            restart_button = box.addButton(
                "Restart now", QMessageBox.ButtonRole.AcceptRole
            )
            box.addButton("Later", QMessageBox.ButtonRole.RejectRole)

        else:
            box.setIcon(QMessageBox.Icon.Information)
            box.setText("yt-dlp is up to date.")
            box.setInformativeText(f"Version {info.current}")
            box.addButton(QMessageBox.StandardButton.Ok)

            if updater.update_active():
                builtin_button = box.addButton(
                    "Use the built-in version",
                    QMessageBox.ButtonRole.ActionRole,
                )

        box.exec()
        updater.set_auto_check(daily.isChecked())

        clicked = box.clickedButton()

        if install_button is not None and clicked is install_button:
            self.install_update(info)

        elif restart_button is not None and clicked is restart_button:
            self.restart_app()

        elif builtin_button is not None and clicked is builtin_button:
            updater.remove_update()
            self.show_status("The built-in yt-dlp will be used after the app restarts.")

    def install_update(self, info):
        progress = self.open_progress("Starting the update…")

        def job(thread):
            return updater.install_update(
                report=lambda text: thread.message.emit(text, "info"),
                info=info,
            )

        task = self.start_task(job)
        task.message.connect(lambda text, level: progress.setLabelText(text))
        task.done.connect(lambda version: self.on_update_installed(progress, version))
        task.failed.connect(lambda message: self.on_update_failed(progress, message))
        task.start()

    def on_update_installed(self, progress, version):
        progress.close()
        self.update_button.setText("Update yt-dlp")
        self.update_button.setToolTip("")
        self.show_status(
            f"✔ yt-dlp {version} is installed. Restart the app to use it.",
            "success",
        )

        answer = QMessageBox.question(
            self,
            "Update yt-dlp",
            f"yt-dlp {version} is installed.\n\nRestart the app now to use it?",
        )

        if answer == QMessageBox.StandardButton.Yes:
            self.restart_app()

    def restart_app(self):
        if updater.restart():
            self.close()
        else:
            self.show_status(
                "Couldn't restart automatically. "
                "Please start the app again yourself.",
                "warning",
            )

    # ----- closing --------------------------------------------------------

    def closeEvent(self, event):
        task = self.task

        if task is not None and task.isRunning():
            if self.state == "downloading":
                answer = QMessageBox.question(
                    self,
                    APP_NAME,
                    "A download is in progress. Cancel it and quit?",
                )

                if answer != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return

                if self.cancel_event is not None:
                    self.cancel_event.set()

            # Fetching can't be cancelled, so stop listening to it, hide the
            # window and let the task finish in the background.
            task.detach()
            self.hide()

            if not task.wait(CLOSE_WAIT_MS):
                # Stuck (for example, no network). Stopping it abruptly is
                # acceptable because the program is exiting.
                task.terminate()
                task.wait(2000)

        if self.tray_icon is not None:
            self.tray_icon.hide()

        event.accept()


def main():
    setup_logging()

    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "maverick.ytdownloader.gui"
            )
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("yt-downloader")

    base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    icon_path = os.path.join(base_dir, "app.ico")
    icon = QIcon(icon_path) if os.path.isfile(icon_path) else QIcon()

    if not icon.isNull():
        app.setWindowIcon(icon)

    window = MainWindow()
    window.apply_current_theme(save=False)
    if not icon.isNull():
        window.setWindowIcon(icon)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()