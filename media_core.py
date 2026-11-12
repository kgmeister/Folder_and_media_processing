#!/usr/bin/env python3
r"""
========================================================================
 MEDIA CORE - processing engine for the Secure Media Processor
========================================================================
Used by app.py (desktop GUI) and secure_media_processor.py (command line).
Entry point: run_pipeline(root, Options(...), progress, cancel).

Combines parallel_rename_clean_deduplicate_media_v7.py (perceptual-hash
dedupe + tidy renaming; its hashing and similarity tests are copied verbatim,
with a BK-Tree to skip comparisons that cannot match - tested to give v7's
exact decisions), the metadata strip + lossless re-encode script
and merge_folders_with_prefix.py (folder merge) into one engine.

Steps (fixed order, each optional):
  1. Folder merge       - '@name' folders merged into 'name'; optional prefix
  2. Metadata strip     - lossless; EXIF/GPS/XMP/IPTC/comments, container
                          tags, chapters, cover art, telemetry streams
  3. Deduplication      - p-hash/d-hash + Hamming distance, per folder;
                          duplicates -> "Duplicates", unreadable -> "Corrupted"
  4. Lossless re-encode - pixel/sample-exact; if that can't change the bytes,
                          16 random padding bytes are added instead
  5. Renaming           - "<folder name> (N).<ext>", oldest first, no gaps
  Optional hash check: sha256 of every file before and after, unchanged
  files are listed.

Every rewritten file is verified (decoded pixels / frames / packets must
match the original) before it replaces the original, and the original's
modification time is kept so "oldest first" ordering stays correct.

REQUIREMENTS:
    pip install Pillow imagehash          (auto-installed if missing)
    ffmpeg + ffprobe on PATH or in tools/ (video/audio steps, video dedupe)
    jpegtran on PATH or in tools/         (JPEG lossless re-encode)
========================================================================
"""
import os, sys, shutil, tempfile, subprocess, time, uuid, logging, importlib.util, threading
import io, struct, zlib, hashlib
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Dict, List, Tuple, Optional, Union
from concurrent.futures import Executor, ThreadPoolExecutor, ProcessPoolExecutor, as_completed

def _ensure_packages():
    need = {"PIL": "Pillow", "imagehash": "ImageHash"}
    missing = [pip for mod, pip in need.items() if importlib.util.find_spec(mod) is None]
    if missing:
        print(f"[setup] Auto-installing required packages: {', '.join(missing)}", file=sys.stderr)
        try: subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", *missing], check=True)
        except subprocess.CalledProcessError:
            subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--upgrade", *missing], check=True)

_ensure_packages()
from PIL import Image, ImageOps, ImageFile, PngImagePlugin
import imagehash

# Pillow configuration
ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = None

# Config
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".jfif", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".flv", ".3gp", ".wmv", ".mpg", ".mpeg", ".webm"}
AUDIO_EXTS = {".mp3", ".flac", ".wav", ".aiff", ".aif", ".m4a", ".ogg", ".opus"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS

JPEG_EXTS = {".jpg", ".jpeg", ".jfif"}
LOSSLESS_AUDIO_EXTS = {".flac", ".wav", ".aiff", ".aif"}   # plus .m4a when the codec is ALAC

DEFAULT_PHASH_THRESHOLD = 10
DEFAULT_DHASH_THRESHOLD_IMG = 2
DEFAULT_DHASH_THRESHOLD_VID = 7
DEFAULT_HASH_SIZE = 16
DEFAULT_NUM_FRAMES = 3

CPU_COUNT = (os.cpu_count() or 4)
DUP_DIRNAME = "Duplicates"
CORRUPT_DIRNAME = "Corrupted"
SKIP_DIRNAMES = (DUP_DIRNAME, CORRUPT_DIRNAME)
TMP_PREFIX = ".mp_tmp_"          # in-progress output files; swept on startup
LOG_NAME = "media_processor_log.txt"
VIDEO_REENCODE_MIN_FREE_FACTOR = 10   # need 10x the source size free before an FFV1 encode

# Logger: the CLI and the GUI attach their own handlers
log = logging.getLogger("media_core")
log.setLevel(logging.INFO)

# Sweep of v7's per-video frame folders, run when a pipeline finishes (normally or not). Only called
# from the main process, so pool workers never sweep folders other workers are still using.
def _sweep_leftover_temp_dirs():
    tdir = Path(tempfile.gettempdir())
    for d in tdir.glob("vidframes_*"):
        try: shutil.rmtree(d, ignore_errors=True)
        except OSError: pass

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
    return fh

def is_media(p: Path) -> bool: return p.is_file() and p.suffix.lower() in MEDIA_EXTS
def is_image(p: Path) -> bool: return p.suffix.lower() in IMAGE_EXTS
def is_video(p: Path) -> bool: return p.suffix.lower() in VIDEO_EXTS
def is_audio(p: Path) -> bool: return p.suffix.lower() in AUDIO_EXTS

def safe_move(src: Path, dst_dir: Path, new_name: Optional[str] = None) -> Optional[Path]:
    ensure_dir(dst_dir)
    tgt = dst_dir / (new_name or src.name)
    if tgt.exists():
        stem, suf = tgt.stem, tgt.suffix
        uid = uuid.uuid4().hex[:6]
        tgt = dst_dir / f"{stem}_{uid}{suf}"
    try:
        return src.rename(tgt)
    except OSError as e:
        log.warning(f"Could not move {src} -> {tgt}: {e}")
        return None

def sha256_bytes(data: bytes) -> str: return hashlib.sha256(data).hexdigest()

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def media_folders(root: Path) -> List[Path]:
    folders = [p for p in root.rglob("*") if p.is_dir() and not (set(p.relative_to(root).parts) & set(SKIP_DIRNAMES))]
    folders.insert(0, root)
    return folders

def media_files(root: Path) -> List[Path]:
    out = []
    for fld in media_folders(root):
        try: out.extend(sorted(p for p in fld.iterdir() if is_media(p) and not p.name.startswith(TMP_PREFIX)))
        except OSError: pass
    return out

def tmp_path_for(final: Path) -> Path:
    return final.with_name(f"{TMP_PREFIX}{uuid.uuid4().hex[:8]}{final.suffix}")

def free_name(p: Path) -> Path:
    if not p.exists(): return p
    k = 1
    while (cand := p.with_name(f"{p.stem}_{k}{p.suffix}")).exists(): k += 1
    return cand

def commit(tmp: Path, original: Path, final: Path, st: os.stat_result):
    """Move a verified temp file over the original, keeping the original's timestamps."""
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(tmp, final)
    if final != original:
        original.unlink()

def discard(tmp: Path):
    try: tmp.unlink()
    except FileNotFoundError: pass
    except OSError as e: log.warning(f"Could not remove temp file {tmp}: {e}")

def run_cmd(cmd: list, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)

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

@dataclass
class StepStats:
    changed: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0

    def add(self, status: str):
        setattr(self, status, getattr(self, status) + 1)

    def __str__(self):
        return f"changed={self.changed} unchanged={self.unchanged} skipped={self.skipped} failed={self.failed}"

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

# ============================================================
# HASHING - verbatim from parallel_rename_clean_deduplicate_media_v7.py (the battle-tested version).
# Video frames are sampled at 1/(n+1), 2/(n+1), ... of the duration (25/50/75% for 3 frames).
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

# ============================================================
# COMPARISON - the similarity tests are verbatim from v7.
# ============================================================
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

def find_duplicates(entries: List[FileEntry], hashes: Dict[Path, Hashes], pth: int, dth_img: int, dth_vid: int
                    ) -> List[Tuple[FileEntry, FileEntry, str]]:
    """Same decisions as v7's step3_compare_and_dedupe_sequential, which compares every file (oldest first)
    against every file kept so far and takes the first match. Here a BK-tree on the phash (images) or on the
    first frame's phash (videos) prunes kept files that can't match - v7's tests require that distance
    <= pth anyway. Survivors are tested with v7's own functions in v7's order, so both the verdict and the
    reported base file are identical. Returns (duplicate, base, distances)."""
    dups: List[Tuple[FileEntry, FileEntry, str]] = []
    trees = {"image": BKTree(), "video": BKTree()}
    kept_order: Dict[Path, int] = {}
    group = [e for e in entries if e.is_image or e.is_video]
    group.sort(key=lambda fe: fe.mtime)
    for fe in group:
        h = hashes.get(fe.path)
        if h is None: continue
        typ = "image" if fe.is_image else "video"
        if not isinstance(h, ImageHashes if typ == "image" else VideoHashes): continue
        key = h.phash if typ == "image" else h.frames[0].phash
        candidates = sorted((node.payload for _, node in trees[typ].search(key, pth)), key=lambda b: kept_order[b.path])
        match = None
        for base in candidates:
            hb = hashes[base.path]
            if typ == "image":
                ok, pd, dd = _images_similar(h, hb, pth, dth_img)
                detail = f"phash={pd} dhash={dd}"
            else:
                ok, dists = _videos_similar(h, hb, pth, dth_vid)
                detail = " ".join(f"[p{p}/d{d}]" for p, d in dists)
            if ok:
                match = (fe, base, detail)
                break
        if match:
            dups.append(match)
        else:
            kept_order[fe.path] = len(kept_order)
            trees[typ].add(key, fe)
    return dups

def run_dedupe(folder: Path, dup_dir: Path, corrupt_dir: Path, pool: Executor, args) -> Tuple[int, int]:
    entries = _collect_folder(folder)
    ffmpeg, ffprobe = which("ffmpeg"), which("ffprobe")
    # Audio is never hashed. Unlike v7, videos are only hashed when ffmpeg+ffprobe exist, so a missing
    # ffmpeg leaves them in place instead of quarantining every video to Corrupted.
    hashable = [e for e in entries if e.is_image or (e.is_video and ffmpeg and ffprobe)]
    hashes: Dict[Path, Hashes] = {}
    n_bad = 0
    futs = [pool.submit(_compute_hashes_worker, e, args.hash_size, args.num_frames, ffmpeg, ffprobe) for e in hashable]
    for fut in as_completed(futs):
        path, h, bad = fut.result()
        if bad:
            log.info(f"CORRUPT | unreadable, moved to {CORRUPT_DIRNAME}: {path}")
            safe_move(path, corrupt_dir)
            n_bad += 1
        elif h is not None:
            hashes[path] = h

    dups = find_duplicates(_collect_folder(folder), hashes, args.phash_threshold, args.dhash_img_threshold, args.dhash_vid_threshold)
    for dup, base, detail in dups:
        kind = "IMG" if dup.is_image else "VID"
        log.info(f"{kind} DUP | base={base.path.name} | dup={dup.path.name} | {detail}")
        safe_move(dup.path, dup_dir, new_name=f"{folder.name}_duplicate_{dup.path.name}")
    return len(dups), n_bad

# ============================================================
# RENAME - v7's tidy renumbering: per folder and extension, oldest first, numbered 1..N with no gaps.
# ============================================================
def _two_phase_rename(mapping: Dict[Path, Path]) -> None:
    tmp_map: Dict[Path, Path] = {}
    for src, dst in mapping.items():
        if src == dst: continue
        tmp = src.with_name(f"__tmp__{src.name}"); i = 2
        while tmp.exists(): tmp = src.with_name(f"__tmp__{i}__{src.name}"); i += 1
        try:
            src.rename(tmp); tmp_map[tmp] = dst
        except OSError as e:
            log.warning(f"Rename failed, left as is: {src} ({e})")
    for tmp, dst in tmp_map.items():
        if dst.exists():
            # v7 deleted whatever was already here; never delete - take the next free name instead.
            log.warning(f"Rename target already exists, not overwriting: {dst}")
            dst = free_name(dst)
        try:
            tmp.rename(dst)
        except OSError as e:
            log.warning(f"Rename failed, file left as {tmp.name}: {e}")

def step4_rerename_tidy(folder: Path) -> int:
    entries = _collect_folder(folder)
    if not entries: return 0
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
    return len(mapping)

# ---------- Lossless verification ----------
def _frame_signature(im: Image.Image, mode: str) -> bytes:
    return (im if im.mode == mode else im.convert(mode)).tobytes()

def same_pixels(a: bytes, b: bytes) -> bool:
    """True when both files decode to identical pixels in every frame (and identical frame timing)."""
    with Image.open(io.BytesIO(a)) as x, Image.open(io.BytesIO(b)) as y:
        n = getattr(x, "n_frames", 1)
        if n != getattr(y, "n_frames", 1):
            return False
        for k in range(n):
            x.seek(k); y.seek(k)
            if x.size != y.size or x.info.get("duration") != y.info.get("duration"):
                return False
            # Palette images may legitimately get a reordered palette, so compare the colours shown.
            mode = x.mode if x.mode == y.mode and x.mode not in ("P", "PA") else "RGBA"
            if _frame_signature(x, mode) != _frame_signature(y, mode):
                return False
    return True

def exif_orientation(data: bytes) -> int:
    try:
        with Image.open(io.BytesIO(data)) as im:
            o = int(im.getexif().get(0x0112, 1) or 1)
            return o if 1 <= o <= 8 else 1
    except Exception:
        return 1

def ffmpeg_streamhash(ffmpeg: str, path: Path, maps: List[str], copy: bool) -> Optional[str]:
    """Per-stream MD5: of the packets (copy=True) or of the decoded frames/samples (copy=False)."""
    cmd = [ffmpeg, "-nostdin", "-v", "error", "-i", str(path)]
    for m in maps: cmd += ["-map", m]
    cmd += (["-c", "copy"] if copy else ["-fps_mode", "passthrough"]) + ["-f", "streamhash", "-hash", "md5", "-"]
    r = run_cmd(cmd)
    out = r.stdout.decode("ascii", "replace").strip()
    return out if r.returncode == 0 and out else None

# ---------- Metadata strip: images (container surgery, pixel data never decoded/re-encoded) ----------
def _min_exif_tiff(orientation: int) -> bytes:
    """A TIFF/EXIF block holding nothing but the Orientation tag."""
    return (b"MM\x00\x2a\x00\x00\x00\x08" + struct.pack(">H", 1)
            + struct.pack(">HHIHH", 0x0112, 3, 1, orientation, 0) + b"\x00\x00\x00\x00")

def _jpeg_segment_name(marker: int, payload: bytes) -> str:
    if marker == 0xFE: return "comment"
    if payload.startswith(b"Exif"): return "EXIF"
    if payload.startswith(b"http://ns.adobe.com/xap") or payload.startswith(b"http://ns.adobe.com/xmp"): return "XMP"
    if payload.startswith(b"MPF"): return "MPF"
    if payload.startswith(b"Photoshop"): return "IPTC/Photoshop"
    if payload.startswith(b"JFXX"): return "JFIF thumbnail"
    return f"APP{marker - 0xE0}"

def strip_jpeg(data: bytes, orientation: int) -> Tuple[bytes, List[str]]:
    if data[:2] != b"\xff\xd8": raise ValueError("not a JPEG (missing SOI marker)")
    out, removed, i, n = bytearray(b"\xff\xd8"), [], 2, len(data)
    if orientation != 1:
        app1 = b"Exif\x00\x00" + _min_exif_tiff(orientation)
        out += b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1
    while True:
        if i >= n or data[i] != 0xFF: raise ValueError(f"no marker at offset {i} (truncated or corrupt JPEG)")
        while i < n and data[i] == 0xFF: i += 1          # fill bytes
        if i >= n: raise ValueError("truncated JPEG")
        m = data[i]; i += 1
        if m == 0xD9:                                    # EOI: anything after it is dropped
            out += b"\xff\xd9"
            if i < n: removed.append(f"{n - i} bytes after end-of-image (embedded extra images/video)")
            return bytes(out), removed
        if 0xD0 <= m <= 0xD7 or m == 0x01:
            out += bytes((0xFF, m)); continue
        if i + 2 > n: raise ValueError("truncated JPEG segment")
        seg_len = struct.unpack(">H", data[i:i + 2])[0]
        if seg_len < 2 or i + seg_len > n: raise ValueError("truncated JPEG segment")
        seg, payload = data[i - 2:i + seg_len], data[i + 2:i + seg_len]
        i += seg_len
        if m == 0xE0: keep = payload.startswith(b"JFIF\x00")
        elif m == 0xE2: keep = payload.startswith(b"ICC_PROFILE\x00")     # colour profile, needed to show colours right
        elif m == 0xEE: keep = payload.startswith(b"Adobe")              # colour transform flag, needed to decode
        elif 0xE1 <= m <= 0xEF or m == 0xFE: keep = False
        else: keep = True
        if keep: out += seg
        else: removed.append(_jpeg_segment_name(m, payload))
        if m == 0xDA:                                    # start of scan: copy entropy-coded data up to next marker
            j = i
            while True:
                j = data.find(b"\xff", j)
                if j < 0 or j + 1 >= n: raise ValueError("JPEG has no end-of-image marker (truncated)")
                nxt = data[j + 1]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7: j += 2
                elif nxt == 0xFF: j += 1
                else: break
            out += data[i:j]; i = j

PNG_SIG = b"\x89PNG\r\n\x1a\n"
PNG_KEEP = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"cHRM", b"gAMA", b"iCCP", b"sBIT", b"sRGB",
            b"cICP", b"mDCV", b"cLLI", b"bKGD", b"pHYs", b"acTL", b"fcTL", b"fdAT"}

def _png_chunk(ctype: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF)

def strip_png(data: bytes, orientation: int) -> Tuple[bytes, List[str]]:
    if not data.startswith(PNG_SIG): raise ValueError("not a PNG")
    out, removed, i = bytearray(PNG_SIG), [], 8
    while i + 8 <= len(data):
        length, ctype = struct.unpack(">I4s", data[i:i + 8])
        end = i + 12 + length
        if end > len(data): raise ValueError("truncated PNG chunk")
        chunk, i = data[i:end], end
        if ctype in PNG_KEEP:
            out += chunk
            if ctype == b"IHDR" and orientation != 1:
                out += _png_chunk(b"eXIf", _min_exif_tiff(orientation))
        else:
            removed.append(ctype.decode("latin-1"))
        if ctype == b"IEND":
            if i < len(data): removed.append(f"{len(data) - i} bytes after IEND")
            return bytes(out), removed
    raise ValueError("PNG has no IEND chunk (truncated)")

WEBP_KEEP = {b"VP8X", b"VP8 ", b"VP8L", b"ALPH", b"ANIM", b"ANMF", b"ICCP"}

def strip_webp(data: bytes, orientation: int) -> Tuple[bytes, List[str]]:
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP": raise ValueError("not a WebP")
    riff_end = min(len(data), 8 + struct.unpack("<I", data[4:8])[0])
    chunks, removed, i = [], [], 12
    while i + 8 <= riff_end:
        fourcc, size = data[i:i + 4], struct.unpack("<I", data[i + 4:i + 8])[0]
        if i + 8 + size > riff_end: raise ValueError("truncated WebP chunk")
        end = min(i + 8 + size + (size & 1), riff_end)
        if fourcc in WEBP_KEEP: chunks.append(bytearray(data[i:end]))
        else: removed.append(fourcc.decode("latin-1").strip())
        i = end
    if len(data) > riff_end: removed.append(f"{len(data) - riff_end} bytes after RIFF data")
    if not removed: return data, []
    vp8x = next((c for c in chunks if c[:4] == b"VP8X"), None)
    if vp8x is not None:
        vp8x[8] &= ~0x0C & 0xFF                          # clear EXIF + XMP flags
        if orientation != 1:
            vp8x[8] |= 0x08
            body = _min_exif_tiff(orientation)
            chunks.append(bytearray(b"EXIF" + struct.pack("<I", len(body)) + body + (b"\x00" if len(body) & 1 else b"")))
    payload = b"WEBP" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(payload)) + payload, removed

GIF_KEEP_APPS = (b"NETSCAPE2.0", b"ANIMEXTS1.0")    # animation looping

def strip_gif(data: bytes, orientation: int = 1) -> Tuple[bytes, List[str]]:
    if data[:6] not in (b"GIF87a", b"GIF89a"): raise ValueError("not a GIF")

    def skip_subblocks(j: int) -> int:
        while True:
            size = data[j]; j += 1
            if size == 0: return j
            j += size

    flags, i = data[10], 13
    if flags & 0x80: i += 3 * (2 << (flags & 7))
    out, removed = bytearray(data[:i]), []
    try:
        while True:
            b = data[i]
            if b == 0x3B:                                # trailer
                out.append(0x3B)
                if i + 1 < len(data): removed.append(f"{len(data) - i - 1} bytes after GIF trailer")
                return bytes(out), removed
            if b == 0x2C:                                # image descriptor + pixel data
                lflags, j = data[i + 9], i + 10
                if lflags & 0x80: j += 3 * (2 << (lflags & 7))
                j = skip_subblocks(j + 1)
                out += data[i:j]
            elif b == 0x21:                              # extension block
                label, j = data[i + 1], skip_subblocks(i + 2)
                if label == 0xFE: removed.append("comment")
                elif label == 0xFF and data[i + 3:i + 14] not in GIF_KEEP_APPS:
                    removed.append(f"application block {data[i + 3:i + 14].decode('latin-1', 'replace')}")
                else: out += data[i:j]
            else:
                raise ValueError(f"unexpected GIF block 0x{b:02x} at offset {i}")
            i = j
    except IndexError:
        raise ValueError("GIF has no trailer (truncated)")

IMAGE_STRIPPERS = {".jpg": strip_jpeg, ".jpeg": strip_jpeg, ".jfif": strip_jpeg,
                   ".png": strip_png, ".webp": strip_webp, ".gif": strip_gif}

def strip_image_worker(path_str: str) -> Tuple[str, str, str]:
    path = Path(path_str)
    stripper = IMAGE_STRIPPERS.get(path.suffix.lower())
    if stripper is None:
        if path.suffix.lower() == ".bmp": return path_str, "unchanged", "BMP has no metadata container"
        return path_str, "skipped", "metadata strip not supported for this format (use exiftool)"
    tmp = None
    try:
        st, data = path.stat(), path.read_bytes()
        orientation = exif_orientation(data)
        new, removed = stripper(data, orientation)
        if new == data:
            return path_str, "unchanged", "no metadata found"
        if not same_pixels(data, new) or exif_orientation(new) != orientation:
            return path_str, "failed", "verification failed (pixels or orientation differ); original kept"
        tmp = tmp_path_for(path)
        tmp.write_bytes(new)
        commit(tmp, path, path, st)
        kept = f" (kept orientation={orientation})" if orientation != 1 else ""
        return path_str, "changed", f"removed {', '.join(dict.fromkeys(removed))}{kept} | sha256 {sha256_bytes(data)[:12]} -> {sha256_bytes(new)[:12]}"
    except Exception as e:
        return path_str, "failed", f"{type(e).__name__}: {e}; original kept"
    finally:
        if tmp is not None: discard(tmp)

# ---------- Metadata strip: video/audio (stream copy, nothing re-encoded) ----------
# 0:V = real video streams (excludes embedded cover-art/thumbnail images), 0:a = audio, 0:s = subtitles.
# Data streams (e.g. GoPro/drone GPS telemetry, timecode) are deliberately NOT mapped, so they are dropped.
AV_STRIP_MAPS = ["0:V?", "0:a?", "0:s?"]

def strip_av(path: Path, ffmpeg: str) -> Tuple[str, str]:
    tmp = tmp_path_for(path)
    try:
        st = path.stat()
        cmd = [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(path)]
        for m in AV_STRIP_MAPS: cmd += ["-map", m]
        cmd += ["-c", "copy", "-map_metadata", "-1", "-map_chapters", "-1",
                "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact", str(tmp)]
        r = run_cmd(cmd, timeout=3600)
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            return "failed", f"ffmpeg failed: {r.stderr.decode('utf-8', 'replace').strip()[-300:]}; original kept"
        before = ffmpeg_streamhash(ffmpeg, path, AV_STRIP_MAPS, copy=True)
        if before is None or before != ffmpeg_streamhash(ffmpeg, tmp, AV_STRIP_MAPS, copy=True):
            return "failed", "verification failed (audio/video packets differ); original kept"
        old_hash, new_hash = sha256_file(path), sha256_file(tmp)
        if old_hash == new_hash:
            return "unchanged", "no metadata found"
        commit(tmp, path, path, st)
        return "changed", f"tags/chapters/cover art/data streams removed | sha256 {old_hash[:12]} -> {new_hash[:12]}"
    except Exception as e:
        return "failed", f"{type(e).__name__}: {e}; original kept"
    finally:
        discard(tmp)

# ---------- Lossless re-encode: images ----------
def _pillow_reencode_options(im: Image.Image, ext: str) -> List[dict]:
    info, n = im.info, getattr(im, "n_frames", 1)
    anim = {}
    if n > 1:
        durations = []
        for k in range(n):
            im.seek(k); durations.append(im.info.get("duration", 0))
        im.seek(0)
        anim = dict(save_all=True, duration=durations, loop=info.get("loop", 0))
    keep = {k: info[k] for k in ("icc_profile", "exif") if info.get(k)}
    if ext == ".png":
        text = PngImagePlugin.PngInfo()
        for k, v in getattr(im, "text", {}).items(): text.add_text(k, v)
        base = dict(format="PNG", pnginfo=text, **keep, **anim)
        return [dict(base, optimize=True), dict(base, compress_level=6), dict(base, compress_level=1)]
    if ext == ".webp":
        if info.get("xmp"): keep["xmp"] = info["xmp"]
        base = dict(format="WEBP", lossless=True, exact=True, **keep, **anim)
        return [dict(base, quality=100, method=6), dict(base, quality=50, method=4)]
    if ext == ".gif":
        base = dict(format="GIF", **anim)
        if info.get("comment"): base["comment"] = info["comment"]
        return [dict(base, optimize=False), dict(base, optimize=True)]
    if ext in (".tif", ".tiff"):
        order = ["tiff_adobe_deflate", "tiff_lzw"] if info.get("compression") == "tiff_lzw" else ["tiff_lzw", "tiff_adobe_deflate"]
        return [dict(format="TIFF", compression=c, save_all=n > 1) for c in order]
    if ext == ".bmp":
        return [dict(format="BMP")]
    return []

def reencode_image_worker(path_str: str, jpegtran: Optional[str], strip_chosen: bool) -> Tuple[str, str, str]:
    path, ext = Path(path_str), Path(path_str).suffix.lower()
    tmp = tmp_path_for(path)
    try:
        st, data = path.stat(), path.read_bytes()
        if ext in JPEG_EXTS:
            if not jpegtran:
                return path_str, "skipped", "jpegtran not installed (no lossy fallback is used)"
            _, extra = strip_jpeg(data, 1)
            if not strip_chosen and any("after end-of-image" in r for r in extra):
                return path_str, "skipped", "has embedded extra images/video (motion photo, depth or HDR gain map) that jpegtran would destroy"
            with Image.open(io.BytesIO(data)) as im:
                progressive = bool(im.info.get("progressive") or im.info.get("progression"))
            # Flip baseline <-> progressive so the byte stream always changes; DCT coefficients stay identical.
            cmd = [jpegtran, "-copy", "all", "-optimize"] + ([] if progressive else ["-progressive"]) + ["-outfile", str(tmp), str(path)]
            r = run_cmd(cmd, timeout=600)
            if r.returncode != 0 or not tmp.exists():
                return path_str, "failed", f"jpegtran failed: {r.stderr.decode('utf-8', 'replace').strip()[-300:]}"
            new = tmp.read_bytes()
        else:
            new = None
            with Image.open(io.BytesIO(data)) as im:
                for opts in _pillow_reencode_options(im, ext):
                    buf = io.BytesIO()
                    im.seek(0)
                    im.save(buf, **opts)
                    if buf.getvalue() != data:
                        new = buf.getvalue()
                        break
            if new is None:
                return path_str, "unchanged", "no alternative lossless encoding produced different bytes"
        if new == data:
            return path_str, "unchanged", "re-encoded bytes identical to original"
        if not same_pixels(data, new):
            return path_str, "failed", "re-encoded output not pixel-identical; original kept"
        if ext not in JPEG_EXTS:
            tmp.write_bytes(new)
        commit(tmp, path, path, st)
        return path_str, "changed", f"size {len(data):,} -> {len(new):,} bytes ({(len(new) / len(data) - 1) * 100:+.1f}%) | sha256 {sha256_bytes(data)[:12]} -> {sha256_bytes(new)[:12]}"
    except Exception as e:
        return path_str, "failed", f"{type(e).__name__}: {e}; original kept"
    finally:
        discard(tmp)

# ---------- Lossless re-encode: video/audio ----------
def reencode_video(path: Path, ffmpeg: str) -> Tuple[str, str]:
    """FFV1 (lossless, intra-only) video in Matroska. Audio and subtitles are stream-copied, not re-encoded."""
    size = path.stat().st_size
    free = shutil.disk_usage(path.parent).free
    if free < size * VIDEO_REENCODE_MIN_FREE_FACTOR:
        return "skipped", f"needs ~{VIDEO_REENCODE_MIN_FREE_FACTOR}x its size free on disk ({size * VIDEO_REENCODE_MIN_FREE_FACTOR / 1e9:.1f} GB), only {free / 1e9:.1f} GB free"
    final = path if path.suffix.lower() == ".mkv" else free_name(path.with_suffix(".mkv"))
    tmp = tmp_path_for(final)
    try:
        st = path.stat()
        cmd = [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(path), "-map", "0:V", "-map", "0:a?", "-map", "0:s?",
               "-c:v", "ffv1", "-level", "3", "-g", "1", "-slices", "16", "-slicecrc", "1",
               "-c:a", "copy", "-c:s", "copy", "-fps_mode", "passthrough", "-fflags", "+bitexact", str(tmp)]
        r = run_cmd(cmd)
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            return "failed", f"ffmpeg failed: {r.stderr.decode('utf-8', 'replace').strip()[-300:]}; original kept"
        before = ffmpeg_streamhash(ffmpeg, path, ["0:V"], copy=False)
        if before is None or before != ffmpeg_streamhash(ffmpeg, tmp, ["0:V"], copy=False):
            return "failed", "verification failed (decoded frames differ); original kept"
        new_size = tmp.stat().st_size
        commit(tmp, path, final, st)
        return "changed", f"-> {final.name} | size {size:,} -> {new_size:,} bytes ({new_size / size:.1f}x)"
    except Exception as e:
        return "failed", f"{type(e).__name__}: {e}; original kept"
    finally:
        discard(tmp)

def audio_codec(path: Path, ffprobe: str) -> Optional[str]:
    r = run_cmd([ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)], timeout=120)
    if r.returncode != 0: return None
    return r.stdout.decode("ascii", "replace").strip() or None

def reencode_audio(path: Path, ffmpeg: str, ffprobe: str) -> Tuple[str, str]:
    """Lossless sources -> FLAC, verified sample-for-sample. Lossy sources can't be re-encoded bit-exactly."""
    ext = path.suffix.lower()
    if ext not in LOSSLESS_AUDIO_EXTS and not (ext == ".m4a" and audio_codec(path, ffprobe) == "alac"):
        return "skipped", "lossy audio cannot be re-encoded bit-exactly (the metadata strip alone changes its hash)"
    final = path if ext == ".flac" else free_name(path.with_suffix(".flac"))
    tmp = tmp_path_for(final)
    try:
        st, size = path.stat(), path.stat().st_size
        cmd = [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(path), "-map", "0:a", "-map", "0:v?",
               "-c:a", "flac", "-compression_level", "8", "-c:v", "copy",
               "-fflags", "+bitexact", "-flags:a", "+bitexact", str(tmp)]
        r = run_cmd(cmd, timeout=3600)
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            return "failed", f"ffmpeg failed: {r.stderr.decode('utf-8', 'replace').strip()[-300:]}; original kept"
        before = ffmpeg_streamhash(ffmpeg, path, ["0:a"], copy=False)
        if before is None or before != ffmpeg_streamhash(ffmpeg, tmp, ["0:a"], copy=False):
            return "failed", "verification failed (decoded samples differ); original kept"
        old_hash, new_hash = sha256_file(path), sha256_file(tmp)
        if old_hash == new_hash:
            return "unchanged", "re-encoded bytes identical to original"
        new_size = tmp.stat().st_size
        commit(tmp, path, final, st)
        return "changed", f"-> {final.name} | size {size:,} -> {new_size:,} bytes | sha256 {old_hash[:12]} -> {new_hash[:12]}"
    except Exception as e:
        return "failed", f"{type(e).__name__}: {e}; original kept"
    finally:
        discard(tmp)

# ---------- Guaranteed hash change: random padding when no lossless re-encode is possible ----------
# The padding is 16 random bytes in a block every decoder ignores (JPEG comment, private PNG chunk,
# GIF comment, container tag). It carries no personal data; it only makes the bytes unique.
def _nonce() -> bytes: return os.urandom(16)

def pad_jpeg(data: bytes) -> bytes:
    if data[:2] != b"\xff\xd8": raise ValueError("not a JPEG (missing SOI marker)")
    # Insert after a leading JFIF APP0 if there is one (it should stay first), otherwise right after SOI.
    # Everything after the insertion point moves by the same amount, so MPF offsets (relative to the MPF
    # header) and offsets measured from the end of file (motion photos) stay valid.
    at = 2
    if data[2:4] == b"\xff\xe0" and data[6:11] == b"JFIF\x00":
        at = 4 + struct.unpack(">H", data[4:6])[0]
    body = b"pad:" + _nonce().hex().encode()
    return data[:at] + b"\xff\xfe" + struct.pack(">H", len(body) + 2) + body + data[at:]

def pad_png(data: bytes) -> bytes:
    if not data.startswith(PNG_SIG): raise ValueError("not a PNG")
    at = 8
    while True:                                        # walk the chunks to the real IEND
        if at + 8 > len(data): raise ValueError("PNG has no IEND chunk (truncated)")
        length, ctype = struct.unpack(">I4s", data[at:at + 8])
        if ctype == b"IEND": break
        at += 12 + length
    return data[:at] + _png_chunk(b"prVt", _nonce()) + data[at:]   # private, ancillary, safe-to-copy

def pad_gif(data: bytes) -> bytes:
    _, removed = strip_gif(data)                       # walks the block structure (raises if truncated)
    trailing = next((int(r.split()[0]) for r in removed if r.endswith("after GIF trailer")), 0)
    at = len(data) - trailing - 1
    if data[at] != 0x3B: raise ValueError("GIF trailer not found")
    n = _nonce()
    return data[:at] + b"\x21\xfe" + bytes((len(n),)) + n + b"\x00" + data[at:]

IMAGE_PADDERS = {".jpg": pad_jpeg, ".jpeg": pad_jpeg, ".jfif": pad_jpeg, ".png": pad_png, ".gif": pad_gif}

def pad_image_worker(path_str: str) -> Tuple[str, str, str]:
    path = Path(path_str)
    padder = IMAGE_PADDERS.get(path.suffix.lower())
    if padder is None:
        return path_str, "skipped", "no lossless way to change this format's bytes; hash NOT changed"
    tmp = tmp_path_for(path)
    try:
        st, data = path.stat(), path.read_bytes()
        new = padder(data)
        if not same_pixels(data, new) or exif_orientation(new) != exif_orientation(data):
            return path_str, "failed", "padding verification failed; original kept"
        tmp.write_bytes(new)
        commit(tmp, path, path, st)
        return path_str, "changed", f"added 16 random padding bytes | sha256 {sha256_bytes(data)[:12]} -> {sha256_bytes(new)[:12]}"
    except Exception as e:
        return path_str, "failed", f"{type(e).__name__}: {e}; original kept"
    finally:
        discard(tmp)

def pad_av(path: Path, ffmpeg: str) -> Tuple[str, str]:
    """Stream copy of every stream plus one random tag; packets verified identical."""
    tmp = tmp_path_for(path)
    try:
        st = path.stat()
        cmd = [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(path), "-map", "0", "-c", "copy",
               "-metadata", f"padding={_nonce().hex()}"]
        if path.suffix.lower() in (".mp4", ".m4v", ".m4a", ".mov", ".3gp"):
            cmd += ["-movflags", "+use_metadata_tags"]
        r = run_cmd(cmd + [str(tmp)], timeout=3600)
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            return "failed", f"ffmpeg failed: {r.stderr.decode('utf-8', 'replace').strip()[-300:]}; original kept"
        before = ffmpeg_streamhash(ffmpeg, path, ["0"], copy=True)
        if before is None or before != ffmpeg_streamhash(ffmpeg, tmp, ["0"], copy=True):
            return "failed", "verification failed (packets differ); original kept"
        old_hash, new_hash = sha256_file(path), sha256_file(tmp)
        if old_hash == new_hash:
            return "failed", "padding did not change the bytes; original kept"
        commit(tmp, path, path, st)
        return "changed", f"added a random padding tag | sha256 {old_hash[:12]} -> {new_hash[:12]}"
    except Exception as e:
        return "failed", f"{type(e).__name__}: {e}; original kept"
    finally:
        discard(tmp)

# ---------- Folder merge (ported from merge_folders_with_prefix.py) ----------
def find_folder_pairs(root: Path) -> List[Tuple[Path, Path]]:
    """Top-level '@name' folders that have a matching 'name' folder."""
    folders = {p.name: p for p in root.iterdir() if p.is_dir()}
    pairs = [(folders[n], folders[n[1:]]) for n in folders if n.startswith("@") and n[1:] in folders]
    return sorted(pairs, key=lambda x: str(x[1]).lower())

def merge_folder_pair(src_folder: Path, dst_folder: Path) -> int:
    """Move the media files of '@name' into 'name' (never overwriting); remove '@name' if left empty."""
    moved = 0
    for src_file in sorted(src_folder.iterdir()):
        if not is_media(src_file):
            log.info(f"MERGE | kept non-media/subfolder in place: {src_file}")
            continue
        target = free_name(dst_folder / src_file.name)
        try:
            src_file.rename(target)
            moved += 1
            log.info(f"MERGE | {src_file} -> {target}")
        except OSError as e:
            log.warning(f"MERGE FAILED | {src_file}: {e}")
    try:
        if not any(src_folder.iterdir()):
            src_folder.rmdir()
            log.info(f"MERGE | removed empty folder {src_folder}")
        else:
            log.info(f"MERGE | {src_folder} still has other content, not removed")
    except OSError as e:
        log.warning(f"MERGE | could not remove {src_folder}: {e}")
    return moved

def rename_folders_with_prefix(root: Path, prefix: str) -> int:
    """Prefix every top-level folder (except Duplicates/Corrupted and ones that already carry the prefix)."""
    renamed = 0
    for folder in sorted(root.iterdir(), key=lambda x: str(x).lower()):
        if not folder.is_dir() or folder.name in SKIP_DIRNAMES or folder.name.startswith(prefix):
            continue
        target = root / f"{prefix}{folder.name}"
        if target.exists():
            log.warning(f"PREFIX | {target} already exists, {folder.name} not renamed")
            continue
        try:
            folder.rename(target)
            renamed += 1
            log.info(f"PREFIX | {folder.name} -> {target.name}")
        except OSError as e:
            log.warning(f"PREFIX FAILED | {folder}: {e}")
    return renamed

# ---------- Pipeline: options, progress, cancellation ----------
class Cancelled(Exception):
    """Raised when the user cancels. Files already processed keep their (verified) changes."""

ProgressFn = Callable[[str, int, int], None]

@dataclass
class Options:
    merge_folders: bool = False        # merge '@name' folders into 'name'
    folder_prefix: str = ""            # prefix for top-level folder names ("" = leave names alone)
    strip: bool = False
    dedupe: bool = False
    reencode: bool = False
    force_new_hash: bool = True        # re-encode fallback: random padding when bytes would not change
    rename: bool = False
    hash_report: bool = True           # sha256 every file before and after, list the unchanged ones
    phash_threshold: int = DEFAULT_PHASH_THRESHOLD
    dhash_img_threshold: int = DEFAULT_DHASH_THRESHOLD_IMG
    dhash_vid_threshold: int = DEFAULT_DHASH_THRESHOLD_VID
    hash_size: int = DEFAULT_HASH_SIZE
    num_frames: int = DEFAULT_NUM_FRAMES
    use_processes: bool = True         # False = threads (GUI / Android, where worker processes are fragile)

    def planned_steps(self) -> List[str]:
        steps = []
        if self.merge_folders or self.folder_prefix: steps.append("Folder merge")
        if self.hash_report and (self.strip or self.reencode): steps.append("Hashing (before)")
        if self.strip: steps.append("Metadata strip")
        if self.dedupe: steps.append("Deduplication")
        if self.reencode: steps.append("Lossless re-encode")
        if self.rename: steps.append("Renaming")
        if self.hash_report and (self.strip or self.reencode): steps.append("Hashing (after)")
        return steps

@dataclass
class RunContext:
    progress: Optional[ProgressFn] = None
    cancel: Optional[threading.Event] = None

    def tick(self, step: str, done: int, total: int):
        if self.progress: self.progress(step, done, total)

    def check(self):
        if self.cancel is not None and self.cancel.is_set(): raise Cancelled()

def _drain(futs, ctx: RunContext, step: str):
    """as_completed() with progress ticks; on cancel, drop queued work and stop."""
    futs = list(futs)
    ctx.tick(step, 0, len(futs))
    try:
        for k, fut in enumerate(as_completed(futs), 1):
            ctx.check()
            yield fut
            ctx.tick(step, k, len(futs))
    except Cancelled:
        for f in futs: f.cancel()
        raise

# ---------- Tools ----------
@dataclass
class Tools:
    ffmpeg: Optional[str]
    ffprobe: Optional[str]
    jpegtran: Optional[str]

def find_tools() -> Tools:
    """PATH lookup, after putting a bundled 'tools' folder (next to the app or the exe) first on PATH.
    Changing PATH (not just remembering paths) means worker processes and v7's hashing code find them too."""
    for d in (Path(sys.executable).resolve().parent / "tools", Path(__file__).resolve().parent / "tools"):
        if d.is_dir() and str(d) not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
    return Tools(which("ffmpeg"), which("ffprobe"), which("jpegtran"))

@dataclass
class ScanResult:
    root: Path
    images: int
    videos: int
    audio: int
    folders: int
    merge_pairs: int
    risky_root: bool                    # drive root or home folder

    @property
    def total(self) -> int: return self.images + self.videos + self.audio

def scan(root: Path) -> ScanResult:
    files = media_files(root)
    pairs = len(find_folder_pairs(root)) if root.is_dir() else 0
    return ScanResult(root, sum(is_image(p) for p in files), sum(is_video(p) for p in files),
                      sum(is_audio(p) for p in files), len(media_folders(root)), pairs,
                      root.parent == root or root == Path.home().resolve())

def default_log_path() -> Path:
    """Outside the processed folder on purpose: the log lists original file names and hashes."""
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "SecureMediaProcessor" / "logs"
    return ensure_dir(base) / f"run_{time.strftime('%Y%m%d_%H%M%S')}.txt"

# ---------- Steps ----------
def _report(step: str, root: Path, path: Path, status: str, detail: str, stats: StepStats):
    stats.add(status)
    rel = path.relative_to(root) if path.is_relative_to(root) else path
    if status == "changed": log.info(f"{step} | {rel} | {detail}")
    elif status in ("skipped", "failed"): log.warning(f"{step} {status.upper()} | {rel} | {detail}")

def step_merge(root: Path, opts: Options, ctx: RunContext) -> str:
    step = "Folder merge"
    log.info(f"=== STEP: {step} ===")
    moved = 0
    pairs = find_folder_pairs(root) if opts.merge_folders else []
    for k, (src, dst) in enumerate(pairs, 1):
        ctx.check()
        moved += merge_folder_pair(src, dst)
        ctx.tick(step, k, len(pairs))
    prefixed = rename_folders_with_prefix(root, opts.folder_prefix) if opts.folder_prefix else 0
    return f"{len(pairs)} folder pair(s) merged ({moved} files moved), {prefixed} folder(s) prefixed"

def hash_all(root: Path, ctx: RunContext, step: str) -> Dict[str, List[Path]]:
    files = media_files(root)
    out: Dict[str, List[Path]] = {}
    with ThreadPoolExecutor(max_workers=4) as tpool:
        futs = {tpool.submit(sha256_file, p): p for p in files}
        for fut in _drain(futs, ctx, step):
            try: out.setdefault(fut.result(), []).append(futs[fut])
            except OSError as e: log.warning(f"Could not hash {futs[fut]}: {e}")
    return out

def step_strip(root: Path, pool: Executor, tools: Tools, ctx: RunContext) -> StepStats:
    step = "Metadata strip"
    stats, files = StepStats(), media_files(root)
    images = [p for p in files if is_image(p)]
    av = [p for p in files if is_video(p) or is_audio(p)]
    log.info(f"=== STEP: {step} ({len(images)} images, {len(av)} video/audio files) ===")
    for fut in _drain([pool.submit(strip_image_worker, str(p)) for p in images], ctx, f"{step} (images)"):
        path, status, detail = fut.result()
        _report("STRIP", root, Path(path), status, detail, stats)
    if av and not tools.ffmpeg:
        log.warning(f"ffmpeg not found: {len(av)} video/audio files were NOT stripped")
        stats.skipped += len(av)
    elif av:
        with ThreadPoolExecutor(max_workers=min(4, CPU_COUNT)) as tpool:
            futs = {tpool.submit(strip_av, p, tools.ffmpeg): p for p in av}
            for fut in _drain(futs, ctx, f"{step} (video/audio)"):
                _report("STRIP", root, futs[fut], *fut.result(), stats)
    return stats

def step_dedupe(root: Path, pool: Executor, opts: Options, tools: Tools, ctx: RunContext) -> Tuple[int, int]:
    step = "Deduplication"
    log.info(f"=== STEP: {step} ===")
    if not (tools.ffmpeg and tools.ffprobe):
        log.warning("ffmpeg/ffprobe not found: videos are NOT being deduplicated (left in place)")
    dup_dir, corrupt_dir = root / DUP_DIRNAME, root / CORRUPT_DIRNAME
    folders = media_folders(root)
    total_dup = total_bad = 0
    for k, fld in enumerate(folders, 1):
        ctx.check()
        try:
            has_media = any(is_image(c) or is_video(c) for c in fld.iterdir() if c.is_file())
        except OSError:
            has_media = False
        if has_media:
            log.info(f"Processing: {fld}")
            n_dup, n_bad = run_dedupe(fld, dup_dir, corrupt_dir, pool, opts)
            total_dup += n_dup; total_bad += n_bad
        ctx.tick(step, k, len(folders))
    return total_dup, total_bad

def step_reencode(root: Path, pool: Executor, opts: Options, tools: Tools, strip_chosen: bool, ctx: RunContext) -> StepStats:
    step = "Lossless re-encode"
    stats, files = StepStats(), media_files(root)
    images = [p for p in files if is_image(p)]
    videos = [p for p in files if is_video(p)]
    audio = [p for p in files if is_audio(p)]
    log.info(f"=== STEP: {step} ({len(images)} images, {len(videos)} videos, {len(audio)} audio) ===")
    to_pad_img: List[Path] = []
    to_pad_av: List[Path] = []
    for fut in _drain([pool.submit(reencode_image_worker, str(p), tools.jpegtran, strip_chosen) for p in images], ctx, f"{step} (images)"):
        path, status, detail = fut.result()
        if status != "changed" and opts.force_new_hash:
            log.info(f"REENCODE | {Path(path).relative_to(root)} | {status}: {detail} -> adding random padding instead")
            to_pad_img.append(Path(path))
            continue
        _report("REENCODE", root, Path(path), status, detail, stats)
    if (videos or audio) and not (tools.ffmpeg and tools.ffprobe):
        log.warning(f"ffmpeg/ffprobe not found: {len(videos) + len(audio)} video/audio files were NOT re-encoded")
        stats.skipped += len(videos) + len(audio)
    else:
        av = videos + audio
        ctx.tick(f"{step} (video/audio)", 0, len(av))
        for k, p in enumerate(av, 1):     # one at a time: ffmpeg already uses every core
            ctx.check()
            if is_video(p):
                log.info(f"Re-encoding video {k}/{len(av)}: {p.relative_to(root)} ({p.stat().st_size / 1e6:,.1f} MB)")
                status, detail = reencode_video(p, tools.ffmpeg)
            else:
                status, detail = reencode_audio(p, tools.ffmpeg, tools.ffprobe)
            if status != "changed" and opts.force_new_hash:
                log.info(f"REENCODE | {p.relative_to(root)} | {status}: {detail} -> adding random padding instead")
                to_pad_av.append(p)
            else:
                _report("REENCODE", root, p, status, detail, stats)
            ctx.tick(f"{step} (video/audio)", k, len(av))
    if to_pad_img:
        for fut in _drain([pool.submit(pad_image_worker, str(p)) for p in to_pad_img], ctx, f"{step} (padding images)"):
            path, status, detail = fut.result()
            _report("PADDING", root, Path(path), status, detail, stats)
    for k, p in enumerate(to_pad_av, 1):
        ctx.check()
        _report("PADDING", root, p, *pad_av(p, tools.ffmpeg), stats)
        ctx.tick(f"{step} (padding video/audio)", k, len(to_pad_av))
    return stats

def step_rename(root: Path, ctx: RunContext) -> int:
    step = "Renaming"
    log.info(f"=== STEP: {step} ===")
    folders = media_folders(root)
    renamed = 0
    for k, fld in enumerate(folders, 1):
        ctx.check()
        renamed += step4_rerename_tidy(fld)
        ctx.tick(step, k, len(folders))
    return renamed

def sweep_tmp_files(root: Path):
    for p in root.rglob(f"{TMP_PREFIX}*"):
        try:
            p.unlink()
            log.info(f"Removed leftover temp file from an interrupted run: {p}")
        except OSError: pass

def run_pipeline(root: Path, opts: Options, progress: Optional[ProgressFn] = None,
                 cancel: Optional[threading.Event] = None, log_path: Optional[Path] = None) -> List[str]:
    """Run the selected steps in a fixed order. Returns one summary line per step.
    Raises Cancelled if `cancel` is set; files already processed keep their verified changes."""
    ctx = RunContext(progress, cancel)
    tools = find_tools()
    handler = setup_file_logger(log_path) if log_path else None
    results: List[str] = []
    started = time.time()
    try:
        log.info(f"Run started on {root} with steps: {', '.join(opts.planned_steps())}")
        sweep_tmp_files(root)
        if opts.merge_folders or opts.folder_prefix:
            results.append(f"Folder merge    : {step_merge(root, opts, ctx)}")
        report = opts.hash_report and (opts.strip or opts.reencode)
        before = hash_all(root, ctx, "Hashing (before)") if report else {}
        executor = ProcessPoolExecutor if opts.use_processes else ThreadPoolExecutor
        with executor(max_workers=CPU_COUNT) as pool:
            if opts.strip:
                results.append(f"Metadata strip  : {step_strip(root, pool, tools, ctx)}")
            if opts.dedupe:
                n_dup, n_bad = step_dedupe(root, pool, opts, tools, ctx)
                results.append(f"Deduplication   : {n_dup} duplicate(s) -> {root / DUP_DIRNAME}, "
                               f"{n_bad} unreadable -> {root / CORRUPT_DIRNAME}")
            if opts.reencode:
                results.append(f"Lossless re-enc : {step_reencode(root, pool, opts, tools, opts.strip, ctx)}")
        if opts.rename:
            results.append(f"Renaming        : {step_rename(root, ctx)} file(s) renamed")
        if report:
            after = hash_all(root, ctx, "Hashing (after)")
            same = sorted(p for h, paths in after.items() if h in before for p in paths)
            n_after = sum(len(v) for v in after.values())
            for p in same:
                log.warning(f"HASH UNCHANGED | {p.relative_to(root)}")
            results.append(f"Hash check      : {n_after - len(same)} of {n_after} files have a new content hash"
                           + (f", {len(same)} UNCHANGED (listed in the log)" if same else ""))
        results.append(f"Finished in {time.time() - started:,.0f}s")
        for line in results: log.info(line)
        return results
    finally:
        _sweep_leftover_temp_dirs()
        if handler:
            log.removeHandler(handler)
            handler.close()
