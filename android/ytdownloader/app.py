"""YT Downloader - modern, responsive mobile interface (BeeWare Toga)."""

from __future__ import annotations

import copy
import logging
from pathlib import Path
import shutil
import sys
import textwrap
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
    ANDROID_INTERNAL_ROOT,
    configure_mobile_paths,
    friendly_path,
    is_android,
    set_mobile_download_dir,
)

THUMBNAIL_MAX_BYTES = 5 * 1024 * 1024

THEME_ORDER = ("obsidian", "light")

THEMES = {
    "obsidian": {
        "label": "Obsidian",
        "toggle_text": "☀ Light",
        "page_bg": "#0B0B0E",
        "card_bg": "#16161A",
        "input_bg": "#202026",
        "border": "#2A2A32",
        "text": "#F5F5F7",
        "muted": "#A1A1AA",
        "hint": "#71717A",
        "accent": "#E11D48",
        "accent_border": "#FB7185",
        "accent_text": "#FFFFFF",
        "secondary_bg": "#23232B",
        "secondary_text": "#F4F4F5",
        "danger_bg": "#33161C",
        "danger_border": "#7F1D1D",
        "danger_text": "#FB7185",
        "status": {
            "info": "#A1A1AA",
            "success": "#34D399",
            "warning": "#FBBF24",
            "error": "#FB7185",
        },
    },
    "light": {
        "label": "Light",
        "toggle_text": "☾ Obsidian",
        "page_bg": "#F4F4F5",
        "card_bg": "#FFFFFF",
        "input_bg": "#F4F4F6",
        "border": "#E4E4E7",
        "text": "#18181B",
        "muted": "#52525B",
        "hint": "#A1A1AA",
        "accent": "#E11D48",
        "accent_border": "#BE123C",
        "accent_text": "#FFFFFF",
        "secondary_bg": "#ECECF0",
        "secondary_text": "#18181B",
        "danger_bg": "#FEE2E2",
        "danger_border": "#FCA5A5",
        "danger_text": "#DC2626",
        "status": {
            "info": "#52525B",
            "success": "#15803D",
            "warning": "#B45309",
            "error": "#DC2626",
        },
    },
}

ANDROID_CLIENT_PROFILES: tuple[list[str] | None, ...] = (
    None,
    ["web", "android_vr", "mweb"],
    ["android_vr", "web"],
    ["android", "web"],
)

_STALE_FORMAT_KEYS = (
    "requested_formats",
    "requested_downloads",
    "format",
    "format_id",
    "format_note",
    "ext",
    "url",
    "filesize",
    "filesize_approx",
    "tbr",
    "vbr",
    "abr",
    "asr",
    "fps",
    "width",
    "height",
    "resolution",
    "vcodec",
    "acodec",
    "dynamic_range",
    "protocol",
    "container",
    "language",
    "http_headers",
    "fragments",
)

_DNS_ERROR_MARKERS = (
    "errno 7",
    "[errno 7]",
    "no address associated with hostname",
    "temporary failure in name resolution",
    "name or service not known",
    "getaddrinfo",
)


def _is_dns_or_network_error(error: Exception) -> bool:
    """Return True if the error indicates DNS lookup or transient network failure."""
    text = str(error).lower()
    return any(marker in text for marker in _DNS_ERROR_MARKERS)


def _strip_stale_format_keys(info: dict) -> dict:
    """Return a copy of yt-dlp info without earlier format selection keys."""
    clean = copy.deepcopy(info)
    for key in _STALE_FORMAT_KEYS:
        clean.pop(key, None)
    return clean


def _android_extractor_args(clients: list[str] | None = None) -> dict:
    """Return YouTube extractor args for a given client list."""
    selected = clients or ["android_vr", "web"]
    return {
        "youtube": {
            "player_client": selected,
        }
    }


def _android_screen_width_dp(widget) -> int | None:
    """Read the real Android screen width in dp from DisplayMetrics."""
    if not is_android():
        return None
    try:
        dm = widget._impl.native.getContext().getResources().getDisplayMetrics()
        if dm.density and dm.widthPixels:
            return int(dm.widthPixels / dm.density)
    except Exception:
        pass
    return None


def _wrap_text(text: str, available_dp: int, char_dp: float = 8.4) -> str:
    """Insert explicit newlines so Toga Labels never clip past the card edge."""
    if not text:
        return ""
    inner_dp = max(180, available_dp - 72)
    max_chars = max(22, int(inner_dp / char_dp))
    wrapped = [
        textwrap.fill(
            para,
            width=max_chars,
            break_long_words=True,
            break_on_hyphens=True,
        )
        for para in text.splitlines()
    ]
    return "\n".join(wrapped)


def _enable_android_multiline(label_widget: toga.Label, max_lines: int = 12):
    """Allow Android TextView labels to render multi-line wrapped text cleanly."""
    if not is_android():
        return
    try:
        native = label_widget._impl.native
        native.setSingleLine(False)
        native.setHorizontallyScrolling(False)
        native.setMaxLines(max_lines)
        native.setEllipsize(None)
    except Exception:
        pass


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
    max_chars = max(24, int((available_px - 72) / 8.0))
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
    def style_input(
        widget,
        bg_hex: str,
        border_hex: str,
        text_hex: str,
        hint_hex: str,
    ):
        """Style text inputs cleanly on both Android and Windows."""
        if is_android():
            try:
                from android.graphics import Color
                from android.graphics.drawable import GradientDrawable

                native = widget._impl.native
                density = native.getContext().getResources().getDisplayMetrics().density
                drawable = GradientDrawable()
                drawable.setColor(Color.parseColor(bg_hex))
                drawable.setCornerRadius(10.0 * density)
                drawable.setStroke(
                    max(1, int(round(density))), Color.parseColor(border_hex)
                )
                native.setBackground(drawable)
                native.setTextColor(Color.parseColor(text_hex))
                native.setHintTextColor(Color.parseColor(hint_hex))
                pad_h = int(12 * density)
                pad_v = int(8 * density)
                native.setPadding(pad_h, pad_v, pad_h, pad_v)
            except Exception:
                pass
        elif sys.platform == "win32":
            try:
                import System.Windows.Forms as WinForms

                widget._impl.native.BorderStyle = WinForms.BorderStyle.FixedSingle
            except Exception:
                pass


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
        with urllib.request.urlopen(req, timeout=5) as resp:
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


def _android_mux_mp4(video_path: Path, audio_path: Path, output_path: Path) -> None:
    """Merge separate MP4 video and M4A audio using Android's native MediaMuxer."""
    from android.media import MediaCodec, MediaExtractor, MediaFormat, MediaMuxer
    from java.nio import ByteBuffer

    v_ext = MediaExtractor()
    a_ext = MediaExtractor()
    muxer = None
    started = False

    try:
        v_ext.setDataSource(str(video_path))
        v_src_track = -1
        v_format = None
        for i in range(v_ext.getTrackCount()):
            fmt = v_ext.getTrackFormat(i)
            mime = fmt.getString(MediaFormat.KEY_MIME) or ""
            if mime.startswith("video/"):
                v_src_track = i
                v_format = fmt
                break
        if v_src_track < 0 or v_format is None:
            raise RuntimeError("No video track found in downloaded stream.")
        v_ext.selectTrack(v_src_track)

        a_ext.setDataSource(str(audio_path))
        a_src_track = -1
        a_format = None
        for i in range(a_ext.getTrackCount()):
            fmt = a_ext.getTrackFormat(i)
            mime = fmt.getString(MediaFormat.KEY_MIME) or ""
            if mime.startswith("audio/"):
                a_src_track = i
                a_format = fmt
                break
        if a_src_track < 0 or a_format is None:
            raise RuntimeError("No audio track found in downloaded stream.")
        a_ext.selectTrack(a_src_track)

        muxer = MediaMuxer(
            str(output_path), MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4
        )
        v_dst_track = muxer.addTrack(v_format)
        a_dst_track = muxer.addTrack(a_format)
        muxer.start()
        started = True

        buf = ByteBuffer.allocateDirect(1024 * 1024)
        buf_info = MediaCodec.BufferInfo()
        sync_flag = MediaExtractor.SAMPLE_FLAG_SYNC
        key_flag = MediaCodec.BUFFER_FLAG_KEY_FRAME

        def _copy_track(extractor, dst_track):
            while True:
                buf.clear()
                sample_size = extractor.readSampleData(buf, 0)
                if sample_size < 0:
                    break
                buf_info.offset = 0
                buf_info.size = sample_size
                buf_info.presentationTimeUs = extractor.getSampleTime()
                buf_info.flags = (
                    key_flag if (extractor.getSampleFlags() & sync_flag) else 0
                )
                muxer.writeSampleData(dst_track, buf, buf_info)
                extractor.advance()

        _copy_track(v_ext, v_dst_track)
        _copy_track(a_ext, a_dst_track)
    finally:
        if muxer is not None:
            if started:
                try:
                    muxer.stop()
                except Exception:
                    pass
            try:
                muxer.release()
            except Exception:
                pass
        try:
            v_ext.release()
        except Exception:
            pass
        try:
            a_ext.release()
        except Exception:
            pass


def _android_fetch_info(url: str) -> dict:
    """Extract video info on Android, rotating client profiles on failure."""
    last_error: Exception | None = None
    best_info: dict | None = None
    saw_dns_error = False

    for clients in ANDROID_CLIENT_PROFILES:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "socket_timeout": 10,
            "retries": 1,
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
            if _is_dns_or_network_error(error):
                saw_dns_error = True
                continue
            msg = str(error).lower()
            if (
                "not a bot" in msg
                or "sign in" in msg
                or "http error 403" in msg
                or "js runtime" in msg
                or "challenge" in msg
            ):
                continue
            raise DownloadError(friendly_error(error)) from error

    if best_info is not None:
        return best_info

    if saw_dns_error:
        raise DownloadError(
            "Android DNS error (Errno 7): could not resolve YouTube host. "
            "Toggle airplane mode or switch Wi-Fi / 5G and retry."
        )

    raise DownloadError(
        friendly_error(last_error)
        if last_error
        else "Could not retrieve video information."
    )


def _download_single_stream(
    url: str,
    format_spec: str,
    format_sort: list[str],
    outtmpl: str,
    reporter: Reporter,
    cancel: threading.Event,
    info: dict | None = None,
) -> None:
    """Download one exact stream, reusing fetched info first for instant start."""
    profiles: tuple[list[str] | None, ...] = (
        None,
        ["android_vr", "web"],
    )
    last_error: Exception | None = None
    saw_dns_error = False

    for attempt_idx, clients in enumerate(profiles):
        if cancel.is_set():
            raise DownloadCancelled()

        tracker = ProgressTracker(reporter, expected_total=None, cancel=cancel)
        opts = {
            "quiet": True,
            "noplaylist": True,
            "format": format_spec,
            "format_sort": format_sort,
            "outtmpl": outtmpl,
            "overwrites": True,
            "progress_hooks": [tracker],
            "restrictfilenames": False,
            "socket_timeout": 10,
            "retries": 2,
            "fragment_retries": 2,
        }
        if clients is not None:
            opts["extractor_args"] = _android_extractor_args(clients)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                if attempt_idx == 0 and info is not None:
                    try:
                        ydl.process_ie_result(
                            _strip_stale_format_keys(info), download=True
                        )
                        return
                    except DownloadCancelled:
                        raise
                    except Exception:
                        pass
                ydl.download([url])
            return
        except DownloadCancelled:
            raise
        except Exception as error:
            last_error = error
            if _is_dns_or_network_error(error):
                saw_dns_error = True
                continue
            msg = str(error).lower()
            if (
                "requested format is not available" in msg
                or "no video formats found" in msg
                or "format is not available" in msg
                or "http error 403" in msg
                or "not a bot" in msg
                or "sign in" in msg
            ):
                continue
            raise DownloadError(friendly_error(error)) from error

    if saw_dns_error:
        raise DownloadError(
            "Android DNS error (Errno 7): no address associated with hostname. "
            "Toggle airplane mode or switch Wi-Fi / 5G and try again."
        )
    raise DownloadError(
        friendly_error(last_error)
        if last_error
        else "No compatible stream is available for this video."
    )


def _android_download_media(
    url: str,
    kind: str,
    setting,
    output_dir: Path,
    reporter: Reporter,
    cancel: threading.Event,
    info: dict | None = None,
) -> DownloadResult:
    """Download the exact selected video quality (muxed via Android MediaMuxer) or M4A audio."""
    output_dir.mkdir(parents=True, exist_ok=True)

    vid = (info or {}).get("id") or str(int(time.time()))
    raw_title = (info or {}).get("title") or vid
    safe_title = (
        yt_dlp.utils.sanitize_filename(raw_title, restricted=False).strip()[:90]
        or vid
    )

    if kind == "audio":
        outtmpl = str(output_dir / f"{safe_title} [{ORIGINAL_AUDIO}] [{vid}].%(ext)s")
        try:
            _download_single_stream(
                url=url,
                format_spec="bestaudio[ext=m4a]/bestaudio/best",
                format_sort=["abr", "proto"],
                outtmpl=outtmpl,
                reporter=reporter,
                cancel=cancel,
                info=info,
            )
            reporter.progress_done()
            return DownloadResult("completed", output_dir)
        except DownloadCancelled:
            reporter.progress_done()
            return DownloadResult("cancelled", output_dir)

    quality = int(setting)
    final_path = output_dir / f"{safe_title} [{quality}p] [{vid}].mp4"
    if final_path.is_file() and final_path.stat().st_size > 0:
        reporter.progress_done()
        return DownloadResult("exists", output_dir, final_path.name)

    tmp_video = output_dir / f".ytdl_v_{vid}_{quality}p.mp4"
    tmp_audio = output_dir / f".ytdl_a_{vid}_{quality}p.m4a"

    try:
        reporter.stage(f"Downloading {quality}p video…")
        video_spec = (
            f"bestvideo[ext=mp4][height={quality}]"
            f"/bestvideo[ext=mp4][height<={quality}]"
            f"/best[ext=mp4][height<={quality}]"
        )
        _download_single_stream(
            url=url,
            format_spec=video_spec,
            format_sort=[f"res:{quality}", "proto", "vcodec:h264"],
            outtmpl=str(tmp_video),
            reporter=reporter,
            cancel=cancel,
            info=info,
        )

        if cancel.is_set():
            raise DownloadCancelled()

        reporter.stage("Downloading audio track…")
        _download_single_stream(
            url=url,
            format_spec="bestaudio[ext=m4a]/bestaudio",
            format_sort=["abr", "proto"],
            outtmpl=str(tmp_audio),
            reporter=reporter,
            cancel=cancel,
            info=info,
        )

        if cancel.is_set():
            raise DownloadCancelled()

        reporter.stage("Merging video and audio…")
        try:
            _android_mux_mp4(tmp_video, tmp_audio, final_path)
        except Exception as mux_err:
            logging.getLogger("ytdl").warning(
                "Native Android mux failed for %sp: %s", quality, mux_err, exc_info=True
            )
            final_path.unlink(missing_ok=True)
            raise DownloadError(
                f"Could not merge {quality}p video on this device. "
                "Try 1080p or lower (H.264 MP4)."
            ) from mux_err

        reporter.progress_done()
        return DownloadResult("completed", output_dir, final_path.name)
    except DownloadCancelled:
        final_path.unlink(missing_ok=True)
        reporter.progress_done()
        return DownloadResult("cancelled", output_dir)
    finally:
        tmp_video.unlink(missing_ok=True)
        tmp_audio.unlink(missing_ok=True)


def _tree_uri_to_local_path(uri_str: str) -> Path | None:
    """Convert an Android SAF document tree URI into a filesystem path when possible."""
    try:
        decoded = urllib.parse.unquote(uri_str)
        if "/tree/" not in decoded:
            return None
        tree_part = decoded.split("/tree/", 1)[1].split("/", 1)[0]
        if ":" not in tree_part:
            return None
        volume, rel = tree_part.split(":", 1)
        if volume.lower() == "primary":
            rel_clean = rel.strip("/")
            return (
                ANDROID_INTERNAL_ROOT / rel_clean
                if rel_clean
                else ANDROID_INTERNAL_ROOT
            )
    except Exception:
        return None
    return None


def _make_card(inner_box: toga.Box) -> toga.Box:
    """Wrap an inner box inside a clean padded card container."""
    inner_box.style.margin = 14
    card = toga.Box(style=Pack(direction=COLUMN))
    card.add(inner_box)
    return card


class YTDownloaderApp(toga.App):
    def startup(self):
        self.download_dir = configure_mobile_paths(self)
        setup_logging()

        self.ui_font = _resolve_ui_font()
        self.styler = _NativeStyler()
        self._current_width = 360

        saved_theme = load_settings().get("mobile_theme")
        self.theme_name = saved_theme if saved_theme in THEMES else "obsidian"
        self._status_level = "info"
        self._raw_status_text = ""

        self.use_ffmpeg = bool(shutil.which("ffmpeg")) and not is_android()
        self.state = "idle"  # idle, fetching, ready, downloading, done
        self.mode = "video"  # "video" or "audio"
        self.url = ""
        self.info = None
        self.summary = None
        self.video_options: list[tuple[int, int | None]] = []
        self.quality_buttons: list[tuple[object, str, toga.Button]] = []
        self.selected_quality = None
        self.cancel_event: threading.Event | None = None

        self._build_ui()

        detected_dp = _android_screen_width_dp(self.status_label)
        if detected_dp:
            self._current_width = detected_dp

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

    def call_on_ui(self, func, *args):
        self.loop.call_soon_threadsafe(func, *args)

    def on_window_resize(self, window, **kwargs):
        detected_dp = _android_screen_width_dp(self.status_label)
        width = detected_dp or getattr(window.size, "width", 0)
        if width and width != self._current_width:
            self._current_width = width
            self._refresh_responsive_text()

    def _refresh_responsive_text(self):
        self.folder_label.text = _format_responsive_path(
            self.download_dir, self._current_width
        )
        if self.summary is not None:
            self.title_label.text = _wrap_text(
                self.summary.title, self._current_width, char_dp=9.2
            )
            self.title_label.refresh()
        if self._raw_status_text:
            self.status_label.text = _wrap_text(
                self._raw_status_text, self._current_width, char_dp=8.4
            )
            self.status_label.refresh()
        self.content_box.refresh()

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
            "☀ Light",
            on_press=self.on_cycle_theme,
            style=Pack(
                font_family=f,
                width=116,
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
            style=Pack(font_family=f, font_size=11, height=42),
        )
        self.fetch_button = toga.Button(
            "Fetch Video",
            on_press=self.on_fetch_pressed,
            style=Pack(
                font_family=f,
                flex=1,
                height=42,
                font_size=11,
                font_weight=BOLD,
            ),
        )
        self.clear_button = toga.Button(
            "Clear",
            on_press=self.on_clear_pressed,
            style=Pack(
                font_family=f,
                width=92,
                height=42,
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
        _enable_android_multiline(self.title_label, max_lines=4)
        _enable_android_multiline(self.meta_label, max_lines=2)
        self._primary_labels.append(self.title_label)
        self._muted_labels.append(self.meta_label)

        preview_inner = toga.Box(style=Pack(direction=COLUMN, gap=6))
        preview_inner.add(thumb_wrap)
        preview_inner.add(self.title_label)
        preview_inner.add(self.meta_label)
        self.preview_card = _make_card(preview_inner)
        self._cards.append(self.preview_card)
        self._card_inners.extend([preview_inner, thumb_wrap])

        # 4. Format & Quality card (tappable high-contrast pill grid)
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
                height=40,
                font_size=11,
                font_weight=BOLD,
            ),
        )
        self.audio_mode_btn = toga.Button(
            "Audio (M4A)",
            on_press=self.on_select_audio_mode,
            style=Pack(
                font_family=f,
                flex=1,
                height=40,
                font_size=11,
                font_weight=BOLD,
            ),
        )
        mode_row = toga.Box(style=Pack(direction=ROW, gap=10))
        mode_row.add(self.video_mode_btn)
        mode_row.add(self.audio_mode_btn)

        self.quality_caption = toga.Label(
            "VIDEO QUALITY",
            style=Pack(font_family=f, font_size=9, font_weight=BOLD, margin_top=6),
        )
        self._muted_labels.append(self.quality_caption)

        self.quality_grid = toga.Box(style=Pack(direction=COLUMN, gap=8))
        self.quality_detail_label = toga.Label(
            "",
            style=Pack(font_family=f, font_size=10, margin_top=2),
        )
        _enable_android_multiline(self.quality_detail_label, max_lines=2)
        self._muted_labels.append(self.quality_detail_label)

        options_inner = toga.Box(style=Pack(direction=COLUMN, gap=8))
        options_inner.add(format_caption)
        options_inner.add(mode_row)
        options_inner.add(self.quality_caption)
        options_inner.add(self.quality_grid)
        options_inner.add(self.quality_detail_label)
        self.options_card = _make_card(options_inner)
        self._cards.append(self.options_card)
        self._card_inners.extend([options_inner, mode_row, self.quality_grid])

        # 5. Save Location card
        save_caption = toga.Label(
            "SAVE LOCATION",
            style=Pack(font_family=f, flex=1, font_size=9, font_weight=BOLD),
        )
        self._muted_labels.append(save_caption)

        self.change_folder_btn = toga.Button(
            "Change",
            on_press=self.on_change_folder,
            style=Pack(
                font_family=f,
                width=110,
                height=34,
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
        _enable_android_multiline(self.folder_label, max_lines=3)
        self._primary_labels.append(self.folder_label)

        self.folder_inner = toga.Box(style=Pack(direction=COLUMN, gap=6))
        self.folder_inner.add(save_header_row)
        self.folder_inner.add(self.folder_label)
        self.folder_card = _make_card(self.folder_inner)
        self._cards.append(self.folder_card)
        self._card_inners.extend([self.folder_inner, save_header_row])

        # 6. Primary Download CTA
        self.download_button = toga.Button(
            "Download",
            on_press=self.on_download_pressed,
            style=Pack(
                font_family=f,
                height=48,
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
        _enable_android_multiline(self.progress_label, max_lines=2)
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

        # 8. Status banner card (explicitly wrapped with newlines for portrait mode)
        self.status_label = toga.Label(
            "",
            style=Pack(font_family=f, font_size=11, font_weight=BOLD),
        )
        _enable_android_multiline(self.status_label, max_lines=12)
        status_inner = toga.Box(style=Pack(direction=COLUMN))
        status_inner.add(self.status_label)
        self.status_card = _make_card(status_inner)
        self._cards.append(self.status_card)
        self._card_inners.append(status_inner)

        # Responsive vertical container inside ScrollContainer
        self.content_box = toga.Box(
            style=Pack(direction=COLUMN, margin=(14, 14, 24, 14), gap=12)
        )
        self.content_box.add(self.header_box)
        self.content_box.add(self.url_card)

        self.root_scroll = toga.ScrollContainer(
            content=self.content_box, horizontal=False
        )

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

        self.theme_button.text = t["toggle_text"]

        for card in self._cards:
            card.style.background_color = t["card_bg"]
            self.styler.apply(card, t["card_bg"], t["border"], radius=14)
        for inner in self._card_inners:
            inner.style.background_color = t["card_bg"]
        for row in self.quality_grid.children:
            row.style.background_color = t["card_bg"]
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

        self.url_input.style.background_color = t["input_bg"]
        self.url_input.style.color = t["text"]
        self.styler.style_input(
            self.url_input,
            t["input_bg"],
            t["border"],
            t["text"],
            t["hint"],
        )

        for accent_btn in (self.fetch_button, self.download_button):
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
        _enable_android_multiline(self.status_label, max_lines=12)

        self._style_mode_buttons()
        self._style_quality_buttons()

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

    def _style_quality_buttons(self):
        t = THEMES[self.theme_name]
        for row in self.quality_grid.children:
            row.style.background_color = t["card_bg"]
        for value, detail, btn in self.quality_buttons:
            active = value == self.selected_quality
            bg = t["accent"] if active else t["secondary_bg"]
            fg = t["accent_text"] if active else t["secondary_text"]
            bd = t["accent_border"] if active else t["border"]
            btn.style.background_color = bg
            btn.style.color = fg
            self.styler.apply(btn, bg, bd, radius=10, is_button=True)
            if active:
                self.quality_detail_label.text = f"Selected: {detail}"

    async def on_change_folder(self, widget):
        if self.state in ("fetching", "downloading"):
            return

        if is_android():
            try:
                from android.content import Intent

                intent = Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
                intent.addFlags(
                    Intent.FLAG_GRANT_READ_URI_PERMISSION
                    | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                )

                def _on_tree_result(result_code, data):
                    if result_code != -1 or data is None:
                        return
                    uri = data.getData()
                    if uri is None:
                        return
                    mapped = _tree_uri_to_local_path(uri.toString())
                    if mapped is None:
                        self.call_on_ui(
                            self.show_status,
                            "Picked folder is not a local path. Choose Download/YT Downloader.",
                            "warning",
                        )
                        return
                    target = (
                        mapped
                        if mapped.name == APP_NAME
                        else mapped / APP_NAME
                    )
                    self.call_on_ui(self._commit_download_folder, target)

                self._impl.start_activity(intent, on_complete=_on_tree_result)
                return
            except Exception:
                pass

        try:
            dialog = toga.SelectFolderDialog(
                title="Choose download folder",
                initial_directory=self.download_dir,
            )
            chosen = await self.main_window.dialog(dialog)
        except NotImplementedError:
            self.show_status(
                "Folder picker is unavailable here; using Download/YT Downloader.",
                "warning",
            )
            return

        if chosen:
            self._commit_download_folder(chosen)

    def _commit_download_folder(self, new_path: str | Path):
        try:
            folder = set_mobile_download_dir(new_path)
        except (OSError, ValueError) as error:
            self.show_status(str(error), "error")
            return

        self.download_dir = folder
        self._refresh_responsive_text()
        self.show_status("Save folder updated.", "success")

    def _rebuild_visible_sections(self):
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
        if self._raw_status_text:
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
        for _, _, btn in self.quality_buttons:
            btn.enabled = editable
        self.download_button.enabled = editable and self.selected_quality is not None

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
        self._raw_status_text = text
        detected_dp = _android_screen_width_dp(self.status_label)
        if detected_dp:
            self._current_width = detected_dp
        self.status_label.text = _wrap_text(
            text, self._current_width, char_dp=8.4
        )
        palette = THEMES[self.theme_name]["status"]
        self.status_label.style.color = palette.get(level, palette["info"])
        _enable_android_multiline(self.status_label, max_lines=12)
        self.status_label.refresh()
        self._rebuild_visible_sections()
        self.content_box.refresh()

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
            info = (
                _android_fetch_info(url)
                if is_android()
                else fetch_video_info(url)
            )
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

    def _on_quality_pill_pressed(self, value):
        if self.state not in ("ready", "done"):
            return
        self.selected_quality = value
        self._style_quality_buttons()
        self.download_button.enabled = self.selected_quality is not None

    def _populate_quality(self):
        settings = load_settings()
        is_video = self.mode == "video"
        f = self.ui_font
        card_bg = THEMES[self.theme_name]["card_bg"]

        for child in list(self.quality_grid.children):
            self.quality_grid.remove(child)
        self.quality_buttons = []

        # entries: list of (value, short_pill_label, full_detail_label)
        entries: list[tuple[object, str, str]] = []

        if is_video:
            self.quality_caption.text = "VIDEO QUALITY"
            options = self.video_options or [(720, None), (480, None), (360, None)]
            for height, size in options:
                detail = f"{height}p MP4"
                if size:
                    detail += f"  ·  ~{format_file_size(size)}"
                entries.append((height, f"{height}p", detail))
            saved = settings.get("video_quality")
            cols = 3
        else:
            self.quality_caption.text = "AUDIO FORMAT"
            if self.use_ffmpeg:
                choices = list(AUDIO_CHOICES) + (
                    [ORIGINAL_AUDIO] if ORIGINAL_AUDIO not in AUDIO_CHOICES else []
                )
                for choice in choices:
                    short = (
                        "Original M4A"
                        if choice == ORIGINAL_AUDIO
                        else f"{choice} kbps"
                    )
                    entries.append((choice, short, audio_label(choice)))
                cols = 2
            else:
                entries.append(
                    (
                        ORIGINAL_AUDIO,
                        "Original Audio (M4A)",
                        "Original audio (M4A, no conversion)",
                    )
                )
                cols = 1
            saved = settings.get("audio_bitrate")

        values = [val for val, _, _ in entries]
        if saved in values:
            self.selected_quality = saved
        elif values:
            self.selected_quality = values[0]
        else:
            self.selected_quality = None

        for i in range(0, len(entries), cols):
            chunk = entries[i : i + cols]
            row = toga.Box(
                style=Pack(direction=ROW, gap=8, background_color=card_bg)
            )
            for value, short_label, detail in chunk:
                btn = toga.Button(
                    short_label,
                    on_press=lambda _w, v=value: self._on_quality_pill_pressed(v),
                    style=Pack(
                        font_family=f,
                        flex=1,
                        height=38,
                        font_size=11,
                        font_weight=BOLD,
                    ),
                )
                row.add(btn)
                self.quality_buttons.append((value, detail, btn))
            for _ in range(cols - len(chunk)):
                row.add(toga.Box(style=Pack(flex=1, background_color=card_bg)))
            self.quality_grid.add(row)

        self._style_quality_buttons()
        self.download_button.enabled = self.selected_quality is not None
        self.quality_grid.refresh()

    def on_download_pressed(self, widget):
        if self.state not in ("ready", "done") or self.selected_quality is None:
            return

        setting = self.selected_quality
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
            if self.use_ffmpeg:
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
                result = _android_download_media(
                    url,
                    kind,
                    setting,
                    self.download_dir,
                    reporter,
                    cancel,
                    info=info,
                )
        except DownloadError as error:
            self.call_on_ui(self.on_download_failed, str(error))
            return
        except Exception as error:
            logging.getLogger("ytdl").error("Download failed", exc_info=True)
            if _is_dns_or_network_error(error):
                msg = (
                    "Android DNS error (Errno 7): no address associated with hostname. "
                    "Toggle airplane mode or switch Wi-Fi / 5G and try again."
                )
            else:
                msg = friendly_error(error)
            self.call_on_ui(self.on_download_failed, msg)
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
        self.cancel_button.enabled = (
            self.state == "downloading" and "Merging" not in text
        )

    def on_progress(self, downloaded, total, speed, eta):
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
            self.show_status("Download complete.", "success")
        elif result.status == "exists":
            self.show_status("Already downloaded.", "warning")
        else:
            self.show_status("Download cancelled.", "warning")

        self._set_state("done")

    def on_download_failed(self, message: str):
        self.show_status(message, "error")
        self._set_state("ready")


def main():
    return YTDownloaderApp()