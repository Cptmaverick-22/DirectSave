import threading

import updater

updater.activate()   # Must run before anything imports yt-dlp.

from updater import UpdateError

import shutil
import sys
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from config import (
    APP_NAME,
    APP_VERSION,
    AUDIO_CHOICES,
    audio_label,
    PLAYLIST_QUALITIES,
    get_download_dir,
    load_settings,
    remember_setting,
    save_download_dir,
)

from desktop import notify, open_folder

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
    has_playlist_and_video,
    parse_range,
    quality_options,
    summarize_video,
)

from logs import LOG_FILE, diagnostics_text, setup_logging

console = Console()


# --------------------------------------------------------------------------
# Terminal display of download events
# --------------------------------------------------------------------------

class RichReporter(Reporter):
    """Shows download events using Rich."""

    STYLES = {"info": None, "warning": "yellow", "error": "red"}

    def __init__(self):
        self.bar = None
        self.task = None

    def message(self, text, level="info"):
        console.print(
            escape(text), style=self.STYLES.get(level), highlight=False
        )

    def stage(self, text):
        console.print(f"[yellow]{escape(text)}[/]")

    def playlist_item(self, number, total, title):
        console.print(
            f"\n[bold cyan]({number}/{total})[/] {escape(title)}"
        )

    def progress(self, downloaded, total, speed, eta):
        if self.bar is None:
            self.bar = Progress(
                TextColumn("[cyan]Downloading"),
                BarColumn(bar_width=30),
                TextColumn("{task.percentage:>5.1f}%"),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=console,
                transient=True,
            )
            self.bar.start()
            self.task = self.bar.add_task("download", total=total)

        self.bar.update(self.task, completed=downloaded, total=total)

    def progress_done(self):
        if self.bar is not None:
            self.bar.stop()
            self.bar = None


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def confirm(prompt):
    return input(f"{prompt} (y/n): ").strip().lower() == "y"


def show_error(error):
    console.print(f"\n[red]{escape(str(error))}[/]")


def check_requirements():
    """Warn about missing tools."""

    for tool in ("ffmpeg", "deno"):
        if not shutil.which(tool):
            console.print(f"[yellow]Warning: {tool} was not found.[/]")


def offer_open_folder(folder=None):
    if not confirm("Open download folder?"):
        return

    if not open_folder(folder or get_download_dir()):
        console.print("[yellow]Could not open the folder.[/]")


def select_option(options, label, default=None):
    hint = f" [Enter = {default}]" if default in options else ""

    while True:
        choice = input(
            f"\nSelect {label}{hint} (Q to cancel): "
        ).strip().lower()

        if choice == "q":
            return None

        if choice == "" and default in options:
            return default

        if choice.isdigit():
            index = int(choice)

            if 1 <= index <= len(options):
                return options[index - 1]

        console.print("[red]Invalid selection. Try again.[/]")


def get_url():
    url = input("\nYouTube URL: ").strip()

    if not url:
        console.print("[red]URL cannot be empty.[/]")
        return None

    return url


update_notice = {"version": None}


def start_update_check():
    """Look for a newer yt-dlp in the background (a request at most once a day)."""

    def work():
        update_notice["version"] = updater.background_check()

    threading.Thread(target=work, daemon=True).start()


def show_menu_notice():
    if update_notice["version"]:
        console.print(
            f"[yellow]A new yt-dlp version is available "
            f"({escape(update_notice['version'])}). Choose 5 to update.[/]"
        )


def offer_daily_check_setting():
    enabled = updater.auto_check_enabled()
    state, other = ("on", "off") if enabled else ("off", "on")

    if confirm(f"The daily update check is {state}. Turn it {other}?"):
        updater.set_auto_check(not enabled)


def update_ytdlp():
    """Check for a newer yt-dlp and install it if the user agrees."""

    try:
        with console.status("Checking for updates..."):
            info = updater.check_for_update()
    except UpdateError as error:
        show_error(error)
        return

    update_notice["version"] = info.latest if info.available else None

    console.print(f"\nYour yt-dlp version: [cyan]{escape(info.current)}[/]")

    if info.available:
        console.print(f"Newest version:      [cyan]{escape(info.latest)}[/]")

        if not confirm("Download and install it?"):
            return

        try:
            with console.status("Updating...") as status:
                version = updater.install_update(
                    report=status.update, info=info
                )
        except UpdateError as error:
            show_error(error)
            return

        console.print(
            f"[green]✔ yt-dlp {escape(str(version))} is installed.[/] "
            "Restart this app to use it. "
        )
        update_notice["version"] = None
        return

    if info.pending_restart:
        console.print(
            f"[yellow]yt-dlp {escape(str(info.installed))} is already "
            "installed.[/] Restart this app to use it."
        )
    else:
        console.print("[green]✔ yt-dlp is up to date.[/]")

    if updater.update_active() and confirm(
        "Go back to the yt-dlp that came with the app?"
    ):
        updater.remove_update()
        console.print("The built-in version will be used after a restart.")

    offer_daily_check_setting()
    
# --------------------------------------------------------------------------
# Menu
# --------------------------------------------------------------------------

def show_menu():
    table = Table.grid(padding=(0, 2))

    for number, label in [
        ("1", "Download Video"),
        ("2", "Download Audio"),
        ("3", "Download Playlist"),
        ("4", "Change Download Location"),
        ("5", "Update yt-dlp"),        
        ("6", "Exit"),
    ]:
        table.add_row(f"[cyan]{number}[/]", label)

    console.print()
    console.print(
        Panel(
            table,
            title=f"[bold]{APP_NAME}[/] v{APP_VERSION}",
            border_style="cyan",
            expand=False,
            padding=(1, 3),
        )
    )

    show_menu_notice()


# --------------------------------------------------------------------------
# Single video and audio
# --------------------------------------------------------------------------

def show_video_card(info):
    """Display a video's details."""

    summary = summarize_video(info)

    body = (
        f"[bold]{escape(summary.title)}[/]\n"
        f"[dim]{escape(summary.channel)}[/]\n\n"
        f"Duration: {format_duration(summary.duration)}"
        f"   Uploaded: {summary.upload_date}"
    )

    console.print()
    console.print(Panel(body, border_style="cyan", expand=False))


def show_quality_table(info):
    """Display available resolutions with estimated sizes."""

    with console.status("Calculating sizes..."):
        options = quality_options(info)

    if not options:
        console.print("\n[yellow]No supported MP4 resolutions found.[/]")
        return []

    table = Table(border_style="cyan", header_style="bold")
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Resolution")
    table.add_column("Est. size", justify="right")

    for index, (quality, size) in enumerate(options, start=1):
        size_text = f"~{format_file_size(size)}" if size else "--"
        table.add_row(str(index), f"{quality}p", size_text)

    console.print()
    console.print(table)

    return [quality for quality, _ in options]


def show_audio_table():
    table = Table(border_style="cyan", header_style="bold")
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Audio")

    for index, choice in enumerate(AUDIO_CHOICES, start=1):
        table.add_row(str(index), audio_label(choice))

    console.print()
    console.print(table)


def choice_text(kind, value, ceiling=False):
    """How a quality choice reads on screen."""

    if kind == "audio":
        return audio_label(value)

    return f"up to {value}p MP4" if ceiling else f"{value}p MP4"

def run_download(function, url, setting, info):
    """Run one download and print the outcome.

    Returns True only when a new file was saved.
    """
    console.print(f"Location: {get_download_dir()}")

    try:
        result = function(url, setting, info=info, reporter=RichReporter())
    except DownloadError as error:
        show_error(error)
        console.print(f"[dim]Details: {escape(str(LOG_FILE))}[/]")
        return False

    if result.status == "completed":
        console.print("[green]✔ Download completed successfully![/]")
        notify("Download complete", info.get("title", "Download"))
        return True

    if result.status == "exists":
        console.print("\n[yellow]This has already been downloaded.[/]")
        console.print(f"File: {escape(result.filename)}")
    else:
        console.print("\n[yellow]Download cancelled.[/]")

    return False


def inspect_single(kind):
    """Download one video ("video") or one track ("audio")."""

    url = get_url()

    if not url:
        return

    if has_playlist_and_video(url):
        console.print("\n[yellow]This link is a video inside a playlist.[/]")

        if confirm("Download the whole playlist instead of just this video?"):
            inspect_playlist(url=url, preset_kind=kind)
            return

    try:
        with console.status("Fetching video information..."):
            info = fetch_video_info(url)

    except PlaylistLinkError:
        console.print("\n[yellow]This is a playlist link.[/]")

        if confirm("Switch to playlist download?"):
            inspect_playlist(url=url, preset_kind=kind)

        return

    except DownloadError as error:
        show_error(error)
        return

    show_video_card(info)

    if kind == "video":
        choices = show_quality_table(info)

        if not choices:
            return

        label, key = "resolution", "video_quality"
        function = download_video

    else:
        show_audio_table()

        choices = AUDIO_CHOICES
        label, key = "audio quality", "audio_bitrate"
        function = download_audio

    setting = select_option(
        choices, label, default=load_settings().get(key)
    )

    if setting is None:
        console.print("[yellow]Cancelled.[/]")
        return

    console.print(f"\nSelected: {choice_text(kind, setting)}")

    if not confirm("Start download?"):
        console.print("[yellow]Cancelled.[/]")
        return

    if run_download(function, url, setting, info):
        remember_setting(key, setting)
        offer_open_folder()


def inspect_playlist(url=None, preset_kind=None):
    if url is None:
        url = get_url()

    if not url:
        return

    try:
        with console.status("Fetching playlist..."):
            playlist = fetch_playlist(url)
    except DownloadError as error:
        show_error(error)
        return

    entries = playlist.entries

    console.print()
    console.print(
        Panel(
            f"[bold]{escape(playlist.title)}[/]\n\n{len(entries)} videos",
            border_style="cyan",
            expand=False,
        )
    )

    if preset_kind in ("video", "audio"):
        kind = preset_kind
    else:
        console.print("\n[cyan]1[/] Video (MP4)    [cyan]2[/] Audio")
        kind = select_option(["video", "audio"], "format")

    if kind is None:
        console.print("[yellow]Cancelled.[/]")
        return

    if kind == "video":
        choices = PLAYLIST_QUALITIES
        label, key = "maximum resolution", "video_quality"
    else:
        choices = AUDIO_CHOICES
        label, key = "audio quality", "audio_bitrate"

    console.print()

    for index, value in enumerate(choices, start=1):
        console.print(
            f"[cyan]{index}[/] {choice_text(kind, value, ceiling=True)}"
        )

    setting = select_option(
        choices, label, default=load_settings().get(key)
    )

    if setting is None:
        console.print("[yellow]Cancelled.[/]")
        return

    while True:
        text = input(
            f"\nRange, e.g. 1-3, 5, 8-10 "
            f"(Enter = all {len(entries)}, Q to cancel): "
        )

        if text.strip().lower() == "q":
            console.print("[yellow]Cancelled.[/]")
            return

        indices = parse_range(text, len(entries))

        if indices is not None:
            break

        console.print("[red]Invalid range. Try again.[/]")

    selected = [entries[i - 1] for i in sorted(indices)]

    folder_name = input(
        f"\nFolder name (Enter = \"{playlist.title}\"): "
    ).strip() or playlist.title

    if not confirm(
        f"\nDownload {len(selected)} item(s) as "
        f"{choice_text(kind, setting, ceiling=True)}?"
    ):
        console.print("[yellow]Cancelled.[/]")
        return

    try:
        result = download_playlist(
            selected, kind, setting, folder_name, reporter=RichReporter()
        )
    except DownloadError as error:
        show_error(error)
        return

    if result.cancelled:
        console.print("\n[yellow]Playlist cancelled.[/]")

    console.print(
        f"\n[green]{result.completed} completed[/], "
        f"{result.skipped} already downloaded, "
        f"{len(result.failed)} failed."
    )

    if result.failed:
        console.print(f"[dim]Details: {escape(str(LOG_FILE))}[/]")

    if result.completed:
        notify(
            "Playlist finished",
            f"{result.completed} of {result.total} downloaded",
        )
        remember_setting(key, setting)
        offer_open_folder(result.folder)

# --------------------------------------------------------------------------
# Settings and main loop
# --------------------------------------------------------------------------

def change_download_location():
    try:
        console.print(f"\nCurrent location:\n[cyan]{get_download_dir()}[/]")

        new_path = input(
            "\nNew download directory (Q to cancel): "
        ).strip()

        if new_path.lower() == "q":
            console.print("[yellow]Cancelled.[/]")
            return

        if not new_path:
            console.print("[red]Directory cannot be empty.[/]")
            return

        new_dir = save_download_dir(new_path)

        console.print(
            f"\n[green]Download location updated:[/]\n{escape(str(new_dir))}"
        )

    except (OSError, ValueError) as error:
        console.print(
            f"\n[red]Unable to change download location:[/] "
            f"{escape(str(error))}"
        )


def main():
    console.print(f"\nWelcome to [bold]{APP_NAME}[/]!")
    check_requirements()
    start_update_check()

    while True:
        show_menu()

        choice = input("\nChoose an option: ").strip()

        if choice == "1":
            inspect_single("video")

        elif choice == "2":
            inspect_single("audio")

        elif choice == "3":
            inspect_playlist()

        elif choice == "4":
            change_download_location()

        elif choice == "5":
            update_ytdlp()

        elif choice == "6":
            console.print("\nGoodbye!")
            break

        else:
            console.print("\n[red]Invalid option.[/]")

        input("\nPress Enter to continue...")


def run():
    setup_logging()

    if "--diagnostics" in sys.argv:
        print(diagnostics_text())
        return

    try:
        main()

    except KeyboardInterrupt:
        print("\n\nApplication interrupted. Goodbye!")

if __name__ == "__main__":
    run()