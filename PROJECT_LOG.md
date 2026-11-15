# Project log

Handover notes for whoever picks this project up next. Read this first, then `README.md` (user-facing docs).
Add a dated entry at the top of **Change log** whenever you change something, and keep **Open items** current.

## What this is

Secure Media Processor: an offline tool that cleans a folder of photos, videos and audio before it is shared or
uploaded. One engine (`src/media_core.py`) runs behind three front ends: the Windows desktop app, the Android APK
(both Flet, `src/app.py`) and the command line (`src/secure_media_processor.py`).

Pipeline steps (fixed order, each optional), all in `run_pipeline()`:

1. Folder merge: `@name` folders merged into `name`; optional prefix for top-level folders
2. Hashing (before): sha256 of every file, for the hash check at the end
3. Metadata strip: lossless; images by container surgery, video/audio by ffmpeg stream copy
4. Deduplication: p-hash/d-hash per folder (logic copied verbatim from `legacy/..._v7.py`, BK-tree speed-up);
   duplicates go to `Duplicates/`, unreadable files to `Corrupted/`
5. Lossless re-encode: images (mozjpeg / Pillow), video to FFV1 `.mkv`, lossless audio to FLAC
6. Unique padding: 16 random bytes per file so no two people's outputs ever share a hash
7. Renaming: `<folder name> (N).<ext>`, oldest first
8. Hashing (after): lists every file whose hash did not change

Every rewritten file is verified (pixels / decoded frames / packet hashes) before it replaces the original, and the
original's modification time is kept. A failed verification always keeps the original.

## Layout

| Path | What |
|---|---|
| `src/media_core.py` | the engine: every step, ffmpeg handling, logging, the end-of-log run summary |
| `src/app.py` | Flet GUI (PC and Android) |
| `src/secure_media_processor.py` | command line |
| `src/updater.py`, `src/tools_installer.py` | PC self-update from GitHub; ffmpeg installer (Windows) |
| `tests/test_core.py` | pytest suite (video/audio tests skip without ffmpeg on PATH) |
| `scripts/` | portable build (`make_portable.py`), Android ffmpeg build + wheel, icon |
| `dist/SecureMediaProcessor/` | the built portable Windows app, tracked byte-for-byte (`-text -diff`) |
| `legacy/` | the original standalone scripts the engine was built from; reference only |
| `.github/workflows/build.yml` | on push to `main`: tests, portable zip, APK, GitHub release |

## Working on it

- Tests: `python -m pytest -q tests` (needs `requirements-dev.txt`; Python 3.11+ for the pyproject test).
- `dist/SecureMediaProcessor/app/` is a copy of `src/`. After changing `src/`, rebuild with
  `python scripts/make_portable.py` on Windows, or at least copy the changed `src/` files across with LF endings.
- The repository stores LF. Windows checkouts show CRLF in the working tree; that is only line endings.
- Android ffmpeg is version 8.0.1 (`scripts/build_android_ffmpeg.sh`): native codecs only, no external libraries.
  ffmpeg's messages differ between versions, so match error text loosely.
- Logs contain original file names and hashes. Keep them out of the processed folder (the app warns about this).
- Remotes: the maintainer pushes with `git push personal main`
  (`personal` = https://github.com/kgmeister/Folder_and_media_processing).

## Change log

### 2026-11-15: run summary at the end of every log; ffmpeg failures fixed and explained

Reported from a phone run on `Signal_content/Duplicates`: both video files failed Metadata strip and Unique padding,
and the log cut off the reason.

- **Run summary.** `RunSummary` in `media_core.py` writes a block at the end of every log: steps, outcome
  (finished / cancelled during X / failed during X), files at start, per-step counts, "N of M processed as
  specified", then every file that was not, each with the step, status (FAILED / SKIPPED / CORRUPT / UNCHANGED)
  and reason. Also written when a run is cancelled or crashes. A notes section lists processed files with remarks
  (e.g. wrong extension). The GUI and CLI results get a `Problems` line pointing to the log.
  `StepStats` now carries `problems` and `notes`; `_report()` fills them; `skip_all()` covers "ffmpeg not found".
  Files moved to `Corrupted/` are passed back from `run_dedupe()` via `corrupt_out`.
- **Readable ffmpeg errors.** ffmpeg now runs with `-loglevel level+warning`. `ffmpeg_error_summary()` keeps the
  lines that give the reason (errors before warnings, specific before generic, no memory addresses or repeats,
  taken from the start). The old code kept the last 300 characters, which cut the reason off.
  `explain_ffmpeg_error()` puts a plain sentence in front.
- **Wrong extension (`.mpeg` that is really MP4/AAC).** ffmpeg chose the MPEG-PS muxer from the extension, and
  MPEG-PS cannot hold AAC. `remux()` now asks ffprobe for the real container (`real_container()`) and forces that
  muxer when it differs from the extension. The file keeps its name, and the log notes it. Reproduced and fixed.
- **MP4 "Error submitting a packet to the muxer: Invalid argument".** If the container writer rejects packets or
  timestamps, `remux()` retries once with `-fflags +genpts -avoid_negative_ts make_zero -max_interleave_delta 0`.
  The packets are still verified identical (streamhash). Not reproduced, because the file was not available.
- `strip_av()` / `pad_av()` take an optional `ffprobe`. The MP4-specific padding options now follow the real
  container, not the extension.
- Tests added: wrong-extension video, error parsing/explanations, timestamp retry, summary content, summary on
  cancel. `README.md` "Logs" section updated. `dist/` copy of `media_core.py` synced.

### Before 2026-11-15

See `git log`: engine built from the legacy scripts (v7 dedupe kept verbatim), Windows portable app (signed
`pythonw.exe` + `.cmd`, for Smart App Control), updater, unique-hash padding, Android APK with ffmpeg built in,
version display, CI releases.

## Open items

- **Confirm the MP4 fix** on `signal-2026-01-16-21-41-10-528-4.mp4` (phone, `Signal_content/Duplicates`). If it
  still fails, the run summary now shows the full ffmpeg reason. Use that, or reproduce with the file on a PC.
- Rebuild the portable app on Windows (`scripts/make_portable.py`) if more than `media_core.py` changes.
- `test_android_ffmpeg_wheel_is_added_to_pyproject` needs Python 3.11+ (`tomllib`). CI uses 3.12.
- Files with a wrong extension are kept under their name. Renaming them to the real extension could be an option.
