#!/usr/bin/env python3
r"""
SECURE MEDIA PROCESSOR - command line

Processes every media file in the folder it is RUN FROM (and all subfolders).
Asks y/n for each step, then asks for a final YES. The engine is media_core.py;
the desktop app (app.py) runs exactly the same steps.

HOW TO RUN:
    cd "D:\path\to\your\media"
    python C:\path\to\secure_media_processor.py

    --root-dir "D:\other\folder"   process another folder
    --update                       pull the latest version from GitHub
    --log-dir "D:\logs"             save the run log there (default: the folder set in the app)
"""
import argparse, logging, sys, time
from pathlib import Path

import media_core as core

def ask_yes_no(question: str) -> bool:
    while True:
        try: ans = input(f"{question} [y/n]: ").strip().lower()
        except EOFError:
            print("\nNo input available; aborting.")
            sys.exit(1)
        if ans in ("y", "yes"): return True
        if ans in ("n", "no"): return False
        print("  Please answer y or n.")

def run_update() -> int:
    import updater
    try:
        info = updater.check()
        print(f"Installed: {(info.local or 'unknown')[:7]}   Latest: {info.remote[:7]} ({info.remote_date}) {info.remote_message}")
        if info.up_to_date:
            print("This copy is ahead of GitHub (unpushed commits). Nothing to update." if info.local_ahead
                  else "Already up to date.")
            return 0
        if not ask_yes_no("Install the latest version?"):
            return 0
        print(updater.apply(info))
        return 0
    except updater.UpdateError as e:
        print(f"Update failed: {e}")
        return 1

def main():
    for stream in (sys.stdout, sys.stderr):
        try: stream.reconfigure(errors="replace")    # non-ASCII file names on legacy Windows consoles
        except (AttributeError, ValueError): pass
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    ap = argparse.ArgumentParser(description="Merge folders, strip metadata, deduplicate, losslessly re-encode and rename media.")
    ap.add_argument("--root-dir", type=str, default=None, help="Folder to process (default: the current folder)")
    ap.add_argument("--update", action="store_true", help="Check GitHub for a newer version and install it")
    ap.add_argument("--log-dir", type=str, default=None,
                    help="Folder for the run log (default: the folder chosen in the app, else %%LOCALAPPDATA%%)")
    ap.add_argument("--phash-threshold", type=int, default=core.DEFAULT_PHASH_THRESHOLD)
    ap.add_argument("--dhash-img-threshold", type=int, default=core.DEFAULT_DHASH_THRESHOLD_IMG)
    ap.add_argument("--dhash-vid-threshold", type=int, default=core.DEFAULT_DHASH_THRESHOLD_VID)
    ap.add_argument("--hash-size", type=int, default=core.DEFAULT_HASH_SIZE)
    ap.add_argument("--num-frames", type=int, default=core.DEFAULT_NUM_FRAMES)
    args = ap.parse_args()
    if args.update:
        sys.exit(run_update())

    root = Path(args.root_dir).resolve() if args.root_dir else Path.cwd().resolve()
    if not root.is_dir():
        print(f"Root not found: {root}")
        sys.exit(1)

    tools = core.find_tools()
    sc = core.scan(root)
    print("=" * 72)
    print(" SECURE MEDIA PROCESSOR")
    print("=" * 72)
    print(f" Root folder : {root}")
    print(f" Found       : {sc.images} images, {sc.videos} videos, {sc.audio} audio files in {sc.folders} folder(s)")
    jpeg = "built in" if core.mozjpeg_lossless is not None else ("jpegtran" if tools.jpegtran else "NO")
    print(f" Tools       : ffmpeg={'yes' if tools.ffmpeg else 'NO'}  ffprobe={'yes' if tools.ffprobe else 'NO'}  "
          f"JPEG optimiser={jpeg}")
    if not (tools.ffmpeg and tools.ffprobe):
        print("               (install ffmpeg from the desktop app, or put ffmpeg.exe/ffprobe.exe in a tools folder)")
    if sc.risky_root:
        print("\n WARNING: this is a drive root or your home folder. Every media file below it will be processed.")
    if not sc.total and not sc.merge_pairs:
        print("\nNo media files found. Nothing to do.")
        return
    print("\n Files are modified IN PLACE. Make sure you have a backup before continuing.\n")

    opts = core.Options(phash_threshold=args.phash_threshold, dhash_img_threshold=args.dhash_img_threshold,
                        dhash_vid_threshold=args.dhash_vid_threshold, hash_size=args.hash_size,
                        num_frames=args.num_frames)

    print("a) FOLDER MERGE")
    print(f"   Moves media from each top-level '@name' folder into 'name' ({sc.merge_pairs} pair(s) found).")
    print("   Never overwrites: clashing names get a _1, _2... suffix. Optionally prefixes top-level folder names.")
    opts.merge_folders = ask_yes_no("   Folder merge needed?")
    if opts.merge_folders:
        opts.folder_prefix = input("   Prefix for top-level folder names (Enter for none): ").strip()

    print("\nb) RENAMING")
    print("   Renames every file to '<folder name> (N).<ext>', numbered 1..N oldest first, per extension.")
    print("   Numbering is redone with no gaps. Removes identifying original names. Content hash: NOT changed.")
    opts.rename = ask_yes_no("   Renaming needed?")

    print("\nc) METADATA STRIPPING  ** CHANGES THE CONTENT HASH (SHA-256/MD5) **")
    print("   Removes EXIF (GPS, camera serial, dates), XMP, IPTC, comments, embedded thumbnails,")
    print("   video/audio tags, chapters, cover art and GPS telemetry streams. Lossless.")
    print("   Every file that HAD metadata gets a new hash; files with none stay byte-identical.")
    if not tools.ffmpeg: print("   NOTE: ffmpeg missing -> video/audio will be skipped.")
    opts.strip = ask_yes_no("   Metadata stripping needed?")

    print("\nd) LOSSLESS RE-ENCODING  ** CHANGES THE CONTENT HASH (SHA-256/MD5) **")
    print("   Rewrites each file; decoded pixels/frames/samples stay bit-identical (verified).")
    print("   Size: JPEG a few % smaller; PNG/GIF/TIFF about the same; lossy WebP LARGER;")
    print("         VIDEO -> FFV1 .mkv MUCH LARGER (typically 5-20x+) and slow.")
    opts.reencode = ask_yes_no("   Lossless re-encoding needed?")

    print("\nd2) UNIQUE HASHES  ** CHANGES THE CONTENT HASH (SHA-256/MD5) **")
    print("   Adds 16 random bytes (OS cryptographic RNG, fresh per file per run) where viewers ignore them.")
    print("   Strip and re-encode alone are deterministic: two people processing the same photo would get")
    print("   the same hash. With this, no other person or run ever produces the same file.")
    opts.unique_hash = ask_yes_no("   Make every file's hash unique?")

    print("\ne) DEDUPLICATION")
    print("   Perceptual hashing (p-hash + d-hash, Hamming distance) within each folder.")
    print(f"   The oldest copy is kept; duplicates go to '{core.DUP_DIRNAME}', unreadable files to '{core.CORRUPT_DIRNAME}'.")
    opts.dedupe = ask_yes_no("   Deduplication needed?")

    if opts.changes_hashes:
        print("\nf) HASH CHECK")
        print("   SHA-256 of every file before and after; lists any file whose hash did NOT change.")
        print("   Reads every file twice (slow on large video libraries).")
        opts.hash_report = ask_yes_no("   Run the hash check?")

    steps = opts.planned_steps()
    if not steps:
        print("\nNothing selected. Exiting without changes.")
        return
    print("\nPlanned run (in this order):")
    for k, step in enumerate(steps, 1): print(f"   {k}. {step}")
    try: confirm = input("\nType YES to start: ").strip()
    except EOFError: confirm = ""
    if confirm != "YES":
        print("Not confirmed. Exiting without changes.")
        return

    last = {"step": None, "t": 0.0}
    def progress(step: str, done: int, total: int):
        now = time.time()
        if step != last["step"] or done == total or now - last["t"] > 5:
            print(f"  ... {step}: {done}/{total}")
            last.update(step=step, t=now)

    log_folder = Path(args.log_dir) if args.log_dir else core.log_dir()
    if core.log_inside_root(log_folder, root):
        print(f"\n NOTE: the log goes inside the processed folder ({log_folder}); it lists original file names.")
    log_path = core.default_log_path(log_folder)
    try:
        results = core.run_pipeline(root, opts, progress=progress, log_path=log_path)
    except (KeyboardInterrupt, core.Cancelled):
        print("\nInterrupted. Files already processed keep their verified changes; the file in progress was left untouched.")
        sys.exit(130)
    print("\nProcessing complete.")
    for line in results: print(f"   {line}")
    print(f"   Log file        : {log_path}")

if __name__ == "__main__":
    main()
