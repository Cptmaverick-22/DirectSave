"""Download logic for YT Downloader.

This module never prints and never asks questions. Interfaces (terminal, GUI)
pass in a Reporter to receive progress and messages, and a threading.Event
to cancel a download.
"""

from __future__ import annotations

import time
import copy
import errno
import logging
import re
import shutil
import sys
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp
from yt_dlp.utils import DownloadCancelled as _YtdlpCancelled
from yt_dlp.utils import DownloadError as _YtdlpDownloadError
from yt_dlp.utils import UnavailableVideoError

from config import AUDIO_BITRATES, ORIGINAL_AUDIO, get_download_dir

# Messages go to the log file once an interface calls logs.setup_logging().
log = logging.getLogger("ytdl")
log.addHandler(logging.NullHandler())


# --------------------------------------------------------------------------
# Errors and reporting
# --------------------------------------------------------------------------

class DownloadError(Exception):
    """A problem described by a message that is safe to show to the user."""


class PlaylistLinkError(DownloadError):
    """A playlist link was given where a single video was expected."""


class DownloadCancelled(_YtdlpCancelled):
    """Raised inside a progress hook to stop yt-dlp."""


class Reporter:
    """Receives events from the download functions.

    Interfaces subclass this and override what they need. Every method
    does nothing by default.
    """

    def message(self, text, level="info"):
        """A line of information. level is "info", "warning" or "error"."""

    def stage(self, text):
        """A processing step, such as merging or converting."""

    def progress(self, downloaded, total, speed, eta):
        """Overall progress in bytes. total, speed and eta may be None."""

    def progress_done(self):
        """The download phase has ended (finished, failed or cancelled)."""

    def playlist_item(self, number, total, title):
        """A playlist item is about to start."""


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

@dataclass
class VideoSummary:
    title: str
    channel: str
    duration: int | None
    upload_date: str
    thumbnail: str | None


@dataclass
class PlaylistInfo:
    title: str
    entries: list


@dataclass
class DownloadResult:
    status: str                    # "completed", "exists" or "cancelled"
    folder: Path | None = None
    filename: str | None = None    # set when status is "exists"


@dataclass
class PlaylistResult:
    total: int
    folder: Path | None = None
    completed: int = 0
    skipped: int = 0
    failed: list = field(default_factory=list)   # (title, message) pairs
    cancelled: bool = False


# --------------------------------------------------------------------------
# yt-dlp access
# --------------------------------------------------------------------------

class _YtdlpLogger:
    """Sends yt-dlp's own messages to the log instead of the terminal."""

    def debug(self, message):
        log.debug(message)

    def info(self, message):
        log.info(message)

    def warning(self, message):
        log.warning(message)

    def error(self, message):
        log.error(message)


def _ydl(**options):
    """A quiet YoutubeDL instance whose messages go to the log."""

    settings = {"quiet": True, "logger": _YtdlpLogger()}
    settings.update(options)

    return yt_dlp.YoutubeDL(settings)


def _is_cancelled(cancel):
    return cancel is not None and cancel.is_set()


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
VIDEO_ID_PATTERN = re.compile(r"^[0-9A-Za-z_-]{11}$")


def validate_youtube_url(url):
    """Validate supported YouTube URLs."""

    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()

        return (
            parsed.scheme in ("http", "https")
            and (
                hostname == "youtu.be"
                or hostname == "youtube.com"
                or hostname.endswith(".youtube.com")
            )
        )

    except (ValueError, AttributeError):
        return False


def _list_id(url):
    """The playlist ID in a link's query string, or None."""

    try:
        values = parse_qs(urlparse(url).query).get("list")
    except ValueError:
        return None

    return values[0] if values else None


def is_mix_list(list_id):
    """True for YouTube's personalised Mix lists (list=RD...)."""

    return bool(list_id) and list_id.startswith("RD") \
        and not list_id.startswith("RDCLAK")


def has_playlist_and_video(url):
    """True if a link points at a video that is inside a real playlist."""

    if not validate_youtube_url(url):
        return False

    list_id = _list_id(url)

    if not list_id or is_mix_list(list_id):
        return False

    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()

    if hostname == "youtu.be":
        return bool(parsed.path.strip("/"))

    return (
        "v" in parse_qs(parsed.query)
        or parsed.path.startswith(("/shorts/", "/live/"))
    )


def format_duration(seconds):
    """Convert seconds into HH:MM:SS."""

    if seconds is None:
        return "Unknown"

    hours, remainder = divmod(int(seconds), 3600)
    minutes, seconds = divmod(remainder, 60)

    return f"{hours:02}:{minutes:02}:{seconds:02}"


def format_file_size(size):
    """Convert bytes into a readable file size."""

    if size is None:
        return "Unknown"

    size = float(size)

    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"

        size /= 1024

    return f"{size:.1f} TB"


# --------------------------------------------------------------------------
# Readable error messages
# --------------------------------------------------------------------------

DISK_FULL = "The disk is full. Free some space or choose another download folder."
NO_PERMISSION = "No permission to write to the download folder. Choose another folder."
NAME_TOO_LONG = (
    "The file name or folder path is too long. "
    "Use a shorter folder name or location."
)

# Errors raised by the operating system, recognised by their error number.
_ERRNO_MESSAGES = {
    errno.ENOSPC: DISK_FULL,
    errno.EACCES: NO_PERMISSION,
    errno.EPERM: NO_PERMISSION,
    errno.EROFS: NO_PERMISSION,
    errno.ENAMETOOLONG: NAME_TOO_LONG,
}

# Messages from yt-dlp, recognised by their text. The first match wins, so
# more specific rules come before general ones.
_ERROR_RULES = [
    (re.compile(p, re.IGNORECASE), text) for p, text in (
        (r"confirm your age|age[- ]restricted|age verification",
         "This video is age-restricted and needs a signed-in account, "
         "which this app doesn't support yet."),
        (r"not a bot",
         "YouTube is asking to confirm you're not a bot. Wait a while, "
         "update yt-dlp, or try another network."),
        (r"members[- ]only|join this channel",
         "This video is only available to channel members."),
        (r"private video",
         "This video is private."),
        (r"sign in|log in|login required|cookies",
         "This video needs a signed-in account, "
         "which this app doesn't support yet."),
        (r"in your country|geo[- ]?restrict|not made this video available"
         r"|available in your region",
         "This video isn't available in your country."),
        (r"premieres in|live event will begin|is upcoming",
         "This is a premiere or live event that hasn't started yet."),
        (r"copyright",
         "This video was blocked for copyright reasons."),
        (r"requested format is not available|no video formats found"
         r"|only images are available",
         "That quality isn't available for this video. Try another one."),
        (r"unsupported url",
         "That link isn't a video this app can download."),
        (r"http error 403",
         "YouTube refused the download (HTTP 403). This is usually "
         "temporary. Try again in a minute, or use Update yt-dlp in the app."),
        (r"http error 429|too many requests",
         "YouTube says there were too many requests (HTTP 429). "
         "Wait a few minutes and try again."),
        (r"http error 5\d\d",
         "YouTube had a server problem. Try again in a moment."),
        (r"unavailable|no longer available|has been removed|been terminated",
         "This video is unavailable. It may have been removed or made private."),
        (r"javascript runtime|js runtime|n challenge|\bnsig\b"
         r"|signature solving|challenge solving",
         "yt-dlp couldn't solve YouTube's JavaScript check. Make sure Deno "
         "is installed (run: deno --version), then use Update yt-dlp in the "
         "app."),
        (r"no space left|not enough space|disk full|\[errno 28\]",
         DISK_FULL),
        (r"permission denied|\[errno 13\]|read-only file system"
         r"|access is denied",
         NO_PERMISSION),
        (r"file name too long|filename too long|\[errno 36\]|path too long",
         NAME_TOO_LONG),
        (r"ffprobe|ffmpeg|postprocessing",
         "Media conversion failed. Check that FFmpeg is installed "
         "and up to date."),
        (r"timed out|timeout|connection|name or service not known"
         r"|getaddrinfo|temporary failure in name resolution"
         r"|network is unreachable|unable to download (webpage|api)",
         "Network error. Check your internet connection and try again."),
    )
]


def friendly_error(error):
    """Turn an exception into a short, readable message."""

    if isinstance(error, OSError) and error.errno in _ERRNO_MESSAGES:
        return _ERRNO_MESSAGES[error.errno]

    message = ANSI_PATTERN.sub("", str(error)).strip()
    message = re.sub(r"^ERROR:\s*", "", message)

    for pattern, text in _ERROR_RULES:
        if pattern.search(message):
            return text

    return f"Download failed: {message[:300]}"


# --------------------------------------------------------------------------
# Safe file and folder names (Windows, Linux and macOS)
# --------------------------------------------------------------------------

WINDOWS_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

REPLACEMENTS = str.maketrans({
    ":": " -",
    "/": "-",
    "\\": "-",
    "|": "-",
    '"': "'",
    "?": None,
    "*": None,
    "<": None,
    ">": None,
})

MAX_TITLE_BYTES = 150
RESERVED_SUFFIX_BYTES = 45    # " [2160p] [id]" plus temp extensions
WINDOWS_PATH_LIMIT = 259


def sanitize_name(name, max_bytes=MAX_TITLE_BYTES, fallback="untitled"):
    """Make a string safe as a file or folder name on every platform."""

    name = unicodedata.normalize("NFC", str(name or ""))

    chars = (" " if c.isspace() else c for c in name)
    name = "".join(
        c for c in chars if unicodedata.category(c) not in ("Cc", "Cf")
    )

    name = name.translate(REPLACEMENTS)
    name = re.sub(r" +", " ", name).strip(" .")

    name = name.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    name = name.strip(" .")

    if name.split(".")[0].strip().upper() in WINDOWS_RESERVED:
        name = "_" + name

    return name or fallback


def max_title_bytes(directory):
    """Shorten titles on Windows when the folder path is already long."""

    limit = MAX_TITLE_BYTES

    if sys.platform == "win32":
        room = (
            WINDOWS_PATH_LIMIT
            - len(str(directory))
            - 1
            - RESERVED_SUFFIX_BYTES
        )
        limit = min(limit, room)

    return max(limit, 30)


def build_outtmpl(directory, title, video_id, tag):
    """Build a safe name template, e.g. 'Title [720p] [id].%(ext)s'."""

    safe_title = sanitize_name(
        title, max_bytes=max_title_bytes(directory), fallback=video_id
    )

    # A literal % would be read as a template field by yt-dlp.
    safe_title = safe_title.replace("%", "%%")

    return f"{safe_title} {tag} [{video_id}].%(ext)s"


def safe_folder_name(name, fallback="Playlist"):
    """Make a string safe to use as a folder name."""

    return sanitize_name(name, max_bytes=80, fallback=fallback)


# --------------------------------------------------------------------------
# Fetching information
# --------------------------------------------------------------------------

def fetch_video_info(url):
    """Return yt-dlp's information for a single video."""

    if not validate_youtube_url(url):
        raise DownloadError("Invalid YouTube URL.")

    started = time.monotonic()

    try:
        with _ydl(noplaylist=True, skip_download=True) as ydl:
            info = ydl.extract_info(url, download=False)

    except Exception as error:
        log.warning("Could not fetch %s: %s", url, error, exc_info=True)
        raise DownloadError(friendly_error(error)) from error

    log.info(
        "Fetched video information in %.1fs: %s",
        time.monotonic() - started, url,
    )

    if info.get("_type") == "playlist":
        raise PlaylistLinkError(
            "This is a playlist link. Use the playlist option instead."
        )

    if info.get("live_status") in ("is_live", "is_upcoming"):
        raise DownloadError(
            "Live streams and upcoming premieres can't be downloaded. "
            "Try again after the stream has ended."
        )

    return info

def summarize_video(info):
    """Pick out the details an interface shows about a video."""

    upload = info.get("upload_date") or ""

    if len(upload) == 8:
        upload = f"{upload[:4]}-{upload[4:6]}-{upload[6:]}"

    return VideoSummary(
        title=info.get("title") or "Unknown",
        channel=info.get("uploader") or info.get("channel") or "Unknown channel",
        duration=info.get("duration"),
        upload_date=upload or "Unknown",
        thumbnail=info.get("thumbnail"),
    )


def is_video_entry(entry):
    """True for a playlist entry that is a real video."""

    return (
        bool(VIDEO_ID_PATTERN.match(entry.get("id") or ""))
        and entry.get("ie_key") in (None, "Youtube")
    )


def fetch_playlist(url):
    """Return a playlist's title and entries (fast, no per-video details)."""

    if not validate_youtube_url(url):
        raise DownloadError("Invalid YouTube URL.")

    list_id = _list_id(url)

    if is_mix_list(list_id):
        raise DownloadError(
            "YouTube Mix lists are personalised and can't be downloaded as "
            "a playlist. Use a normal playlist link, or download the video "
            "on its own."
        )

    if list_id in ("WL", "LL"):
        raise DownloadError(
            "Watch Later and Liked videos need you to be signed in, so they "
            "can't be downloaded here."
        )

    try:
        with _ydl(extract_flat="in_playlist", skip_download=True) as ydl:
            info = ydl.extract_info(url, download=False)

    except Exception as error:
        log.warning("Could not fetch playlist %s: %s", url, error, exc_info=True)
        raise DownloadError(friendly_error(error)) from error

    found = [entry for entry in (info.get("entries") or []) if entry]
    entries = [entry for entry in found if is_video_entry(entry)]

    log.info(
        "Playlist %s: %d entries, %d videos", url, len(found), len(entries)
    )

    if not entries:
        if found:
            raise DownloadError(
                "This looks like a channel or a page, not a playlist. "
                "Open one of its playlists and paste that link."
            )

        raise DownloadError("No videos found. Is this a playlist link?")

    return PlaylistInfo(title=info.get("title") or "Playlist", entries=entries)


def parse_range(text, total):
    """Turn a selection like '1-3, 5, 8-10' into a set of 1-based numbers.

    An empty text selects everything. '-4' means 1-4 and '7-' means 7 to the
    end. Returns None if any part is invalid or outside 1..total.
    """

    text = text.strip()

    if not text:
        return set(range(1, total + 1))

    indices = set()

    for part in text.split(","):
        part = part.strip()

        if not part:
            continue

        try:
            if "-" in part:
                first, last = part.split("-", 1)
                start = int(first) if first.strip() else 1
                end = int(last) if last.strip() else total
            else:
                start = end = int(part)

        except ValueError:
            return None

        if start < 1 or start > total or start > end:
            return None

        indices.update(range(start, min(end, total) + 1))

    return indices or None


# --------------------------------------------------------------------------
# Qualities and size estimates
# --------------------------------------------------------------------------

# Leftovers from yt-dlp's default format choice that must not
# influence a new selection.
STALE_FORMAT_KEYS = (
    "requested_formats", "requested_downloads", "format", "format_id",
    "format_note", "ext", "url", "filesize", "filesize_approx", "tbr",
    "vbr", "abr", "asr", "fps", "width", "height", "resolution",
    "vcodec", "acodec", "dynamic_range", "protocol", "container",
    "language", "http_headers", "fragments",
)


NOTE_PATTERN = re.compile(r"^(\d{3,4})p")


def resolution_label(video_format):
    """The resolution YouTube calls a format, such as 1080 for '1080p60'.

    YouTube's own label is used when there is one. Otherwise the shorter
    side of the picture is used.
    """

    match = NOTE_PATTERN.match(video_format.get("format_note") or "")

    if match:
        return int(match.group(1))

    height = video_format.get("height")
    width = video_format.get("width")

    if height and width:
        return min(height, width)

    return height or None


def available_qualities(info):
    """Find available MP4 video resolutions, highest first.

    YouTube's labelled formats are used when there are any. Extra variants
    without a label (such as the HLS ones) are ignored then, so they don't
    add odd entries like "128p".
    """

    labelled, guessed = set(), set()

    for video_format in info.get("formats", []):
        if (
            video_format.get("ext") != "mp4"
            or video_format.get("vcodec") in (None, "none")
        ):
            continue

        label = resolution_label(video_format)

        if not label:
            continue

        has_note = NOTE_PATTERN.match(video_format.get("format_note") or "")
        (labelled if has_note else guessed).add(label)

    return sorted(labelled or guessed, reverse=True)


def video_selection(quality, ceiling=False):
    """yt-dlp options that pick the streams for a resolution.

    "res" compares the picture size YouTube advertises, so wide and tall
    videos work like normal ones. "proto" puts direct downloads ahead of
    HLS streaming variants, and "vcodec:h264" then prefers H.264, which
    plays almost everywhere. YouTube has no H.264 above 1080p, so there
    the direct AV1 stream is used.
    """

    spec = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]"

    if ceiling:
        # Fallback for videos that have no MP4 streams.
        spec += "/bestvideo+bestaudio/best"

    return {
        "format": spec,
        "format_sort": [f"res:{quality}", "proto", "vcodec:h264"],
    }

def _without_selection(info):
    """A copy of the info without yt-dlp's earlier format choice."""

    clean = copy.deepcopy(info)

    for key in STALE_FORMAT_KEYS:
        clean.pop(key, None)

    return clean


def select_formats(info, selection):
    """What yt-dlp would pick for a selection, or None if it can't pick.

    Only used for estimates, so the formats are not test-downloaded.
    """

    try:
        with _ydl(noplaylist=True, check_formats=False, **selection) as ydl:
            return ydl.process_ie_result(
                _without_selection(info), download=False
            )
    except Exception:
        log.debug("Format selection failed for %s", selection, exc_info=True)
        return None
    
def _streams_size(selected):
    """Combined size in bytes of a selection's streams, or None."""

    streams = selected.get("requested_formats") or [selected]
    duration = selected.get("duration")
    total = 0

    for stream in streams:
        size = stream.get("filesize") or stream.get("filesize_approx")

        # Fall back to bitrate x duration (tbr is in kbit/s).
        if not size and stream.get("tbr") and duration:
            size = stream["tbr"] * 125 * duration

        if not size:
            return None  # A partial total would be misleading.

        total += size

    return int(total)


def estimate_size(info, selection):
    """Estimated download size in bytes for a selection, or None."""

    selected = select_formats(info, selection)

    return _streams_size(selected) if selected else None


def quality_options(info):
    """List (resolution, estimated_size_or_None) for each available quality."""

    started = time.monotonic()

    options = [
        (quality, estimate_size(info, video_selection(quality)))
        for quality in available_qualities(info)
    ]

    log.info(
        "Estimated sizes for %d qualities in %.1fs",
        len(options), time.monotonic() - started,
    )

    return options


def _video_tag(selected, requested):
    """The '[1080p]' part of a file name, from the picture actually chosen."""

    selected = selected or {}
    label = None

    for stream in selected.get("requested_formats") or [selected]:
        if stream.get("vcodec") not in (None, "none"):
            label = resolution_label(stream)
            break

    return f"[{label or requested}p]"

# --------------------------------------------------------------------------
# Files and folders
# --------------------------------------------------------------------------

# Files yt-dlp leaves behind when a download stops part-way: partial and
# resume files, separate streams (.f137.mp4), conversion files (.temp.mp3)
# and thumbnails.
TEMP_FILE_PATTERN = re.compile(
    r"(\.part(-Frag\d+)?"
    r"|\.ytdl"
    r"|\.temp\.\w+"
    r"|\.f[\w-]+\.\w+(\.part)?"
    r"|\.(webp|jpg|jpeg|png))$"
)

# Disk space needed, as a multiple of the estimated download size. A video
# needs room for its separate streams and the merged file at the same time.
SPACE_FACTOR = {"video": 2.1, "audio": 1.6}
SPACE_SLACK = 50 * 1024 * 1024


def get_output_directory(output_dir=None):
    """Resolve, validate, and create the destination directory."""

    directory = (
        Path(output_dir).expanduser().resolve()
        if output_dir is not None
        else get_download_dir()
    )

    if directory.exists() and not directory.is_dir():
        raise NotADirectoryError(
            f"Download location is not a directory: {directory}"
        )

    directory.mkdir(parents=True, exist_ok=True)

    return directory


def _check_disk_space(folder, expected_size, kind):
    """Raise DownloadError if the download clearly won't fit on the disk."""

    if not expected_size:
        return

    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        return

    needed = int(expected_size * SPACE_FACTOR[kind]) + SPACE_SLACK

    if free < needed:
        raise DownloadError(
            f"Not enough free disk space in {folder}: about "
            f"{format_file_size(needed)} needed, "
            f"{format_file_size(free)} available."
        )


def find_existing_download(output_dir, video_id, extension, quality_label=None):
    """Find an earlier download of this video.

    extension is one extension or a list of them. With a quality_label, only
    that quality matches. Without one, any quality of the video matches.
    """

    extensions = (extension,) if isinstance(extension, str) else tuple(extension)
    suffixes = []

    for ext in extensions:
        if quality_label:
            suffixes.append(f"[{quality_label}] [{video_id}].{ext}")
            suffixes.append(f"[{video_id}] [{quality_label}].{ext}")
        else:
            suffixes.append(f"[{video_id}].{ext}")

    suffixes = tuple(suffixes)

    for file in output_dir.iterdir():
        if file.is_file() and file.name.endswith(suffixes):
            return file

    return None

def cleanup_incomplete_files(output_dir, video_id):
    """Remove leftover files of one video. Returns the removed names."""

    removed = []

    if output_dir is None or not video_id:
        return removed

    marker = f"[{video_id}]"

    try:
        for file in output_dir.iterdir():
            if (
                file.is_file()
                and marker in file.name
                and TEMP_FILE_PATTERN.search(file.name)
            ):
                try:
                    file.unlink()
                    removed.append(file.name)
                except OSError:
                    pass

    except OSError:
        pass

    return removed


# --------------------------------------------------------------------------
# Downloading
# --------------------------------------------------------------------------

class ProgressTracker:
    """yt-dlp progress hook: one overall progress across all streams."""

    def __init__(self, reporter, expected_total=None, cancel=None):
        self.reporter = reporter
        self.expected_total = expected_total
        self.cancel = cancel
        self.streams = {}

    def __call__(self, d):
        if _is_cancelled(self.cancel):
            raise DownloadCancelled()

        status = d.get("status")
        key = d.get("filename") or "stream"

        if status == "finished":
            size = d.get("total_bytes") or d.get("downloaded_bytes")

            if size:
                self.streams[key] = size

        elif status == "downloading":
            self.streams[key] = d.get("downloaded_bytes") or 0

        else:
            return

        downloaded = sum(self.streams.values())

        if self.expected_total:
            self.expected_total = max(self.expected_total, downloaded)
            total = self.expected_total
        else:
            # Unknown total: fall back to the current stream's numbers.
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = self.streams[key]

        speed = d.get("speed")
        eta = (
            max(0.0, (total - downloaded) / speed)
            if total and speed
            else None
        )

        self.reporter.progress(downloaded, total, speed, eta)


def _postprocessor_hook(reporter):
    messages = {
        "Merger": "Merging video and audio...",
        "ExtractAudio": "Converting to MP3...",
        "FFmpegExtractAudio": "Converting to MP3...",
    }
    shown = set()

    def hook(d):
        if d.get("status") != "started":
            return

        text = messages.get(d.get("postprocessor", ""))

        if text and text not in shown:
            shown.add(text)
            reporter.stage(text)

    return hook


RETRY_DELAYS = (3, 8)   # seconds to wait before the 2nd and 3rd attempt
RETRYABLE = re.compile(r"HTTP Error (403|429|5\d\d)")


def _run_once(options, url, info):
    """Download once.

    With info, the video isn't extracted again, which saves several seconds.
    If reusing the information fails unexpectedly, fall back to the normal way.
    """

    with _ydl(**options) as ydl:
        if info is not None:
            try:
                ydl.process_ie_result(_without_selection(info), download=True)
                return

            except UnavailableVideoError as error:
                ydl.report_error(error)   # Raises yt-dlp's DownloadError.

            except (KeyError, TypeError, AttributeError, ValueError):
                log.warning(
                    "Could not reuse the fetched information; "
                    "extracting again",
                    exc_info=True,
                )

        if ydl.download([url]):
            raise DownloadError("Download failed. See the log for details.")


def _run_with_retries(options, url, reporter, cancel, info=None):
    """Run yt-dlp, retrying temporary HTTP errors.

    The first attempt reuses the already fetched information. A retry
    extracts again, because the download links may be what failed.
    Partial files are kept, so a retry resumes where it stopped.
    """

    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            _run_once(options, url, info if attempt == 0 else None)
            return

        except _YtdlpDownloadError as error:
            match = RETRYABLE.search(ANSI_PATTERN.sub("", str(error)))

            if not match or attempt == len(RETRY_DELAYS):
                raise

            delay = RETRY_DELAYS[attempt]

            log.warning(
                "HTTP %s for %s; retrying in %s seconds (attempt %d)",
                match.group(1), url, delay, attempt + 2,
            )
            reporter.message(
                f"Temporary error (HTTP {match.group(1)}). "
                f"Retrying in {delay} seconds...",
                "warning",
            )

            if (cancel or threading.Event()).wait(delay):
                raise DownloadCancelled()

def _check_request(url, kind, setting):
    """Raise DownloadError if the request can't be carried out."""

    if not validate_youtube_url(url):
        raise DownloadError("Invalid YouTube URL.")

    if kind == "video":
        if (
            not isinstance(setting, int)
            or isinstance(setting, bool)
            or setting <= 0
        ):
            raise DownloadError("Invalid video resolution.")

    elif kind == "audio":
        if setting != ORIGINAL_AUDIO and setting not in AUDIO_BITRATES:
            raise DownloadError("Unsupported audio quality.")

    else:
        raise DownloadError("Unsupported download type.")

    if not shutil.which("ffmpeg"):
        raise DownloadError("FFmpeg is required but was not found.")


def _plan(kind, setting, ceiling):
    """What to download and how, for a video or an audio request."""

    if kind == "video":
        return {
            "extension": "mp4",
            "quality_label": None if ceiling else f"{setting}p",
            "description": (
                f"MP4 (up to {setting}p)" if ceiling else f"MP4 ({setting}p)"
            ),
            "selection": video_selection(setting, ceiling),
            # In ceiling mode the name uses the resolution actually chosen.
            "tag": None if ceiling else f"[{setting}p]",
            "options": {"merge_output_format": "mp4"},
        }

    # Tags and cover art, for both kinds of audio.
    finishing = [
        {"key": "FFmpegMetadata", "add_metadata": True},
        {
            "key": "FFmpegThumbnailsConvertor",
            "format": "jpg",
            "when": "before_dl",
        },
        {"key": "EmbedThumbnail"},
    ]

    if setting == ORIGINAL_AUDIO:
        return {
            "extension": "m4a",
            "quality_label": ORIGINAL_AUDIO,
            "description": "original audio (M4A, no conversion)",
            "selection": {"format": "bestaudio[ext=m4a]"},
            "tag": f"[{ORIGINAL_AUDIO}]",
            "options": {
                "writethumbnail": True,
                "postprocessors": finishing,
            },
        }

    return {
        "extension": "mp3",
        "quality_label": f"{setting}kbps",
        "description": f"MP3 ({setting} kbps)",
        "selection": {"format": "bestaudio/best"},
        "tag": f"[{setting}kbps]",
        "options": {
            "writethumbnail": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": str(setting),
                },
                *finishing,
            ],
        },
    }

def _existing_result(folder, video_id, plan):
    """A DownloadResult("exists") if this was downloaded before, else None."""

    if not video_id:
        return None

    existing = find_existing_download(
        folder, video_id, plan["extension"], plan["quality_label"]
    )

    if existing:
        log.debug("Already downloaded: %s", existing.name)
        return DownloadResult("exists", folder, existing.name)

    return None


def _download(
    url, kind, setting, output_dir, info, ceiling, reporter, cancel,
    video_id=None,
):
    """Download one video ("video") or audio track ("audio").

    A known video_id lets a finished item be skipped without any network
    request.
    """

    reporter = reporter or Reporter()

    _check_request(url, kind, setting)

    plan = _plan(kind, setting, ceiling)
    folder = None

    try:
        folder = get_output_directory(output_dir)

        if _is_cancelled(cancel):
            return DownloadResult("cancelled", folder)

        video_id = video_id or (info or {}).get("id")

        found = _existing_result(folder, video_id, plan)

        if found:
            return found

        if info is None:
            info = fetch_video_info(url)
            video_id = info.get("id")

            found = _existing_result(folder, video_id, plan)

            if found:
                return found

        if not video_id:
            raise DownloadError("Could not identify this video.")

        log.info(
            "Downloading %s (%s) from %s into %s",
            kind, setting, url, folder,
        )
        reporter.message(f"Downloading {plan['description']}...")

        selected = select_formats(info, plan["selection"])
        expected = _streams_size(selected) if selected else None
        _check_disk_space(folder, expected, kind)

        tag = plan["tag"] or _video_tag(selected, setting)
        tracker = ProgressTracker(reporter, expected, cancel)

        options = {
            **plan["selection"],
            "paths": {"home": str(folder)},
            "outtmpl": build_outtmpl(folder, info.get("title"), video_id, tag),
            "windowsfilenames": True,
            "noplaylist": True,
            "noprogress": True,
            "nooverwrites": True,
            "continuedl": True,
            "retries": 5,
            "fragment_retries": 10,
            "extractor_retries": 3,
            "socket_timeout": 30,
            "progress_hooks": [tracker],
            "postprocessor_hooks": [_postprocessor_hook(reporter)],
        }
        options.update(plan["options"])

        started = time.monotonic()

        try:
            _run_with_retries(options, url, reporter, cancel, info)
        finally:
            reporter.progress_done()

        log.info(
            "Completed: %s in %.1fs", url, time.monotonic() - started
        )

        return DownloadResult("completed", folder)

    except (DownloadCancelled, KeyboardInterrupt):
        removed = cleanup_incomplete_files(folder, video_id)

        log.info("Cancelled: %s (removed %d leftover files)", url, len(removed))

        if removed:
            reporter.message(
                f"Removed {len(removed)} incomplete file(s).", "warning"
            )

        return DownloadResult("cancelled", folder)

    except DownloadError as error:
        log.warning("Stopped: %s (%s)", error, url)
        raise

    except Exception as error:
        log.error("Download failed for %s", url, exc_info=True)
        raise DownloadError(friendly_error(error)) from error


def download_video(
    url, quality, output_dir=None, info=None, ceiling=False,
    reporter=None, cancel=None,
):
    """Download and merge video and audio into an MP4 file.

    With ceiling=True, quality is a maximum height rather than an exact one.
    Returns a DownloadResult, or raises DownloadError.
    """

    return _download(
        url, "video", quality, output_dir, info, ceiling, reporter, cancel
    )


def download_audio(
    url, bitrate, output_dir=None, info=None, reporter=None, cancel=None,
):
    """Download audio with cover art and tags.

    bitrate is an MP3 bitrate (such as 192), or ORIGINAL_AUDIO to save
    YouTube's audio as it is, without converting it.
    Returns a DownloadResult, or raises DownloadError.
    """

    return _download(
        url, "audio", bitrate, output_dir, info, False, reporter, cancel
    )


def download_playlist(
    entries, kind, setting, folder_name, reporter=None, cancel=None,
):
    """Download playlist entries one by one into their own folder.

    kind is "video" (setting = maximum height) or "audio" (setting = bitrate).
    A failing item is recorded and the playlist continues.
    """

    reporter = reporter or Reporter()

    try:
        folder = get_output_directory() / safe_folder_name(folder_name)
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        log.error("Could not create playlist folder", exc_info=True)
        raise DownloadError(friendly_error(error)) from error

    log.info("Playlist of %d items into %s", len(entries), folder)
    reporter.message(f"Folder: {folder}")

    result = PlaylistResult(total=len(entries), folder=folder)

    for number, entry in enumerate(entries, start=1):
        if _is_cancelled(cancel):
            result.cancelled = True
            break

        video_id = entry.get("id")
        title = entry.get("title") or video_id or f"Item {number}"

        reporter.playlist_item(number, result.total, title)

        if not video_id:
            reporter.message(f"{title}: missing video ID.", "error")
            result.failed.append((title, "Missing video ID."))
            continue

        try:
            outcome = _download(
                f"https://www.youtube.com/watch?v={video_id}",
                kind, setting, folder, None, True, reporter, cancel,
                video_id=video_id,
            )

        except DownloadError as error:
            reporter.message(str(error), "error")
            result.failed.append((title, str(error)))
            continue

        if outcome.status == "completed":
            result.completed += 1

        elif outcome.status == "exists":
            result.skipped += 1
            reporter.message(
                f"Already downloaded: {outcome.filename}", "warning"
            )

        else:
            result.cancelled = True
            break

    log.info(
        "Playlist done: %d completed, %d skipped, %d failed%s",
        result.completed, result.skipped, len(result.failed),
        " (cancelled)" if result.cancelled else "",
    )

    return result