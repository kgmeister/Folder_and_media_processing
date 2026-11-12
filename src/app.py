#!/usr/bin/env python3
"""
Secure Media Processor - desktop app (Flet). Same engine as the command line
(media_core.py), so both always process files identically.

    pip install -r src/requirements.txt
    python src/app.py

Layout: 1. Folder  ->  2. Options  ->  3. Run (progress, log, cancel).
The only network access is what you click: "Check for updates" and "Install ffmpeg".
"""
import asyncio, collections, logging, os, sys, threading
from pathlib import Path
from typing import Optional

import flet as ft

import media_core as core
import tools_installer
import updater

APP_TITLE = "Secure Media Processor"
ASSETS = Path(__file__).resolve().parent / "assets"
MAX_LOG_LINES = 1500

class UiLogHandler(logging.Handler):
    """Collects engine log lines from worker threads; the UI loop drains them."""
    def __init__(self):
        super().__init__(logging.INFO)
        self.lines = collections.deque()

    def emit(self, record):
        self.lines.append((record.levelno, record.getMessage()))

def badge(text: str) -> ft.Container:
    return ft.Container(ft.Text(text, size=11, color=ft.Colors.BLACK, weight=ft.FontWeight.W_600),
                        bgcolor=ft.Colors.AMBER_200, border_radius=10,
                        padding=ft.Padding.symmetric(horizontal=8, vertical=2))

def section(number: int, title: str, body: ft.Control) -> ft.Card:
    head = ft.Row([ft.Container(ft.Text(str(number), weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
                                bgcolor=ft.Colors.TEAL_700, border_radius=14, width=28, height=28,
                                alignment=ft.Alignment.CENTER),
                   ft.Text(title, size=18, weight=ft.FontWeight.W_600)], spacing=10)
    return ft.Card(ft.Container(ft.Column([head, body], spacing=12), padding=16))

def option(checkbox: ft.Checkbox, description: str, tag: Optional[ft.Control] = None,
           below: Optional[ft.Control] = None) -> ft.Column:
    indent = lambda c: ft.Container(c, padding=ft.Padding.only(left=40))
    rows = [ft.Row([checkbox, tag] if tag else [checkbox], spacing=8,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            indent(ft.Text(description, size=12, color=ft.Colors.ON_SURFACE_VARIANT))]
    if below:
        rows.append(indent(below))
    return ft.Column(rows, spacing=2)

def main(page: ft.Page):
    page.title = APP_TITLE
    page.theme = ft.Theme(color_scheme_seed=ft.Colors.TEAL)
    page.theme_mode = ft.ThemeMode.SYSTEM
    page.padding = 16
    page.window.width, page.window.height = 1000, 900
    page.window.min_width, page.window.min_height = 720, 640
    page.window.icon = str(ASSETS / "icon.ico")

    log_handler = UiLogHandler()
    core.log.addHandler(log_handler)
    picker = ft.FilePicker()
    state = {"root": None, "scan": None, "running": False, "progress": None, "cancel": None,
             "log_dir": core.log_dir(), "tools": None}

    # ---------------- 1. Folder ----------------
    folder_field = ft.TextField(label="Root folder", read_only=True, expand=True,
                                hint_text="Choose the folder to process (all subfolders are included)")
    scan_text = ft.Text("No folder selected.", size=13)
    tools_text = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT, visible=False)
    warn_text = ft.Text("", size=13, color=ft.Colors.ERROR, weight=ft.FontWeight.W_600, visible=False)

    def describe_tools(t: core.Tools) -> str:
        jpeg = ("built in" if core.mozjpeg_lossless is not None else
                "jpegtran" if t.jpegtran else "MISSING (JPEGs are not re-encoded)")
        av = ("found" if (t.ffmpeg and t.ffprobe) else
              "BLOCKED by Windows (video/audio steps and video dedupe are skipped)" if "ffmpeg" in t.blocked else
              "MISSING (video/audio steps and video dedupe are skipped)")
        return f"ffmpeg: {av} · JPEG optimiser: {jpeg}"

    async def refresh_tools():
        t = await asyncio.to_thread(core.find_tools)
        state["tools"] = t
        tools_text.value, tools_text.visible = describe_tools(t), True
        install_btn.visible = tools_installer.supported() and not (t.ffmpeg and t.ffprobe)
        page.update()
        return t

    async def install_ffmpeg(e=None):
        bar = ft.ProgressBar(value=0, bar_height=6, border_radius=3)
        msg = ft.Text(f"Downloading ffmpeg {tools_installer.VERSION} (about {tools_installer.APPROX_DOWNLOAD_MB} MB) "
                      "from GitHub...")
        close = ft.TextButton("Close", on_click=lambda ev: page.pop_dialog(), disabled=True)
        page.show_dialog(ft.AlertDialog(modal=True, title=ft.Text("Installing ffmpeg"),
                                        content=ft.Column([msg, bar], tight=True, width=480), actions=[close]))
        progress = {"done": 0, "total": 0}
        job = asyncio.create_task(asyncio.to_thread(
            tools_installer.install_ffmpeg, lambda d, t: progress.update(done=d, total=t)))
        while not job.done():
            if progress["total"]:
                bar.value = progress["done"] / progress["total"]
                msg.value = f"Downloading ffmpeg: {progress['done'] / 1e6:,.0f} / {progress['total'] / 1e6:,.0f} MB"
            page.update()
            await asyncio.sleep(0.3)
        try:
            msg.value = job.result() + "\nThe download matched its published sha256 checksum."
            bar.value = 1
        except tools_installer.InstallError as ex:
            msg.value = f"ffmpeg was not installed: {ex}"
        except Exception as ex:   # noqa: BLE001 - never leave the dialog stuck
            msg.value = f"ffmpeg was not installed: {type(ex).__name__}: {ex}"
        close.disabled = False
        page.update()
        await refresh_tools()
        refresh()

    async def offer_ffmpeg():
        """Ask once at startup when ffmpeg is missing (downloading is never automatic: it is network access)."""
        t = await refresh_tools()
        if t.ffmpeg and t.ffprobe or not tools_installer.supported():
            return

        async def yes(ev):
            page.pop_dialog()
            await install_ffmpeg()

        page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("ffmpeg is not installed"),
            content=ft.Text("Video and audio processing, and video duplicate detection, need ffmpeg.\n\n"
                            f"Download it now? About {tools_installer.APPROX_DOWNLOAD_MB} MB: the official gyan.dev "
                            f"release {tools_installer.VERSION} from GitHub, checked against its published checksum, "
                            "installed only for this app (no admin rights, nothing added to the system).", width=480),
            actions=[ft.TextButton("Not now", on_click=lambda ev: page.pop_dialog()),
                     ft.FilledButton("Download and install", icon=ft.Icons.DOWNLOAD, on_click=yes)]))

    def show_error(title: str, text: str):
        page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text(title), content=ft.Text(text, selectable=True),
            actions=[ft.TextButton("OK", on_click=lambda ev: page.pop_dialog())]))

    async def pick_directory(title: str, start: Optional[Path] = None) -> Optional[str]:
        """Windows' folder dialog fails (0x80070002) when told to start in a folder that does not exist, e.g. a
        log folder not created yet. Start in the nearest existing parent, retry without a start folder, and
        report anything else in a dialog instead of letting it take down the whole window."""
        while start is not None and not start.is_dir():
            start = start.parent if start.parent != start else None
        attempts = [str(start), None] if start else [None]
        error = None
        for initial in attempts:
            try:
                return await picker.get_directory_path(dialog_title=title, initial_directory=initial)
            except Exception as exc:          # noqa: BLE001 - shown to the user below
                error = exc
        show_error("Could not open the folder dialog", f"{error}")
        return None

    async def choose_folder(e):
        path = await pick_directory("Choose the folder to process", state["root"])
        if path:
            await load_folder(Path(path))

    async def load_folder(root: Path):
        root = root.resolve()
        folder_field.value = str(root)
        scan_text.value = "Scanning..."
        page.update()
        sc = await asyncio.to_thread(core.scan, root)
        state["root"], state["scan"] = root, sc
        scan_text.value = (f"{sc.images:,} images · {sc.videos:,} videos · {sc.audio:,} audio files "
                           f"in {sc.folders:,} folder(s) · {sc.merge_pairs} '@' folder pair(s) to merge")
        warn_text.value = "This is a drive root or your home folder - every media file below it will be processed."
        warn_text.visible = sc.risky_root
        refresh()

    choose_btn = ft.FilledButton("Choose folder...", icon=ft.Icons.FOLDER_OPEN, on_click=choose_folder)
    install_btn = ft.TextButton("Install ffmpeg", icon=ft.Icons.DOWNLOAD, on_click=install_ffmpeg, visible=False)

    # ---------------- 2. Options ----------------
    cb_merge = ft.Checkbox(label="Folder merge", value=False)
    prefix_field = ft.TextField(label="Folder name prefix (optional)", dense=True, width=420,
                                disabled=True, tooltip="Added to the start of each top-level folder name")
    cb_strip = ft.Checkbox(label="Strip metadata", value=False)
    cb_dedupe = ft.Checkbox(label="Remove duplicates", value=False)
    cb_reencode = ft.Checkbox(label="Lossless re-encode", value=False)
    cb_unique = ft.Checkbox(label="Make every file's hash unique", value=True)
    cb_rename = ft.Checkbox(label="Rename files after their folder", value=False)
    cb_hash = ft.Checkbox(label="Hash check: list any file whose content hash did NOT change", value=True,
                          disabled=True)

    num = lambda label, v: ft.TextField(label=label, value=str(v), width=150, dense=True,
                                        input_filter=ft.NumbersOnlyInputFilter())
    f_phash = num("pHash threshold", core.DEFAULT_PHASH_THRESHOLD)
    f_dimg = num("dHash (images)", core.DEFAULT_DHASH_THRESHOLD_IMG)
    f_dvid = num("dHash (videos)", core.DEFAULT_DHASH_THRESHOLD_VID)
    f_hsize = num("Hash size", core.DEFAULT_HASH_SIZE)
    f_frames = num("Frames per video", core.DEFAULT_NUM_FRAMES)
    advanced = ft.ExpansionTile(
        title=ft.Text("Advanced: duplicate detection thresholds", size=13),
        subtitle=ft.Text("Defaults are the tested values from v7. Lower = stricter.", size=12),
        controls=[ft.Row([f_phash, f_dimg, f_dvid, f_hsize, f_frames], wrap=True, spacing=10)],
        controls_padding=ft.Padding.only(left=16, bottom=8), dense=True)

    options_col = ft.Column([
        option(cb_merge, "Moves the media in each top-level '@name' folder into 'name'. Never overwrites: "
                         "clashing names get _1, _2... Non-media and subfolders stay in '@name'.", below=prefix_field),
        option(cb_strip, "Removes EXIF (GPS, camera serial, dates), XMP, IPTC, comments, embedded thumbnails, "
                         "video/audio tags, chapters, cover art and GPS telemetry. Lossless: pixels, frames and "
                         "samples are untouched; orientation and colour profile are kept. Files that had no "
                         "metadata stay byte-identical.", badge("changes file hash")),
        option(cb_dedupe, "Perceptual hashing (pHash + dHash, Hamming distance) within each folder. The oldest copy "
                          f"is kept; duplicates move to '{core.DUP_DIRNAME}', unreadable files to "
                          f"'{core.CORRUPT_DIRNAME}'."),
        option(cb_reencode, "Rewrites every file; decoded pixels/frames/samples stay bit-identical (verified). "
                            "JPEG a few % smaller (built-in mozjpeg), PNG/GIF/TIFF about the same, lossy WebP larger. "
                            "VIDEO becomes FFV1 .mkv: typically 5-20x LARGER and slow.", badge("changes file hash")),
        option(cb_unique, "Adds 16 random bytes (OS cryptographic random generator, fresh for every file on every "
                          "run) where viewers ignore them. Strip and re-encode alone are deterministic: two people "
                          "processing the same photo would get the same hash. With this, nobody else - and no other "
                          "run - ever produces the same file. No personal data is added.", badge("changes file hash")),
        option(cb_rename, "'<folder name> (N).<ext>', numbered 1..N oldest first per extension, no gaps. Removes "
                          "identifying original names such as IMG_20250101_123456.jpg."),
        option(cb_hash, "SHA-256 of every file before and after the run. Reads every file twice."),
        advanced,
    ], spacing=10)

    # ---------------- 3. Run ----------------
    start_btn = ft.FilledButton("Start", icon=ft.Icons.PLAY_ARROW, disabled=True)
    cancel_btn = ft.OutlinedButton("Cancel", icon=ft.Icons.STOP, disabled=True)
    step_text = ft.Text("Choose a folder and at least one option.", size=13)
    progress_bar = ft.ProgressBar(value=0, bar_height=8, border_radius=4)
    log_view = ft.ListView(auto_scroll=True, spacing=1, height=300)
    log_box = ft.Container(log_view, border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=8, padding=8)
    summary_col = ft.Column(spacing=2)

    # Log folder: chosen by the user, remembered for the app and the command line.
    log_dir_text = ft.Text("", size=12, selectable=True, expand=True)
    log_warn_text = ft.Text("", size=12, color=ft.Colors.ORANGE_800, visible=False)

    def show_log_dir():
        d = state["log_dir"]
        default = d == core.DEFAULT_LOG_DIR
        log_dir_text.value = f"Log folder: {d}" + ("  (default)" if default else "")
        inside = state["root"] is not None and core.log_inside_root(d, state["root"])
        log_warn_text.value = ("The log folder is inside the folder being processed. Logs list original file names "
                               "and hashes, so they would travel with your files.")
        log_warn_text.visible = inside

    async def change_log_dir(e):
        path = await pick_directory("Choose where run logs are saved", Path(state["log_dir"]))
        if path:
            core.set_log_dir(Path(path))
            state["log_dir"] = core.log_dir()
            refresh()

    def reset_log_dir(e):
        core.set_log_dir(None)
        state["log_dir"] = core.log_dir()
        refresh()

    log_change_btn = ft.TextButton("Change...", icon=ft.Icons.EDIT_NOTE, on_click=change_log_dir)
    log_reset_btn = ft.TextButton("Use default", on_click=reset_log_dir)

    def build_options() -> core.Options:
        to_int = lambda f, d: int(f.value) if (f.value or "").isdigit() else d
        return core.Options(
            merge_folders=bool(cb_merge.value), folder_prefix=(prefix_field.value or "").strip() if cb_merge.value else "",
            strip=bool(cb_strip.value), dedupe=bool(cb_dedupe.value), reencode=bool(cb_reencode.value),
            unique_hash=bool(cb_unique.value), rename=bool(cb_rename.value), hash_report=bool(cb_hash.value),
            phash_threshold=to_int(f_phash, core.DEFAULT_PHASH_THRESHOLD),
            dhash_img_threshold=to_int(f_dimg, core.DEFAULT_DHASH_THRESHOLD_IMG),
            dhash_vid_threshold=to_int(f_dvid, core.DEFAULT_DHASH_THRESHOLD_VID),
            hash_size=to_int(f_hsize, core.DEFAULT_HASH_SIZE), num_frames=to_int(f_frames, core.DEFAULT_NUM_FRAMES),
            use_processes=False)

    def refresh(e=None):
        running = state["running"]
        opts = build_options()
        prefix_field.disabled = running or not cb_merge.value
        cb_hash.disabled = running or not opts.changes_hashes
        for c in (cb_merge, cb_strip, cb_dedupe, cb_reencode, cb_unique, cb_rename, choose_btn, f_phash, f_dimg,
                  f_dvid, f_hsize, f_frames, log_change_btn, log_reset_btn, install_btn):
            c.disabled = running
        steps = opts.planned_steps()
        show_log_dir()
        start_btn.disabled = running or state["root"] is None or not steps
        cancel_btn.disabled = not running
        update_btn.disabled = running
        if not running:
            step_text.value = ("Choose a folder first." if state["root"] is None else
                               "Choose at least one option." if not steps else
                               "Ready: " + " -> ".join(steps))
        page.update()

    for c in (cb_merge, cb_strip, cb_dedupe, cb_reencode, cb_unique, cb_rename, cb_hash):
        c.on_change = refresh

    def flush_ui():
        lines = log_handler.lines
        while lines:
            level, msg = lines.popleft()
            color = (ft.Colors.ERROR if level >= logging.ERROR else ft.Colors.ORANGE_800 if level >= logging.WARNING
                     else None)
            log_view.controls.append(ft.Text(msg, size=12, selectable=True, color=color))
        if len(log_view.controls) > MAX_LOG_LINES:
            del log_view.controls[:len(log_view.controls) - MAX_LOG_LINES]
        if state["progress"]:
            step, done, total = state["progress"]
            steps = state["steps"]
            idx = next((k for k, s in enumerate(steps, 1) if step.startswith(s)), 0)
            cancelling = state["cancel"].is_set()
            step_text.value = (("Cancelling after the current file... " if cancelling else "")
                               + f"Step {idx}/{len(steps)}: {step} - {done:,}/{total:,}")
            progress_bar.value = (done / total) if total else None
        page.update()

    async def run_job(opts: core.Options):
        root = state["root"]
        state.update(running=True, cancel=threading.Event(), progress=None, steps=opts.planned_steps())
        log_view.controls.clear(); summary_col.controls.clear()
        progress_bar.value = None
        step_text.value = "Starting..."
        refresh()
        log_path = core.default_log_path(state["log_dir"])

        def on_progress(step, done, total):
            state["progress"] = (step, done, total)

        job = asyncio.create_task(asyncio.to_thread(core.run_pipeline, root, opts, on_progress, state["cancel"], log_path))
        while not job.done():
            flush_ui()
            await asyncio.sleep(0.25)
        state["running"] = False
        flush_ui()
        try:
            results = job.result()
            step_text.value = "Done."
            progress_bar.value = 1
            title, color = "Finished", ft.Colors.TEAL_700
        except core.Cancelled:
            results = ["Cancelled. Files already processed keep their verified changes; "
                       "the file in progress was left untouched."]
            step_text.value = "Cancelled."
            title, color = "Cancelled", ft.Colors.ORANGE_800
        except Exception as ex:   # noqa: BLE001 - show anything unexpected instead of dying silently
            core.log.exception("Run failed")
            results = [f"Run failed: {type(ex).__name__}: {ex}"]
            step_text.value = "Failed."
            title, color = "Failed", ft.Colors.ERROR
        summary_col.controls = [ft.Text(title, size=16, weight=ft.FontWeight.W_600, color=color),
                                *[ft.Text(r, size=13, selectable=True) for r in results],
                                ft.Row([ft.Text(f"Log: {log_path}", size=12, selectable=True,
                                                color=ft.Colors.ON_SURFACE_VARIANT, expand=True),
                                        *([ft.TextButton("Open log folder", icon=ft.Icons.FOLDER,
                                                         on_click=lambda e: os.startfile(log_path.parent))]
                                          if hasattr(os, "startfile") else [])])]
        if state["scan"] is not None:
            state["scan"] = await asyncio.to_thread(core.scan, root)
        final_status = step_text.value
        refresh()                          # re-enables the controls; it also resets the status line...
        step_text.value = final_status     # ...so put the outcome back
        page.update()

    def confirm_and_start(e):
        opts = build_options()
        sc: core.ScanResult = state["scan"]
        ack = ft.Checkbox(label="I have a backup of this folder", value=False)
        go = ft.FilledButton("Start", icon=ft.Icons.PLAY_ARROW, disabled=True)

        def on_ack(ev):
            go.disabled = not ack.value
            page.update()

        async def on_go(ev):
            page.pop_dialog()
            await run_job(opts)

        ack.on_change = on_ack
        go.on_click = on_go
        body = [ft.Text(f"Folder: {state['root']}", selectable=True),
                ft.Text(f"{sc.total:,} media files in {sc.folders:,} folder(s)." if sc else ""),
                ft.Text("Steps, in this order:", weight=ft.FontWeight.W_600),
                *[ft.Text(f"   {k}. {s}") for k, s in enumerate(opts.planned_steps(), 1)],
                ft.Text("Files are changed IN PLACE.", color=ft.Colors.ERROR, weight=ft.FontWeight.W_600)]
        if sc and sc.risky_root:
            body.append(ft.Text("This is a drive root or your home folder.", color=ft.Colors.ERROR))
        if core.log_inside_root(state["log_dir"], state["root"]):
            body.append(ft.Text("The log will be saved inside this folder (it lists original file names).",
                                color=ft.Colors.ORANGE_800))
        if opts.reencode and sc and sc.videos and state["tools"] and state["tools"].ffmpeg:
            body.append(ft.Text(f"{sc.videos:,} video(s) will be re-encoded to FFV1: expect 5-20x the disk space.",
                                color=ft.Colors.ORANGE_800))
        body.append(ack)
        page.show_dialog(ft.AlertDialog(modal=True, title=ft.Text("Start processing?"),
                                        content=ft.Column(body, tight=True, spacing=6, width=520),
                                        actions=[ft.TextButton("Back", on_click=lambda ev: page.pop_dialog()), go]))

    def cancel_job(e):
        if state["cancel"] is not None:
            state["cancel"].set()
            cancel_btn.disabled = True
            page.update()

    start_btn.on_click = confirm_and_start
    cancel_btn.on_click = cancel_job

    # ---------------- Updates ----------------
    async def check_updates(e):
        msg = ft.Text("Checking GitHub for the latest version...")
        actions = [ft.TextButton("Close", on_click=lambda ev: page.pop_dialog())]
        dlg = ft.AlertDialog(modal=True, title=ft.Text("Updates"), content=ft.Column([msg], tight=True, width=480),
                             actions=actions)
        page.show_dialog(dlg)
        try:
            info = await asyncio.to_thread(updater.check)
        except updater.UpdateError as ex:
            msg.value = f"Could not check for updates: {ex}"
            page.update()
            return
        installed = (info.local or "unknown")[:7]
        if info.up_to_date:
            msg.value = (f"This copy ({installed}) is ahead of GitHub ({info.remote[:7]}): it has commits that "
                         "are not pushed yet. Nothing to update." if info.local_ahead else
                         f"You have the latest version ({installed}).")
            page.update()
            return
        msg.value = (f"Installed: {installed}\nLatest:    {info.remote[:7]}  ({info.remote_date[:10]})\n"
                     f"{info.remote_message}")

        async def do_update(ev):
            install.disabled = True
            msg.value += "\n\nInstalling..."
            page.update()
            try:
                result = await asyncio.to_thread(updater.apply, info)
                msg.value = f"{result}\n\nRestart the app to use the new version."
                dlg.actions = [ft.FilledButton("Restart now", icon=ft.Icons.RESTART_ALT,
                                               on_click=lambda ev: updater.restart()), *actions]
            except updater.UpdateError as ex:
                msg.value = f"Update failed, nothing was changed: {ex}"
            page.update()

        install = ft.FilledButton("Update now", icon=ft.Icons.DOWNLOAD, on_click=do_update)
        dlg.actions = [install, *actions]
        page.update()

    update_btn = ft.TextButton("Check for updates", icon=ft.Icons.SYSTEM_UPDATE, on_click=check_updates)

    # ---------------- Layout ----------------
    version = updater.local_version()
    header = ft.Row([ft.Image(src="icon.png", width=44, height=44),
                     ft.Column([ft.Text(APP_TITLE, size=22, weight=ft.FontWeight.BOLD),
                                ft.Text("Offline. Nothing leaves this computer unless you press Install or Update.",
                                        size=12, color=ft.Colors.ON_SURFACE_VARIANT)], spacing=0, expand=True),
                     ft.Text(f"version {version[:7]}" if version else "", size=12,
                             color=ft.Colors.ON_SURFACE_VARIANT),
                     update_btn])
    page.add(ft.Column([
        header,
        section(1, "Folder", ft.Column([ft.Row([folder_field, choose_btn]), scan_text,
                                        ft.Row([tools_text, install_btn], wrap=True), warn_text], spacing=6)),
        section(2, "Options", options_col),
        section(3, "Run", ft.Column([ft.Row([log_dir_text, log_change_btn, log_reset_btn]), log_warn_text,
                                     ft.Row([start_btn, cancel_btn]), step_text, progress_bar, log_box, summary_col],
                                    spacing=8)),
    ], spacing=12, scroll=ft.ScrollMode.AUTO, expand=True))
    refresh()
    page.run_task(offer_ffmpeg)
    # Optional: `python app.py "D:\folder"` opens with that folder already selected.
    if len(sys.argv) > 1 and Path(sys.argv[1]).is_dir():
        page.run_task(load_folder, Path(sys.argv[1]))

if __name__ == "__main__":
    ft.run(main, assets_dir=str(ASSETS))
