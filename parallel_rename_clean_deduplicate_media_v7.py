#!/usr/bin/env python3
# parallel_rename_clean_deduplicate_media_v7.py
# ==================================================================
# Parallel media cleaner with safe rename -> EXIF strip -> hash -> tidy
#
# v6 notes (2025-10-03):
#   • Groups dedup by media type (image/video) instead of extension, allowing cross-ext duplicate detection (e.g., same image as .jpg and .png).
#   • Keeps all other logic identical to v5.
# ==================================================================

import os, re, sys, shutil, argparse, tempfile, subprocess, time, uuid, atexit
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional, Iterable, Union
from concurrent.futures import ThreadPoolExecutor, as_completed
import importlib.util

# ---------- Auto-install third-party deps (install if missing) ----------
def _ensure_packages():
    # Map import name -> pip package name
    need = {
        "PIL": "Pillow",
        "imagehash": "ImageHash",
        "numpy": "numpy",
    }
    missing = [pip for mod, pip in need.items() if importlib.util.find_spec(mod) is None]
    if missing:
        print(f"[setup] Installing required packages: {', '.join(missing)}", file=sys.stderr)
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", *missing], check=True)
        except subprocess.CalledProcessError:
            # Fallback to --user on locked systems
            subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--upgrade", *missing], check=True)

_ensure_packages()

# Third-party (safe to import now)
from PIL import Image, ImageOps, ImageFile
import imagehash
import numpy as np  # noqa: F401

# Make Pillow tolerant to truncated/corrupt images to reduce hangs
ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = None  # disable DecompressionBomb checks

# ---------- Config / Constants ----------
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".jfif"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".flv", ".3gp", ".wmv", ".mpg", ".mpeg"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

DEFAULT_PHASH_THRESHOLD = 10
DEFAULT_DHASH_THRESHOLD_IMG = 2
DEFAULT_DHASH_THRESHOLD_VID = 7
DEFAULT_HASH_SIZE = 16
DEFAULT_NUM_FRAMES = 3

CPU = (os.cpu_count() or 4)
DEFAULT_WORKERS = max(4, CPU)
DEFAULT_RENAME_WORKERS = max(4, CPU)

DUP_DIRNAME = "Duplicates"
CORRUPT_DIRNAME = "Corrupted"

RE_PATTERN_NUMBERED = re.compile(r"^(?P<base>.+?) \((?P<num>\d+)\)\.(?P<ext>[^.]+)$", re.I)

# ---------- Small FS helpers / logging ----------
def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True); return p

def which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)

def write_log(log_path: Path, lines: Iterable[str]) -> None:
    ensure_dir(log_path.parent)
    with log_path.open("a", encoding="utf-8") as f:
        for ln in lines: f.write(ln.rstrip() + "\n")

def is_media(p: Path) -> bool: return p.is_file() and p.suffix.lower() in MEDIA_EXTS
def is_image(p: Path) -> bool: return p.suffix.lower() in IMAGE_EXTS
def is_video(p: Path) -> bool: return p.suffix.lower() in VIDEO_EXTS

def safe_move(src: Path, dst_dir: Path, new_name: Optional[str] = None) -> Path:
    ensure_dir(dst_dir)
    tgt = dst_dir / (new_name or src.name)
    if tgt.exists():
        stem, suf = tgt.stem, tgt.suffix
        i = 2
        while (cand := tgt.with_name(f"{stem}__{i}{suf}")).exists(): i += 1
        tgt = cand
    return src.rename(tgt)

# ---------- Data classes ----------
@dataclass
class ImageHashes:
    phash: imagehash.ImageHash
    dhash: imagehash.ImageHash

@dataclass
class VideoHashes:
    frames: List[ImageHashes]

Hashes = Union[ImageHashes, VideoHashes]

@dataclass
class FileEntry:
    path: Path
    mtime: float
    is_image: bool
    is_video: bool

# ============================================================
# 1) INITIAL RENAME — PARALLEL (safe two-phase per folder)
# ============================================================
def _collect_folder_media(folder: Path) -> List[FileEntry]:
    out: List[FileEntry] = []
    try:
        files = [p for p in folder.iterdir() if is_media(p)]
    except Exception:
        files = []
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0)
    for p in files:
        try:
            st = p.stat()
            out.append(FileEntry(path=p, mtime=st.st_mtime, is_image=is_image(p), is_video=is_video(p)))
        except Exception:
            continue
    return out

def _next_index_map(folder: Path, entries: List[FileEntry]) -> Dict[str, int]:
    base = folder.name
    highest: Dict[str, int] = {}
    for fe in entries:
        m = RE_PATTERN_NUMBERED.match(fe.path.name)
        if not m or m.group("base") != base: continue
        ext = "." + m.group("ext").lower()
        try: num = int(m.group("num"))
        except Exception: continue
        highest[ext] = max(highest.get(ext, 0), num)
    return {ext: n + 1 for ext, n in highest.items()}

def _plan_initial_renames(folder: Path, entries: List[FileEntry]) -> Dict[Path, Path]:
    base = folder.name
    next_idx = _next_index_map(folder, entries)
    nonconf = [fe for fe in entries if not (m := RE_PATTERN_NUMBERED.match(fe.path.name)) or m.group("base") != base]
    nonconf.sort(key=lambda fe: fe.mtime)
    mapping: Dict[Path, Path] = {}
    for fe in nonconf:
        ext = fe.path.suffix.lower()
        idx = next_idx.get(ext, 1)
        dst = folder / f"{base} ({idx}){ext}"
        next_idx[ext] = idx + 1
        mapping[fe.path] = dst
    return mapping

def _rename_to_tmp(src: Path) -> Path:
    tmp = src.with_name(f"__tmp_init_{uuid.uuid4().hex}_{src.name}")
    i = 2
    while tmp.exists():
        tmp = src.with_name(f"__tmp_init_{uuid.uuid4().hex}_{i}_{src.name}"); i += 1
    return src.rename(tmp)

def step1_rename_parallel(folder: Path, corrupt_dir: Path, workers: int) -> List[FileEntry]:
    entries = _collect_folder_media(folder)
    if not entries: return []
    plan = _plan_initial_renames(folder, entries)
    if not plan:
        return entries

    # Phase A: to temp (parallel)
    tmp_to_final: Dict[Path, Path] = {}
    errors: List[Tuple[Path, str]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_rename_to_tmp, src): (src, dst) for src, dst in plan.items()}
        for fut in as_completed(futs):
            src, dst = futs[fut]
            try:
                tmp = fut.result()
                tmp_to_final[tmp] = dst
            except Exception as e:
                errors.append((src, str(e)))

    for src, _ in errors:
        try: safe_move(src, corrupt_dir)
        except Exception: pass

    # Phase B: temp → final (parallel)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(lambda t, d: t.rename(d), t, d): (t, d) for t, d in tmp_to_final.items()}
        for fut in as_completed(futs):
            t, d = futs[fut]
            try:
                fut.result()
            except Exception:
                try: safe_move(t, corrupt_dir)
                except Exception: pass

    return _collect_folder_media(folder)

# ============================================================
# 2) STRIP EXIF — PARALLEL (images) — defensive
# ============================================================
def _strip_exif_one(path: Path) -> Tuple[Path, Optional[str]]:
    if path.suffix.lower() == ".gif":
        return path, None
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            fmt = (im.format or path.suffix.replace(".", "")).upper()
            if fmt in ("JPG", "JPEG"):
                im.convert("RGB").save(path, format="JPEG", quality=95, optimize=True, exif=b"")
            elif fmt == "PNG":
                im.save(path, format="PNG", optimize=True)
            elif fmt == "WEBP":
                im.save(path, format="WEBP", quality=95, method=6)
            else:
                im.save(path)
        return path, None
    except Exception as e:
        return path, str(e)

def step2_strip_exif_parallel(entries: List[FileEntry], corrupt_dir: Path, workers: int) -> None:
    imgs = [fe.path for fe in entries if fe.is_image]
    if not imgs: return
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_strip_exif_one, p) for p in imgs]
        for fut in as_completed(futs):
            p, err = fut.result()
            if err:
                try: safe_move(p, corrupt_dir)
                except Exception: pass

# ============================================================
# 3) HASH (PARALLEL) → COMPARE (SEQUENTIAL)
#   • If hashing fails (image/video), mark as bad; caller will move to Corrupted.
# ============================================================
def _compute_image_hashes(path: Path, hash_size: int) -> Optional[ImageHashes]:
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            return ImageHashes(
                phash=imagehash.phash(im, hash_size=hash_size),
                dhash=imagehash.dhash(im, hash_size=hash_size)
            )
    except Exception:
        return None

def _probe_duration(ffprobe: str, path: Path) -> float:
    try:
        out = subprocess.check_output(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            stderr=subprocess.STDOUT
        )
        return float(out.strip())
    except Exception:
        return 10.0

def _extract_frame_at(ffmpeg: str, src: Path, t: float, out_dir: Path, idx: int) -> Optional[Path]:
    out_path = out_dir / f"frame_{idx:02d}.png"
    try:
        # Hardened flags to reduce decoder noise/hangs
        cmd = [
            ffmpeg, "-y", "-loglevel", "error",
            "-ss", f"{t:.3f}", "-analyzeduration", "0", "-probesize", "500000",
            "-err_detect", "ignore_err",
            "-i", str(src),
            "-frames:v", "1", "-q:v", "2", str(out_path)
        ]
        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return out_path if out_path.exists() else None
    except Exception:
        return None

def _compute_video_hashes(path: Path, hash_size: int, num_frames: int, ffmpeg: str, ffprobe: str) -> Optional[VideoHashes]:
    dur = _probe_duration(ffprobe, path)
    if dur <= 0:
        dur = 10.0
    times = [(dur * (i + 1) / (num_frames + 1)) for i in range(num_frames)]
    tmp = Path(tempfile.mkdtemp(prefix="vidframes_"))
    frames: List[ImageHashes] = []
    try:
        for i, t in enumerate(times):
            fp = _extract_frame_at(ffmpeg, path, t, tmp, i)
            if not fp:
                continue
            ih = _compute_image_hashes(fp, hash_size)
            if not ih:
                continue
            frames.append(ih)
        if not frames:
            return None
        return VideoHashes(frames=frames)
    finally:
        # Immediate cleanup of per-video temp directory
        shutil.rmtree(tmp, ignore_errors=True)

def _compute_hashes_worker(fe: FileEntry, hash_size: int, num_frames: int, ffmpeg: Optional[str], ffprobe: Optional[str]) -> Tuple[Path, Optional[Hashes], bool]:
    """Return (path, hashes_or_None, bad_flag)."""
    if fe.is_image:
        h = _compute_image_hashes(fe.path, hash_size)
        return fe.path, h, (h is None)
    if fe.is_video and ffmpeg and ffprobe:
        h = _compute_video_hashes(fe.path, hash_size, num_frames, ffmpeg, ffprobe)
        return fe.path, h, (h is None)
    # If it's a video but ffmpeg unavailable, treat as 'bad' so it gets quarantined.
    if fe.is_video and (not ffmpeg or not ffprobe):
        return fe.path, None, True
    return fe.path, None, False

def step3_compute_hashes_parallel(entries: List[FileEntry], hash_size: int, num_frames: int, workers: int
                                  ) -> Tuple[Dict[Path, Optional[Hashes]], List[Path]]:
    ffmpeg, ffprobe = which("ffmpeg"), which("ffprobe")
    if not ffmpeg or not ffprobe:
        print("[warn] ffmpeg/ffprobe not found → video hashing skipped; videos will be moved to Corrupted.", file=sys.stderr)
    out: Dict[Path, Optional[Hashes]] = {}
    bad: List[Path] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_compute_hashes_worker, fe, hash_size, num_frames, ffmpeg, ffprobe) for fe in entries]
        for fut in as_completed(futs):
            p, h, is_bad = fut.result()
            out[p] = h
            if is_bad:
                bad.append(p)
    return out, bad

def _images_similar(a: ImageHashes, b: ImageHashes, pth: int, dth: int) -> Tuple[bool, int, int]:
    pd, dd = int(a.phash - b.phash), int(a.dhash - b.dhash)
    return (pd <= pth and dd <= dth), pd, dd

def _videos_similar(a: VideoHashes, b: VideoHashes, pth: int, dth: int) -> Tuple[bool, List[Tuple[int, int]]]:
    if len(a.frames) != len(b.frames):
        return False, []
    dists: List[Tuple[int, int]] = []
    for fa, fb in zip(a.frames, b.frames):
        pd, dd = int(fa.phash - fb.phash), int(fa.dhash - fb.dhash)
        dists.append((pd, dd))
        if not (pd <= pth and dd <= dth): return False, dists
    return True, dists

def step3_compare_and_dedupe_sequential(folder: Path, entries: List[FileEntry], hashes: Dict[Path, Optional[Hashes]],
                                        dup_dir: Path, log_path: Path, pth: int, dth_img: int, dth_vid: int) -> None:
    by_type: Dict[str, List[FileEntry]] = {'image': [], 'video': []}
    for fe in entries:
        if fe.is_image:
            by_type['image'].append(fe)
        elif fe.is_video:
            by_type['video'].append(fe)

    for typ, group in by_type.items():
        if not group: continue
        group.sort(key=lambda fe: fe.mtime)
        kept: List[FileEntry] = []
        for fe in group:
            h = hashes.get(fe.path)
            if h is None:  # couldn't hash (should already be moved to Corrupted by caller)
                continue
            is_dup = False; lines: List[str] = []
            for base in kept:
                hb = hashes.get(base.path)
                if hb is None: continue
                if typ == 'image' and isinstance(h, ImageHashes) and isinstance(hb, ImageHashes):
                    ok, pd, dd = _images_similar(h, hb, pth, dth_img)
                    if ok:
                        is_dup = True
                        lines.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | IMG DUP | base={base.path} | dup={fe.path} | phash={pd} dhash={dd}")
                        break
                elif typ == 'video' and isinstance(h, VideoHashes) and isinstance(hb, VideoHashes):
                    ok, dists = _videos_similar(h, hb, pth, dth_vid)
                    if ok:
                        is_dup = True
                        ds = " ".join([f"[p{p}/d{d}]" for p, d in dists])
                        lines.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | VID DUP | base={base.path} | dup={fe.path} | {ds}")
                        break
            if is_dup:
                try:
                    safe_move(fe.path, dup_dir, new_name=f"{folder.name}_duplicate_{fe.path.name}")
                    if lines: write_log(log_path, lines)
                except Exception as e:
                    write_log(log_path, [f"{time.strftime('%Y-%m-%d %H:%M:%S')} | ERROR moving dup | {fe.path} | {e}"])
            else:
                kept.append(fe)

# ============================================================
# 4) RE-RENAME — SEQUENTIAL (tidy numbering per ext)
# ============================================================
def _two_phase_rename(mapping: Dict[Path, Path]) -> None:
    tmp_map: Dict[Path, Path] = {}
    for src, dst in mapping.items():
        if src == dst: continue
        tmp = src.with_name(f"__tmp__{src.name}"); i = 2
        while tmp.exists(): tmp = src.with_name(f"__tmp__{i}__{src.name}"); i += 1
        src.rename(tmp); tmp_map[tmp] = dst
    for tmp, dst in tmp_map.items():
        if dst.exists(): dst.unlink()
        tmp.rename(dst)

def step4_rerename_tidy(folder: Path) -> None:
    entries = _collect_folder_media(folder)
    if not entries: return
    by_ext: Dict[str, List[FileEntry]] = {}
    for fe in entries: by_ext.setdefault(fe.path.suffix.lower(), []).append(fe)
    base = folder.name
    mapping: Dict[Path, Path] = {}
    for ext, group in by_ext.items():
        group.sort(key=lambda fe: fe.mtime)
        for i, fe in enumerate(group, 1):
            final = folder / f"{base} ({i}){ext}"
            if fe.path != final: mapping[fe.path] = final
    if mapping: _two_phase_rename(mapping)

# ---------- Folder discovery / Orchestration ----------
def iter_media_folders(root: Path) -> List[Path]:
    folders: List[Path] = []
    try:
        if any(is_media(p) for p in root.iterdir() if p.is_file()):
            folders.append(root)
    except Exception:
        pass
    for p in root.rglob("*"):
        if not p.is_dir(): continue
        if p.name in (DUP_DIRNAME, CORRUPT_DIRNAME): continue
        try:
            has_media = any(is_media(c) for c in p.iterdir() if c.is_file())
        except Exception:
            has_media = False
        if has_media: folders.append(p)
    folders.sort(key=lambda x: str(x).lower())
    return folders

# Best-effort temp sweep at exit (in case of abrupt stop)
def _sweep_leftover_temp_dirs():
    tdir = Path(tempfile.gettempdir())
    for d in tdir.glob("vidframes_*"):
        try: shutil.rmtree(d, ignore_errors=True)
        except Exception: pass
atexit.register(_sweep_leftover_temp_dirs)

def main():
    ap = argparse.ArgumentParser(description="Parallel media cleaner & deduplicator (v6).")
    ap.add_argument("--root-dir", type=str, default="Content")
    ap.add_argument("--phash-threshold", type=int, default=DEFAULT_PHASH_THRESHOLD)
    ap.add_argument("--dhash-img-threshold", type=int, default=DEFAULT_DHASH_THRESHOLD_IMG)
    ap.add_argument("--dhash-vid-threshold", type=int, default=DEFAULT_DHASH_THRESHOLD_VID)
    ap.add_argument("--hash-size", type=int, default=DEFAULT_HASH_SIZE)
    ap.add_argument("--num-frames", type=int, default=DEFAULT_NUM_FRAMES)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="Parallel workers for EXIF/hash.")
    ap.add_argument("--rename-workers", type=int, default=DEFAULT_RENAME_WORKERS, help="Parallel workers for initial rename.")
    args = ap.parse_args()

    root = Path(args.root_dir).resolve()
    if not root.exists():
        print(f"[error] Root not found: {root}", file=sys.stderr); sys.exit(1)

    dup_dir = ensure_dir(root / DUP_DIRNAME)
    corrupt_dir = ensure_dir(root / CORRUPT_DIRNAME)
    log_path = root / "deduplication_log.txt"

    folders = iter_media_folders(root)
    if not folders:
        print("[info] No media folders found."); return

    print(f"[info] Processing {len(folders)} folder(s) under: {root}")
    for folder in folders:
        if folder.name in (DUP_DIRNAME, CORRUPT_DIRNAME): continue
        print(f"\n[folder] {folder}")

        # 1) Initial rename (parallel within folder; two-phase)
        entries = step1_rename_parallel(folder, corrupt_dir, workers=args.rename_workers)
        if not entries: print("  - No media after rename."); continue

        # 2) Strip EXIF (parallel)
        step2_strip_exif_parallel(entries, corrupt_dir, workers=args.workers)

        # Re-collect (some may have moved to Corrupted)
        entries = _collect_folder_media(folder)
        if not entries: print("  - No media after EXIF step."); continue

        # 3a) Hash (parallel) — collect 'bad' files (hash failures)
        hashes, bad_files = step3_compute_hashes_parallel(entries, args.hash_size, args.num_frames, args.workers)

        # 3a.1) Immediately quarantine any 'bad' files to Corrupted
        for bp in bad_files:
            try: safe_move(Path(bp), corrupt_dir)
            except Exception: pass
            # Also drop from hashes so later steps ignore them
            hashes.pop(Path(bp), None)

        # Re-scan entries after moving 'bad' ones
        entries = _collect_folder_media(folder)
        if not entries: print("  - No media after hash step."); continue

        # 3b) Compare (sequential) + move dupes
        step3_compare_and_dedupe_sequential(
            folder, entries, hashes, dup_dir, log_path,
            pth=args.phash_threshold, dth_img=args.dhash_img_threshold, dth_vid=args.dhash_vid_threshold
        )

        # 4) Re-rename tidy (sequential)
        step4_rerename_tidy(folder)

        # Nothing else persists for this folder — temp resources already cleared by 'finally' blocks.

    print("\n[done] Processing complete.")

if __name__ == "__main__":
    main()


