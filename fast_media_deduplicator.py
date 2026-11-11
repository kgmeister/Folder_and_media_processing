#!/usr/bin/env python3
r"""
========================================================================
 FAST MEDIA DEDUPLICATOR (BK-Tree Edition) 
========================================================================
A blazing fast script to recursively find, rename, and deduplicate 
images and videos using perceptual hashing and BK-Trees.

REQUIREMENTS:
    pip install Pillow imagehash

HOW TO RUN:
1. By default, it will look for a folder named "Content" right next 
   to this script and process it:
   
    python fast_media_deduplicator.py

2. If your media folder has a different name or is in a different 
   directory, use the --root-dir command line argument:

    python fast_media_deduplicator.py --root-dir "/path/to/your/photos"

   (Remember to put your path in quotes if it contains spaces!)
========================================================================
"""
import os, re, sys, shutil, argparse, tempfile, subprocess, time, uuid, atexit, logging, importlib.util
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional, Iterable, Union
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed

def _ensure_packages():
    need = {"PIL": "Pillow", "imagehash": "ImageHash"}
    missing = [pip for mod, pip in need.items() if importlib.util.find_spec(mod) is None]
    if missing:
        print(f"[setup] Auto-installing required packages: {', '.join(missing)}", file=sys.stderr)
        try: subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", *missing], check=True)
        except subprocess.CalledProcessError:
            subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--upgrade", *missing], check=True)

_ensure_packages()
from PIL import Image, ImageOps, ImageFile
import imagehash

# Pillow configuration
ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = None

# Config
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".jfif"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".flv", ".3gp", ".wmv", ".mpg", ".mpeg"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

DEFAULT_PHASH_THRESHOLD = 10
DEFAULT_DHASH_THRESHOLD_IMG = 2
DEFAULT_DHASH_THRESHOLD_VID = 7
DEFAULT_HASH_SIZE = 16
DEFAULT_NUM_FRAMES = 3

CPU_COUNT = (os.cpu_count() or 4)
DUP_DIRNAME = "Duplicates"
CORRUPT_DIRNAME = "Corrupted"

RE_PATTERN_NUMBERED = re.compile(r"^(?P<base>.+?) \((?P<num>\d+)\)\.(?P<ext>[^.]+)$", re.I)

# Logger setup
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# Best-effort temp sweep at exit (in case of abrupt crash/Ctrl+C)
def _sweep_leftover_temp_dirs():
    tdir = Path(tempfile.gettempdir())
    for d in tdir.glob("vf_*"):
        try: shutil.rmtree(d, ignore_errors=True)
        except OSError: pass
atexit.register(_sweep_leftover_temp_dirs)

# ---------- FS / Logging Helpers ----------
def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p

def which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)

def setup_file_logger(log_path: Path):
    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.setFormatter(logging.Formatter("%(asctime)s | %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(fh)

def is_media(p: Path) -> bool: return p.is_file() and p.suffix.lower() in MEDIA_EXTS
def is_image(p: Path) -> bool: return p.suffix.lower() in IMAGE_EXTS
def is_video(p: Path) -> bool: return p.suffix.lower() in VIDEO_EXTS

def safe_move(src: Path, dst_dir: Path, new_name: Optional[str] = None) -> Path:
    ensure_dir(dst_dir)
    tgt = dst_dir / (new_name or src.name)
    if tgt.exists():
        stem, suf = tgt.stem, tgt.suffix
        uid = uuid.uuid4().hex[:6]
        tgt = dst_dir / f"{stem}_{uid}{suf}"
    try:
        return src.rename(tgt)
    except OSError:
        pass

# ---------- Data Classes ----------
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

# ---------- BK-Tree Implementation ----------
class BKNode:
    __slots__ = ['hash_val', 'payload', 'children']
    def __init__(self, hash_val: imagehash.ImageHash, payload: FileEntry):
        self.hash_val = hash_val
        self.payload = payload
        self.children = {}

class BKTree:
    def __init__(self):
        self.root = None
    
    def add(self, hash_val: imagehash.ImageHash, payload: FileEntry):
        if self.root is None:
            self.root = BKNode(hash_val, payload)
            return
        curr = self.root
        while True:
            dist = int(hash_val - curr.hash_val)
            if dist not in curr.children:
                curr.children[dist] = BKNode(hash_val, payload)
                break
            curr = curr.children[dist]

    def search(self, hash_val: imagehash.ImageHash, threshold: int) -> List[Tuple[int, BKNode]]:
        if self.root is None:
            return []
        candidates = [self.root]
        results = []
        while candidates:
            curr = candidates.pop()
            dist = int(hash_val - curr.hash_val)
            if dist <= threshold:
                results.append((dist, curr))
            
            low, high = dist - threshold, dist + threshold
            for d, child in curr.children.items():
                if low <= d <= high:
                    candidates.append(child)
        return results

# ---------- Core Processing Functions ----------
def _collect_folder(folder: Path) -> List[FileEntry]:
    try: files = [p for p in folder.iterdir() if is_media(p)]
    except Exception: return []
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0)
    
    out = []
    for p in files:
        try:
            out.append(FileEntry(p, p.stat().st_mtime, is_image(p), is_video(p)))
        except OSError: pass
    return out

def _rename_to_tmp(src: Path) -> tuple:
    uid = uuid.uuid4().hex[:6]
    tmp = src.with_name(f"__tmp_{uid}_{src.name}")
    return src.rename(tmp), src

def step1_rename(folder: Path, entries: List[FileEntry], workers: int) -> List[FileEntry]:
    base = folder.name
    highest, mapping = {}, {}
    
    # Find next indices
    for fe in entries:
        m = RE_PATTERN_NUMBERED.match(fe.path.name)
        if m and m.group("base") == base:
            ext = "." + m.group("ext").lower()
            try: highest[ext] = max(highest.get(ext, 0), int(m.group("num")))
            except ValueError: pass
            
    next_idx = {e: n + 1 for e, n in highest.items()}
    nonconf = [fe for fe in entries if not (m := RE_PATTERN_NUMBERED.match(fe.path.name)) or m.group("base") != base]
    
    for fe in sorted(nonconf, key=lambda f: f.mtime):
        ext = fe.path.suffix.lower()
        idx = next_idx.get(ext, 1)
        next_idx[ext] = idx + 1
        mapping[fe.path] = folder / f"{base} ({idx}){ext}"

    if not mapping: return entries

    tmp_to_final = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(_rename_to_tmp, s) for s in mapping]):
            try:
                tmp, src = fut.result()
                tmp_to_final[tmp] = mapping[src]
            except OSError: pass

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(lambda t, d: t.rename(d), t, d) for t, d in tmp_to_final.items()]):
            try: fut.result()
            except OSError: pass
            
    return _collect_folder(folder)

def _strip_exif_one(path: Path) -> Tuple[Path, Optional[str]]:
    if path.suffix.lower() == ".gif": return path, None
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            fmt = (im.format or path.suffix.strip(".")).upper()
            if fmt in ("JPG", "JPEG"): im.convert("RGB").save(path, format="JPEG", quality=95, optimize=True, exif=b"")
            elif fmt == "PNG": im.save(path, format="PNG", optimize=True)
            elif fmt == "WEBP": im.save(path, format="WEBP", quality=95, method=6)
            else: im.save(path)
        return path, None
    except Exception as e:
        return path, str(e)

def _compute_hashes_worker(path: Path, is_vid: bool, hsize: int, frames: int, 
                           ffmpeg: str, ffprobe: str) -> Tuple[Path, Optional[Hashes], bool]:
    if not is_vid:
        try:
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                return path, ImageHashes(imagehash.phash(im, hash_size=hsize), imagehash.dhash(im, hash_size=hsize)), False
        except Exception: return path, None, True

    if not ffmpeg: return path, None, True

    try:
        dur_out = subprocess.check_output([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)], stderr=subprocess.STDOUT)
        dur = max(float(dur_out.strip()), 1.0)
        
        # Batch extract via fps filter
        tmp_dir = Path(tempfile.mkdtemp(prefix="vf_"))
        rate = frames / dur
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(path), "-vf", f"fps={rate}", "-frames:v", str(frames), "-q:v", "2", str(tmp_dir / "f_%03d.png")]
        subprocess.run(cmd, check=False)
        
        res_frames = []
        for fp in sorted(tmp_dir.glob("*.png")):
            try:
                with Image.open(fp) as im:
                    im.convert("RGB")
                    res_frames.append(ImageHashes(imagehash.phash(im, hash_size=hsize), imagehash.dhash(im, hash_size=hsize)))
            except Exception: pass
            
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return path, (VideoHashes(res_frames) if len(res_frames) > 0 else None), len(res_frames) == 0
    except Exception:
        return path, None, True

def run_dedupe(folder: Path, dup_dir: Path, corrupt_dir: Path, workers: int, args):
    entries = step1_rename(folder, entries=_collect_folder(folder), workers=workers)
    
    # EXIF Stripping (CPU Bound)
    imgs = [e for e in entries if e.is_image]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(_strip_exif_one, e.path) for e in imgs]):
            path, err = fut.result()
            if err: safe_move(path, corrupt_dir)
            
    entries = _collect_folder(folder)
    
    # Hashing (CPU Bound + Subprocesses)
    hashes: Dict[Path, Hashes] = {}
    ffmpeg, ffprobe = which("ffmpeg"), which("ffprobe")
    
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_compute_hashes_worker, e.path, e.is_video, args.hash_size, args.num_frames, ffmpeg, ffprobe) for e in entries]
        for fut in as_completed(futs):
            path, h_obj, bad = fut.result()
            if bad: safe_move(path, corrupt_dir)
            if h_obj: hashes[path] = h_obj

    entries = _collect_folder(folder)
    
    # Fast Deduplication via BK-Tree
    tree_img = BKTree()
    tree_vid = BKTree() # Video tree indexes the first frame's phash
    
    for fe in sorted(entries, key=lambda x: x.mtime):
        h = hashes.get(fe.path)
        if not h: continue

        is_dup = False
        if fe.is_image:
            candidates = tree_img.search(h.phash, args.phash_threshold)
            for pd, node in candidates:
                dd = int(h.dhash - node.hash_val.dhash)
                if dd <= args.dhash_img_threshold:
                    is_dup = True
                    log.info(f"IMG DUP | base={node.payload.path.name} | dup={fe.path.name} | p={pd} d={dd}")
                    break
            if not is_dup:
                # Add root hash, passing the full Hash object so dhash is preserved
                payload_node = BKNode(h.phash, fe)
                payload_node.hash_val = h # Dirty cast to carry the full object
                tree_img.add(h.phash, fe)
                
        elif fe.is_video:
            h_vid = h.frames[0]
            candidates = tree_vid.search(h_vid.phash, args.phash_threshold)
            for pd, node in candidates:
                base_h = hashes[node.payload.path]
                if len(h.frames) == len(base_h.frames):
                    dists = [(int(fa.phash-fb.phash), int(fa.dhash-fb.dhash)) for fa, fb in zip(h.frames, base_h.frames)]
                    if all(p <= args.phash_threshold and d <= args.dhash_vid_threshold for p, d in dists):
                        is_dup = True
                        log.info(f"VID DUP | base={node.payload.path.name} | dup={fe.path.name} | dists={[d for d in dists]}")
                        break
            if not is_dup:
                payload_node = BKNode(h_vid.phash, fe)
                payload_node.hash_val = h # Dirty cast
                tree_vid.add(h_vid.phash, fe)
                
        if is_dup:
            safe_move(fe.path, dup_dir, new_name=f"{folder.name}_dup_{fe.path.name}")

    # Final rename
    step1_rename(folder, _collect_folder(folder), workers)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root-dir", type=str, default="Content")
    ap.add_argument("--phash-threshold", type=int, default=DEFAULT_PHASH_THRESHOLD)
    ap.add_argument("--dhash-img-threshold", type=int, default=DEFAULT_DHASH_THRESHOLD_IMG)
    ap.add_argument("--dhash-vid-threshold", type=int, default=DEFAULT_DHASH_THRESHOLD_VID)
    ap.add_argument("--hash-size", type=int, default=DEFAULT_HASH_SIZE)
    ap.add_argument("--num-frames", type=int, default=DEFAULT_NUM_FRAMES)
    args = ap.parse_args()

    root = Path(args.root_dir).resolve()
    if not root.exists():
        log.error(f"Root not found: {root}")
        sys.exit(1)

    dup_dir = ensure_dir(root / DUP_DIRNAME)
    corrupt_dir = ensure_dir(root / CORRUPT_DIRNAME)
    setup_file_logger(root / "dedup_log.txt")

    folders = [p for p in root.rglob("*") if p.is_dir() and p.name not in (DUP_DIRNAME, CORRUPT_DIRNAME)]
    if root not in folders: folders.insert(0, root)

    processed = 0
    for fld in folders:
        try:
            if not any(is_media(c) for c in fld.iterdir()): continue
            log.info(f"Processing: {fld}")
            run_dedupe(fld, dup_dir, corrupt_dir, CPU_COUNT, args)
            processed += 1
        except OSError: pass

    # === Completion Message ===
    print(f"\n Processing complete! {processed} folder(s) processed.")
    print(f"   Duplicates folder → {dup_dir}")
    print(f"   Corrupted folder  → {corrupt_dir}")
    print(f"   Log file          → {root / 'dedup_log.txt'}")

if __name__ == "__main__":
    main()
