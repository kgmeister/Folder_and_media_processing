# Secure Media Processor

Offline tool that cleans a folder of photos, videos and audio before it goes anywhere near a cloud:
merges `@name` folders, strips metadata, removes duplicates, re-encodes losslessly, gives every file a
unique content hash, and renames files after their folder. Desktop app and command line run the same engine.

## Get it (Windows, portable)

The PC version is **portable**: one folder, nothing installed, nothing written outside it.

1. Download `SecureMediaProcessor-portable.zip` from the latest
   [release](https://github.com/kgmeister/Folder_and_media_processing/releases) (built on every push to `main`),
   or build it yourself with `python make_portable.py` (output in `dist\SecureMediaProcessor`).
2. Unzip it anywhere: another drive, a USB stick, any folder.
3. Double-click **Secure Media Processor.cmd** (a console window flashes for a moment, then the app opens).

```
SecureMediaProcessor\
  Secure Media Processor.cmd   start the app
  app\                         the app's code and icon
  runtime\                     its own Python (official python.org files) and libraries
  data\                        settings, logs and ffmpeg - everything the app writes
```

To remove it, delete the folder. It works with Windows 11 **Smart App Control** on: Smart App Control blocks unsigned
programs (which is why there is no `.exe`), but the program that actually runs is Python's own `pythonw.exe`, which
is signed by the Python Software Foundation.

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

Needed for video/audio and video duplicate detection. The app offers to install it on first start: about 106 MB, the
official [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) release 8.0.1 from its GitHub mirror, checked against a pinned
sha256, saved in the app's `data\tools` folder (no admin rights). It is pinned to that exact release because ffmpeg is
not code-signed, and Smart App Control only lets unsigned programs run when the exact file is widely known; daily builds
get blocked. A `tools/` folder next to the app, or ffmpeg on `PATH`, also works. JPEG optimisation needs nothing extra.

## Logs

Saved to a folder you choose (**Run > Log folder > Change...**; the command line uses the same setting or
`--log-dir`). Default: the portable folder's `data\logs` (`%LOCALAPPDATA%\SecureMediaProcessor\logs` when run from
source). Logs list original file names and hashes, so the
app warns when the log folder is inside the folder being processed.

## Updates

"Check for updates" in the app, or `python secure_media_processor.py --update`. Network access happens only when you
press Install or Update.

- Portable app / plain folder: downloads the latest code, checks it compiles, backs up replaced files to
  `.update_backup/`, updates the libraries if `requirements.txt` changed, and offers **Restart now**.
- git checkout: `git pull --ff-only` (refuses to run over uncommitted changes; says so when your copy is ahead).

The repository is public, so no credentials are needed. If it becomes private, put a read-only token in
`update_config.json` in the `app` folder: `{"github_token": "github_pat_..."}`. That file is git-ignored;
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
- `make_portable.py`: builds the portable Windows folder
- `assets/`: the owl icon (`assets/make_icon.py` draws it)
- `parallel_rename_clean_deduplicate_media_v7.py`: the original, tested dedupe script (reference)
- `merge_folders_with_prefix.py`: the original folder merge script (reference)

## Android (planned)

A sideloaded APK, built from the same Python code with Flet (`flet build apk`). Its update button will download
and install the newest APK. Open item: running ffmpeg on Android.
