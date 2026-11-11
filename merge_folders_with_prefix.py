#!/usr/bin/env python3
# merge_folders_with_prefix.py
# ==================================================================
# Simple script to merge @-prefixed folders into non-prefixed counterparts
# and rename all folders with a common prefix.
#
# Notes (2025-10-04):
#   • Merges @folder1 into folder1, moving all files, appending suffix for duplicates.
#   • Preserves @ in file names (e.g., @file.jpg stays @file.jpg or becomes @file__2.jpg).
#   • Deletes empty @-prefixed folders after moving.
#   • Renames all folders with user-specified prefix (default: empty string).
#   • Only processes media files; no deduplication (handled later by v6/v7).
#   • Logs actions to folder_merge_log.txt.
#   • Auto-installs Pillow and ImageHash for consistency with v6, though not used here.
# ==================================================================

import os
import sys
import subprocess
import argparse
from pathlib import Path
from datetime import datetime
import importlib.util

# ---------- Auto-install third-party deps ----------
def _ensure_packages():
    need = {"PIL": "Pillow", "imagehash": "ImageHash"}
    missing = [pip for mod, pip in need.items() if importlib.util.find_spec(mod) is None]
    if missing:
        print(f"[setup] Installing required packages: {', '.join(missing)}", file=sys.stderr)
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", *missing], check=True)
        except subprocess.CalledProcessError:
            subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--upgrade", *missing], check=True)

_ensure_packages()

# ---------- Config / Constants ----------
MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".jfif",
              ".mp4", ".mov", ".avi", ".mkv", ".m4v"}

# ---------- Small FS helpers / logging ----------
def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p

def write_log(log_path: Path, lines: list[str]) -> None:
    ensure_dir(log_path.parent)
    with log_path.open("a", encoding="utf-8") as f:
        for ln in lines: f.write(ln.rstrip() + "\n")

def is_media(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in MEDIA_EXTS

def safe_move(src: Path, dst_dir: Path) -> Path:
    ensure_dir(dst_dir)
    tgt = dst_dir / src.name
    if tgt.exists():
        stem, suf = tgt.stem, tgt.suffix
        i = 2
        while (cand := tgt.with_name(f"{stem}__{i}{suf}")).exists():
            i += 1
        tgt = cand
    return src.rename(tgt)

# ---------- Folder merging and renaming ----------
def find_folder_pairs(root: Path) -> list[tuple[Path, Path]]:
    folders = {p.name: p for p in root.iterdir() if p.is_dir()}
    pairs = [(folders[name], folders[name[1:]]) for name in folders if name.startswith('@') and name[1:] in folders]
    return sorted(pairs, key=lambda x: str(x[1]).lower())

def merge_folder_pair(src_folder: Path, dst_folder: Path, log_path: Path) -> None:
    log_lines = [f"{datetime.now():%Y-%m-%d %H:%M:%S} | Merging {src_folder} into {dst_folder}"]
    ensure_dir(dst_folder)
    for src_file in src_folder.iterdir():
        if not is_media(src_file):
            log_lines.append(f"{datetime.now():%Y-%m-%d %H:%M:%S} | Skipped non-media: {src_file}")
            continue
        try:
            dst_file = safe_move(src_file, dst_folder)
            log_lines.append(f"{datetime.now():%Y-%m-%d %H:%M:%S} | Moved: {src_file} -> {dst_file}")
        except Exception as e:
            log_lines.append(f"{datetime.now():%Y-%m-%d %H:%M:%S} | Error moving {src_file}: {e}")
    try:
        if not any(src_folder.iterdir()):
            src_folder.rmdir()
            log_lines.append(f"{datetime.now():%Y-%m-%d %H:%M:%S} | Deleted empty folder: {src_folder}")
        else:
            log_lines.append(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {src_folder} not empty, not deleted")
    except Exception as e:
        log_lines.append(f"{datetime.now():%Y-%m-%d %H:%M:%S} | Error deleting {src_folder}: {e}")
    write_log(log_path, log_lines)

def rename_folders_with_prefix(root: Path, prefix: str, log_path: Path) -> None:
    log_lines = [f"{datetime.now():%Y-%m-%d %H:%M:%S} | Renaming folders with prefix: '{prefix}'"]
    for folder in sorted(root.iterdir(), key=lambda x: str(x).lower()):
        if not folder.is_dir() or folder.name in ("Duplicates", "Corrupted"):
            continue
        new_name = f"{prefix}{folder.name}"
        new_path = root / new_name
        try:
            folder.rename(new_path)
            log_lines.append(f"{datetime.now():%Y-%m-%d %H:%M:%S} | Renamed: {folder} -> {new_path}")
        except Exception as e:
            log_lines.append(f"{datetime.now():%Y-%m-%d %H:%M:%S} | Error renaming {folder} to {new_path}: {e}")
    write_log(log_path, log_lines)

def main():
    parser = argparse.ArgumentParser(description="Merge @-prefixed folders into non-prefixed counterparts and rename with a common prefix.")
    parser.add_argument("--root-dir", type=str, default="Content", help="Root directory containing folders to merge")
    parser.add_argument("--prefix", type=str, default="", help="Common prefix for renamed folders (default: none)")
    args = parser.parse_args()

    root = Path(args.root_dir).resolve()
    if not root.exists():
        print(f"[error] Root not found: {root}", file=sys.stderr)
        sys.exit(1)

    log_path = root / "folder_merge_log.txt"
    pairs = find_folder_pairs(root)
    if not pairs:
        print("[info] No folder pairs (@folderX, folderX) found.")
    else:
        print(f"[info] Found {len(pairs)} folder pair(s) to merge.")
        for src, dst in pairs:
            print(f"[merge] {src} -> {dst}")
            merge_folder_pair(src, dst, log_path)

    print(f"[info] Renaming folders with prefix: '{args.prefix}'")
    rename_folders_with_prefix(root, args.prefix, log_path)
    print("[done] Folder merging and renaming complete.")

if __name__ == "__main__":
    main()