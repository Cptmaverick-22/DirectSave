"""YT Downloader - modern, responsive mobile interface (BeeWare Toga)."""

from __future__ import annotations

import logging
from pathlib import Path
import shutil
import sys
import threading
import time
import urllib.parse
import urllib.request

import toga
from toga.style import Pack
from toga.style.pack import BOLD, CENTER, COLUMN, RIGHT, ROW, SANS_SERIF

import yt_dlp

from config import (
    APP_NAME,
    APP_VERSION,
    AUDIO_CHOICES,
    ORIGINAL_AUDIO,
    audio_label,
    load_settings,
    remember_setting,
)
from downloader import (
    DownloadCancelled,
    DownloadError,
    DownloadResult,
    PlaylistLinkError,
    ProgressTracker,
    Reporter,
    download_audio,
    download_video,
    fetch_video_info,
    format_duration,
    format_file_size,
    friendly_error,
    quality_options,
    summarize_video,
)
from logs import setup_logging
from ytdownloader.storage import (
    android_preset_dirs,
    configure_mobile_paths,
    friendly_path,
    is_android,
    set_mobile_download_dir,
)

THUMBNAIL_MAX_BYTES = 5 * 1024 * 1024

THEME_ORDER = ("obsidian", "forest", "light")

THEMES = {
    "obsidian": {
        "label": "Obsidian",
        "page_bg": "#121212",
        "card_bg": "#1C1C1E",
        "input_bg": "#242427",
        "border": "#303036",
        "text": "#F5F5F7",
        "muted": "#9E9EA7",
        "accent": "#E11D48",
        "accent_border": "#FB7185",
        "accent_text": "#FFFFFF",
        "secondary_bg": "#27272B",
        "secondary_text": "#ECECEF",
        "danger_bg": "#33161C",
        "danger_border": "#7F1D1D",
        "danger_text": "#FB7185",
        "status": {
            "info": "#9E9EA7",
            "success": "#34D399",
            "warning": "#FBBF24",
            "error": "#FB7185",
        },
    },
    "forest": {
        "label": "Forest",
        "page_bg": "#0F1412",
        "card_bg": "#171F1B",
        "input_bg": "#1F2A25",
        "border": "#2C3B34",
        "text": "#F2F7F4",
        "muted": "#93A89E",
        "accent": "#10B981",
        "accent_border": "#34D399",
        "accent_text": "#041C14",
        "secondary_bg": "#222E28",
        "secondary_text": "#E3EFE9",
        "danger_bg": "#2E1518",
        "danger_border": "#7F1D1D",
        "danger_text": "#F87171",
        "status": {
            "info": "#93A89E",
            "success": "#34D399",
            "warning": "#FBBF24",
            "error": "#F87171",
        },
    },
    "light": {
        "label": "Warm Light",
        "page_bg": "#F5F4F0",
        "card_bg": "#FFFFFF",
        "input_bg": "#F2F1EC",
        "border": "#DFDDD6",
        "text": "#18181B",
        "muted": "#6E6E77",
        "accent": "#E11D48",
        "accent_border": "#BE123C",
        "accent_text": "#FFFFFF",
        "secondary_bg": "#ECEBE6",
        "secondary_text": "#18181B",
        "danger_bg": "#FEE2E2",
        "danger_border": "#FCA5A5",
        "danger_text": "#DC2626",
        "status": {
            "info": "#6E6E77",
            "success": "#15803D",
            "warning": "#B45309",
            "error": "#DC2626",
        },
    },
}


# --------------------------------------------------------------------------
# Typography & native rounded-corner helpers
# --------------------------------------------------------------------------

def _resolve_ui_font() -> str:
    """Use Inter if bundled or installed, else fall back to a clean system UI font."""
    resources = Path(__file__).parent / "resources"
    regular_ttf = resources / "Inter-Regular.ttf"
    bold_ttf = resources / "Inter-Bold.ttf"

    if regular_ttf.is_file():
        try:
            toga.Font.register("Inter", regular_ttf)
            if bold_ttf.is_file():
                toga.Font.register("Inter", bold_ttf, weight=BOLD)
            return "Inter"
        except Exception:
            pass

    if sys.platform == "win32":
        try:
            import System.Drawing.Text as DrawingText

            installed = {
                f.Name for f in DrawingText.InstalledFontCollection().Families
            }
            for candidate in ("Inter", "Segoe UI"):
                if candidate in installed:
                    return candidate
        except Exception:
            pass

    return SANS_SERIF


def _format_responsive_path(path: Path, available_px: int) -> str:
    """Show full path when space allows; shorten at folder boundaries when narrow."""
    text = friendly_path(path)
    max_chars = max(24, int((available_px - 68) / 7.5))
    if len(text) <= max_chars:
        return text

    sep = "\\" if "\\" in text else "/"
    parts = [p for p in text.split(sep) if p]
    if len(parts) >= 3:
        head = parts[0]
        tail = parts[-1]
        for extra in reversed(parts[1:-1]):
            candidate = f"{head}{sep}…{sep}{extra}{sep}{tail}"
            if len(candidate) <= max_chars:
                tail = f"{extra}{sep}{tail}"
            else:
                break
        shortened = f"{head}{sep}…{sep}{tail}"
        if len(shortened) <= max_chars:
            return shortened

    keep_start = max_chars // 2 - 2
    keep_end = max_chars - keep_start - 3
    return f"{text[:keep_start]}…{text[-keep_end:]}"


def _winforms_rounded_path(width: int, height: int, radius: int, inset: int = 0):
    """Build a .NET GraphicsPath with smooth rounded corners."""
    import System.Drawing.Drawing2D as Drawing2D

    w = max(2, width - inset * 2)
    h = max(2, height - inset * 2)
    d = max(2, min(radius * 2, w - 1, h - 1))
    x0 = float(inset)
    y0 = float(inset)

    path = Drawing2D.GraphicsPath()
    path.AddArc(x0, y0, float(d), float(d), 180.0, 90.0)
    path.AddArc(x0 + w - d, y0, float(d), float(d), 270.0, 90.0)
    path.AddArc(x0 + w - d, y0 + h - d, float(d), float(d), 0.0, 90.0)
    path.AddArc(x0, y0 + h - d, float(d), float(d), 90.0, 90.0)
    path.CloseFigure()
    return path


class _NativeStyler:
    """Applies rounded corners & crisp 1px borders on both Android and Windows."""

    def __init__(self):
        self._styles: dict[int, dict] = {}
        self._hooked: set[int] = set()

    def apply(
        self,
        widget,
        bg_hex: str,
        border_hex: str,
        radius: int = 12,
        is_button: bool = False,
    ):
        if is_android():
            self._apply_android(widget, bg_hex, border_hex, radius)
        elif sys.platform == "win32":
            self._apply_winforms(widget, bg_hex, border_hex, radius, is_button)

    @staticmethod
    def _apply_android(widget, bg_hex: str, border_hex: str, radius_dp: int):
        try:
            from android.graphics import Color
            from android.graphics.drawable import GradientDrawable

            native = widget._impl.native
            density = native.getContext().getResources().getDisplayMetrics().density
            drawable = GradientDrawable()
            drawable.setColor(Color.parseColor(bg_hex))
            drawable.setCornerRadius(float(radius_dp) * density)
            drawable.setStroke(max(1, int(round(density))), Color.parseColor(border_hex))
            native.setBackground(drawable)
        except Exception:
            pass

    def _apply_winforms(
        self,
        widget,
        bg_hex: str,
        border_hex: str,
        radius: int,
        is_button: bool,
    ):
        try:
            import System.Drawing as Drawing
            import System.Drawing.Drawing2D as Drawing2D
            import System.Windows.Forms as WinForms

            native = widget._impl.native
            key = id(widget)
            self._styles[key] = {"radius": radius, "border": border_hex}

            if is_button:
                native.FlatStyle = WinForms.FlatStyle.Flat
                native.FlatAppearance.BorderSize = 0
                native.Cursor = WinForms.Cursors.Hand
                native.UseVisualStyleBackColor = False

            if key not in self._hooked:
                self._hooked.add(key)

                def _update_region(sender, _args, k=key):
                    try:
                        w, h = sender.Width, sender.Height
                        if w > 4 and h > 4:
                            r = self._styles[k]["radius"]
                            sender.Region = Drawing.Region(
                                _winforms_rounded_path(w, h, r, inset=0)
                            )
                    except Exception:
                        pass

                def _paint_border(sender, args, k=key):
                    try:
                        w, h = sender.Width, sender.Height
                        if w <= 4 or h <= 4:
                            return
                        cfg = self._styles[k]
                        g = args.Graphics
                        g.SmoothingMode = Drawing2D.SmoothingMode.AntiAlias
                        color = Drawing.ColorTranslator.FromHtml(cfg["border"])
                        pen = Drawing.Pen(color, 1.0)
                        path = _winforms_rounded_path(
                            w - 1, h - 1, max(2, cfg["radius"] - 1), inset=0
                        )
                        g.DrawPath(pen, path)
                        pen.Dispose()
                        path.Dispose()
                    except Exception:
                        pass

                native.SizeChanged += _update_region
                native.Paint += _paint_border

            if native.Width > 4 and native.Height > 4:
                native.Region = Drawing.Region(
                    _winforms_rounded_path(native.Width, native.Height, radius, inset=0)
                )
            native.Invalidate()
        except Exception:
            pass

    @staticmethod
    def style_input(widget):
        """Remove old 3D sunken borders from WinForms TextBox inputs."""
        if sys.platform != "win32":
            return
        try:
            import System.Windows.Forms as WinForms

            widget._impl.native.BorderStyle = WinForms.BorderStyle.FixedSingle
        except Exception:
            pass


# --------------------------------------------------------------------------
# Core downloader bridge
# --------------------------------------------------------------------------

class TogaReporter(Reporter):
    """Forwards downloader.py events safely to Toga's main UI thread."""

    def __init__(self, app: "YTDownloaderApp"):
        self.app = app
        self._last_progress = 0.0

    def message(self, text, level="info"):
        self.app.call_on_ui(self.app.show_status, text, level)

    def stage(self, text):
        self.app.call_on_ui(self.app.on_stage, text)

    def progress(self, downloaded, total, speed, eta):
        now = time.monotonic()
        if now - self._last_progress < 0.15:
            return
        self._last_progress = now
        self.app.call_on_ui(self.app.on_progress, downloaded, total, speed, eta)

    def progress_done(self):
        self._last_progress = 0.0
        self.app.call_on_ui(self.app.on_progress_done)


def _fetch_thumbnail_bytes(url: str | None) -> bytes | None:
    """Download HTTPS thumbnail bytes safely, or return None."""
    if not url:
        return None
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            return None
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if urllib.parse.urlparse(resp.geturl()).scheme != "https":
                return None
            length = resp.headers.get("Content-Length")
            if length and int(length) > THUMBNAIL_MAX_BYTES:
                return None
            data = resp.read(THUMBNAIL_MAX_BYTES + 1)
            return None if len(data) > THUMBNAIL_MAX_BYTES else data
    except Exception:
        return None


def _pick_jpeg_thumbnail_url(info: dict, fallback_url: str | None) -> str | None:
    """Prefer a JPEG/PNG thumbnail URL (Toga doesn't decode WebP on Windows)."""
    thumbnails = info.get("thumbnails") or []
    jpg_candidates = [
        t
        for t in thumbnails
        if isinstance(t, dict)
        and (t.get("url") or "")
        .lower()
        .split("?", 1)[0]
        .endswith((".jpg", ".jpeg", ".png"))
    ]
    if jpg_candidates:
        jpg_candidates.sort(key=lambda t: t.get("width") or 0)
        for t in jpg_candidates:
            if (t.get("width") or 0) >= 480:
                return t["url"]
        return jpg_candidates[-1]["url"]

    video_id = info.get("id")
    if video_id:
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return fallback_url


def _direct_download(
    url: str,
    kind: str,
    setting,
    output_dir: Path,
    reporter: Reporter,
    cancel: threading.Event,
) -> DownloadResult:
    """Fallback single-stream download when FFmpeg is not bundled (Android)."""
    output_dir.mkdir(parents=True, exist_ok=True)

    if kind == "video":
        fmt = (
            f"best[ext=mp4][height<={setting}][acodec!=none][vcodec!=none]/"
            "best[ext=mp4][acodec!=none][vcodec!=none]/"
            "best[acodec!=none][vcodec!=none]"
        )
        tag = f"[{setting}p]"
    else:
        fmt = "bestaudio[ext=m4a]/bestaudio"
        tag = f"[{ORIGINAL_AUDIO}]"

    outtmpl = str(output_dir / f"%(title).120B {tag} [%(id)s].%(ext)s")
    tracker = ProgressTracker(reporter, expected_total=None, cancel=cancel)
    opts = {
        "quiet": True,
        "noplaylist": True,
        "format": fmt,
        "outtmpl": outtmpl,
        "progress_hooks": [tracker],
        "restrictfilenames": False,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except DownloadCancelled:
        reporter.progress_done()
        return DownloadResult("cancelled", output_dir)
    except Exception as error:
        reporter.progress_done()
        raise DownloadError(friendly_error(error)) from error

    reporter.progress_done()
    return DownloadResult("completed", output_dir)


def _make_card(inner_box: toga.Box) -> toga.Box:
    """Wrap an inner box inside a clean padded card container."""
    inner_box.style.margin = 14
    card = toga.Box(style=Pack(direction=COLUMN))
    card.add(inner_box)
    return card


# --------------------------------------------------------------------------
# Main Application
# --------------------------------------------------------------------------

class YTDownloaderApp(toga.App):
    def startup(self):
        self.download_dir = configure_mobile_paths(self)
        setup_logging()

        self.ui_font = _resolve_ui_font()
        self.styler = _NativeStyler()
        self._current_width = 430

        saved_theme = load_settings().get("mobile_theme")
        self.theme_name = saved_theme if saved_theme in THEMES else "obsidian"
        self._status_level = "info"
        self._editing_folder = False

        self.has_ffmpeg = bool(shutil.which("ffmpeg"))
        self.state = "idle"  # idle, fetching, ready, downloading, done
        self.mode = "video"  # "video" or "audio"
        self.url = ""
        self.info = None
        self.summary = None
        self.video_options: list[tuple[int, int | None]] = []
        self.quality_values: list = []
        self.cancel_event: threading.Event | None = None

        self._build_ui()

        self.main_window = toga.MainWindow(
            title=f"{APP_NAME} v{APP_VERSION}",
            size=(430, 680),
            on_resize=self.on_window_resize,
        )
        self.main_window.content = self.root_scroll
        self._apply_theme(save=False)
        self._set_state("idle")
        self._refresh_responsive_text()
        self.main_window.show()

    # ----- thread-safe UI dispatch ----------------------------------------

    def call_on_ui(self, func, *args):
        self.loop.call_soon_threadsafe(func, *args)

    # ----- responsive text sizing -----------------------------------------

    def on_window_resize(self, window, **kwargs):
        width = getattr(window.size, "width", 0)
        if width and width != self._current_width:
            self._current_width = width
            self._refresh_responsive_text()

    def _refresh_responsive_text(self):
        self.folder_label.text = _format_responsive_path(
            self.download_dir, self._current_width
        )
        if self.summary is not None:
            title = self.summary.title
            max_title = max(36, int((self._current_width - 68) / 7.2) * 2)
            self.title_label.text = (
                title if len(title) <= max_title else f"{title[: max_title - 1]}…"
            )

    # ----- layout ---------------------------------------------------------

    def _build_ui(self):
        f = self.ui_font
        self._cards: list[toga.Box] = []
        self._card_inners: list[toga.Box] = []
        self._primary_labels: list[toga.Label] = []
        self._muted_labels: list[toga.Label] = []
        self._secondary_buttons: list[toga.Button] = []

        # 1. Header bar
        self.header_box = toga.Box(
            style=Pack(direction=ROW, align_items=CENTER, gap=10, margin_bottom=2)
        )
        icon_file = Path(__file__).parent / "resources" / "icon.png"
        if icon_file.is_file():
            self.header_box.add(
                toga.ImageView(
                    toga.Image(icon_file),
                    style=Pack(width=34, height=34),
                )
            )

        self.app_title_label = toga.Label(
            APP_NAME,
            style=Pack(font_family=f, font_size=16, font_weight=BOLD),
        )
        self.app_sub_label = toga.Label(
            f"v{APP_VERSION}",
            style=Pack(font_family=f, font_size=9),
        )

        self.title_col = toga.Box(style=Pack(direction=COLUMN, flex=1, gap=1))
        self.title_col.add(self.app_title_label)
        self.title_col.add(self.app_sub_label)
        self.header_box.add(self.title_col)

        self.theme_button = toga.Button(
            "Obsidian",
            on_press=self.on_cycle_theme,
            style=Pack(
                font_family=f,
                width=108,
                height=34,
                font_size=10,
                font_weight=BOLD,
            ),
        )
        self._secondary_buttons.append(self.theme_button)
        self.header_box.add(self.theme_button)

        # 2. URL input card
        url_caption = toga.Label(
            "YOUTUBE LINK",
            style=Pack(font_family=f, font_size=9, font_weight=BOLD),
        )
        self._muted_labels.append(url_caption)

        self.url_input = toga.TextInput(
            placeholder="Paste a YouTube video link…",
            on_change=self.on_url_edited,
            on_confirm=self.on_fetch_pressed,
            style=Pack(font_family=f, font_size=11, height=40),
        )
        self.fetch_button = toga.Button(
            "Fetch Video",
            on_press=self.on_fetch_pressed,
            style=Pack(
                font_family=f,
                flex=1,
                height=40,
                font_size=11,
                font_weight=BOLD,
            ),
        )
        self.clear_button = toga.Button(
            "Clear",
            on_press=self.on_clear_pressed,
            style=Pack(
                font_family=f,
                width=82,
                height=40,
                font_size=11,
                font_weight=BOLD,
            ),
        )
        self._secondary_buttons.append(self.clear_button)

        url_actions = toga.Box(style=Pack(direction=ROW, gap=10, margin_top=2))
        url_actions.add(self.fetch_button)
        url_actions.add(self.clear_button)

        url_inner = toga.Box(style=Pack(direction=COLUMN, gap=8))
        url_inner.add(url_caption)
        url_inner.add(self.url_input)
        url_inner.add(url_actions)
        self.url_card = _make_card(url_inner)
        self._cards.append(self.url_card)
        self._card_inners.extend([url_inner, url_actions])

        # 3. Media preview card
        self.thumb_view = toga.ImageView(style=Pack(height=176, flex=1))
        thumb_wrap = toga.Box(
            style=Pack(direction=ROW, align_items=CENTER, margin_bottom=4)
        )
        thumb_wrap.add(self.thumb_view)

        self.title_label = toga.Label(
            "",
            style=Pack(font_family=f, font_size=13, font_weight=BOLD),
        )
        self.meta_label = toga.Label(
            "",
            style=Pack(font_family=f, font_size=10),
        )
        self._primary_labels.append(self.title_label)
        self._muted_labels.append(self.meta_label)

        preview_inner = toga.Box(style=Pack(direction=COLUMN, gap=5))
        preview_inner.add(thumb_wrap)
        preview_inner.add(self.title_label)
        preview_inner.add(self.meta_label)
        self.preview_card = _make_card(preview_inner)
        self._cards.append(self.preview_card)
        self._card_inners.extend([preview_inner, thumb_wrap])

        # 4. Format & Quality card
        format_caption = toga.Label(
            "FORMAT",
            style=Pack(font_family=f, font_size=9, font_weight=BOLD),
        )
        self._muted_labels.append(format_caption)

        self.video_mode_btn = toga.Button(
            "Video (MP4)",
            on_press=self.on_select_video_mode,
            style=Pack(
                font_family=f,
                flex=1,
                height=38,
                font_size=11,
                font_weight=BOLD,
            ),
        )
        self.audio_mode_btn = toga.Button(
            "Audio",
            on_press=self.on_select_audio_mode,
            style=Pack(
                font_family=f,
                flex=1,
                height=38,
                font_size=11,
                font_weight=BOLD,
            ),
        )
        mode_row = toga.Box(style=Pack(direction=ROW, gap=10))
        mode_row.add(self.video_mode_btn)
        mode_row.add(self.audio_mode_btn)

        self.quality_caption = toga.Label(
            "VIDEO QUALITY",
            style=Pack(font_family=f, font_size=9, font_weight=BOLD, margin_top=4),
        )
        self._muted_labels.append(self.quality_caption)

        self.quality_selection = toga.Selection(
            items=[],
            style=Pack(font_family=f, font_size=11, height=38),
        )

        options_inner = toga.Box(style=Pack(direction=COLUMN, gap=8))
        options_inner.add(format_caption)
        options_inner.add(mode_row)
        options_inner.add(toga.Divider(style=Pack(margin_top=2, margin_bottom=2)))
        options_inner.add(self.quality_caption)
        options_inner.add(self.quality_selection)
        self.options_card = _make_card(options_inner)
        self._cards.append(self.options_card)
        self._card_inners.extend([options_inner, mode_row])

        # 5. Save Location card (responsive header row + full-width path below)
        save_caption = toga.Label(
            "SAVE LOCATION",
            style=Pack(font_family=f, flex=1, font_size=9, font_weight=BOLD),
        )
        self._muted_labels.append(save_caption)

        self.change_folder_btn = toga.Button(
            "Change…",
            on_press=self.on_change_folder,
            style=Pack(
                font_family=f,
                width=88,
                height=30,
                font_size=10,
                font_weight=BOLD,
            ),
        )
        self._secondary_buttons.append(self.change_folder_btn)

        save_header_row = toga.Box(
            style=Pack(direction=ROW, align_items=CENTER, gap=8)
        )
        save_header_row.add(save_caption)
        save_header_row.add(self.change_folder_btn)

        self.folder_label = toga.Label(
            _format_responsive_path(self.download_dir, self._current_width),
            style=Pack(font_family=f, font_size=11),
        )
        self._primary_labels.append(self.folder_label)

        # Inline fallback path editor (used if native picker isn't available)
        self.folder_input = toga.TextInput(
            value=str(self.download_dir),
            placeholder="/storage/emulated/0/YT Downloader",
            style=Pack(font_family=f, font_size=11, height=38),
        )
        self.save_path_btn = toga.Button(
            "Save Path",
            on_press=self.on_save_manual_folder,
            style=Pack(
                font_family=f,
                flex=1,
                height=36,
                font_size=10,
                font_weight=BOLD,
            ),
        )

        preset_row = toga.Box(style=Pack(direction=ROW, gap=8))
        for preset_name, preset_path in android_preset_dirs():
            btn = toga.Button(
                preset_name,
                on_press=lambda _w, p=preset_path: self._apply_preset_folder(p),
                style=Pack(font_family=f, flex=1, height=32, font_size=10),
            )
            self._secondary_buttons.append(btn)
            preset_row.add(btn)

        self.folder_editor_box = toga.Box(
            style=Pack(direction=COLUMN, gap=8, margin_top=6)
        )
        self.folder_editor_box.add(self.folder_input)
        if is_android():
            self.folder_editor_box.add(preset_row)
        self.folder_editor_box.add(self.save_path_btn)

        self.folder_inner = toga.Box(style=Pack(direction=COLUMN, gap=6))
        self.folder_inner.add(save_header_row)
        self.folder_inner.add(self.folder_label)
        self.folder_card = _make_card(self.folder_inner)
        self._cards.append(self.folder_card)
        self._card_inners.extend(
            [self.folder_inner, save_header_row, self.folder_editor_box, preset_row]
        )

        # 6. Primary Download CTA
        self.download_button = toga.Button(
            "Download",
            on_press=self.on_download_pressed,
            style=Pack(
                font_family=f,
                height=46,
                font_size=12,
                font_weight=BOLD,
                margin_top=2,
            ),
        )

        # 7. Progress card
        self.stage_label = toga.Label(
            "",
            style=Pack(font_family=f, flex=1, font_size=11, font_weight=BOLD),
        )
        self.percent_label = toga.Label(
            "",
            style=Pack(
                font_family=f,
                width=50,
                font_size=11,
                font_weight=BOLD,
                text_align=RIGHT,
            ),
        )
        self._primary_labels.extend([self.stage_label, self.percent_label])

        progress_top = toga.Box(style=Pack(direction=ROW, align_items=CENTER))
        progress_top.add(self.stage_label)
        progress_top.add(self.percent_label)

        self.progress_bar = toga.ProgressBar(max=100, value=0)
        self.progress_label = toga.Label(
            "", style=Pack(font_family=f, font_size=10)
        )
        self._muted_labels.append(self.progress_label)

        self.cancel_button = toga.Button(
            "Cancel Download",
            on_press=self.on_cancel_pressed,
            style=Pack(
                font_family=f,
                height=38,
                font_size=11,
                font_weight=BOLD,
                margin_top=2,
            ),
        )

        progress_inner = toga.Box(style=Pack(direction=COLUMN, gap=8))
        progress_inner.add(progress_top)
        progress_inner.add(self.progress_bar)
        progress_inner.add(self.progress_label)
        progress_inner.add(self.cancel_button)
        self.progress_card = _make_card(progress_inner)
        self._cards.append(self.progress_card)
        self._card_inners.extend([progress_inner, progress_top])

        # 8. Status banner card
        self.status_label = toga.Label(
            "",
            style=Pack(font_family=f, font_size=11, font_weight=BOLD),
        )
        status_inner = toga.Box(style=Pack(direction=COLUMN))
        status_inner.add(self.status_label)
        self.status_card = _make_card(status_inner)
        self._cards.append(self.status_card)
        self._card_inners.append(status_inner)

        # Responsive vertical container inside ScrollContainer
        self.content_box = toga.Box(
            style=Pack(direction=COLUMN, margin=(14, 14, 20, 14), gap=12)
        )
        self.content_box.add(self.header_box)
        self.content_box.add(self.url_card)

        self.root_scroll = toga.ScrollContainer(
            content=self.content_box, horizontal=False
        )

    # ----- theme ----------------------------------------------------------

    def on_cycle_theme(self, widget):
        idx = THEME_ORDER.index(self.theme_name)
        self.theme_name = THEME_ORDER[(idx + 1) % len(THEME_ORDER)]
        self._apply_theme(save=True)

    def _apply_theme(self, save: bool = False):
        t = THEMES[self.theme_name]
        if save:
            remember_setting("mobile_theme", self.theme_name)

        self.root_scroll.style.background_color = t["page_bg"]
        self.content_box.style.background_color = t["page_bg"]
        self.header_box.style.background_color = t["page_bg"]
        self.title_col.style.background_color = t["page_bg"]

        self.app_title_label.style.color = t["text"]
        self.app_title_label.style.background_color = t["page_bg"]
        self.app_sub_label.style.color = t["muted"]
        self.app_sub_label.style.background_color = t["page_bg"]

        self.theme_button.text = f"◐ {t['label']}"

        for card in self._cards:
            card.style.background_color = t["card_bg"]
            self.styler.apply(card, t["card_bg"], t["border"], radius=14)
        for inner in self._card_inners:
            inner.style.background_color = t["card_bg"]
        for lbl in self._primary_labels:
            lbl.style.color = t["text"]
            lbl.style.background_color = t["card_bg"]
        for lbl in self._muted_labels:
            lbl.style.color = t["muted"]
            lbl.style.background_color = t["card_bg"]
        for btn in self._secondary_buttons:
            btn.style.background_color = t["secondary_bg"]
            btn.style.color = t["secondary_text"]
            self.styler.apply(
                btn, t["secondary_bg"], t["border"], radius=10, is_button=True
            )

        for inp in (self.url_input, self.folder_input):
            inp.style.background_color = t["input_bg"]
            inp.style.color = t["text"]
            self.styler.style_input(inp)

        for accent_btn in (
            self.fetch_button,
            self.download_button,
            self.save_path_btn,
        ):
            accent_btn.style.background_color = t["accent"]
            accent_btn.style.color = t["accent_text"]
            self.styler.apply(
                accent_btn,
                t["accent"],
                t["accent_border"],
                radius=10,
                is_button=True,
            )

        self.cancel_button.style.background_color = t["danger_bg"]
        self.cancel_button.style.color = t["danger_text"]
        self.styler.apply(
            self.cancel_button,
            t["danger_bg"],
            t["danger_border"],
            radius=10,
            is_button=True,
        )

        self.status_label.style.background_color = t["card_bg"]
        self.status_label.style.color = t["status"].get(
            self._status_level, t["status"]["info"]
        )

        self._style_mode_buttons()

    def _style_mode_buttons(self):
        t = THEMES[self.theme_name]
        is_video = self.mode == "video"

        v_bg = t["accent"] if is_video else t["secondary_bg"]
        v_fg = t["accent_text"] if is_video else t["secondary_text"]
        v_bd = t["accent_border"] if is_video else t["border"]

        a_bg = t["accent"] if not is_video else t["secondary_bg"]
        a_fg = t["accent_text"] if not is_video else t["secondary_text"]
        a_bd = t["accent_border"] if not is_video else t["border"]

        self.video_mode_btn.style.background_color = v_bg
        self.video_mode_btn.style.color = v_fg
        self.styler.apply(self.video_mode_btn, v_bg, v_bd, radius=10, is_button=True)

        self.audio_mode_btn.style.background_color = a_bg
        self.audio_mode_btn.style.color = a_fg
        self.styler.apply(self.audio_mode_btn, a_bg, a_bd, radius=10, is_button=True)

    # ----- folder selection -----------------------------------------------

    async def on_change_folder(self, widget):
        if self.state in ("fetching", "downloading"):
            return

        try:
            dialog = toga.SelectFolderDialog(
                title="Choose download folder",
                initial_directory=self.download_dir,
            )
            chosen = await self.main_window.dialog(dialog)
        except NotImplementedError:
            self._toggle_folder_editor()
            return

        if chosen:
            self._commit_download_folder(chosen)

    def _toggle_folder_editor(self):
        self._editing_folder = not self._editing_folder
        if self._editing_folder:
            self.folder_input.value = str(self.download_dir)
            if self.folder_editor_box not in self.folder_inner.children:
                self.folder_inner.add(self.folder_editor_box)
        elif self.folder_editor_box in self.folder_inner.children:
            self.folder_inner.remove(self.folder_editor_box)

    def _apply_preset_folder(self, path: Path):
        self.folder_input.value = str(path)
        self._commit_download_folder(path)

    def on_save_manual_folder(self, widget):
        self._commit_download_folder(self.folder_input.value or "")

    def _commit_download_folder(self, new_path: str | Path):
        try:
            folder = set_mobile_download_dir(new_path)
        except (OSError, ValueError) as error:
            self.show_status(str(error), "error")
            return

        self.download_dir = folder
        self._refresh_responsive_text()
        if self._editing_folder:
            self._toggle_folder_editor()
        self.show_status(
            f"Save folder updated: {_format_responsive_path(folder, self._current_width)}",
            "success",
        )

    # ----- state & visibility ---------------------------------------------

    def _rebuild_visible_sections(self):
        """Show only active cards so hidden sections take zero vertical space."""
        busy = self.state in ("fetching", "downloading")
        has_media = self.state in ("ready", "downloading", "done")
        editable = self.state in ("ready", "done")

        desired = [self.header_box, self.url_card]
        if has_media:
            desired.append(self.preview_card)
            desired.append(self.options_card)
        desired.append(self.folder_card)
        if editable:
            desired.append(self.download_button)
        if busy:
            desired.append(self.progress_card)
        if self.status_label.text:
            desired.append(self.status_card)

        for child in list(self.content_box.children)[2:]:
            self.content_box.remove(child)
        for child in desired[2:]:
            self.content_box.add(child)

    def _set_state(self, state: str):
        self.state = state
        busy = state in ("fetching", "downloading")
        editable = state in ("ready", "done")

        self.url_input.enabled = not busy
        self.fetch_button.enabled = not busy
        self.fetch_button.text = "Fetching…" if state == "fetching" else "Fetch Video"
        self.clear_button.enabled = not busy
        self.change_folder_btn.enabled = not busy

        self.video_mode_btn.enabled = editable
        self.audio_mode_btn.enabled = editable
        self.quality_selection.enabled = editable
        self.download_button.enabled = editable and bool(self.quality_values)

        self.cancel_button.enabled = state == "downloading"
        self.cancel_button.text = "Cancel Download"

        if state == "fetching":
            self.percent_label.text = ""
            self.progress_bar.max = None
            self.progress_bar.start()
        elif state != "downloading":
            self.percent_label.text = ""
            self.progress_bar.stop()
            self.progress_bar.max = 100
            self.progress_bar.value = 0

        self._rebuild_visible_sections()

    def show_status(self, text: str, level: str = "info"):
        self._status_level = level
        self.status_label.text = text
        palette = THEMES[self.theme_name]["status"]
        self.status_label.style.color = palette.get(level, palette["info"])
        self._rebuild_visible_sections()

    # ----- URL & Fetching -------------------------------------------------

    def on_url_edited(self, widget):
        if self.state in ("ready", "done"):
            self.info = None
            self.summary = None
            self.video_options = []
            self.show_status("")
            self._set_state("idle")

    def on_clear_pressed(self, widget):
        self.url_input.value = ""
        self.info = None
        self.summary = None
        self.video_options = []
        self.show_status("")
        self._set_state("idle")

    def on_fetch_pressed(self, widget):
        if self.state in ("fetching", "downloading"):
            return

        url = (self.url_input.value or "").strip()
        if not url:
            self.show_status("Paste a YouTube link first.", "error")
            return

        self.url = url
        self.show_status("")
        self.stage_label.text = "Fetching video details…"
        self.progress_label.text = ""
        self._set_state("fetching")

        threading.Thread(
            target=self._fetch_worker, args=(url,), daemon=True
        ).start()

    def _fetch_worker(self, url: str):
        try:
            info = fetch_video_info(url)
            summary = summarize_video(info)
            options = quality_options(info)
            thumb_url = _pick_jpeg_thumbnail_url(info, summary.thumbnail)
            thumb_bytes = _fetch_thumbnail_bytes(thumb_url)
        except PlaylistLinkError:
            self.call_on_ui(
                self.on_fetch_failed,
                "Playlist support is coming next—paste a single video link for now.",
            )
            return
        except DownloadError as error:
            self.call_on_ui(self.on_fetch_failed, str(error))
            return
        except Exception as error:
            logging.getLogger("ytdl").error("Fetch failed", exc_info=True)
            self.call_on_ui(self.on_fetch_failed, friendly_error(error))
            return

        self.call_on_ui(self.on_fetched, info, summary, options, thumb_bytes)

    def on_fetched(self, info, summary, options, thumb_bytes):
        self.info = info
        self.summary = summary
        self.video_options = options

        self.meta_label.text = (
            f"{summary.channel}  ·  "
            f"{format_duration(summary.duration)}  ·  "
            f"{summary.upload_date}"
        )

        if thumb_bytes:
            try:
                self.thumb_view.image = toga.Image(thumb_bytes)
            except Exception:
                self.thumb_view.image = None
        else:
            self.thumb_view.image = None

        self._populate_quality()
        self._refresh_responsive_text()
        self.show_status("")
        self._set_state("ready")

    def on_fetch_failed(self, message: str):
        self.show_status(message, "error")
        self._set_state("idle")

    # ----- Format & Quality -----------------------------------------------

    def on_select_video_mode(self, widget):
        if self.mode == "video":
            return
        self.mode = "video"
        self._style_mode_buttons()
        if self.info is not None:
            self._populate_quality()

    def on_select_audio_mode(self, widget):
        if self.mode == "audio":
            return
        self.mode = "audio"
        self._style_mode_buttons()
        if self.info is not None:
            self._populate_quality()

    def _populate_quality(self):
        settings = load_settings()
        is_video = self.mode == "video"

        labels: list[str] = []
        values: list = []

        if is_video:
            self.quality_caption.text = "VIDEO QUALITY"
            if self.has_ffmpeg:
                for height, size in self.video_options:
                    text = f"{height}p"
                    if size:
                        text += f"  ·  ~{format_file_size(size)}"
                    labels.append(text)
                    values.append(height)
            else:
                for height in (720, 480, 360):
                    labels.append(f"Up to {height}p (Direct MP4)")
                    values.append(height)
            saved = settings.get("video_quality")
        else:
            self.quality_caption.text = "AUDIO QUALITY"
            choices = AUDIO_CHOICES if self.has_ffmpeg else [ORIGINAL_AUDIO]
            for choice in choices:
                labels.append(audio_label(choice))
                values.append(choice)
            saved = settings.get("audio_bitrate")

        self.quality_values = values
        self.quality_selection.items = labels

        if saved in values:
            self.quality_selection.value = labels[values.index(saved)]
        elif labels:
            self.quality_selection.value = labels[0]

        self.download_button.enabled = bool(values)

    # ----- Downloading ----------------------------------------------------

    def on_download_pressed(self, widget):
        if self.state not in ("ready", "done") or not self.quality_values:
            return

        selected_label = self.quality_selection.value
        items = [item.value for item in self.quality_selection.items]
        idx = items.index(selected_label) if selected_label in items else 0
        setting = self.quality_values[idx]
        kind = self.mode

        self.cancel_event = threading.Event()
        self.show_status("")
        self.stage_label.text = "Starting download…"
        self.percent_label.text = ""
        self.progress_label.text = ""
        self.progress_bar.max = None
        self.progress_bar.start()
        self._set_state("downloading")

        threading.Thread(
            target=self._download_worker,
            args=(self.url, kind, setting, self.info, self.cancel_event),
            daemon=True,
        ).start()

    def _download_worker(self, url, kind, setting, info, cancel):
        reporter = TogaReporter(self)
        try:
            if self.has_ffmpeg:
                fn = download_video if kind == "video" else download_audio
                result = fn(
                    url,
                    setting,
                    output_dir=self.download_dir,
                    info=info,
                    reporter=reporter,
                    cancel=cancel,
                )
            else:
                result = _direct_download(
                    url, kind, setting, self.download_dir, reporter, cancel
                )
        except DownloadError as error:
            self.call_on_ui(self.on_download_failed, str(error))
            return
        except Exception as error:
            logging.getLogger("ytdl").error("Download failed", exc_info=True)
            self.call_on_ui(self.on_download_failed, friendly_error(error))
            return

        self.call_on_ui(self.on_download_done, result, kind, setting)

    def on_cancel_pressed(self, widget):
        if self.cancel_event is not None:
            self.cancel_event.set()
        self.cancel_button.enabled = False
        self.cancel_button.text = "Cancelling…"

    def on_stage(self, text: str):
        self.stage_label.text = text
        self.percent_label.text = ""
        self.progress_bar.max = None
        self.progress_bar.start()
        self.progress_label.text = ""
        self.cancel_button.enabled = False

    def on_progress(self, downloaded, total, speed, eta):
        self.stage_label.text = "Downloading…"
        if total:
            pct = min(100, int(downloaded * 100 / total))
            self.percent_label.text = f"{pct}%"
            self.progress_bar.stop()
            self.progress_bar.max = 100
            self.progress_bar.value = pct
            parts = [
                f"{format_file_size(downloaded)} / {format_file_size(total)}"
            ]
        else:
            self.percent_label.text = ""
            self.progress_bar.max = None
            parts = [format_file_size(downloaded)]

        if speed:
            parts.append(f"{format_file_size(speed)}/s")
        if eta is not None:
            parts.append(f"ETA {format_duration(eta)}")

        self.progress_label.text = "  ·  ".join(parts)

    def on_progress_done(self):
        self.percent_label.text = ""
        self.progress_bar.stop()
        self.progress_bar.max = 100
        self.progress_bar.value = 0
        self.progress_label.text = ""

    def on_download_done(self, result: DownloadResult, kind: str, setting):
        if result.status == "completed":
            key = "video_quality" if kind == "video" else "audio_bitrate"
            remember_setting(key, setting)
            self.show_status(
                f"✔ Download complete · {_format_responsive_path(result.folder, self._current_width)}",
                "success",
            )
        elif result.status == "exists":
            self.show_status(
                f"Already downloaded: {result.filename}", "warning"
            )
        else:
            self.show_status("Download cancelled.", "warning")

        self._set_state("done")

    def on_download_failed(self, message: str):
        self.show_status(message, "error")
        self._set_state("ready")


def main():
    return YTDownloaderApp()