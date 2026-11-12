# Secure Media Processor

An offline tool that cleans a folder of photos, videos and audio before it goes anywhere near a cloud: it merges
`@name` folders, strips metadata, removes duplicates, re-encodes losslessly, gives every file a unique content hash,
and renames files after their folder. The desktop app and the command line run the same engine.

## Get it (Windows, portable)

One folder, nothing installed, nothing written outside it.

1. `git clone` this repository (the ready-to-run app is in `dist\SecureMediaProcessor`), or download
   `SecureMediaProcessor-portable.zip` from the latest
   [release](https://github.com/kgmeister/Folder_and_media_processing/releases).
2. If you downloaded the zip: right-click it > **Properties** > tick **Unblock** > **OK**, then unzip it anywhere.
   Without this, Windows marks the extracted files as coming from the internet and Smart App Control blocks them.
   (Already unzipped: `Get-ChildItem -Recurse <folder> | Unblock-File` in PowerShell.)
3. Double-click **Secure Media Processor.cmd**. A console window flashes briefly, then the app opens.

```
SecureMediaProcessor\
  Secure Media Processor.cmd   starts the app
  app\                         the app (a copy of src\)
  runtime\                     its own Python (official python.org files) and libraries
  data\                        settings, logs and ffmpeg: everything the app writes
```

To remove it, delete the folder. The program that runs is Python's own `pythonw.exe`, signed by the Python Software
Foundation, so it works with Windows 11 Smart App Control turned on.

## Using the app

**1. Folder**: pick the root; all subfolders are included. **2. Options**. **3. Run**: log folder, confirmation,
progress, live log, cancel. Files are changed **in place**, so keep a backup.

| Step (in run order) | What it does | Changes content hash |
|---|---|---|
| Folder merge | Media from each top-level `@name` folder moves into `name`; optional prefix for top-level folder names | no |
| Strip metadata | Removes EXIF/GPS/XMP/IPTC/comments, container tags, chapters, cover art, telemetry. Lossless; keeps orientation and colour profile | yes, for files that had metadata |
| Remove duplicates | pHash + dHash within each folder; the oldest copy is kept, the rest move to `Duplicates/` | no |
| Lossless re-encode | Pixel/sample-exact re-encode, verified. JPEG via the built-in mozjpeg optimiser; video to FFV1 `.mkv` (**5-20x larger**) | yes |
| Unique padding (on by default) | 16 random bytes per file, see below | yes |
| Rename | `<folder> (N).<ext>`, oldest first, no gaps | no |
| Hash check | SHA-256 of every file before and after; lists files whose hash did not change | - |

### Unique hashes

Strip and re-encode are deterministic: two people processing the same photo would get byte-identical files, and so
the same hash. Unique padding adds 16 bytes from the OS cryptographic random generator (`os.urandom`), fresh for every
file on every run, in a place viewers ignore (JPEG comment, private PNG chunk, GIF comment, unknown WebP chunk,
unreferenced bytes after TIFF/BMP data, a container tag for video/audio). No personal data is added.

**No inflation**: processing a file again, with any copy of the app, *replaces* the previous padding, so file sizes
stay the same however often a file is re-processed.

## ffmpeg

Needed for video/audio and for video duplicate detection. The app offers to install it on first start: the official
[gyan.dev](https://www.gyan.dev/ffmpeg/builds/) 8.0.1 release (about 106 MB) from its GitHub mirror, checked against a
pinned sha256, saved in `data\tools` (no admin rights). That exact release is known to Smart App Control, so it is
allowed to run. ffmpeg on `PATH` also works. JPEG optimisation needs nothing extra.

## Logs

Saved to a folder you choose (**Run > Log folder > Change...**; the command line uses the same setting or
`--log-dir`). Default: `data\logs` in the portable folder, `%LOCALAPPDATA%\SecureMediaProcessor\logs` when run from
source. Logs list original file names and hashes, so the app warns when the log folder is inside the folder being
processed.

## Updates

**Check for updates** in the app, or `python src/secure_media_processor.py --update`. The app only goes online when
you press Install or Update.

- **In a git clone** (including the app in `dist\`): `git pull --ff-only`. It refuses to run over uncommitted changes.
- **Portable app from the zip**: downloads only the app files (`src/`), checks each against GitHub's hash and that it
  compiles, backs up replaced files to `.update_backup\`, updates the libraries if `requirements.txt` changed, and
  offers **Restart now**.

The repository is public, so no credentials are needed. If it is ever made private, put a read-only token in
`update_config.json` in the app's folder: `{"github_token": "github_pat_..."}`. That file is git-ignored; never
commit it.

## Development

```
pip install -r requirements-dev.txt
python src/app.py [folder]              # desktop app
python src/secure_media_processor.py    # command line, processes the current folder
python -m pytest -q tests               # video tests are skipped when ffmpeg is not on PATH
python scripts/make_portable.py         # rebuilds dist\SecureMediaProcessor; commit it with the code
```

```
src/                    the app (copied as-is to the portable app's app\ folder)
  app.py                desktop app (Flet)
  secure_media_processor.py   command line
  media_core.py         processing engine used by both
  updater.py            "Check for updates"
  tools_installer.py    ffmpeg installer
  assets/               the owl icon
  requirements.txt
scripts/
  make_portable.py      builds dist\SecureMediaProcessor
  make_icon.py          draws the owl icon into src/assets/
tests/                  pytest suite
legacy/                 the original standalone scripts the engine is built from (reference only):
                        parallel_rename_clean_deduplicate_media_v7.py (duplicate detection) and
                        merge_folders_with_prefix.py (folder merge)
dist/                   the built portable app
requirements-dev.txt    src/requirements.txt + pytest
```

## Android (planned)

A sideloaded APK built from the same Python code with Flet (`flet build apk`), with an update button that downloads
and installs the newest APK. Open item: running ffmpeg on Android.
