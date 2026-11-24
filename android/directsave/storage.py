"""Platform-safe storage paths for the Android / Toga interface."""

from __future__ import annotations

from pathlib import Path
import sys

import config

ANDROID_INTERNAL_ROOT = Path("/storage/emulated/0")


def is_android() -> bool:
    """Return True when running inside an Android runtime."""
    return sys.platform == "android" or hasattr(sys, "getandroidapilevel")


def _is_writable_dir(path: Path) -> bool:
    """Create a directory if needed and verify write access via a temp probe."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def configure_mobile_paths(app) -> Path:
    """Point config.py at app-writable storage and return the active download dir."""
    data_dir = Path(app.paths.data)
    data_dir.mkdir(parents=True, exist_ok=True)

    config.CONFIG_DIR = data_dir
    config.SETTINGS_FILE = data_dir / "settings.json"

    saved = config.load_settings().get("download_dir")
    if saved:
        saved_path = Path(saved)
        if _is_writable_dir(saved_path):
            return saved_path

    download_dir = _resolve_default_download_dir(data_dir)
    download_dir.mkdir(parents=True, exist_ok=True)
    config.remember_setting("download_dir", str(download_dir))
    return download_dir


def set_mobile_download_dir(new_path: str | Path) -> Path:
    """Validate, create, and persist a user-selected download folder."""
    raw = str(new_path).strip()
    if not raw:
        raise ValueError("Folder path cannot be empty.")

    target = Path(raw).expanduser()
    if target.exists() and not target.is_dir():
        raise ValueError("Selected path is a file, not a folder.")

    try:
        writable = _is_writable_dir(target)
    except OSError as exc:
        writable = False
        last_exc = exc
    else:
        last_exc = None

    if not writable:
        if is_android() and str(target).startswith(str(ANDROID_INTERNAL_ROOT)):
            raise PermissionError(
                f"Android blocked writing to that folder. Pick Download/{config.APP_NAME} "
                "or grant All files access in App Settings."
            ) from last_exc
        raise PermissionError(
            "Cannot write to the selected folder. Choose another folder."
        ) from last_exc

    config.remember_setting("download_dir", str(target))
    return target


def _resolve_default_download_dir(fallback_root: Path) -> Path:
    """Resolve a reliable default download folder on Android and desktop."""
    folder_name = config.APP_NAME

    if is_android():
        candidates = [
            ANDROID_INTERNAL_ROOT / "Download" / folder_name,
            ANDROID_INTERNAL_ROOT / folder_name,
            fallback_root / folder_name,
        ]
        for candidate in candidates:
            if _is_writable_dir(candidate):
                return candidate

    return fallback_root / folder_name


def android_preset_dirs() -> list[tuple[str, Path]]:
    """Return reliable quick-pick folders for Android and desktop previews."""
    folder_name = config.APP_NAME
    if is_android():
        return [
            ("Download folder", ANDROID_INTERNAL_ROOT / "Download" / folder_name),
            ("Internal root", ANDROID_INTERNAL_ROOT / folder_name),
        ]
    return [
        ("Downloads folder", Path.home() / "Downloads" / folder_name),
    ]


def friendly_path(path: Path) -> str:
    """Format a storage path cleanly for display."""
    text = str(path)
    root_prefix = str(ANDROID_INTERNAL_ROOT)
    if text.startswith(root_prefix):
        relative = text[len(root_prefix) :].lstrip("/")
        return f"Internal Storage / {relative}" if relative else "Internal Storage"
    return text