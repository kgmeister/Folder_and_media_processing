#!/usr/bin/env python3
r"""
========================================================================
 SECURE MEDIA PROCESSOR
========================================================================
Combines fast_media_deduplicator.py (perceptual-hash dedupe, BK-Tree) and
Media_processor.py (metadata strip + lossless re-encode) into one tool.

It acts on every media file in the folder it is RUN FROM (the current
working directory) and all of its subfolders. Before touching anything it
asks, one by one, which steps you want, then asks for a final YES.

Steps (executed in this order, each one optional):
  1. Metadata strip     - lossless; removes EXIF/GPS/XMP/IPTC/comments,
                          container tags, chapters, embedded thumbnails
  2. Deduplication      - p-hash/d-hash + Hamming distance (BK-Tree),
                          duplicates moved to "Duplicates", unreadable
                          files moved to "Corrupted"
  3. Lossless re-encode - pixel/sample-exact re-encode (changes hashes)
  4. Renaming           - "<folder name> (N).<ext>", oldest file first

Every rewritten file is verified (decoded pixels / frames / packets must
match the original) before it replaces the original, and the original's
modification time is kept so "oldest first" ordering stays correct.

REQUIREMENTS:
    pip install Pillow imagehash          (auto-installed if missing)
    ffmpeg + ffprobe on PATH              (video/audio steps, video dedupe)
    jpegtran on PATH (libjpeg-turbo)      (JPEG lossless re-encode only)

HOW TO RUN:
    cd "D:\path\to\your\media"
    python C:\path\to\secure_media_processor.py

    Optional: --root-dir "D:\other\folder" to target another folder.
========================================================================
"""
import os, re, sys, shutil, argparse, tempfile, subprocess, time, uuid, atexit, logging, importlib.util
import io, struct, zlib, hashlib
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

RE_PATTERN_NUMBERED = re.compile(r"^(?P<base>.+?) \((?P<num>\d+)\)\.(?P<ext>[^.]+)$", re.I)

# Logger setup
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# Best-effort temp sweep at exit (in case of abrupt crash/Ctrl+C). Registered from main() only,
# so pool worker processes don't sweep frame folders that other workers are still using.
def _sweep_leftover_temp_dirs():
    tdir = Path(tempfile.gettempdir())
    for d in tdir.glob("vf_*"):
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
            except OSError as e: log.warning(f"Rename failed: {e}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(lambda t, d: t.rename(d), t, d) for t, d in tmp_to_final.items()]):
            try: fut.result()
            except OSError as e: log.warning(f"Rename failed: {e}")

    return _collect_folder(folder)

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

def run_dedupe(folder: Path, dup_dir: Path, corrupt_dir: Path, pool: ProcessPoolExecutor, args) -> Tuple[int, int]:
    entries = _collect_folder(folder)

    # Hashing (CPU Bound + Subprocesses)
    hashes: Dict[Path, Hashes] = {}
    ffmpeg, ffprobe = which("ffmpeg"), which("ffprobe")
    # Audio is never hashed, and videos are only hashed when ffmpeg+ffprobe exist (otherwise the
    # worker reports them as bad and they would all be moved to Corrupted).
    hashable = [e for e in entries if e.is_image or (e.is_video and ffmpeg and ffprobe)]
    n_dup = n_bad = 0

    futs = [pool.submit(_compute_hashes_worker, e.path, e.is_video, args.hash_size, args.num_frames, ffmpeg, ffprobe) for e in hashable]
    for fut in as_completed(futs):
        path, h_obj, bad = fut.result()
        if bad:
            log.info(f"CORRUPT | unreadable, moved to {CORRUPT_DIRNAME}: {path}")
            safe_move(path, corrupt_dir)
            n_bad += 1
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
                # The tree stores only the phash, so look up the base file's dhash
                # (the original read node.hash_val.dhash, which raised AttributeError).
                dd = int(h.dhash - hashes[node.payload.path].dhash)
                if dd <= args.dhash_img_threshold:
                    is_dup = True
                    log.info(f"IMG DUP | base={node.payload.path.name} | dup={fe.path.name} | p={pd} d={dd}")
                    break
            if not is_dup:
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
                tree_vid.add(h_vid.phash, fe)

        if is_dup:
            safe_move(fe.path, dup_dir, new_name=f"{folder.name}_dup_{fe.path.name}")
            n_dup += 1

    return n_dup, n_bad

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

# ---------- Steps ----------
def _report(step: str, root: Path, path: Path, status: str, detail: str, stats: StepStats):
    stats.add(status)
    rel = path.relative_to(root) if path.is_relative_to(root) else path
    if status == "changed": log.info(f"{step} | {rel} | {detail}")
    elif status in ("skipped", "failed"): log.warning(f"{step} {status.upper()} | {rel} | {detail}")

def step_strip(root: Path, pool: ProcessPoolExecutor, ffmpeg: Optional[str]) -> StepStats:
    stats, files = StepStats(), media_files(root)
    images = [p for p in files if is_image(p)]
    av = [p for p in files if is_video(p) or is_audio(p)]
    log.info(f"=== STEP: metadata strip ({len(images)} images, {len(av)} video/audio files) ===")
    for fut in as_completed([pool.submit(strip_image_worker, str(p)) for p in images]):
        path, status, detail = fut.result()
        _report("STRIP", root, Path(path), status, detail, stats)
    if av and not ffmpeg:
        log.warning(f"ffmpeg not found: {len(av)} video/audio files were NOT stripped")
        stats.skipped += len(av)
    elif av:
        with ThreadPoolExecutor(max_workers=min(4, CPU_COUNT)) as tpool:
            futs = {tpool.submit(strip_av, p, ffmpeg): p for p in av}
            for fut in as_completed(futs):
                _report("STRIP", root, futs[fut], *fut.result(), stats)
    return stats

def step_dedupe(root: Path, pool: ProcessPoolExecutor, args) -> Tuple[int, int]:
    log.info("=== STEP: deduplication ===")
    if not (which("ffmpeg") and which("ffprobe")):
        log.warning("ffmpeg/ffprobe not found: videos are NOT being deduplicated (left in place)")
    dup_dir, corrupt_dir = root / DUP_DIRNAME, root / CORRUPT_DIRNAME
    total_dup = total_bad = 0
    for fld in media_folders(root):
        try:
            if not any(is_image(c) or is_video(c) for c in fld.iterdir() if c.is_file()): continue
        except OSError: continue
        log.info(f"Processing: {fld}")
        n_dup, n_bad = run_dedupe(fld, dup_dir, corrupt_dir, pool, args)
        total_dup += n_dup; total_bad += n_bad
    return total_dup, total_bad

def step_reencode(root: Path, pool: ProcessPoolExecutor, ffmpeg: Optional[str], ffprobe: Optional[str],
                  jpegtran: Optional[str], strip_chosen: bool) -> StepStats:
    stats, files = StepStats(), media_files(root)
    images = [p for p in files if is_image(p)]
    videos = [p for p in files if is_video(p)]
    audio = [p for p in files if is_audio(p)]
    log.info(f"=== STEP: lossless re-encode ({len(images)} images, {len(videos)} videos, {len(audio)} audio) ===")
    for fut in as_completed([pool.submit(reencode_image_worker, str(p), jpegtran, strip_chosen) for p in images]):
        path, status, detail = fut.result()
        _report("REENCODE", root, Path(path), status, detail, stats)
    if (videos or audio) and not (ffmpeg and ffprobe):
        log.warning(f"ffmpeg/ffprobe not found: {len(videos) + len(audio)} video/audio files were NOT re-encoded")
        stats.skipped += len(videos) + len(audio)
        return stats
    for k, p in enumerate(videos, 1):     # one at a time: ffmpeg already uses every core
        log.info(f"Re-encoding video {k}/{len(videos)}: {p.relative_to(root)} ({p.stat().st_size / 1e6:,.1f} MB)")
        _report("REENCODE", root, p, *reencode_video(p, ffmpeg), stats)
    for p in audio:
        _report("REENCODE", root, p, *reencode_audio(p, ffmpeg, ffprobe), stats)
    return stats

def step_rename(root: Path) -> int:
    log.info("=== STEP: renaming ===")
    renamed = 0
    for fld in media_folders(root):
        before = {fe.path.name for fe in _collect_folder(fld)}
        if not before: continue
        after = {fe.path.name for fe in step1_rename(fld, _collect_folder(fld), CPU_COUNT)}
        renamed += len(after - before)
    return renamed

# ---------- Interactive prompts ----------
def ask_yes_no(question: str) -> bool:
    while True:
        try: ans = input(f"{question} [y/n]: ").strip().lower()
        except EOFError:
            print("\nNo input available; aborting.")
            sys.exit(1)
        if ans in ("y", "yes"): return True
        if ans in ("n", "no"): return False
        print("  Please answer y or n.")

def sweep_tmp_files(root: Path):
    for p in root.rglob(f"{TMP_PREFIX}*"):
        try:
            p.unlink()
            log.info(f"Removed leftover temp file from an interrupted run: {p}")
        except OSError: pass

def main():
    for stream in (sys.stdout, sys.stderr):
        try: stream.reconfigure(errors="replace")    # non-ASCII file names on legacy Windows consoles
        except (AttributeError, ValueError): pass

    ap = argparse.ArgumentParser(description="Strip metadata, deduplicate, losslessly re-encode and rename media.")
    ap.add_argument("--root-dir", type=str, default=None, help="Folder to process (default: the current folder)")
    ap.add_argument("--phash-threshold", type=int, default=DEFAULT_PHASH_THRESHOLD)
    ap.add_argument("--dhash-img-threshold", type=int, default=DEFAULT_DHASH_THRESHOLD_IMG)
    ap.add_argument("--dhash-vid-threshold", type=int, default=DEFAULT_DHASH_THRESHOLD_VID)
    ap.add_argument("--hash-size", type=int, default=DEFAULT_HASH_SIZE)
    ap.add_argument("--num-frames", type=int, default=DEFAULT_NUM_FRAMES)
    args = ap.parse_args()

    root = Path(args.root_dir).resolve() if args.root_dir else Path.cwd().resolve()
    if not root.is_dir():
        log.error(f"Root not found: {root}")
        sys.exit(1)

    ffmpeg, ffprobe, jpegtran = which("ffmpeg"), which("ffprobe"), which("jpegtran")
    files = media_files(root)
    n_img = sum(is_image(p) for p in files)
    n_vid = sum(is_video(p) for p in files)
    n_aud = sum(is_audio(p) for p in files)

    print("=" * 72)
    print(" SECURE MEDIA PROCESSOR")
    print("=" * 72)
    print(f" Root folder : {root}")
    print(f" Found       : {n_img} images, {n_vid} videos, {n_aud} audio files "
          f"in {len(media_folders(root))} folder(s) (incl. subfolders)")
    print(f" Tools       : ffmpeg={'yes' if ffmpeg else 'NO'}  ffprobe={'yes' if ffprobe else 'NO'}  "
          f"jpegtran={'yes' if jpegtran else 'NO'}")
    if root.parent == root or root == Path.home().resolve():
        print("\n WARNING: this is a drive root or your home folder. Every media file below it will be processed.")
    if not files:
        print("\nNo media files found. Nothing to do.")
        return
    print("\n Files are modified IN PLACE. Make sure you have a backup before continuing.\n")

    print("a) RENAMING")
    print("   Renames every file to '<folder name> (N).<ext>', numbered oldest first.")
    print("   Removes identifying original names (e.g. IMG_20250101_123456.jpg). Content hash: NOT changed.")
    do_rename = ask_yes_no("   Renaming needed?")

    print("\nb) METADATA STRIPPING  ** CHANGES THE CONTENT HASH (SHA-256/MD5) **")
    print("   Removes EXIF (GPS, camera serial, dates), XMP, IPTC, comments, embedded thumbnails,")
    print("   video/audio tags, chapters, cover art and GPS telemetry streams.")
    print("   Lossless: pixels/frames/samples are untouched; only orientation and colour profile are kept.")
    print("   Every file that HAD metadata gets a new hash; files with none stay byte-identical.")
    print("   JPEG/PNG/WebP/GIF via built-in parser; video/audio need ffmpeg; TIFF/BMP not stripped.")
    if not ffmpeg: print("   NOTE: ffmpeg missing -> video/audio will be skipped.")
    do_strip = ask_yes_no("   Metadata stripping needed?")

    print("\nc) LOSSLESS RE-ENCODING  ** CHANGES THE CONTENT HASH (SHA-256/MD5) **")
    print("   Rewrites each file; decoded pixels/frames/samples stay bit-identical (verified).")
    print("   File size impact:")
    print("     JPEG (jpegtran)      : usually a few % SMALLER")
    print("     PNG / GIF / TIFF     : roughly the same")
    print("     Lossy WebP           : LARGER (stored losslessly)")
    print("     VIDEO -> FFV1 .mkv   : MUCH LARGER (typically 5-20x+) and slow; extension becomes .mkv")
    print("     Lossless audio->FLAC : about the same or smaller; lossy audio (mp3/aac/ogg/opus) is skipped")
    if not jpegtran: print("   NOTE: jpegtran missing -> JPEGs will be skipped (no lossy fallback).")
    if not (ffmpeg and ffprobe): print("   NOTE: ffmpeg/ffprobe missing -> video/audio will be skipped.")
    do_reencode = ask_yes_no("   Lossless re-encoding needed?")

    print("\nd) DEDUPLICATION")
    print("   Perceptual hashing (p-hash + d-hash, Hamming distance) within each folder.")
    print(f"   The oldest copy is kept; duplicates go to '{DUP_DIRNAME}', unreadable files to '{CORRUPT_DIRNAME}'.")
    if not (ffmpeg and ffprobe): print("   NOTE: ffmpeg/ffprobe missing -> videos are left alone.")
    do_dedupe = ask_yes_no("   Deduplication needed?")

    plan = [name for name, on in (("1. Metadata strip", do_strip), ("2. Deduplication", do_dedupe),
                                  ("3. Lossless re-encode", do_reencode), ("4. Renaming", do_rename)) if on]
    if not plan:
        print("\nNothing selected. Exiting without changes.")
        return
    print("\nPlanned run (in this order):")
    for step in plan: print(f"   {step}")
    print(f" on {len(files)} files under {root}")
    try: confirm = input("\nType YES to start: ").strip()
    except EOFError: confirm = ""
    if confirm != "YES":
        print("Not confirmed. Exiting without changes.")
        return

    atexit.register(_sweep_leftover_temp_dirs)
    setup_file_logger(root / LOG_NAME)
    sweep_tmp_files(root)
    started = time.time()
    results = []
    try:
        with ProcessPoolExecutor(max_workers=CPU_COUNT) as pool:
            if do_strip:
                results.append(f"Metadata strip  : {step_strip(root, pool, ffmpeg)}")
            if do_dedupe:
                n_dup, n_bad = step_dedupe(root, pool, args)
                results.append(f"Deduplication   : {n_dup} duplicate(s) -> {root / DUP_DIRNAME}, "
                               f"{n_bad} unreadable -> {root / CORRUPT_DIRNAME}")
            if do_reencode:
                results.append(f"Lossless re-enc : {step_reencode(root, pool, ffmpeg, ffprobe, jpegtran, do_strip)}")
        if do_rename:
            results.append(f"Renaming        : {step_rename(root)} file(s) renamed")
    except KeyboardInterrupt:
        print("\nInterrupted. Files already processed keep their changes; the file in progress was left untouched.")
        sys.exit(130)

    print(f"\nProcessing complete in {time.time() - started:,.0f}s.")
    for line in results: print(f"   {line}")
    print(f"   Log file        : {root / LOG_NAME}")

if __name__ == "__main__":
    main()
