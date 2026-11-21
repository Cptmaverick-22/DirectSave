# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

project_root = Path(SPECPATH)
bin_dir = project_root / "bin"

# Bundle Windows external tools placed in ./bin/ by the build workflow.
binaries = []
for tool in ("ffmpeg.exe", "ffprobe.exe", "deno.exe"):
    tool_path = bin_dir / tool
    if tool_path.is_file():
        binaries.append((str(tool_path), "."))

# Preserve yt-dlp package metadata (for updater._installed_version)
# and yt-dlp-ejs JavaScript assets (for YouTube challenge solving).
datas = []
datas += copy_metadata("yt-dlp")
try:
    datas += collect_data_files("yt_dlp_ejs")
except Exception:
    pass

# Use app.ico automatically if you add one to the project root later.
icon_path = project_root / "app.ico"
app_icon = str(icon_path) if icon_path.is_file() else None

a = Analysis(
    ["gui.py"],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        "yt_dlp_ejs",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "rich",
        "tkinter",
        "pytest",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="YTDownloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,        # Disabled: UPX increases Windows Defender false positives
    console=False,    # Windowed GUI app (no black terminal window)
    icon=app_icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="YTDownloader",
)