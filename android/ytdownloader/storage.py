"""Platform-safe storage paths for the Android / Toga interface."""

from pathlib import Path
import sys

import config

ANDROID_INTERNAL_ROOT = Path("/storage/emulated/0")


def is_android() -> bool:
    return sys.platform == "android" or hasattr(sys, "getandroidapilevel")


def _is_writable_dir(path: Path) -> bool:
    """Create path if needed and verify we can actually write a file inside."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def configure_mobile_paths(app) -> Path:
    """Point config.py at app-writable storage and return the download dir."""
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
    if not _is_writable_dir(target):
        raise OSError(f"Cannot write to {target}. Check storage permissions.")

    config.remember_setting("download_dir", str(target))
    return target


def _resolve_default_download_dir(fallback_root: Path) -> Path:
    """Default download folder: Internal Storage / YT Downloader."""
    folder_name = config.APP_NAME  # "YT Downloader"

    if is_android():
        candidates = [
            ANDROID_INTERNAL_ROOT / folder_name,
            ANDROID_INTERNAL_ROOT / "Download" / folder_name,
        ]
        for candidate in candidates:
            if _is_writable_dir(candidate):
                return candidate

    return fallback_root / folder_name


def android_preset_dirs() -> list[tuple[str, Path]]:
    """Quick-pick Android folders for the inline path editor."""
    folder_name = config.APP_NAME
    return [
        ("Internal Storage", ANDROID_INTERNAL_ROOT / folder_name),
        ("Downloads", ANDROID_INTERNAL_ROOT / "Download" / folder_name),
    ]


def friendly_path(path: Path) -> str:
    """Format a storage path cleanly for display."""
    text = str(path)
    root_prefix = str(ANDROID_INTERNAL_ROOT)
    if text.startswith(root_prefix):
        relative = text[len(root_prefix):].lstrip("/")
        return f"Internal Storage / {relative}" if relative else "Internal Storage"
    return text