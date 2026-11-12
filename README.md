# Secure Media Processor

Offline tool that cleans a folder of photos, videos and audio before it goes anywhere near a cloud:
merges `@name` folders, strips metadata, removes duplicates, re-encodes losslessly so every file gets a
new content hash, and renames files after their folder. Desktop app and command line run the same engine.

## Run it

```
pip install -r requirements.txt
python app.py                      # desktop app
python app.py "D:\Photos\Trip"     # desktop app with a folder preselected
python secure_media_processor.py   # command line, processes the current folder
```

The app has three sections: **1. Folder** (pick the root; all subfolders are included),
**2. Options**, **3. Run** (confirmation, progress, log, cancel). Files are changed **in place**; keep a backup.

## Options (run in this order)

| Step | What it does | Changes content hash |
|---|---|---|
| Folder merge | Media from each top-level `@name` folder moves into `name`; optional prefix for top-level folder names | no |
| Strip metadata | Removes EXIF/GPS/XMP/IPTC/comments, container tags, chapters, cover art, telemetry. Lossless; keeps orientation and colour profile | yes, for files that had metadata |
| Remove duplicates | pHash + dHash per folder (logic from `parallel_rename_clean_deduplicate_media_v7.py`); oldest kept, rest to `Duplicates/` | no |
| Lossless re-encode | Pixel/sample-exact re-encode, verified. If that can't change the bytes, adds 16 random padding bytes | yes |
| Rename | `<folder> (N).<ext>`, oldest first, no gaps | no |
| Hash check | SHA-256 of every file before and after; lists files whose hash did not change | - |

Video re-encoding produces FFV1 `.mkv` files that are typically **5-20x larger**.

## External tools

Found on `PATH` or in a `tools/` folder next to `app.py` (git-ignored):

- **ffmpeg + ffprobe**: video/audio steps and video dedupe. `winget install Gyan.FFmpeg`
- **jpegtran** (libjpeg-turbo): lossless JPEG re-encode. Without it, JPEGs get random padding instead.

## Logs

Written to `%LOCALAPPDATA%\SecureMediaProcessor\logs\`, deliberately **outside** the processed folder,
because the log lists original file names and hashes.

## Updates

"Check for updates" in the app, or `python secure_media_processor.py --update`. This is the only network access,
and only when you ask for it. A git checkout is updated with `git pull --ff-only` (refuses to run over uncommitted
changes). A plain folder downloads the latest snapshot, checks it compiles, and backs up replaced files to `.update_backup/`.
The repository is public, so no credentials are needed. If it becomes private, put a read-only token in
`update_config.json` next to `app.py`: `{"github_token": "github_pat_..."}`. That file is git-ignored; never commit it.

## Tests

```
python -m pytest -q tests      # video tests are skipped when ffmpeg is not on PATH
```

## Files

- `app.py`: desktop app (Flet)
- `secure_media_processor.py`: command line
- `media_core.py`: processing engine used by both
- `updater.py`: manual updater
- `parallel_rename_clean_deduplicate_media_v7.py`: the original, tested dedupe script (reference)
- `merge_folders_with_prefix.py`: the original folder merge script (reference)

## Android (planned)

The app is built with Flet so the same Python code can be packaged as a sideloaded APK (`flet build apk`).
Open items: running ffmpeg/jpegtran on Android, and replacing the updater with an APK download.
