"""DirectSave - mobile & cross-platform UI (BeeWare Toga)."""

from __future__ import annotations

import copy
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.parse
import urllib.request

import toga
from toga.style import Pack
from toga.style.pack import BOLD, CENTER, COLUMN, ROW
import yt_dlp

import config
from config import (
    APP_NAME,
    APP_VERSION,
    AUDIO_CHOICES,
    ORIGINAL_AUDIO,
    PLAYLIST_QUALITIES,
    audio_label,
    load_settings,
    remember_setting,
)
from downloader import (
    DownloadError,
    DownloadResult,
    PlaylistInfo,
    PlaylistLinkError,
    Reporter,
    VideoSummary,
    download_audio,
    download_video,
    fetch_playlist,
    fetch_video_info,
    format_duration,
    format_file_size,
    friendly_error,
    has_playlist_and_video,
    quality_options,
    safe_folder_name,
    sanitize_name,
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
MAX_HISTORY_ITEMS = 150

THEME_ORDER = ("midnight", "lavender", "ocean", "obsidian")

_LEGACY_THEME_MAP = {
    "dark": "midnight",
    "light": "lavender",
}

THEMES = {
    "midnight": {
        "label": "Midnight",
        "badge_text": "✦ Midnight",
        "dark": True,
        "bg": "#0E1019",
        "surface": "#171A27",
        "surface_alt": "#1F2335",
        "border": "#262A3E",
        "text": "#F3F4FA",
        "muted": "#8E93B2",
        "accent": "#7464FF",
        "accent_text": "#FFFFFF",
        "progress": "#10B981",
        "bar_bg": "#0E1019",
        "bar_text": "#F3F4FA",
        "error": "#F87171",
        "warning": "#FBBF24",
        "success": "#34D399",
    },
    "lavender": {
        "label": "Lavender",
        "badge_text": "☀ Lavender",
        "dark": False,
        "bg": "#F3F1FA",
        "surface": "#FFFFFF",
        "surface_alt": "#EAE6F8",
        "border": "#E2DDF5",
        "text": "#171625",
        "muted": "#6E6B8A",
        "accent": "#6D4AFF",
        "accent_text": "#FFFFFF",
        "progress": "#059669",
        "bar_bg": "#F3F1FA",
        "bar_text": "#171625",
        "error": "#DC2626",
        "warning": "#D97706",
        "success": "#059669",
    },
    "ocean": {
        "label": "Ocean",
        "badge_text": "🌊 Ocean",
        "dark": True,
        "bg": "#0B192C",
        "surface": "#132640",
        "surface_alt": "#1A3252",
        "border": "#223C5E",
        "text": "#F0F6FC",
        "muted": "#8BA6C7",
        "accent": "#3898FF",
        "accent_text": "#FFFFFF",
        "progress": "#10B981",
        "bar_bg": "#0B192C",
        "bar_text": "#F0F6FC",
        "error": "#F87171",
        "warning": "#FBBF24",
        "success": "#34D399",
    },
    "obsidian": {
        "label": "Obsidian",
        "badge_text": "◆ Obsidian",
        "dark": True,
        "bg": "#0B0B0E",
        "surface": "#15161C",
        "surface_alt": "#1E2029",
        "border": "#272935",
        "text": "#F4F4F6",
        "muted": "#989AA6",
        "accent": "#E11D48",
        "accent_text": "#FFFFFF",
        "progress": "#E11D48",
        "bar_bg": "#0B0B0E",
        "bar_text": "#F4F4F6",
        "error": "#F87171",
        "warning": "#FBBF24",
        "success": "#4ADE80",
    },
}

ANDROID_CLIENT_PROFILES: tuple[list[str] | None, ...] = (
    None,
    ["web", "android_vr", "mweb"],
    ["android_vr", "web"],
    ["android", "web"],
)


def _normalize_theme(name: str | None) -> str:
    if not name:
        return "midnight"
    mapped = _LEGACY_THEME_MAP.get(name, name)
    return mapped if mapped in THEMES else "midnight"


def _time_greeting() -> tuple[str, str]:
    """Return a time-of-day greeting and the DirectSave tagline."""
    hour = datetime.now().hour
    if 5 <= hour < 12:
        greeting = "Good morning"
    elif 12 <= hour < 17:
        greeting = "Good afternoon"
    elif 17 <= hour < 22:
        greeting = "Good evening"
    else:
        greeting = "Good night"
    return greeting, "Save what you love"


def _resolve_ui_font() -> str:
    """Pick a clean sans-serif font for the current platform."""
    if sys.platform == "win32":
        for candidate in ("Inter", "Segoe UI"):
            try:
                toga.Font(candidate, 12)
                return candidate
            except Exception:
                continue
    return "sans-serif"


def _screen_dp_width(fallback: int = 380) -> int:
    """Return usable screen width in dp on Android, or fallback width."""
    if is_android():
        try:
            from android.content import res

            metrics = res.Resources.getSystem().getDisplayMetrics()
            if metrics and metrics.density > 0:
                return max(280, int(metrics.widthPixels / metrics.density))
        except Exception:
            pass
    return fallback


def _wrap_for_screen(
    text: str,
    available_dp: int,
    char_dp: float = 7.4,
    padding_dp: int = 64,
    max_lines: int | None = None,
) -> str:
    """Insert explicit newlines so Toga labels wrap cleanly on Android."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    cols = max(24, int((available_dp - padding_dp) / char_dp))
    lines: list[str] = []
    for paragraph in raw.splitlines():
        wrapped = textwrap.wrap(
            paragraph,
            width=cols,
            break_long_words=True,
            break_on_hyphens=True,
        )
        lines.extend(wrapped or [""])
    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        if len(lines[-1]) > 3:
            lines[-1] = lines[-1][:-1].rstrip() + "…"
    return "\n".join(lines)


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


def _tree_uri_to_path(uri_str: str) -> Path | None:
    """Convert an Android SAF tree URI into a filesystem Path when possible."""
    try:
        parsed = urllib.parse.urlparse(uri_str)
        decoded = urllib.parse.unquote(parsed.path)
        if "/tree/" not in decoded:
            return None
        tree_part = decoded.split("/tree/", 1)[1]
        if ":" not in tree_part:
            return None
        volume, rel = tree_part.split(":", 1)
        rel_clean = rel.strip("/")
        if volume == "primary":
            base = Path("/storage/emulated/0")
            return base / rel_clean if rel_clean else base
        if volume and volume != "raw":
            base = Path("/storage") / volume
            return base / rel_clean if rel_clean else base
        if volume == "raw" and rel:
            return Path(rel)
    except Exception:
        pass
    return None


def _get_clipboard_text() -> str:
    """Read text from the system clipboard on Android or desktop."""
    if is_android():
        try:
            from android.content import Context
            from org.beeware.android import MainActivity

            activity = MainActivity.singletonThis
            cm = activity.getSystemService(Context.CLIPBOARD_SERVICE)
            if cm is not None and cm.hasPrimaryClip():
                clip = cm.getPrimaryClip()
                if clip is not None and clip.getItemCount() > 0:
                    item = clip.getItemAt(0)
                    text = item.coerceToText(activity)
                    return str(text).strip() if text is not None else ""
        except Exception:
            return ""

    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            return str(root.clipboard_get()).strip()
        finally:
            root.destroy()
    except Exception:
        return ""


def _open_media_externally(file_path: Path) -> bool:
    """Launch the platform's native media player for a downloaded file."""
    if not file_path.is_file():
        return False

    if is_android():
        try:
            from android.content import Intent
            from android.net import Uri
            from android.os import StrictMode
            from java.io import File as JavaFile
            from org.beeware.android import MainActivity

            # Allow file:// URI sharing across apps for user-owned media playback.
            builder = StrictMode.VmPolicy.Builder()
            StrictMode.setVmPolicy(builder.build())

            ext = file_path.suffix.lower()
            mime = "audio/*" if ext in (".m4a", ".mp3", ".opus", ".ogg", ".wav") else "video/*"

            intent = Intent(Intent.ACTION_VIEW)
            uri = Uri.fromFile(JavaFile(str(file_path)))
            intent.setDataAndType(uri, mime)
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)

            chooser = Intent.createChooser(intent, "Play with")
            chooser.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            MainActivity.singletonThis.startActivity(chooser)
            return True
        except Exception:
            logging.getLogger("ytdl").warning(
                "Android media intent failed", exc_info=True
            )
            return False

    try:
        if sys.platform == "win32":
            os.startfile(str(file_path))
            return True
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, str(file_path)])
        return True
    except Exception:
        return False


def _merge_mp4_and_m4a(video_path: Path, audio_path: Path, output_path: Path) -> None:
    """Merge separate MP4 video and M4A audio files natively using Android MediaMuxer."""
    from android.media import MediaCodec, MediaExtractor, MediaFormat, MediaMuxer
    from java.nio import ByteBuffer

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    muxer = MediaMuxer(
        str(output_path), MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4
    )
    video_extractor = MediaExtractor()
    audio_extractor = MediaExtractor()

    try:
        video_extractor.setDataSource(str(video_path))
        audio_extractor.setDataSource(str(audio_path))

        v_src_track = -1
        v_dst_track = -1
        max_buf = 1024 * 1024

        for i in range(video_extractor.getTrackCount()):
            fmt = video_extractor.getTrackFormat(i)
            mime = fmt.getString(MediaFormat.KEY_MIME) or ""
            if mime.startswith("video/"):
                video_extractor.selectTrack(i)
                v_src_track = i
                v_dst_track = muxer.addTrack(fmt)
                if fmt.containsKey(MediaFormat.KEY_MAX_INPUT_SIZE):
                    max_buf = max(max_buf, fmt.getInteger(MediaFormat.KEY_MAX_INPUT_SIZE))
                break

        a_src_track = -1
        a_dst_track = -1
        for i in range(audio_extractor.getTrackCount()):
            fmt = audio_extractor.getTrackFormat(i)
            mime = fmt.getString(MediaFormat.KEY_MIME) or ""
            if mime.startswith("audio/"):
                audio_extractor.selectTrack(i)
                a_src_track = i
                a_dst_track = muxer.addTrack(fmt)
                if fmt.containsKey(MediaFormat.KEY_MAX_INPUT_SIZE):
                    max_buf = max(max_buf, fmt.getInteger(MediaFormat.KEY_MAX_INPUT_SIZE))
                break

        if v_src_track < 0 or a_src_track < 0:
            raise DownloadError("Could not find valid video/audio tracks to merge.")

        muxer.start()
        buffer = ByteBuffer.allocate(max_buf)
        info = MediaCodec.BufferInfo()

        for extractor, dst_track in (
            (video_extractor, v_dst_track),
            (audio_extractor, a_dst_track),
        ):
            while True:
                buffer.clear()
                sample_size = extractor.readSampleData(buffer, 0)
                if sample_size < 0:
                    break
                info.offset = 0
                info.size = sample_size
                info.presentationTimeUs = extractor.getSampleTime()
                info.flags = extractor.getSampleFlags()
                muxer.writeSampleData(dst_track, buffer, info)
                extractor.advance()

        muxer.stop()
    finally:
        try:
            video_extractor.release()
        except Exception:
            pass
        try:
            audio_extractor.release()
        except Exception:
            pass
        try:
            muxer.release()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Native styling helper
# --------------------------------------------------------------------------


class _NativeStyler:
    """Applies rounded corners, borders, and native Android text wrapping."""

    def apply(
        self,
        widget,
        bg_hex: str,
        border_hex: str,
        radius: int = 12,
        is_button: bool = False,
    ):
        if is_android():
            self._apply_android(widget, bg_hex, border_hex, radius, is_button)
        elif sys.platform == "win32":
            self._apply_winforms(widget, bg_hex, border_hex, radius, is_button)

    @staticmethod
    def _apply_android(
        widget,
        bg_hex: str,
        border_hex: str,
        radius_dp: int,
        is_button: bool = False,
    ):
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
            if is_button:
                native.setAllCaps(False)
                pad_h = int(10 * density)
                native.setPadding(pad_h, 0, pad_h, 0)
        except Exception:
            pass

    @staticmethod
    def _apply_winforms(
        widget,
        bg_hex: str,
        border_hex: str,
        _radius: int,
        is_button: bool = False,
    ):
        try:
            from System.Drawing import ColorTranslator
            from System.Windows.Forms import FlatStyle

            native = widget._impl.native
            native.BackColor = ColorTranslator.FromHtml(bg_hex)
            if is_button:
                native.FlatStyle = FlatStyle.Flat
                native.FlatAppearance.BorderColor = ColorTranslator.FromHtml(border_hex)
                native.FlatAppearance.BorderSize = 1
        except Exception:
            pass

    @staticmethod
    def enable_multiline_android(label, max_lines: int = 8):
        if not is_android():
            return
        try:
            native = label._impl.native
            native.setSingleLine(False)
            native.setHorizontallyScrolling(False)
            native.setMaxLines(max_lines)
        except Exception:
            pass


# --------------------------------------------------------------------------
# Reporter & background helpers
# --------------------------------------------------------------------------


class MobileReporter(Reporter):
    """Forwards download progress and stage updates safely onto Toga's UI loop."""

    def __init__(self, app: DirectSaveMobileApp):
        self.app = app
        self.last_tick = 0.0

    def message(self, text, level="info"):
        self.app.call_on_ui(self.app.show_status, text, level)

    def stage(self, text):
        self.app.call_on_ui(self.app.on_stage, text)

    def playlist_item(self, number, total, title):
        self.app.call_on_ui(self.app.on_playlist_item, number, total, title)

    def progress(self, downloaded, total, speed, eta):
        now = time.monotonic()
        if now - self.last_tick < 0.12:
            return
        self.last_tick = now
        self.app.call_on_ui(self.app.on_progress, downloaded, total, speed, eta)

    def progress_done(self):
        self.last_tick = 0.0
        self.app.call_on_ui(self.app.on_progress_done)


class _DownloadCancelled(Exception):
    """Raised inside yt-dlp progress hook when user cancels."""


def load_thumbnail_bytes(url: str | None) -> bytes | None:
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
            return data if len(data) <= THUMBNAIL_MAX_BYTES else None
    except Exception:
        return None


def _android_extractor_args(clients: list[str]) -> dict:
    return {"youtube": {"player_client": clients}}


def _android_fetch_info(url: str) -> dict:
    """Extract full video format list on Android, rotating client profiles on failure."""
    last_error: Exception | None = None
    best_info: dict | None = None

    for clients in ANDROID_CLIENT_PROFILES:
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
        }
        if clients is not None:
            opts["extractor_args"] = _android_extractor_args(clients)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info:
                best_info = info
                if len(quality_options(info)) > 1:
                    return info
        except Exception as error:
            last_error = error
            msg = str(error).lower()
            if (
                "not a bot" in msg
                or "sign in" in msg
                or "http error 403" in msg
                or "js runtime" in msg
                or "challenge" in msg
                or "errno 7" in msg
            ):
                continue
            raise DownloadError(friendly_error(error)) from error

    if best_info is not None:
        return best_info

    raise DownloadError(
        friendly_error(last_error)
        if last_error
        else "Could not retrieve video information."
    )


def _android_download_media(
    url: str,
    kind: str,
    setting,
    output_dir: Path,
    info: dict | None,
    reporter: Reporter,
    cancel: threading.Event,
) -> DownloadResult:
    """Download exact video quality (via native MediaMuxer) or M4A audio on Android."""
    output_dir.mkdir(parents=True, exist_ok=True)

    title = (info or {}).get("title") or "video"
    video_id = (info or {}).get("id") or "media"
    safe_base = sanitize_name(title, fallback=video_id)
    ext = "m4a" if kind == "audio" else "mp4"
    tag = "[M4A]" if kind == "audio" else f"[{setting}p]"
    final_name = f"{safe_base} {tag} [{video_id}].{ext}"
    final_path = output_dir / final_name

    if final_path.is_file() and final_path.stat().st_size > 0:
        return DownloadResult(status="exists", folder=output_dir, filename=final_name)

    def hook(d):
        if cancel.is_set():
            raise _DownloadCancelled()
        status = d.get("status")
        if status == "downloading":
            reporter.progress(
                d.get("downloaded_bytes") or 0,
                d.get("total_bytes") or d.get("total_bytes_estimate"),
                d.get("speed"),
                d.get("eta"),
            )
        elif status == "finished":
            reporter.progress_done()

    last_error: Exception | None = None

    for clients in ANDROID_CLIENT_PROFILES:
        if cancel.is_set():
            return DownloadResult(status="cancelled")

        base_opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "progress_hooks": [hook],
            "retries": 3,
            "fragment_retries": 3,
        }
        if clients is not None:
            base_opts["extractor_args"] = _android_extractor_args(clients)

        try:
            if kind == "audio":
                reporter.stage("Downloading audio track…")
                opts = dict(base_opts)
                opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
                opts["outtmpl"] = str(final_path)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    if info is not None and clients is None:
                        ydl.process_ie_result(copy.deepcopy(info), download=True)
                    else:
                        ydl.download([url])
                if final_path.is_file():
                    return DownloadResult(
                        status="completed", folder=output_dir, filename=final_name
                    )
                continue

            # Video mode: download exact MP4 video stream + M4A audio stream, then mux natively.
            with tempfile.TemporaryDirectory(prefix="directsave_") as tmp_dir:
                tmp_path = Path(tmp_dir)
                v_tmp = tmp_path / "video.mp4"
                a_tmp = tmp_path / "audio.m4a"

                reporter.stage(f"Downloading {setting}p video…")
                v_opts = dict(base_opts)
                v_opts["format"] = (
                    f"bestvideo[ext=mp4][height<={setting}]/"
                    f"bestvideo[height<={setting}]/"
                    f"best[ext=mp4][height<={setting}]/best[ext=mp4]"
                )
                v_opts["format_sort"] = [f"res:{setting}", "proto", "vcodec:h264"]
                v_opts["outtmpl"] = str(v_tmp)

                with yt_dlp.YoutubeDL(v_opts) as ydl:
                    ydl.download([url])

                if not v_tmp.is_file():
                    continue

                reporter.stage("Downloading audio track…")
                a_opts = dict(base_opts)
                a_opts["format"] = "bestaudio[ext=m4a]/bestaudio"
                a_opts["outtmpl"] = str(a_tmp)

                try:
                    with yt_dlp.YoutubeDL(a_opts) as ydl:
                        ydl.download([url])
                except Exception:
                    pass

                if a_tmp.is_file() and is_android():
                    reporter.stage("Merging video and audio…")
                    _merge_mp4_and_m4a(v_tmp, a_tmp, final_path)
                else:
                    shutil.move(str(v_tmp), str(final_path))

                if final_path.is_file():
                    return DownloadResult(
                        status="completed", folder=output_dir, filename=final_name
                    )

        except _DownloadCancelled:
            return DownloadResult(status="cancelled")
        except Exception as error:
            last_error = error
            msg = str(error).lower()
            if (
                "http error 403" in msg
                or "not a bot" in msg
                or "sign in" in msg
                or "requested format is not available" in msg
            ):
                continue
            raise DownloadError(friendly_error(error)) from error

    raise DownloadError(
        friendly_error(last_error)
        if last_error
        else "Could not download this media. Try another quality."
    )


# --------------------------------------------------------------------------
# Main Toga Application
# --------------------------------------------------------------------------


class DirectSaveMobileApp(toga.App):
    def startup(self):
        setup_logging()
        self.download_dir = configure_mobile_paths(self)
        self.history_file = Path(config.CONFIG_DIR) / "history.json"
        self.history_items: list[dict] = self._load_history()

        saved_theme = load_settings().get("theme")
        self.theme_name = _normalize_theme(saved_theme)
        self.styler = _NativeStyler()

        self.active_tab = "downloader"  # "downloader" or "history"
        self.state = "idle"  # idle, fetching, ready, downloading, done
        self.mode = "video"  # "video" or "playlist"
        self.media_kind = "video"  # "video" or "audio"
        self.url = ""
        self.info: dict | None = None
        self.summary: VideoSummary | None = None
        self.video_options: list[tuple[int, int | None]] = []
        self.playlist: PlaylistInfo | None = None
        self.playlist_checks: list[toga.Switch] = []
        self.cancel_event: threading.Event | None = None
        self.has_ffmpeg = bool(shutil.which("ffmpeg"))

        self._cards: list[toga.Box] = []
        self._alt_boxes: list[toga.Box] = []
        self._inputs: list[toga.Widget] = []
        self._primary_buttons: list[toga.Button] = []
        self._secondary_buttons: list[toga.Button] = []
        self._primary_labels: list[toga.Label] = []
        self._muted_labels: list[toga.Label] = []

        self._current_width = _screen_dp_width(430)
        self._status_raw = ""
        self._status_level = "info"

        self._build_ui()

        self.main_window = toga.MainWindow(
            title=f"{APP_NAME} v{APP_VERSION}",
            size=(430, 720),
            on_resize=self.on_window_resize,
        )
        self.main_window.content = self.root_scroll
        self._apply_theme(save=False)
        self._set_state("idle")
        self._refresh_responsive_text()
        self._update_history_tab_text()
        self.main_window.show()

    def call_on_ui(self, func, *args):
        self.loop.call_soon_threadsafe(func, *args)

    # ----- responsive text ------------------------------------------------

    def on_window_resize(self, window, **kwargs):
        width = getattr(window.size, "width", 0)
        if width and width != self._current_width:
            self._current_width = width
            self._refresh_responsive_text()

    def _refresh_responsive_text(self):
        dp_w = _screen_dp_width(self._current_width)
        self.folder_label.text = _format_responsive_path(self.download_dir, dp_w)
        if self.summary is not None:
            self.title_label.text = _wrap_for_screen(
                self.summary.title, dp_w, char_dp=7.6, padding_dp=68, max_lines=3
            )
        elif self.playlist is not None:
            self.title_label.text = _wrap_for_screen(
                self.playlist.title, dp_w, char_dp=7.6, padding_dp=68, max_lines=3
            )
        if self._status_raw:
            self.status_label.text = _wrap_for_screen(
                self._status_raw, dp_w, char_dp=7.2, padding_dp=68, max_lines=8
            )

    # ----- history persistence --------------------------------------------

    def _load_history(self) -> list[dict]:
        try:
            if self.history_file.is_file():
                data = json.loads(self.history_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data[:MAX_HISTORY_ITEMS]
        except (OSError, ValueError):
            pass
        return []

    def _save_history(self):
        try:
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.history_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self.history_items[:MAX_HISTORY_ITEMS], indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.history_file)
        except OSError:
            pass

    def _record_history(self, title: str, kind: str, quality: str, file_path: Path):
        path_str = str(file_path)
        entry = {
            "id": str(int(time.time() * 1000)),
            "title": title,
            "kind": kind,
            "quality": quality,
            "path": path_str,
            "timestamp": datetime.now().strftime("%b %d, %I:%M %p"),
        }
        self.history_items = [
            item for item in self.history_items if item.get("path") != path_str
        ]
        self.history_items.insert(0, entry)
        self.history_items = self.history_items[:MAX_HISTORY_ITEMS]
        self._save_history()
        self._update_history_tab_text()
        if self.active_tab == "history":
            self._render_history_cards()

    def _update_history_tab_text(self):
        count = len(self.history_items)
        self.tab_history_btn.text = f"History ({count})" if count else "History"

    def _switch_tab(self, tab_name: str):
        if self.active_tab == tab_name:
            return
        self.active_tab = tab_name
        is_dl = tab_name == "downloader"

        if is_dl:
            if self.history_container in self.content_col.children:
                self.content_col.remove(self.history_container)
            if self.downloader_container not in self.content_col.children:
                self.content_col.add(self.downloader_container)
        else:
            if self.downloader_container in self.content_col.children:
                self.content_col.remove(self.downloader_container)
            if self.history_container not in self.content_col.children:
                self.content_col.add(self.history_container)
            self._render_history_cards()

        self._apply_theme(save=False)

    def _render_history_cards(self):
        for child in list(self.history_list_box.children):
            self.history_list_box.remove(child)

        t = THEMES[self.theme_name]
        f = _resolve_ui_font()
        dp_w = _screen_dp_width(self._current_width)

        if not self.history_items:
            empty_card = toga.Box(
                style=Pack(
                    direction=COLUMN,
                    padding=20,
                    background_color=t["surface"],
                )
            )
            empty_lbl = toga.Label(
                "No downloads yet. Completed videos and audio will appear here.",
                style=Pack(font_family=f, font_size=11, color=t["muted"]),
            )
            self.styler.enable_multiline_android(empty_lbl, max_lines=4)
            empty_card.add(empty_lbl)
            self.history_list_box.add(empty_card)
            self.styler.apply(empty_card, t["surface"], t["border"], radius=14)
            return

        for item in self.history_items:
            kind = item.get("kind", "video")
            quality = item.get("quality", "")
            ts = item.get("timestamp", "")
            path_str = item.get("path", "")
            title = item.get("title", "Untitled")

            card = toga.Box(
                style=Pack(
                    direction=COLUMN,
                    padding=14,
                    gap=8,
                    background_color=t["surface"],
                )
            )

            prefix = "AUDIO" if kind == "audio" else "VIDEO"
            meta_lbl = toga.Label(
                f"{prefix} · {quality} · {ts}",
                style=Pack(font_family=f, font_size=10, color=t["muted"]),
            )
            title_lbl = toga.Label(
                _wrap_for_screen(title, dp_w, char_dp=7.6, padding_dp=72, max_lines=3),
                style=Pack(
                    font_family=f,
                    font_size=12,
                    font_weight=BOLD,
                    color=t["text"],
                ),
            )
            self.styler.enable_multiline_android(title_lbl, max_lines=3)

            actions_row = toga.Box(style=Pack(direction=ROW, gap=8, align_items=CENTER))
            play_label = "▶ Play Audio" if kind == "audio" else "▶ Play Video"
            play_btn = toga.Button(
                play_label,
                on_press=lambda _w, p=path_str: self._play_history_item(p),
                style=Pack(
                    font_family=f,
                    flex=1,
                    height=38,
                    font_size=11,
                    font_weight=BOLD,
                    background_color=t["accent"],
                    color=t["accent_text"],
                ),
            )
            remove_btn = toga.Button(
                "Remove",
                on_press=lambda _w, p=path_str: self._remove_history_item(p),
                style=Pack(
                    font_family=f,
                    width=88,
                    height=38,
                    font_size=10,
                    background_color=t["surface_alt"],
                    color=t["text"],
                ),
            )
            actions_row.add(play_btn)
            actions_row.add(remove_btn)

            card.add(meta_lbl)
            card.add(title_lbl)
            card.add(actions_row)
            self.history_list_box.add(card)

            self.styler.apply(card, t["surface"], t["border"], radius=14)
            self.styler.apply(play_btn, t["accent"], t["accent"], radius=10, is_button=True)
            self.styler.apply(
                remove_btn, t["surface_alt"], t["border"], radius=10, is_button=True
            )

    def _play_history_item(self, path_str: str):
        target = Path(path_str)
        if not target.is_file():
            self.main_window.info_dialog(
                "File Not Found",
                "This file could not be found on your device. It may have been moved or deleted.",
            )
            return
        if not _open_media_externally(target):
            self.main_window.info_dialog(
                "Playback Error",
                "Could not launch a media player for this file.",
            )

    def _remove_history_item(self, path_str: str):
        self.history_items = [
            item for item in self.history_items if item.get("path") != path_str
        ]
        self._save_history()
        self._update_history_tab_text()
        self._render_history_cards()

    def _clear_history(self, _widget=None):
        if not self.history_items:
            return
        self.history_items = []
        self._save_history()
        self._update_history_tab_text()
        self._render_history_cards()

    # ----- theme & system bars --------------------------------------------

    def on_cycle_theme(self, _widget):
        idx = THEME_ORDER.index(self.theme_name)
        self.theme_name = THEME_ORDER[(idx + 1) % len(THEME_ORDER)]
        self._apply_theme(save=True)

    def _sync_android_system_bars(self, t: dict):
        if not is_android():
            return
        try:
            from android.graphics import Color
            from android.graphics.drawable import ColorDrawable
            from android.text import SpannableString, ForegroundColorSpan
            from android.view import View, WindowManager
            from org.beeware.android import MainActivity

            activity = MainActivity.singletonThis
            window = activity.getWindow()
            window.addFlags(WindowManager.LayoutParams.FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS)
            window.setStatusBarColor(Color.parseColor(t["bar_bg"]))
            window.setNavigationBarColor(Color.parseColor(t["bg"]))

            decor = window.getDecorView()
            flags = decor.getSystemUiVisibility()
            light_flag = View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR
            if t["dark"]:
                flags &= ~light_flag
            else:
                flags |= light_flag
            decor.setSystemUiVisibility(flags)

            action_bar = activity.getActionBar()
            if action_bar is not None:
                action_bar.setBackgroundDrawable(
                    ColorDrawable(Color.parseColor(t["bar_bg"]))
                )
                title = SpannableString(APP_NAME)
                title.setSpan(
                    ForegroundColorSpan(Color.parseColor(t["bar_text"])),
                    0,
                    len(APP_NAME),
                    0,
                )
                action_bar.setTitle(title)
        except Exception:
            pass

    def _apply_theme(self, save: bool = True):
        t = THEMES[self.theme_name]
        if save:
            remember_setting("theme", self.theme_name)

        self.root_scroll.style.background_color = t["bg"]
        self.content_col.style.background_color = t["bg"]
        self.downloader_container.style.background_color = t["bg"]
        self.history_container.style.background_color = t["bg"]
        self.history_list_box.style.background_color = t["bg"]

        self.theme_button.text = t["badge_text"]

        for card in self._cards:
            card.style.background_color = t["surface"]
            self.styler.apply(card, t["surface"], t["border"], radius=14)

        for box in self._alt_boxes:
            box.style.background_color = t["surface_alt"]
            self.styler.apply(box, t["surface_alt"], t["border"], radius=12)

        for inp in self._inputs:
            inp.style.background_color = t["surface_alt"]
            inp.style.color = t["text"]
            self.styler.apply(inp, t["surface_alt"], t["border"], radius=10)

        for lbl in self._primary_labels:
            lbl.style.color = t["text"]

        for lbl in self._muted_labels:
            lbl.style.color = t["muted"]

        for btn in self._primary_buttons:
            btn.style.background_color = t["accent"]
            btn.style.color = t["accent_text"]
            self.styler.apply(
                btn, t["accent"], t["accent"], radius=12, is_button=True
            )

        for btn in self._secondary_buttons:
            btn.style.background_color = t["surface_alt"]
            btn.style.color = t["text"]
            self.styler.apply(
                btn, t["surface_alt"], t["border"], radius=10, is_button=True
            )

        self._style_segment_buttons()
        self._update_status_color()

        if self.active_tab == "history":
            self._render_history_cards()

        self._sync_android_system_bars(t)

    def _style_segment_buttons(self):
        t = THEMES[self.theme_name]

        # Top navigation tabs (Downloader vs History)
        for btn, active in (
            (self.tab_downloader_btn, self.active_tab == "downloader"),
            (self.tab_history_btn, self.active_tab == "history"),
        ):
            bg = t["accent"] if active else t["surface_alt"]
            fg = t["accent_text"] if active else t["muted"]
            border = t["accent"] if active else t["border"]
            btn.style.background_color = bg
            btn.style.color = fg
            self.styler.apply(btn, bg, border, radius=10, is_button=True)

        # Format switcher (Video vs Audio)
        for btn, active in (
            (self.video_btn, self.media_kind == "video"),
            (self.audio_btn, self.media_kind == "audio"),
        ):
            bg = t["accent"] if active else t["surface_alt"]
            fg = t["accent_text"] if active else t["muted"]
            border = t["accent"] if active else t["border"]
            btn.style.background_color = bg
            btn.style.color = fg
            self.styler.apply(btn, bg, border, radius=10, is_button=True)

    # ----- UI construction ------------------------------------------------

    def _build_ui(self):
        f = _resolve_ui_font()
        dp_w = _screen_dp_width(430)

        self.root_scroll = toga.ScrollContainer(horizontal=False)
        self.content_col = toga.Box(
            style=Pack(direction=COLUMN, padding=16, gap=12)
        )
        self.root_scroll.content = self.content_col

        # 1. Header bar (time-based greeting + active 4-theme pill)
        header_box = toga.Box(
            style=Pack(direction=ROW, align_items=CENTER, gap=10, padding_bottom=2)
        )
        greeting, tagline = _time_greeting()
        self.greeting_label = toga.Label(
            greeting,
            style=Pack(font_family=f, font_size=17, font_weight=BOLD),
        )
        self.greeting_sub_label = toga.Label(
            tagline,
            style=Pack(font_family=f, font_size=10),
        )
        self._primary_labels.append(self.greeting_label)
        self._muted_labels.append(self.greeting_sub_label)

        title_col = toga.Box(style=Pack(direction=COLUMN, flex=1, gap=2))
        title_col.add(self.greeting_label)
        title_col.add(self.greeting_sub_label)
        header_box.add(title_col)

        self.theme_button = toga.Button(
            THEMES[self.theme_name]["badge_text"],
            on_press=self.on_cycle_theme,
            style=Pack(
                font_family=f,
                width=126,
                height=36,
                font_size=10,
                font_weight=BOLD,
            ),
        )
        self._secondary_buttons.append(self.theme_button)
        header_box.add(self.theme_button)
        self.content_col.add(header_box)

        # 2. Top Navigation Tabs (Downloader | History)
        nav_box = toga.Box(style=Pack(direction=ROW, padding=4, gap=6))
        self._alt_boxes.append(nav_box)

        self.tab_downloader_btn = toga.Button(
            "Downloader",
            on_press=lambda _w: self._switch_tab("downloader"),
            style=Pack(font_family=f, flex=1, height=38, font_size=11, font_weight=BOLD),
        )
        self.tab_history_btn = toga.Button(
            "History",
            on_press=lambda _w: self._switch_tab("history"),
            style=Pack(font_family=f, flex=1, height=38, font_size=11, font_weight=BOLD),
        )
        nav_box.add(self.tab_downloader_btn)
        nav_box.add(self.tab_history_btn)
        self.content_col.add(nav_box)

        # ── Downloader Container ──────────────────────────────────────────
        self.downloader_container = toga.Box(style=Pack(direction=COLUMN, gap=12))

        # 3. URL Input Card (Link + Paste + Fetch)
        url_card = toga.Box(style=Pack(direction=COLUMN, padding=14, gap=10))
        self._cards.append(url_card)

        self.url_input = toga.TextInput(
            placeholder="Paste a video or playlist link here…",
            on_change=self.on_url_changed,
            on_confirm=self.on_fetch,
            style=Pack(font_family=f, height=44, font_size=12),
        )
        self._inputs.append(self.url_input)
        url_card.add(self.url_input)

        url_actions = toga.Box(style=Pack(direction=ROW, gap=8))
        self.paste_btn = toga.Button(
            "Paste",
            on_press=self.on_paste_and_fetch,
            style=Pack(font_family=f, width=96, height=42, font_size=11, font_weight=BOLD),
        )
        self._secondary_buttons.append(self.paste_btn)

        self.fetch_btn = toga.Button(
            "Fetch",
            on_press=self.on_fetch,
            style=Pack(font_family=f, flex=1, height=42, font_size=12, font_weight=BOLD),
        )
        self._primary_buttons.append(self.fetch_btn)

        url_actions.add(self.paste_btn)
        url_actions.add(self.fetch_btn)
        url_card.add(url_actions)
        self.downloader_container.add(url_card)

        # 4. Preview Card
        self.preview_card = toga.Box(style=Pack(direction=COLUMN, padding=14, gap=10))
        self._cards.append(self.preview_card)

        thumb_row = toga.Box(style=Pack(direction=ROW, align_items=CENTER))
        self.thumb_view = toga.ImageView(style=Pack(width=280, height=158))
        thumb_row.add(toga.Box(style=Pack(flex=1)))
        thumb_row.add(self.thumb_view)
        thumb_row.add(toga.Box(style=Pack(flex=1)))
        self.preview_card.add(thumb_row)

        self.title_label = toga.Label(
            "",
            style=Pack(font_family=f, font_size=13, font_weight=BOLD),
        )
        self.styler.enable_multiline_android(self.title_label, max_lines=3)
        self._primary_labels.append(self.title_label)

        self.meta_label = toga.Label(
            "",
            style=Pack(font_family=f, font_size=10),
        )
        self._muted_labels.append(self.meta_label)

        self.preview_card.add(self.title_label)
        self.preview_card.add(self.meta_label)

        # 5. Format & Options Card
        self.options_card = toga.Box(style=Pack(direction=COLUMN, padding=14, gap=12))
        self._cards.append(self.options_card)

        mode_row = toga.Box(style=Pack(direction=ROW, gap=8))
        self.video_btn = toga.Button(
            "Video (MP4)",
            on_press=lambda _w: self.on_select_kind("video"),
            style=Pack(font_family=f, flex=1, height=40, font_size=11, font_weight=BOLD),
        )
        self.audio_btn = toga.Button(
            "Audio (M4A)",
            on_press=lambda _w: self.on_select_kind("audio"),
            style=Pack(font_family=f, flex=1, height=40, font_size=11, font_weight=BOLD),
        )
        mode_row.add(self.video_btn)
        mode_row.add(self.audio_btn)
        self.options_card.add(mode_row)

        self.quality_heading = toga.Label(
            "Quality",
            style=Pack(font_family=f, font_size=10, font_weight=BOLD),
        )
        self._muted_labels.append(self.quality_heading)
        self.options_card.add(self.quality_heading)

        self.quality_select = toga.Selection(
            items=["Select quality"],
            style=Pack(font_family=f, height=42, font_size=11),
        )
        self.options_card.add(self.quality_select)

        # Playlist-specific section
        self.playlist_box = toga.Box(style=Pack(direction=COLUMN, gap=8, padding_top=4))

        pl_bar = toga.Box(style=Pack(direction=ROW, align_items=CENTER, gap=8))
        self.playlist_count_label = toga.Label(
            "0 selected",
            style=Pack(font_family=f, flex=1, font_size=10, font_weight=BOLD),
        )
        self._muted_labels.append(self.playlist_count_label)

        self.pl_all_btn = toga.Button(
            "All",
            on_press=lambda _w: self._set_all_playlist_checks(True),
            style=Pack(font_family=f, width=64, height=34, font_size=10, font_weight=BOLD),
        )
        self.pl_none_btn = toga.Button(
            "None",
            on_press=lambda _w: self._set_all_playlist_checks(False),
            style=Pack(font_family=f, width=68, height=34, font_size=10, font_weight=BOLD),
        )
        self._secondary_buttons.extend([self.pl_all_btn, self.pl_none_btn])

        pl_bar.add(self.playlist_count_label)
        pl_bar.add(self.pl_all_btn)
        pl_bar.add(self.pl_none_btn)
        self.playlist_box.add(pl_bar)

        self.playlist_items_col = toga.Box(style=Pack(direction=COLUMN, gap=6))
        self.playlist_box.add(self.playlist_items_col)

        # 6. Save Location Card
        self.folder_card = toga.Box(style=Pack(direction=COLUMN, padding=14, gap=8))
        self._cards.append(self.folder_card)

        folder_top = toga.Box(style=Pack(direction=ROW, align_items=CENTER, gap=8))
        save_title = toga.Label(
            "Save Location",
            style=Pack(font_family=f, flex=1, font_size=10, font_weight=BOLD),
        )
        self._muted_labels.append(save_title)

        self.change_folder_btn = toga.Button(
            "Change",
            on_press=self.on_change_folder,
            style=Pack(font_family=f, width=96, height=36, font_size=10, font_weight=BOLD),
        )
        self._secondary_buttons.append(self.change_folder_btn)

        folder_top.add(save_title)
        folder_top.add(self.change_folder_btn)
        self.folder_card.add(folder_top)

        self.folder_label = toga.Label(
            _format_responsive_path(self.download_dir, dp_w),
            style=Pack(font_family=f, font_size=11),
        )
        self._primary_labels.append(self.folder_label)
        self.folder_card.add(self.folder_label)
        self.downloader_container.add(self.folder_card)

        # 7. Primary Download CTA
        self.download_btn = toga.Button(
            "Download",
            on_press=self.on_download,
            style=Pack(font_family=f, height=48, font_size=13, font_weight=BOLD),
        )
        self._primary_buttons.append(self.download_btn)

        # 8. Progress Card
        self.progress_card = toga.Box(style=Pack(direction=COLUMN, padding=14, gap=8))
        self._cards.append(self.progress_card)

        prog_top = toga.Box(style=Pack(direction=ROW, align_items=CENTER, gap=8))
        self.stage_label = toga.Label(
            "Working…",
            style=Pack(font_family=f, flex=1, font_size=11, font_weight=BOLD),
        )
        self._primary_labels.append(self.stage_label)

        self.cancel_btn = toga.Button(
            "Cancel",
            on_press=self.on_cancel,
            style=Pack(font_family=f, width=88, height=36, font_size=10, font_weight=BOLD),
        )
        self._secondary_buttons.append(self.cancel_btn)

        prog_top.add(self.stage_label)
        prog_top.add(self.cancel_btn)
        self.progress_card.add(prog_top)

        self.progress_bar = toga.ProgressBar(max=100, style=Pack(height=10))
        self.progress_card.add(self.progress_bar)

        self.progress_detail_label = toga.Label(
            "",
            style=Pack(font_family=f, font_size=10),
        )
        self._muted_labels.append(self.progress_detail_label)
        self.progress_card.add(self.progress_detail_label)

        # 9. Status Banner Card
        self.status_card = toga.Box(style=Pack(direction=COLUMN, padding=14))
        self._cards.append(self.status_card)

        self.status_label = toga.Label(
            "",
            style=Pack(font_family=f, font_size=11, font_weight=BOLD),
        )
        self.styler.enable_multiline_android(self.status_label, max_lines=8)
        self.status_card.add(self.status_label)

        self.content_col.add(self.downloader_container)

        # ── History Container ─────────────────────────────────────────────
        self.history_container = toga.Box(style=Pack(direction=COLUMN, gap=12))

        hist_header = toga.Box(style=Pack(direction=ROW, align_items=CENTER, gap=8))
        hist_title = toga.Label(
            "Download History",
            style=Pack(font_family=f, flex=1, font_size=13, font_weight=BOLD),
        )
        self._primary_labels.append(hist_title)

        self.clear_history_btn = toga.Button(
            "Clear All",
            on_press=self._clear_history,
            style=Pack(font_family=f, width=96, height=36, font_size=10, font_weight=BOLD),
        )
        self._secondary_buttons.append(self.clear_history_btn)

        hist_header.add(hist_title)
        hist_header.add(self.clear_history_btn)
        self.history_container.add(hist_header)

        self.history_list_box = toga.Box(style=Pack(direction=COLUMN, gap=10))
        self.history_container.add(self.history_list_box)

    # ----- state management -----------------------------------------------

    def _set_visible(self, container: toga.Box, widget: toga.Widget, visible: bool):
        present = widget in container.children
        if visible and not present:
            container.add(widget)
        elif not visible and present:
            container.remove(widget)

    def _set_state(self, state: str):
        self.state = state
        busy = state in ("fetching", "downloading")
        has_media = state in ("ready", "downloading", "done")
        editable = state in ("ready", "done")
        is_playlist = self.mode == "playlist"

        self.url_input.enabled = not busy
        self.paste_btn.enabled = not busy
        self.fetch_btn.enabled = not busy
        self.fetch_btn.text = "Fetching…" if state == "fetching" else "Fetch"
        self.change_folder_btn.enabled = not busy

        # Keep cards in clean vertical order inside downloader_container
        for w in (
            self.preview_card,
            self.options_card,
            self.folder_card,
            self.download_btn,
            self.progress_card,
            self.status_card,
        ):
            if w in self.downloader_container.children:
                self.downloader_container.remove(w)

        if has_media:
            self.downloader_container.add(self.preview_card)
            self.downloader_container.add(self.options_card)
            self._set_visible(
                self.options_card, self.playlist_box, is_playlist
            )

        self.downloader_container.add(self.folder_card)

        if editable:
            self._update_download_cta()
            self.downloader_container.add(self.download_btn)

        if busy:
            self.downloader_container.add(self.progress_card)

        if self._status_raw:
            self.downloader_container.add(self.status_card)

        self.video_btn.enabled = editable
        self.audio_btn.enabled = editable
        self.quality_select.enabled = editable
        self.pl_all_btn.enabled = editable
        self.pl_none_btn.enabled = editable
        for sw in self.playlist_checks:
            sw.enabled = editable

    def show_status(self, text: str, level: str = "info"):
        self._status_raw = text.strip()
        self._status_level = level
        dp_w = _screen_dp_width(self._current_width)
        self.status_label.text = _wrap_for_screen(
            self._status_raw, dp_w, char_dp=7.2, padding_dp=68, max_lines=8
        )
        self._update_status_color()
        self._set_visible(
            self.downloader_container, self.status_card, bool(self._status_raw)
        )

    def _update_status_color(self):
        t = THEMES[self.theme_name]
        color = t.get(self._status_level, t["text"])
        self.status_label.style.color = color

    # ----- folder selection -----------------------------------------------

    def on_change_folder(self, _widget):
        if is_android():
            try:
                from android.content import Intent
                from org.beeware.android import MainActivity

                intent = Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
                intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                intent.addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
                intent.addFlags(Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION)

                def _on_result(result_code, data):
                    if data is None:
                        return
                    uri = data.getData()
                    if uri is None:
                        return
                    picked = _tree_uri_to_path(str(uri.toString()))
                    if picked is not None:
                        self.call_on_ui(self._apply_picked_folder, picked)
                    else:
                        self.call_on_ui(
                            self.show_status,
                            "Selected folder could not be mapped. Using Downloads/DirectSave.",
                            "warning",
                        )

                MainActivity.singletonThis.startActivityForResult(intent, _on_result)
                return
            except Exception:
                pass

        # Fallback preset dialog
        presets = android_preset_dirs()
        if presets:
            self._apply_picked_folder(presets[0][1])

    def _apply_picked_folder(self, candidate: Path):
        try:
            folder = set_mobile_download_dir(candidate)
            self.download_dir = folder
            self._refresh_responsive_text()
            self.show_status(
                f"Save folder updated: {_format_responsive_path(folder, self._current_width)}",
                "success",
            )
        except Exception as error:
            self.show_status(str(error), "error")

    # ----- fetching -------------------------------------------------------

    def on_url_changed(self, _widget):
        if self.state in ("ready", "done"):
            self.info = None
            self.summary = None
            self.playlist = None
            self.playlist_checks = []
            self._status_raw = ""
            self._set_state("idle")

    def on_paste_and_fetch(self, _widget):
        if self.state in ("fetching", "downloading"):
            return
        text = _get_clipboard_text()
        if not text:
            self.show_status("Clipboard is empty.", "warning")
            return
        self.url_input.value = text
        self.on_url_changed(self.url_input)
        self.on_fetch(self.fetch_btn)

    def on_fetch(self, _widget):
        if self.state in ("fetching", "downloading"):
            return

        url = (self.url_input.value or "").strip()
        if not url:
            self.show_status("Paste a video or playlist link first.", "error")
            return

        if has_playlist_and_video(url):
            self._prompt_mixed_link(url)
            return

        self._begin_fetch(url, prefer_playlist=False)

    def _prompt_mixed_link(self, url: str):
        async def _ask():
            whole = await self.main_window.question_dialog(
                APP_NAME,
                "This link is a video inside a playlist.\n\n"
                "Choose Yes to fetch the whole playlist, or No for just this video.",
            )
            self._begin_fetch(url, prefer_playlist=bool(whole))

        self.loop.create_task(_ask())

    def _begin_fetch(self, url: str, prefer_playlist: bool):
        self.url = url
        self._status_raw = ""
        self.stage_label.text = (
            "Fetching playlist details…"
            if prefer_playlist
            else "Fetching video details…"
        )
        self.progress_detail_label.text = ""
        self.progress_bar.max = None
        self._set_state("fetching")

        def worker():
            try:
                if not prefer_playlist:
                    try:
                        info = (
                            _android_fetch_info(url)
                            if is_android()
                            else fetch_video_info(url)
                        )
                    except PlaylistLinkError:
                        pass
                    else:
                        summary = summarize_video(info)
                        opts = quality_options(info)
                        thumb = load_thumbnail_bytes(summary.thumbnail)
                        self.call_on_ui(
                            self.on_video_fetched, info, summary, opts, thumb
                        )
                        return

                playlist = fetch_playlist(url)
                thumb_url = None
                if playlist.entries:
                    first = playlist.entries[0]
                    thumb_url = (
                        first.get("thumbnail")
                        or f"https://i.ytimg.com/vi/{first.get('id')}/hqdefault.jpg"
                    )
                thumb = load_thumbnail_bytes(thumb_url)
                self.call_on_ui(self.on_playlist_fetched, playlist, thumb)
            except DownloadError as error:
                self.call_on_ui(self.on_fetch_failed, str(error))
            except Exception as error:
                self.call_on_ui(self.on_fetch_failed, friendly_error(error))

        threading.Thread(target=worker, daemon=True).start()

    def on_video_fetched(
        self,
        info: dict,
        summary: VideoSummary,
        opts: list[tuple[int, int | None]],
        thumb_bytes: bytes | None,
    ):
        self.mode = "video"
        self.info = info
        self.summary = summary
        self.video_options = opts
        self.playlist = None
        self.playlist_checks = []

        self.meta_label.text = (
            f"{summary.channel} · {format_duration(summary.duration)}"
        )
        self._set_thumbnail(thumb_bytes)
        self._populate_quality_choices()
        self._refresh_responsive_text()
        self._set_state("ready")

    def on_playlist_fetched(
        self, playlist: PlaylistInfo, thumb_bytes: bytes | None
    ):
        self.mode = "playlist"
        self.playlist = playlist
        self.info = None
        self.summary = None
        self.video_options = []

        count = len(playlist.entries)
        self.meta_label.text = (
            f"Playlist · {count} {'video' if count == 1 else 'videos'}"
        )
        self._set_thumbnail(thumb_bytes)
        self._populate_playlist_items()
        self._populate_quality_choices()
        self._refresh_responsive_text()
        self._set_state("ready")

    def on_fetch_failed(self, message: str):
        self.show_status(message, "error")
        self._set_state("idle")

    def _set_thumbnail(self, data: bytes | None):
        if data:
            try:
                self.thumb_view.image = toga.Image(data=data)
                return
            except Exception:
                pass
        self.thumb_view.image = None

    def _populate_playlist_items(self):
        for child in list(self.playlist_items_col.children):
            self.playlist_items_col.remove(child)
        self.playlist_checks = []

        if not self.playlist:
            return

        t = THEMES[self.theme_name]
        f = _resolve_ui_font()
        dp_w = _screen_dp_width(self._current_width)

        for idx, entry in enumerate(self.playlist.entries, start=1):
            raw_title = entry.get("title") or entry.get("id") or "Untitled"
            wrapped = _wrap_for_screen(
                f"{idx}. {raw_title}", dp_w, char_dp=7.8, padding_dp=96, max_lines=2
            )
            sw = toga.Switch(
                wrapped,
                value=True,
                on_change=self._on_playlist_check_changed,
                style=Pack(font_family=f, font_size=11, color=t["text"]),
            )
            self.playlist_checks.append(sw)
            self.playlist_items_col.add(sw)

        self._update_playlist_count()

    def _on_playlist_check_changed(self, _widget):
        self._update_playlist_count()

    def _set_all_playlist_checks(self, checked: bool):
        for sw in self.playlist_checks:
            sw.value = checked
        self._update_playlist_count()

    def _update_playlist_count(self):
        total = len(self.playlist_checks)
        selected = sum(1 for sw in self.playlist_checks if sw.value)
        self.playlist_count_label.text = f"{selected} of {total} selected"
        self._update_download_cta()

    def _update_download_cta(self):
        if self.mode == "playlist" and self.playlist is not None:
            selected = sum(1 for sw in self.playlist_checks if sw.value)
            self.download_btn.text = (
                f"Download {selected} item{'s' if selected != 1 else ''}"
            )
            self.download_btn.enabled = selected > 0
        else:
            self.download_btn.text = "Download"
            self.download_btn.enabled = True

    def on_select_kind(self, kind: str):
        if self.media_kind == kind:
            return
        self.media_kind = kind
        self._style_segment_buttons()
        self._populate_quality_choices()

    def _populate_quality_choices(self):
        settings = load_settings()
        self._quality_map: dict[str, int | str] = {}
        labels: list[str] = []

        if self.media_kind == "video":
            if self.mode == "video":
                self.quality_heading.text = "Video Quality"
                for height, size in self.video_options:
                    lbl = f"{height}p"
                    if size:
                        lbl += f" · ~{format_file_size(size)}"
                    labels.append(lbl)
                    self._quality_map[lbl] = height
            else:
                self.quality_heading.text = "Max Playlist Quality"
                for height in PLAYLIST_QUALITIES:
                    lbl = f"Up to {height}p"
                    labels.append(lbl)
                    self._quality_map[lbl] = height
            saved = settings.get("video_quality")
        else:
            self.quality_heading.text = "Audio Format"
            if is_android() and not self.has_ffmpeg:
                lbl = "Original audio (M4A, no conversion)"
                labels.append(lbl)
                self._quality_map[lbl] = ORIGINAL_AUDIO
                saved = ORIGINAL_AUDIO
            else:
                for choice in AUDIO_CHOICES:
                    lbl = audio_label(choice)
                    labels.append(lbl)
                    self._quality_map[lbl] = choice
                saved = settings.get("audio_bitrate")

        if not labels:
            labels = ["No compatible formats found"]

        self.quality_select.items = labels
        for lbl, val in self._quality_map.items():
            if val == saved:
                self.quality_select.value = lbl
                break

    # ----- downloading ----------------------------------------------------

    def on_download(self, _widget):
        if self.state in ("fetching", "downloading"):
            return

        selected_label = self.quality_select.value
        setting = self._quality_map.get(selected_label)
        if setting is None:
            self.show_status("No playable format available for this selection.", "warning")
            return

        self.cancel_event = threading.Event()
        cancel = self.cancel_event
        kind = self.media_kind
        reporter = MobileReporter(self)

        self._status_raw = ""
        self.stage_label.text = "Starting download…"
        self.progress_detail_label.text = ""
        self.progress_bar.max = None
        self._set_state("downloading")

        if self.mode == "playlist" and self.playlist is not None:
            entries = [
                entry
                for entry, sw in zip(self.playlist.entries, self.playlist_checks)
                if sw.value
            ]
            if not entries:
                self.show_status("Select at least one video to download.", "error")
                self._set_state("ready")
                return

            pl_title = self.playlist.title
            pl_dir = self.download_dir / safe_folder_name(pl_title)

            def playlist_worker():
                completed = 0
                skipped = 0
                failed = 0
                total = len(entries)

                for idx, entry in enumerate(entries, start=1):
                    if cancel.is_set():
                        break
                    e_title = entry.get("title") or entry.get("id") or f"Item {idx}"
                    e_url = entry.get("webpage_url") or entry.get("url")
                    if not e_url and entry.get("id"):
                        e_url = f"https://www.youtube.com/watch?v={entry['id']}"

                    reporter.playlist_item(idx, total, e_title)
                    try:
                        if is_android() or not self.has_ffmpeg:
                            res = _android_download_media(
                                e_url,
                                kind,
                                setting,
                                pl_dir,
                                info=entry,
                                reporter=reporter,
                                cancel=cancel,
                            )
                        else:
                            fn = download_video if kind == "video" else download_audio
                            res = fn(
                                e_url,
                                setting,
                                folder=pl_dir,
                                reporter=reporter,
                                cancel=cancel,
                            )

                        if res.status == "completed":
                            completed += 1
                            if res.filename:
                                q_str = "M4A" if kind == "audio" else f"{setting}p"
                                self.call_on_ui(
                                    self._record_history,
                                    e_title,
                                    kind,
                                    q_str,
                                    pl_dir / res.filename,
                                )
                        elif res.status == "exists":
                            skipped += 1
                    except Exception:
                        failed += 1

                self.call_on_ui(
                    self.on_playlist_done,
                    completed,
                    skipped,
                    failed,
                    total,
                    cancel.is_set(),
                )

            threading.Thread(target=playlist_worker, daemon=True).start()
            return

        # Single video or audio download
        url = self.url
        info = self.info
        title = self.summary.title if self.summary else "Downloaded media"
        out_dir = self.download_dir

        def single_worker():
            try:
                if is_android() or not self.has_ffmpeg:
                    res = _android_download_media(
                        url,
                        kind,
                        setting,
                        out_dir,
                        info=info,
                        reporter=reporter,
                        cancel=cancel,
                    )
                else:
                    fn = download_video if kind == "video" else download_audio
                    res = fn(
                        url,
                        setting,
                        folder=out_dir,
                        info=info,
                        reporter=reporter,
                        cancel=cancel,
                    )
                self.call_on_ui(self.on_download_done, res, kind, setting, title)
            except DownloadError as error:
                self.call_on_ui(self.on_download_failed, str(error))
            except Exception as error:
                self.call_on_ui(self.on_download_failed, friendly_error(error))

        threading.Thread(target=single_worker, daemon=True).start()

    def on_cancel(self, _widget):
        if self.cancel_event is not None:
            self.cancel_event.set()
        self.cancel_btn.enabled = False
        self.cancel_btn.text = "Cancelling…"

    def on_playlist_item(self, number: int, total: int, title: str):
        dp_w = _screen_dp_width(self._current_width)
        self.stage_label.text = _wrap_for_screen(
            f"({number}/{total}) {title}",
            dp_w,
            char_dp=7.4,
            padding_dp=150,
            max_lines=2,
        )
        self.progress_bar.max = None
        self.progress_detail_label.text = ""

    def on_stage(self, text: str):
        self.stage_label.text = text
        self.progress_bar.max = None
        self.progress_detail_label.text = ""

    def on_progress(
        self,
        downloaded: int,
        total: int | None,
        speed: float | None,
        eta: int | None,
    ):
        if total and total > 0:
            self.progress_bar.max = 100
            self.progress_bar.value = min(100, int(downloaded * 100 / total))
            parts = [f"{format_file_size(downloaded)} / {format_file_size(total)}"]
        else:
            self.progress_bar.max = None
            parts = [format_file_size(downloaded)]

        if speed:
            parts.append(f"{format_file_size(speed)}/s")
        if eta is not None:
            parts.append(f"ETA {format_duration(eta)}")

        self.progress_detail_label.text = " · ".join(parts)

    def on_progress_done(self):
        self.progress_bar.max = 100
        self.progress_bar.value = 0
        self.progress_detail_label.text = ""

    def on_download_done(
        self, result: DownloadResult, kind: str, setting, title: str
    ):
        if result.status == "completed":
            key = "video_quality" if kind == "video" else "audio_bitrate"
            remember_setting(key, setting)
            if result.filename:
                q_str = "M4A" if kind == "audio" else f"{setting}p"
                self._record_history(
                    title, kind, q_str, self.download_dir / result.filename
                )
            self.show_status("✔ Download complete · Saved to History.", "success")
        elif result.status == "exists":
            self.show_status("Already downloaded.", "warning")
        else:
            self.show_status("Download cancelled.", "warning")

        self._set_state("done")

    def on_playlist_done(
        self,
        completed: int,
        skipped: int,
        failed: int,
        total: int,
        cancelled: bool,
    ):
        summary = f"{completed} completed, {skipped} skipped, {failed} failed."
        if cancelled:
            self.show_status(f"Playlist cancelled — {summary}", "warning")
        elif failed and completed == 0:
            self.show_status(f"Playlist failed — {summary}", "error")
        elif failed:
            self.show_status(f"Playlist finished with errors — {summary}", "warning")
        else:
            self.show_status(f"✔ Playlist complete — {summary}", "success")

        self._set_state("done")

    def on_download_failed(self, message: str):
        self.show_status(message, "error")
        self._set_state("ready")


def main():
    return DirectSaveMobileApp(APP_NAME, "com.maverick.ytdownloader")