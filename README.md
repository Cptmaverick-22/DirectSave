<p align="center">
  <img src="android/directsave/resources/icon.png" alt="DirectSave Logo" width="96" height="96" />
</p>

# DirectSave

[![Latest Release](https://img.shields.io/github/v/release/Cptmaverick-22/DirectSave?style=flat-square&color=7464FF)](https://github.com/Cptmaverick-22/DirectSave/releases/latest)
[![Platform](https://img.shields.io/badge/platform-Android%20%7C%20Windows%20%7C%20Linux%20%7C%20macOS-171A27?style=flat-square)](https://github.com/Cptmaverick-22/DirectSave/releases)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square)](https://www.python.org/)
[![License: GPL v3+](https://img.shields.io/badge/license-GPL--3.0%2B-10B981?style=flat-square)](LICENSE)

**Save what you love** — a clean, fast, open-source video and audio downloader powered by `yt-dlp`. Available as a **signed Android APK**, a **standalone Windows desktop installer**, and a **cross-platform Python GUI & terminal app**.

---

## Quick Downloads

| Platform | Download | Notes |
| :--- | :--- | :--- |
| **Android** | [`DirectSave-Android.apk`](https://github.com/Cptmaverick-22/DirectSave/releases/latest) | Signed release APK (`com.maverick.directsave`, no FFmpeg required) |
| **Windows** | [`DirectSave-Setup-1.4.exe`](https://github.com/Cptmaverick-22/DirectSave/releases/latest) | Standalone installer with FFmpeg, FFprobe & Deno bundled |
| **Linux / macOS** | [Run from source](#linux--macos-from-source) | Qt desktop GUI (`gui.py`) & Rich terminal UI (`main.py`) |

---

## Features

### Android App (BeeWare Toga)
- **Exact Quality Video Downloads (`144p` – `2160p` 4K):** Fetches exact resolutions with estimated file sizes and merges separate MP4 video + M4A audio streams natively on-device using Android's `MediaMuxer`.
- **Direct Audio Extraction:** Fast, original-quality `M4A` audio downloads without slow re-encoding.
- **One-Tap Paste & Fetch:** Reads links directly from your clipboard and starts fetching metadata in a single tap.
- **Playlist Support (Video & Audio):** Automatically detects playlist links, lists all entries with **Select All / Clear Selection**, supports both MP4 (with max-resolution ceiling) and M4A audio, and saves neatly into a playlist subfolder.
- **Download History & Native Playback:** Built-in **History** tab (with **All / Video / Audio** filters) keeps track of downloaded media with one-tap **Play Video** / **Play Audio** launching your phone's native media player.
- **Four Curated Themes:** Cycle between **Midnight**, **Lavender**, **Ocean**, and **Obsidian** themes with synced Android ActionBar and system bar styling, time-based greeting header, and high-contrast quality pills.
- **Custom Save Location:** Saves to `Internal Storage / Download/DirectSave` by default, with a built-in Android folder picker.

### Desktop App (Windows, Linux, macOS)
- **Qt Desktop GUI & Rich Terminal UI:** Choose between a full graphical interface (`gui.py`) or a sleek terminal interface (`main.py`) powered by the exact same download core.
- **Full Format & MP3 Support:** Merges high-resolution streams via FFmpeg and converts audio to `128`, `192`, `256`, or `320 kbps` MP3—or keeps the original audio stream.
- **Smart Playlist Handling:** Supports range selection (e.g., `1-5, 8`), ceiling resolution limits, automatic skipping of already-downloaded files, and organized playlist subfolders.
- **In-App `yt-dlp` Self-Updater:** One-click updates from PyPI without needing to reinstall the app.
- **Quality-of-Life Extras:** Four themes (**Midnight**, **Lavender**, **Ocean**, **Obsidian**), one-click clipboard paste, desktop completion notifications, persistent user preferences, and rotating diagnostic logs.

---

## Installation

### Android (Signed APK)
1. Open the [**Latest Release**](https://github.com/Cptmaverick-22/DirectSave/releases/latest) page on your phone.
2. Download **`DirectSave-Android.apk`**.
3. Tap the downloaded APK to install (enable **Install unknown apps** for your browser or file manager if prompted). Since `v1.4` uses a new package name (`com.maverick.directsave`), perform a fresh install.

> **Automatic Updates on Android:** You can track new releases automatically with [Obtainium](https://github.com/ImranR98/Obtainium) by adding `https://github.com/Cptmaverick-22/DirectSave`.

---

### Windows (Standalone Installer)
1. Open the [**Latest Release**](https://github.com/Cptmaverick-22/DirectSave/releases/latest) page.
2. Download and run **`DirectSave-Setup-1.4.exe`**.
3. **FFmpeg**, **FFprobe**, and **Deno** are bundled automatically—no additional setup required.

> **Windows SmartScreen Note:** Because the installer is not EV code-signed, Windows may display *"Windows protected your PC"*. Click **More info → Run anyway** to continue.

---

### Linux / macOS (From Source)

#### Prerequisites
- **Python 3.10+**
- **FFmpeg** (required for HD video/audio merging and MP3 conversion)
- **Deno** (recommended JS runtime for extraction)

Install prerequisites on Linux:
```bash
# Fedora
sudo dnf install python3 ffmpeg

# Ubuntu / Debian
sudo apt update && sudo apt install python3 python3-venv ffmpeg

# Arch
sudo pacman -S python ffmpeg
```

#### Clone & Run
```bash
git clone https://github.com/Cptmaverick-22/DirectSave.git
cd DirectSave
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Launch Desktop GUI
python gui.py

# Or launch Terminal Interface
python main.py
```

---

## For Developers

### Project Structure
```text
DirectSave/
├── android/
│   └── directsave/
│       ├── app.py                        # Android UI (BeeWare Toga + native MediaMuxer)
│       ├── storage.py                    # Android storage & path helpers
│       └── resources/icon.png            # Android launcher icon
├── .github/workflows/
│   ├── build-android.yml                 # Signed Android APK build & GitHub Release workflow
│   └── build-windows.yml                 # Windows PyInstaller + Inno Setup Release workflow
├── gui.py                                # Desktop GUI app (Qt)
├── themes.py                             # Desktop themes (Midnight, Lavender, Ocean, Obsidian)
├── main.py                               # Terminal app (Rich)
├── downloader.py                         # Shared core: fetching, downloading, playlists
├── updater.py                            # In-app yt-dlp updater (stdlib only)
├── config.py                             # Unified app version, presets, config dir & settings
├── logs.py                               # Rotating logs & diagnostics
├── desktop.py                            # Desktop notifications & folder actions
├── pyproject.toml                        # BeeWare Briefcase Android configuration
├── directsave.spec                       # PyInstaller Windows build specification
├── installer.iss                         # Inno Setup Windows installer script
├── requirements.txt
├── requirements-dev.txt
├── LICENSE                               # GPL-3.0+
├── README.md
└── tests/
```

### Architecture Notes
| Module | Responsibility |
| :--- | :--- |
| `downloader.py` | UI-agnostic download engine; frontends pass a `Reporter` and a `threading.Event` cancel flag. |
| `android/directsave/app.py` | Mobile Toga UI featuring native Android `MediaMuxer` merging, playlist selection, and history playback. |
| `gui.py` / `main.py` | Desktop Qt GUI and Rich CLI interfaces built over `downloader.py`. |
| `updater.py` | Fetches `yt-dlp` updates from PyPI and injects them ahead of bundled imports. |
| `config.py` | Single source of truth for `APP_VERSION`, quality presets, `directsave` config path, and user preferences. |

> **Entry-Point Rule:** In desktop entry points (`gui.py` and `main.py`), `updater.activate()` must be called **before** any module imports `yt_dlp`.

### Local Lint & Tests
```bash
pip install -r requirements-dev.txt
python -m pyflakes *.py android/directsave/app.py
python -m unittest discover tests -v
```

### Unified Release Workflow (CI/CD)
Both platforms are built and attached automatically via GitHub Actions when a version tag is pushed:
1. Update `APP_VERSION` in `config.py` (e.g., `"1.4"`) and `version` in `pyproject.toml` (e.g., `"1.4.0"`).
2. Commit and push to `main`.
3. Tag and push:
   ```bash
   git tag v1.4
   git push origin v1.4
   ```
GitHub Actions automatically builds, signs `DirectSave-Android.apk` (using repository signing secrets), compiles `DirectSave-Setup-<version>.exe`, and attaches both assets to the GitHub Release.

---

## Legal & Responsible Use

This software is intended solely for downloading content that you own, have explicit permission to download, or are otherwise legally permitted to save. Please respect copyright laws, platform Terms of Service, and content creators' rights.

## License

Licensed under the **GNU General Public License v3.0 or later (`GPL-3.0+`)**. See [LICENSE](LICENSE) for full terms.