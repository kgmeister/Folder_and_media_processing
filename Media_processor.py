#!/usr/bin/env python3
"""
Lossless metadata-strip + re-encode script.
Changes content hashes while keeping decoded pixels/samples bit-identical.
"""

import argparse
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from PIL import Image

# ----------------------------- Configuration -----------------------------

IMAGE_EXTS = {".png", ".webp", ".tif", ".tiff", ".bmp", ".gif"}
JPEG_EXTS  = {".jpg", ".jpeg"}
AUDIO_EXTS = {".flac", ".wav", ".aiff", ".aif", ".m4a", ".alac", ".ogg", ".opus"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv", ".flv"}

ALL_MEDIA = IMAGE_EXTS | JPEG_EXTS | AUDIO_EXTS | VIDEO_EXTS

# ----------------------------- Helpers -----------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)

def run(cmd: list, timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )

# ----------------------------- Processors --------------------------------

def process_jpeg(src: Path, dst: Path, jpegtran: Optional[str]) -> bool:
    """True lossless JPEG: strip all markers + optimize Huffman tables."""
    if jpegtran:
        # -copy none  → strip all metadata
        # -optimize   → rebuild Huffman tables (changes bytes)
        # -progressive → progressive JPEG (also changes structure)
        cmd = [
            jpegtran,
            "-copy", "none",
            "-optimize",
            "-progressive",
            "-outfile", str(dst),
            str(src),
        ]
        r = run(cmd)
        return r.returncode == 0 and dst.exists() and dst.stat().st_size > 0

    # Fallback: Pillow (near-lossless at high quality, not bit-exact coefficients)
    try:
        with Image.open(src) as im:
            im = im.convert("RGB") if im.mode not in ("RGB", "L") else im
            im.save(
                dst,
                format="JPEG",
                quality=95,
                optimize=True,
                progressive=True,
                subsampling=0,  # 4:4:4
            )
        return True
    except Exception as e:
        logging.error("Pillow JPEG failed for %s: %s", src, e)
        return False

def process_image(src: Path, dst: Path) -> bool:
    """Lossless re-encode for PNG / WebP / TIFF / BMP / GIF."""
    try:
        with Image.open(src) as im:
            ext = src.suffix.lower()

            if ext == ".png":
                im.save(dst, format="PNG", compress_level=6, optimize=True)
            elif ext == ".webp":
                im.save(dst, format="WEBP", lossless=True, method=6)
            elif ext in (".tif", ".tiff"):
                im.save(dst, format="TIFF", compression="tiff_lzw")
            elif ext == ".bmp":
                im.save(dst, format="BMP")
            elif ext == ".gif":
                # GIF is limited; this still changes the file
                im.save(dst, format="GIF", save_all=True, optimize=True)
            else:
                return False
        return True
    except Exception as e:
        logging.error("Image processing failed for %s: %s", src, e)
        return False

def process_audio(src: Path, dst: Path) -> bool:
    """Lossless audio re-encode + strip metadata → FLAC."""
    # Always output FLAC for guaranteed lossless + new hash
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-map_metadata", "-1",          # strip global metadata
        "-c:a", "flac",
        "-compression_level", "8",
        str(dst.with_suffix(".flac")),
    ]
    r = run(cmd)
    if r.returncode == 0:
        # If original wasn't .flac we still succeeded; caller will handle rename if needed
        final = dst.with_suffix(".flac")
        if final != dst and final.exists():
            final.replace(dst)
        return dst.exists()
    return False

def process_video(src: Path, dst: Path) -> bool:
    """
    True lossless video re-encode with FFV1 + FLAC audio + strip metadata.
    Output is always Matroska (.mkv) because it is the best container for FFV1.
    """
    out = dst.with_suffix(".mkv")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-map_metadata", "-1",
        "-map", "0",                    # keep all streams if possible
        "-c:v", "ffv1",
        "-level", "3",
        "-g", "1",                      # intra-only
        "-slices", "16",
        "-slicecrc", "1",
        "-c:a", "flac",
        "-compression_level", "8",
        str(out),
    ]
    r = run(cmd, timeout=7200)  # videos can take a long time
    if r.returncode == 0 and out.exists():
        if out != dst:
            # We force .mkv; remove original extension expectation
            out.replace(dst.with_suffix(".mkv"))
        return True
    logging.error("Video failed %s\n%s", src, r.stderr[-500:] if r.stderr else "")
    return False

# ----------------------------- Main logic --------------------------------

def process_file(path: Path, dry_run: bool, jpegtran: Optional[str]) -> None:
    ext = path.suffix.lower()
    if ext not in ALL_MEDIA:
        return

    original_hash = sha256_file(path)
    original_size = path.stat().st_size

    with tempfile.TemporaryDirectory(dir=path.parent) as tmpdir:
        tmp = Path(tmpdir) / f"reencoded_{path.name}"

        success = False
        new_ext = path.suffix

        if ext in JPEG_EXTS:
            success = process_jpeg(path, tmp, jpegtran)
        elif ext in IMAGE_EXTS:
            success = process_image(path, tmp)
        elif ext in AUDIO_EXTS:
            success = process_audio(path, tmp)
            new_ext = ".flac"
        elif ext in VIDEO_EXTS:
            success = process_video(path, tmp)
            new_ext = ".mkv"

        if not success or not tmp.exists() or tmp.stat().st_size == 0:
            logging.warning("Skipped / failed: %s", path)
            return

        new_hash = sha256_file(tmp)
        new_size = tmp.stat().st_size

        if new_hash == original_hash:
            logging.info("Hash unchanged (already clean?): %s", path)
            return

        logging.info(
            "%s\n  %s → %s\n  size: %d → %d bytes (%.1f%%)",
            path,
            original_hash[:16],
            new_hash[:16],
            original_size,
            new_size,
            (new_size / original_size - 1) * 100,
        )

        if dry_run:
            return

        # Atomic replace
        final_path = path.with_suffix(new_ext) if new_ext != path.suffix else path
        tmp.replace(final_path)

        # If extension changed, remove the old file
        if final_path != path and path.exists():
            path.unlink()

def main():
    parser = argparse.ArgumentParser(
        description="Losslessly re-encode media + strip metadata to change content hashes"
    )
    parser.add_argument("root", type=Path, help="Root directory to process")
    parser.add_argument("--dry-run", action="store_true", help="Do not modify files")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if not args.root.is_dir():
        logging.error("Not a directory: %s", args.root)
        sys.exit(1)

    # Check tools
    if not which("ffmpeg"):
        logging.error("ffmpeg not found. Install it first.")
        sys.exit(1)

    jpegtran = which("jpegtran")
    if not jpegtran:
        logging.warning(
            "jpegtran not found. JPEG files will use Pillow (near-lossless). "
            "Install libjpeg-turbo for true lossless JPEG handling."
        )

    logging.info("Starting %s (dry-run=%s)", args.root, args.dry_run)

    count = 0
    for dirpath, _, filenames in os.walk(args.root):
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix.lower() in ALL_MEDIA:
                process_file(path, args.dry_run, jpegtran)
                count += 1

    logging.info("Finished. Processed %d media files.", count)

if __name__ == "__main__":
    main()