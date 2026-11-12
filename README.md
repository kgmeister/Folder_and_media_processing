# Secure Media Processor

Offline tool that cleans a folder of photos, videos and audio before it goes anywhere near a cloud:
merges `@name` folders, strips metadata, removes duplicates, re-encodes losslessly, gives every file a
unique content hash, and renames files after their folder. Desktop app and command line run the same engine.

## Install (Windows)

**PC with Smart App Control on** (Windows 11 blocks unsigned programs, including a self-built `.exe`):

```
python install_windows.py
```

This installs the app for your user in `%LOCALAPPDATA%\Programs\Secure Media Processor` with its own private Python,
and adds **Secure Media Processor** to the Start Menu and Desktop. The shortcuts start it through Python's signed
`pythonw.exe`, which Smart App Control allows. Remove with `python install_windows.py --uninstall`.

**Other PCs**: download `SecureMediaProcessor.exe` from the latest
[release](https://github.com/kgmeister/Folder_and_media_processing/releases) and double-click it. Each push to `main`
builds a new one (`.github/workflows/build-windows.yml`). To build locally: `pip install -r requirements.txt pyinstaller`,
then `python build_exe.py` (output in `dist/`).

**From source**: `pip install -r requirements.txt`, then `python app.py` (optionally with a folder path), or
`python secure_media_processor.py` for the command line, which processes the current folder.

## Using the app

**1. Folder**: pick the root; all subfolders are included. **2. Options**. **3. Run**: log folder, confirmation,
progress, live log, cancel. Files are changed **in place**; keep a backup.

| Step (in run order) | What it does | Changes content hash |
|---|---|---|
| Folder merge | Media from each top-level `@name` folder moves into `name`; optional prefix for top-level folder names | no |
| Strip metadata | Removes EXIF/GPS/XMP/IPTC/comments, container tags, chapters, cover art, telemetry. Lossless; keeps orientation and colour profile | yes, for files that had metadata |
| Remove duplicates | pHash + dHash per folder (logic from `parallel_rename_clean_deduplicate_media_v7.py`); oldest kept, rest to `Duplicates/` | no |
| Lossless re-encode | Pixel/sample-exact re-encode, verified. JPEG via the built-in mozjpeg optimiser; video to FFV1 `.mkv` (**5-20x larger**) | yes |
| Unique padding (on by default) | 16 random bytes per file, see below | yes |
| Rename | `<folder> (N).<ext>`, oldest first, no gaps | no |
| Hash check | SHA-256 of every file before and after; lists files whose hash did not change | - |

### Unique hashes

Strip and re-encode are deterministic: two people processing the same photo would get byte-identical files, and so
the same hash. Unique padding adds 16 bytes from the OS cryptographic random generator (`os.urandom`), fresh for every
file on every run, in a place viewers ignore (JPEG comment, private PNG chunk, GIF comment, unknown WebP chunk,
unreferenced bytes after TIFF/BMP data, a container tag for video/audio). No personal data is added.

**No inflation**: processing a file again, with this copy or anyone else's, *replaces* the previous padding, so file
sizes stay the same however often a file is re-processed (`test_reprocessing_does_not_inflate_files`).

## ffmpeg

Needed for video/audio and video duplicate detection. The app offers to install it on first start (about 77 MB from
the [BtbN FFmpeg builds](https://github.com/BtbN/FFmpeg-Builds), checked against their published sha256, installed only
for this app in `%LOCALAPPDATA%\SecureMediaProcessor\tools`, no admin rights). A `tools/` folder next to the app or
the `.exe`, or ffmpeg on `PATH`, also works. JPEG optimisation needs nothing extra.

## Logs

Saved to a folder you choose (**Run > Log folder > Change...**; the command line uses the same setting or
`--log-dir`). Default: `%LOCALAPPDATA%\SecureMediaProcessor\logs`. Logs list original file names and hashes, so the
app warns when the log folder is inside the folder being processed.

## Updates

"Check for updates" in the app, or `python secure_media_processor.py --update`. Network access happens only when you
press Install or Update.

- `.exe`: downloads the exe from the latest release, checks GitHub's sha256, swaps it in, offers **Restart now**.
- Installed app / plain folder: downloads the latest code, checks it compiles, backs up replaced files to
  `.update_backup/`, and updates libraries if `requirements.txt` changed.
- git checkout: `git pull --ff-only` (refuses to run over uncommitted changes; says so when your copy is ahead).

The repository is public, so no credentials are needed. If it becomes private, put a read-only token in
`update_config.json` next to the app (or the `.exe`): `{"github_token": "github_pat_..."}`. That file is git-ignored;
never commit it.

## Tests

```
python -m pytest -q tests      # video tests are skipped when ffmpeg is not on PATH
```

## Files

- `app.py`: desktop app (Flet)
- `secure_media_processor.py`: command line
- `media_core.py`: processing engine used by both
- `updater.py`, `tools_installer.py`: manual updater, ffmpeg installer
- `install_windows.py`, `build_exe.py`, `assets/`: Windows install, exe build, icon
- `parallel_rename_clean_deduplicate_media_v7.py`: the original, tested dedupe script (reference)
- `merge_folders_with_prefix.py`: the original folder merge script (reference)

## Android (planned)

A sideloaded APK, built from the same Python code with Flet (`flet build apk`). Its update button will download
and install the newest APK. Open item: running ffmpeg on Android.
