# Project log

Handover notes for whoever picks this project up next. Read this first, then `README.md` (user-facing docs).
Add a dated entry at the top of **Change log** whenever you change something, and keep **Open items** current.

## What this is

Secure Media Processor: an offline tool that cleans a folder of photos, videos and audio before it is shared or
uploaded. One engine (`src/media_core.py`) runs behind three front ends: the Windows desktop app, the Android APK
(both Flet, `src/app.py`) and the command line (`src/secure_media_processor.py`).

Pipeline steps (fixed order, each optional), all in `run_pipeline()`:

1. Folder merge: `@name` folders merged into `name`; optional prefix for top-level folders
   (then Extension fix / check, with any file step: wrong extensions are renamed, or with the option off kept,
   and every later step processes the file as its real format either way)
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

### 2026-11-16: options: one-line summaries, plain-language details behind a chevron

Replaces the 2026-11-15 badge fix below: `option()`'s `narrow=` parameter is gone.

- **Layout.** `option(checkbox, summary, details, tag, below)` in `app.py`. The checkbox has no label of its own
  (`visual_density=COMPACT`, 40 px on phone and PC). Title, badge and one-line summary sit in a column to its
  right, and a chevron `IconButton` on the far right shows or hides the details. A labelled Checkbox sits in a
  48 px tap-target row, which left an awkward gap above the description and made the old badge line necessary. The
  badge now sits in a wrapping Row beside the title and wraps only when the line is too narrow; this works because
  the title is a plain Text, not the Checkbox's full-width label. Tapping the title or summary toggles the box.
  The title Text is stored in `checkbox.data`, and `sync_option_titles()` (called from `refresh()`) greys it out when
  the box is disabled.
- **Text.** Summaries stay within about 40 characters, so they fit on one line on a portrait phone. The details are
  written for beginners: no EXIF/pHash/FFV1 jargon, and a hash is called a fingerprint. If you add an option, follow
  the same pattern.
- **Advanced thresholds.** The "pHash threshold" floating label was clipped against the tile header; there is now
  12 px of top padding. On phones the five fields form a two-column `ResponsiveRow` (`col=6`) instead of a centred
  single column; the PC keeps the wrapping row of 140 px fields.
- `dist/` copy of `app.py` synced (LF). Checked against the bundled Flet 1.1.2 (option builds, toggles, chevron
  opens and closes); not yet looked at on a phone.

### 2026-11-15: phone layout, "changes file hash" badge no longer clipped

On a portrait phone (larger Android font size) the badge beside "Make every file's hash unique" ran off the screen.
`option(..., narrow=mobile)` in `app.py` now puts the badge on its own line under the checkbox, lined up with the
description; the PC keeps it beside the checkbox. (A wrapping Row does not work: Flet's labelled Checkbox takes the
full row width, so the badge always wrapped, on the PC too.) The phone header's version/update row wraps. Checked
with `SMP_MOBILE_PREVIEW=1` at 412 and 360 px wide.

### 2026-11-15 (later): wrong extensions renamed or kept; invalid packet durations repaired

A second phone run on `Signal_fails` (the open item below) showed the real reason the MP4 still failed:
`Application provided duration: 3878973300 in stream 0 is invalid`. That is a broken sample duration (a negative
32-bit value, -415993996, read as unsigned) on the last packet. ffmpeg's muxer only accepts 0..2^31-1, and
`+genpts` does not touch durations, so the earlier retry failed the same way.

- **Packet durations.** The `remux()` retry now also adds `-bsf FIX_DURATIONS_BSF` (`setts`), which replaces a
  duration outside 0..2^31-1 with the previous packet's (or 0). Packet data is untouched; streamhash verification
  still passes. With 0 instead of the previous duration, the output reported ~6000 s, hence the previous one.
  `explain_ffmpeg_error()` recognises "provided duration". Reproduced with ffmpeg 8.0.1 (the Android version) by
  patching the last `stts` entry of a generated MP4 (`mp4_with_broken_last_duration()` in the tests) and fixed.
  The Android build does not disable bitstream filters, so `setts` is there.
- **Extension fix step** (`Options.fix_extensions`, default on; GUI checkbox, CLI prompt "e2"). Runs after Folder
  merge and before everything else, whenever a step that works on files is selected (`Options.processes_files`).
  `true_extension()`: images by signature (`image_format()`), anything else by `probe_container()` (ffprobe:
  container + whether there is a real video stream). An MP4/MOV without video becomes `.m4a`; MPEG-TS becomes
  `.mts`. Same container family (e.g. `.mov`/`.mp4`, `.webm`/`.mkv`) is not renamed; unknown formats are left alone.
  Renames use `free_name()`, so they never overwrite. Each rename is a summary note; a failed rename is a problem.
  `container_override()` stays for runs with the option off. `.mts`/`.m2ts` added to `VIDEO_EXTS`.
- **Keeping wrong extensions** (option off = "Extension check"). Before this, with names kept, only video/audio
  within their own category worked: a PNG named `.jpg` failed strip and padding, and an MP4 named `.gif` was moved to
  `Corrupted/` by dedupe. Now the check step records each file's real extension in `_REAL_EXT` (main process; cleared
  at the start and end of every run), and `is_image/is_video/is_audio` go through `kind_ext()`, so every step sorts
  the file by its real format. Image workers (strip, pad, re-encode) choose their parser with `image_ext()` from the
  file's signature, which also works in worker processes. `reencode_audio()` uses `kind_ext()`. The remux "file is
  really X" note is not repeated for files the check step already noted. Re-encoded videos/audio still get
  `.mkv`/`.flac`, since their content changes format. "Files at start" is counted after the check, by real format.
- Tests added: broken-duration MP4 (strip and pad), image signatures, step planning, full pipeline with four
  wrong-extension files plus a name clash, kept wrong image extensions, kept `.gif` video. `dist/` copies of `media_core.py`, `app.py`, `secure_media_processor.py`
  synced. README steps table and "Logs" section updated.

### 2026-11-15: Windows fixes (CI test failure; ffmpeg error parsing)

- **CI failure on Windows** in `test_log_ends_with_summary_of_files_not_processed`. The run summary writes
  OS-native paths (`Album\broken.jpg` on Windows), like every other log line, but the test searched for
  `Album/broken.jpg`. Fixed in the test (`os.path.join`), not the engine.
- **ffmpeg errors misread on Windows.** Windows ffmpeg builds print context addresses without `0x`
  (`[mpeg @ 00000248b41fa080]`), and `_FF_LINE` only recognised `@ 0x...`. As a result the address leaked into
  the message, and `[warning]` lines were counted as the specific error. On Windows a `.mpeg` file that is really
  MP4/AAC (stripped without ffprobe) therefore got the generic "could not copy its streams" text, citing a VBV
  warning, instead of the wrong-extension explanation. The address prefix is now `@ (?:0x)?[0-9a-fA-F]+`.
  This was caught locally by `test_video_with_wrong_extension_keeps_its_real_container`. CI skips that test
  because the runner has no ffmpeg.
- `dist/` copy of `media_core.py` synced (LF). All 42 tests pass on Windows with ffmpeg on PATH.

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

- **Confirm the duration fix on the phone** with `signal-2026-01-16-21-41-10-528-4.mp4` (`Signal_fails`). It was
  reproduced and fixed on a PC with a patched file, not with the original.
- Rebuild the portable app on Windows (`scripts/make_portable.py`) if more than the `src/*.py` copies change.
- CI has no ffmpeg, so the video/audio tests are skipped there. The 2026-11-15 Windows parsing bug got through
  because of this. Installing ffmpeg in `.github/workflows/build.yml` (at least on the Windows job) would close
  the gap. Until then, run the suite locally with ffmpeg on PATH before pushing ffmpeg changes.
- `test_android_ffmpeg_wheel_is_added_to_pyproject` needs Python 3.11+ (`tomllib`). CI uses 3.12.
- `test_android_ffmpeg_wheel_installs_as_flet_would` needs a full pip; the portable runtime's pip is trimmed, so
  it fails when the suite runs on that runtime (environment only).
- Extension fix only looks at files that already have a media extension; a video named `.bin` is not found.
