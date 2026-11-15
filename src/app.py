#!/usr/bin/env python3
"""
Secure Media Processor - the app (Flet), for the PC and as the Android APK. Same engine as the command line
(media_core.py), so all of them process files identically.

    pip install -r src/requirements.txt
    python src/app.py

Layout: 1. Folder  ->  2. Options  ->  3. Run (progress, log, cancel).
The only network access is what you click: "Check for updates" and "Install ffmpeg".
On a phone: one column, "All files access" is asked for before a folder can be chosen, photos only
(no ffmpeg on Android), and "Check for updates" opens the newest APK for Android to install.
SMP_MOBILE_PREVIEW=1 shows the phone layout in a phone-sized window on the PC.
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

OPTION_TITLE_DISABLED = ft.Colors.with_opacity(0.38, ft.Colors.ON_SURFACE)

def option(checkbox: ft.Checkbox, summary: str, details: str, tag: Optional[ft.Control] = None,
           below: Optional[ft.Control] = None) -> ft.Row:
    """Checkbox on the left; title, tag and a one-line summary stacked tightly on the right, and a chevron on the
    far right that shows or hides the full explanation. The title is not the checkbox's own label: a labelled
    checkbox sits in a 40-48 px tap-target row, which left a wide gap above the text. The tag wraps under the title
    only when the line is too narrow. Tapping the title or summary toggles the box; the title Text is kept in
    checkbox.data so sync_option_titles() can grey it out when the box is disabled."""
    title = ft.Text(checkbox.label, size=15)
    checkbox.label, checkbox.data = None, title
    checkbox.visual_density = ft.VisualDensity.COMPACT      # same 40 px box on phone and PC, so the padding fits

    def toggle(e):
        if not checkbox.disabled:
            checkbox.value = not checkbox.value
            if checkbox.on_change:
                checkbox.on_change(None)

    head = ft.Row([title, tag] if tag else [title], wrap=True, spacing=8, run_spacing=4,
                  vertical_alignment=ft.CrossAxisAlignment.CENTER)
    text = ft.Container(ft.Column([head, ft.Text(summary, size=12, color=ft.Colors.ON_SURFACE_VARIANT)],
                                  spacing=2),
                        padding=ft.Padding.only(top=9), on_click=toggle)   # centres the title on the 40 px box
    more = ft.Container(ft.Text(details, size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST, border_radius=8,
                        padding=ft.Padding.symmetric(horizontal=10, vertical=8), visible=False)

    def show_more(e):
        more.visible = expand_btn.selected = not more.visible
        more.update()
        expand_btn.update()

    expand_btn = ft.IconButton(icon=ft.Icons.EXPAND_MORE, selected_icon=ft.Icons.EXPAND_LESS, selected=False,
                               icon_size=20, icon_color=ft.Colors.ON_SURFACE_VARIANT,
                               visual_density=ft.VisualDensity.COMPACT, tooltip="More details", on_click=show_more)
    return ft.Row([checkbox, ft.Column([text, more, below] if below else [text, more], spacing=6, expand=True),
                   expand_btn], spacing=4, vertical_alignment=ft.CrossAxisAlignment.START)

def sync_option_titles(checkboxes) -> None:
    for c in checkboxes:
        c.data.color = OPTION_TITLE_DISABLED if c.disabled else None

def main(page: ft.Page):
    page.title = APP_TITLE
    page.theme = ft.Theme(color_scheme_seed=ft.Colors.TEAL)
    page.theme_mode = ft.ThemeMode.SYSTEM
    on_android = page.platform == ft.PagePlatform.ANDROID
    preview = os.environ.get("SMP_MOBILE_PREVIEW") == "1"
    mobile = on_android or page.platform == ft.PagePlatform.IOS or preview
    dlg_w = None if mobile else 480          # dialogs: fixed width on the PC, screen width on a phone
    page.padding = 8 if mobile else 16
    if not mobile or preview:
        page.window.width, page.window.height = (412, 860) if preview else (1000, 900)
        page.window.min_width, page.window.min_height = (360, 600) if preview else (720, 640)
        page.window.icon = str(ASSETS / "icon.ico")

    log_handler = UiLogHandler()
    core.log.addHandler(log_handler)
    picker = ft.FilePicker()
    state = {"root": None, "scan": None, "running": False, "progress": None, "cancel": None,
             "log_dir": core.log_dir(), "tools": None}

    # ---------------- 1. Folder ----------------
    folder_field = ft.TextField(label="Root folder", read_only=True, expand=not mobile,
                                hint_text="Choose the folder to process (all subfolders are included)")
    scan_text = ft.Text("No folder selected.", size=13)
    tools_text = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT, visible=False)
    warn_text = ft.Text("", size=13, color=ft.Colors.ERROR, weight=ft.FontWeight.W_600, visible=False)

    def describe_tools(t: core.Tools) -> str:
        if on_android or preview:
            if t.ffmpeg and t.ffprobe:
                return ("ffmpeg: built in · Photos, videos and audio are processed as on the PC. Only JPEG "
                        "re-encoding is not available on a phone (its optimiser does not exist for Android).")
            return ("ffmpeg: NOT WORKING on this phone" + (" (it would not start)" if t.blocked else "") + ". Videos "
                    "and audio are only merged and renamed; photos are processed as usual.")
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
        install_btn.visible = tools_installer.supported() and not mobile and not (t.ffmpeg and t.ffprobe)
        page.update()
        return t

    async def install_ffmpeg(e=None):
        bar = ft.ProgressBar(value=0, bar_height=6, border_radius=3)
        msg = ft.Text(f"Downloading ffmpeg {tools_installer.VERSION} (about {tools_installer.APPROX_DOWNLOAD_MB} MB) "
                      "from GitHub...")
        close = ft.TextButton("Close", on_click=lambda ev: page.pop_dialog(), disabled=True)
        page.show_dialog(ft.AlertDialog(modal=True, title=ft.Text("Installing ffmpeg"),
                                        content=ft.Column([msg, bar], tight=True, width=dlg_w), actions=[close]))
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
        if t.ffmpeg and t.ffprobe or not tools_installer.supported() or mobile:
            return

        async def yes(ev):
            page.pop_dialog()
            await install_ffmpeg()

        page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("ffmpeg is not installed"),
            content=ft.Text("Video and audio processing, and video duplicate detection, need ffmpeg.\n\n"
                            f"Download it now? About {tools_installer.APPROX_DOWNLOAD_MB} MB: the official gyan.dev "
                            f"release {tools_installer.VERSION} from GitHub, checked against its published checksum, "
                            "installed only for this app (no admin rights, nothing added to the system).", width=dlg_w),
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

    perms = None
    if on_android:
        import flet_permission_handler as fph      # Android only; not needed (or installed) on the PC
        perms = fph.PermissionHandler()

    async def storage_allowed() -> bool:
        """Android: editing files in place in shared folders (DCIM, Pictures, ...) needs 'All files access'."""
        if perms is None:
            return True
        try:
            if await perms.get_status(fph.Permission.MANAGE_EXTERNAL_STORAGE) == fph.PermissionStatus.GRANTED:
                return True
        except Exception as exc:        # noqa: BLE001
            show_error("Could not check storage access", f"{exc}")
            return False
        result = {}

        async def allow(ev):
            page.pop_dialog()
            try:      # opens Android's "All files access" switch for this app
                result["status"] = await perms.request(fph.Permission.MANAGE_EXTERNAL_STORAGE)
            except Exception as exc:    # noqa: BLE001
                show_error("Could not ask for storage access", f"{exc}")
            done.set()

        def not_now(ev):
            page.pop_dialog()
            done.set()

        done = asyncio.Event()
        page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("Allow access to your files"),
            content=ft.Text("To clean photos in place, the app needs Android's 'All files access'. Android will show "
                            "a switch: turn it on, then come back.\n\nThe app stays offline: it only reads and "
                            "changes the files in the folder you choose.", width=dlg_w),
            actions=[ft.TextButton("Not now", on_click=not_now),
                     ft.FilledButton("Allow", icon=ft.Icons.LOCK_OPEN, on_click=allow)]))
        await done.wait()
        return result.get("status") == fph.PermissionStatus.GRANTED

    async def choose_folder(e):
        if not await storage_allowed():
            scan_text.value = "No access to your files yet: tap 'Choose folder' again to allow it."
            page.update()
            return
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
        warn_text.value = ("This is a whole storage root or your home folder - every media file below it will be "
                           "processed.")
        warn_text.visible = sc.risky_root
        refresh()

    choose_btn = ft.FilledButton("Choose folder...", icon=ft.Icons.FOLDER_OPEN, on_click=choose_folder)
    install_btn = ft.TextButton("Install ffmpeg", icon=ft.Icons.DOWNLOAD, on_click=install_ffmpeg, visible=False)

    # ---------------- 2. Options ----------------
    cb_merge = ft.Checkbox(label="Folder merge", value=False)
    cb_fixext = ft.Checkbox(label="Fix wrong file extensions", value=True)
    prefix_field = ft.TextField(label="Folder name prefix (optional)", dense=True, width=None if mobile else 420,
                                disabled=True, tooltip="Added to the start of each top-level folder name")
    cb_strip = ft.Checkbox(label="Strip metadata", value=False)
    cb_dedupe = ft.Checkbox(label="Remove duplicates", value=False)
    cb_reencode = ft.Checkbox(label="Lossless re-encode", value=False)
    cb_unique = ft.Checkbox(label="Make every file's hash unique", value=True)
    cb_rename = ft.Checkbox(label="Rename files after their folder", value=False)
    cb_hash = ft.Checkbox(label="Hash check" if mobile else "Hash check: list any file whose content hash did NOT change",
                          value=True, disabled=True)

    # Phone: a two-column grid that fills the width (a centred single column of narrow boxes looked stranded).
    # PC: fixed-width boxes in one wrapping row.
    num = lambda label, v: ft.TextField(label=label, value=str(v), dense=True, width=None if mobile else 140,
                                        col=6 if mobile else None, input_filter=ft.NumbersOnlyInputFilter())
    f_phash = num("pHash threshold", core.DEFAULT_PHASH_THRESHOLD)
    f_dimg = num("dHash (images)", core.DEFAULT_DHASH_THRESHOLD_IMG)
    f_dvid = num("dHash (videos)", core.DEFAULT_DHASH_THRESHOLD_VID)
    f_hsize = num("Hash size", core.DEFAULT_HASH_SIZE)
    f_frames = num("Frames per video", core.DEFAULT_NUM_FRAMES)
    thresholds = [f_phash, f_dimg, f_dvid, f_hsize, f_frames]
    advanced = ft.ExpansionTile(
        title=ft.Text("Advanced: duplicate detection thresholds", size=13),
        subtitle=ft.Text("Defaults are the tested values from v7. Lower = stricter.", size=12),
        controls=[ft.ResponsiveRow(thresholds, spacing=12, run_spacing=16) if mobile else
                  ft.Row(thresholds, wrap=True, spacing=10, run_spacing=16)],
        # top padding: room for the outlined fields' floating labels, which were clipped against the header
        controls_padding=ft.Padding.only(left=16, right=16, top=12, bottom=12),
        expanded_cross_axis_alignment=ft.CrossAxisAlignment.START, dense=True)

    tag = lambda: badge("changes file hash")
    # Summaries stay within about 40 characters so they fit on one line on a portrait phone.
    options_col = ft.Column([
        option(cb_merge, "Merge each '@name' folder into 'name'.",
               "If you have folders like '@Holiday' and 'Holiday' side by side, the photos and videos in '@Holiday' "
               "are moved into 'Holiday'. Nothing is ever overwritten: if a name is already taken, the moved file "
               "gets _1, _2 and so on added. Other files and subfolders are left in '@Holiday'.", below=prefix_field),
        option(cb_fixext, "Rename files to their true extension.",
               "Sometimes a file's ending does not match what it really is, for example a video saved as '.gif', or "
               "an MP4 named '.mpeg'. This gives such files the right ending so other apps open them properly. The "
               "file itself is not changed. If you untick this, names stay as they are, and the app still handles "
               "each file correctly."),
        option(cb_strip, "Remove EXIF, GPS and other metadata.",
               "Photos and videos carry hidden information: where they were taken (GPS), when, which camera or phone "
               "was used, plus small preview images and tags. This removes all of it. The picture, video and sound "
               "themselves are not touched, and photos keep their correct rotation and colours.", tag()),
        option(cb_dedupe, f"Move look-alike copies to '{core.DUP_DIRNAME}'.",
               "Compares what photos and videos look like, so copies are found even if they were resized or saved "
               "again. Only files in the same folder are compared. The oldest copy stays where it is; the others "
               f"move to a '{core.DUP_DIRNAME}' folder (nothing is deleted). Files that cannot be opened move to "
               f"'{core.CORRUPT_DIRNAME}'."),
        option(cb_reencode, "Lossless rewrite; videos get much larger.",
               "Saves every file again in a new form while keeping the picture and sound exactly the same (the app "
               "checks this). JPEG photos are skipped on a phone. Most images stay about the same size. Videos are "
               "saved in a lossless .mkv format and usually become 5 to 20 times LARGER, and this is slow on a phone."
               if mobile else
               "Saves every file again in a new form while keeping the picture and sound exactly the same (the app "
               "checks this). JPEG photos get a little smaller; most other images stay about the same size. Videos "
               "are saved in a lossless .mkv format and usually become 5 to 20 times LARGER, and this is slow.",
               tag()),
        option(cb_unique, "Add random bytes so no hash ever repeats.",
               "A file's 'hash' is like its fingerprint. Without this option, two people cleaning the same photo "
               "would end up with identical files that could be matched to each other. This adds a little random "
               "data that viewers ignore, so every file is one of a kind, every time. Nothing about you is added.",
               tag()),
        option(cb_rename, "Name files '<folder> (N)', oldest first.",
               "Original names often give away the date and the device, like 'IMG_20250101_123456.jpg'. Files are "
               "renamed after their folder instead, for example 'Holiday (1).jpg', 'Holiday (2).jpg', numbered from "
               "oldest to newest."),
        option(cb_hash, "List files whose hash did NOT change." if mobile else "Fingerprint every file, before and after.",
               "Takes a fingerprint (hash) of every file before and after the run, and lists any file that did not "
               "change, so you can see what was left as it was. It reads every file twice, so it adds some time."),
        advanced,
    ], spacing=14)

    # ---------------- 3. Run ----------------
    start_btn = ft.FilledButton("Start", icon=ft.Icons.PLAY_ARROW, disabled=True)
    cancel_btn = ft.OutlinedButton("Cancel", icon=ft.Icons.STOP, disabled=True)
    step_text = ft.Text("Choose a folder and at least one option.", size=13)
    progress_bar = ft.ProgressBar(value=0, bar_height=8, border_radius=4)
    log_view = ft.ListView(auto_scroll=True, spacing=1, height=240 if mobile else 300)
    log_box = ft.Container(log_view, border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=8, padding=8)
    summary_col = ft.Column(spacing=2)

    # Log folder: chosen by the user, remembered for the app and the command line.
    log_dir_text = ft.Text("", size=12, selectable=True, expand=not mobile)
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
            fix_extensions=bool(cb_fixext.value),
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
        cb_fixext.disabled = running or not opts.processes_files
        for c in (cb_merge, cb_strip, cb_dedupe, cb_reencode, cb_unique, cb_rename, choose_btn, f_phash, f_dimg,
                  f_dvid, f_hsize, f_frames, log_change_btn, log_reset_btn, install_btn):
            c.disabled = running
        sync_option_titles((cb_merge, cb_fixext, cb_strip, cb_dedupe, cb_reencode, cb_unique, cb_rename, cb_hash))
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

    for c in (cb_merge, cb_fixext, cb_strip, cb_dedupe, cb_reencode, cb_unique, cb_rename, cb_hash):
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
                                        content=ft.Column(body, tight=True, spacing=6, width=None if mobile else 520,
                                                          scroll=ft.ScrollMode.AUTO),
                                        actions=[ft.TextButton("Back", on_click=lambda ev: page.pop_dialog()), go]))

    def cancel_job(e):
        if state["cancel"] is not None:
            state["cancel"].set()
            cancel_btn.disabled = True
            page.update()

    start_btn.on_click = confirm_and_start
    cancel_btn.on_click = cancel_job

    # ---------------- Updates ----------------
    def version_label(name: Optional[str], sha: Optional[str]) -> str:
        if name and sha:
            return f"{name}  ({sha[:7]})"
        return name or (f"commit {sha[:7]}" if sha else "unknown")

    async def check_updates(e):
        """The only moment the app asks GitHub anything: when this button is pressed."""
        msg = ft.Text("Checking GitHub for the latest version...")
        status = ft.Text("", weight=ft.FontWeight.W_600)
        actions = [ft.TextButton("Close", on_click=lambda ev: page.pop_dialog())]
        dlg = ft.AlertDialog(modal=True, title=ft.Text("Updates"),
                             content=ft.Column([msg, status], tight=True, spacing=10, width=dlg_w), actions=actions)
        page.show_dialog(dlg)
        try:
            info = await asyncio.to_thread(updater.check)
        except updater.UpdateError as ex:
            msg.value = f"This version: {updater.describe_local()}\n\nCould not check for updates: {ex}"
            page.update()
            return
        msg.value = (f"This version:   {version_label(info.local_name, info.local)}\n"
                     f"Latest version: {version_label(info.remote_name, info.remote)}"
                     + (f", released {info.remote_date[:10]}" if info.remote_date else "")
                     + (f"\nWhat's new: {info.remote_message}" if info.remote_message else ""))
        version_text.value = f"Version {info.local_name or updater.describe_local()} · latest {info.remote_name}"
        if info.up_to_date:
            status.value, status.color = (
                ("This copy is newer than the latest release: it has changes that are not released yet.",
                 ft.Colors.ON_SURFACE_VARIANT) if info.local_ahead else
                ("You have the latest version.", ft.Colors.TEAL_700))
            page.update()
            return
        status.value, status.color = "An update is available.", ft.Colors.ORANGE_800

        if info.download_url:          # Android: Android itself installs the new APK
            async def get_apk(ev):
                await ft.UrlLauncher().launch_url(info.download_url, mode=ft.LaunchMode.EXTERNAL_APPLICATION)
                msg.value += ("\n\nThe download has started in your browser. Open it when it finishes and tap "
                              "Update. (The first time, Android asks you to allow installs from your browser.) "
                              "Your settings are kept.")
                page.update()
            dlg.actions = [ft.FilledButton(f"Download {info.remote_name}", icon=ft.Icons.DOWNLOAD, on_click=get_apk),
                           *actions]
            page.update()
            return

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

        install = ft.FilledButton(f"Update to {info.remote_name}", icon=ft.Icons.DOWNLOAD, on_click=do_update)
        dlg.actions = [install, *actions]
        page.update()

    update_btn = ft.TextButton("Check for updates", icon=ft.Icons.SYSTEM_UPDATE, on_click=check_updates)

    # ---------------- Layout ----------------
    device = "phone" if mobile else "computer"
    title_col = ft.Column([ft.Text(APP_TITLE, size=20 if mobile else 22, weight=ft.FontWeight.BOLD),
                           ft.Text(f"Offline. Nothing leaves this {device} unless you press Install or Update.",
                                   size=12, color=ft.Colors.ON_SURFACE_VARIANT)], spacing=0, expand=True)
    # This copy's version, read locally (no network). The latest version is shown after "Check for updates".
    version_text = ft.Text(f"Version {updater.describe_local()}", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
    icon = ft.Image(src="icon.png", width=40 if mobile else 44, height=40 if mobile else 44)
    if mobile:      # narrow screen: stack what sits side by side on the PC
        header = ft.Column([ft.Row([icon, title_col]), ft.Row([version_text, update_btn], wrap=True,
                                                              alignment=ft.MainAxisAlignment.SPACE_BETWEEN)], spacing=2)
        folder_row = ft.Column([folder_field, choose_btn], spacing=8)
        log_row = ft.Column([log_dir_text, ft.Row([log_change_btn, log_reset_btn])], spacing=0)
    else:
        header = ft.Row([icon, title_col, version_text, update_btn])
        folder_row = ft.Row([folder_field, choose_btn])
        log_row = ft.Row([log_dir_text, log_change_btn, log_reset_btn])
    page.add(ft.SafeArea(ft.Column([
        header,
        section(1, "Folder", ft.Column([folder_row, scan_text,
                                        ft.Row([tools_text, install_btn], wrap=True), warn_text], spacing=6)),
        section(2, "Options", options_col),
        section(3, "Run", ft.Column([log_row, log_warn_text,
                                     ft.Row([start_btn, cancel_btn]), step_text, progress_bar, log_box, summary_col],
                                    spacing=8)),
    ], spacing=12, scroll=ft.ScrollMode.AUTO, expand=True), expand=True))
    refresh()
    page.run_task(offer_ffmpeg)
    # Optional: `python app.py "D:\folder"` opens with that folder already selected.
    if len(sys.argv) > 1 and Path(sys.argv[1]).is_dir():
        page.run_task(load_folder, Path(sys.argv[1]))

if __name__ == "__main__":
    ft.run(main, assets_dir=str(ASSETS))
