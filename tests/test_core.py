"""Tests for media_core and updater.  Run:  python -m pytest -q tests
Video/audio tests are skipped when ffmpeg/ffprobe are not on PATH."""
import importlib.util, io, json, os, random, shutil, subprocess, sys, threading, time, zipfile
from pathlib import Path

import numpy as np
import imagehash
import pytest
from PIL import Image, ImageDraw, PngImagePlugin

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import media_core as core   # noqa: E402
import updater              # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not on PATH")

def load_v7():
    spec = importlib.util.spec_from_file_location("v7", ROOT / "parallel_rename_clean_deduplicate_media_v7.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod

def picture(seed: int, w: int = 400, h: int = 300) -> Image.Image:
    rnd = random.Random(seed); im = Image.new("RGB", (w, h)); d = ImageDraw.Draw(im)
    for _ in range(60):
        x0, x1 = sorted(rnd.sample(range(w), 2)); y0, y1 = sorted(rnd.sample(range(h), 2))
        d.rectangle([x0, y0, x1, y1], fill=tuple(rnd.randint(0, 255) for _ in range(3)))
    return im

def jpeg_with_metadata(orientation: int = 6, trailing: bytes = b"") -> bytes:
    ex = Image.Exif(); ex[0x0112] = orientation; ex[0x010F] = "SecretCam"
    ex[0x8825] = {1: "N", 2: (1.0, 21.0, 7.0), 3: "E", 4: (103.0, 49.0, 11.0)}
    buf = io.BytesIO()
    picture(3, 640, 480).save(buf, "JPEG", quality=90, exif=ex.tobytes(), comment=b"secret comment",
                              xmp=b"<x:xmpmeta>Singapore</x:xmpmeta>")
    return buf.getvalue() + trailing

def set_mtime(p: Path, k: int):
    t = time.time() - 100000 + k * 100
    os.utime(p, (t, t))

# ---------------------------------------------------------------- dedupe == v7
def test_find_duplicates_matches_v7_scan():
    v7 = load_v7()
    moves, lines = [], []
    v7.safe_move = lambda src, dst_dir, new_name=None: moves.append(src)
    v7.write_log = lambda log_path, ls: lines.extend(ls)
    rng = np.random.default_rng(7)
    def bits(center=None, flips=0):
        b = rng.integers(0, 2, (16, 16)).astype(bool) if center is None else center.copy()
        if flips: b.flat[rng.choice(256, flips, replace=False)] ^= True
        return b
    for trial in range(150):
        n = int(rng.integers(5, 100)); n_vid = int(rng.integers(0, n // 2 + 1))
        centers = [(bits(), bits()) for _ in range(int(rng.integers(1, 7)))]
        entries, h7, hn = [], {}, {}
        for k in range(n):
            vid = k < n_vid
            p = Path(f"f{k}.{'mp4' if vid else 'jpg'}")
            def pair():
                c = centers[int(rng.integers(len(centers)))]
                return (imagehash.ImageHash(bits(c[0], int(rng.integers(0, 16)))),
                        imagehash.ImageHash(bits(c[1], int(rng.integers(0, 9)))))
            if vid:
                fr = [pair() for _ in range(int(rng.choice([2, 3, 3])))]
                h7[p] = v7.VideoHashes([v7.ImageHashes(a, b) for a, b in fr])
                hn[p] = core.VideoHashes([core.ImageHashes(a, b) for a, b in fr])
            else:
                a, b = pair(); h7[p] = v7.ImageHashes(a, b); hn[p] = core.ImageHashes(a, b)
            entries.append((p, float(rng.integers(0, 30)), not vid, vid))
        random.Random(trial).shuffle(entries)
        pth, dimg, dvid = int(rng.integers(4, 14)), int(rng.integers(1, 6)), int(rng.integers(3, 9))
        moves.clear(); lines.clear()
        v7.step3_compare_and_dedupe_sequential(Path("F"), [v7.FileEntry(*e) for e in entries], h7,
                                               Path("D"), Path("L"), pth, dimg, dvid)
        got = core.find_duplicates([core.FileEntry(*e) for e in entries], hn, pth, dimg, dvid)
        base7 = {l.split("| dup=")[1].split(" |")[0]: l.split("base=")[1].split(" |")[0] for l in lines}
        assert sorted(map(str, moves)) == sorted(str(d.path) for d, _, _ in got)
        assert all(base7[str(d.path)] == str(b.path) for d, b, _ in got)

def test_image_hashes_match_v7(tmp_path):
    v7 = load_v7()
    picture(1).save(tmp_path / "a.png"); (tmp_path / "b.jpg").write_bytes(jpeg_with_metadata())
    for f in tmp_path.iterdir():
        a = v7._compute_image_hashes(f, 16); b = core._compute_image_hashes(f, 16)
        assert (a.phash, a.dhash) == (b.phash, b.dhash)

# ---------------------------------------------------------------- strip / padding
def test_strip_jpeg_removes_metadata_keeps_pixels_and_orientation():
    data = jpeg_with_metadata(6, trailing=b"MOTIONPHOTO" * 50)
    new, removed = core.strip_jpeg(data, core.exif_orientation(data))
    assert {"EXIF", "XMP", "comment"} <= set(removed)
    for leak in (b"SecretCam", b"Singapore", b"secret comment", b"MOTIONPHOTO"):
        assert leak not in new
    assert core.same_pixels(data, new) and core.exif_orientation(new) == 6

def test_strip_png_drops_text_chunks(tmp_path):
    info = PngImagePlugin.PngInfo(); info.add_text("Author", "Kenneth")
    buf = io.BytesIO(); picture(2).save(buf, "PNG", pnginfo=info); data = buf.getvalue()
    new, removed = core.strip_png(data, 1)
    assert "tEXt" in removed and b"Kenneth" not in new and core.same_pixels(data, new)

@pytest.mark.parametrize("fmt", ["jpg", "png", "gif"])
def test_padding_changes_bytes_not_pixels(fmt):
    buf = io.BytesIO()
    if fmt == "jpg": buf.write(jpeg_with_metadata(3, trailing=b"EXTRA"))
    elif fmt == "png": picture(4).save(buf, "PNG")
    else:
        frames = [picture(10 + k, 120, 90).convert("P") for k in range(3)]
        frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=[100, 200, 300], loop=0)
    data = buf.getvalue()
    new = core.IMAGE_PADDERS["." + fmt](data)
    assert new != data and core.same_pixels(data, new)
    assert core.exif_orientation(new) == core.exif_orientation(data)
    if fmt == "jpg":
        assert new.endswith(b"EXTRA")                 # embedded extra data after the image survives

# ---------------------------------------------------------------- folder merge
def test_folder_merge_and_prefix(tmp_path):
    (tmp_path / "Trip").mkdir(); (tmp_path / "@Trip").mkdir(); (tmp_path / "@Lonely").mkdir()
    picture(1).save(tmp_path / "Trip" / "a.png"); picture(2).save(tmp_path / "@Trip" / "a.png")
    picture(3).save(tmp_path / "@Trip" / "b.png"); (tmp_path / "@Trip" / "notes.txt").write_text("x")
    opts = core.Options(merge_folders=True, folder_prefix="X_")
    core.step_merge(tmp_path, opts, core.RunContext())
    assert sorted(p.name for p in (tmp_path / "X_Trip").iterdir()) == ["a.png", "a_1.png", "b.png"]
    assert (tmp_path / "X_@Trip" / "notes.txt").exists()   # non-media kept, folder not deleted
    assert (tmp_path / "X_@Lonely").exists()               # unpaired folders are only prefixed
    core.step_merge(tmp_path, opts, core.RunContext())     # prefix is not applied twice
    assert sorted(p.name for p in tmp_path.iterdir()) == ["X_@Lonely", "X_@Trip", "X_Trip"]

# ---------------------------------------------------------------- pipeline
def build_tree(root: Path):
    alb = root / "Album"; alb.mkdir(parents=True)
    (alb / "IMG_0001.jpg").write_bytes(jpeg_with_metadata()); set_mtime(alb / "IMG_0001.jpg", 1)
    picture(1).save(alb / "x.png"); set_mtime(alb / "x.png", 2)
    picture(1).save(alb / "x_copy.png"); set_mtime(alb / "x_copy.png", 3)
    picture(2).save(alb / "y.png"); set_mtime(alb / "y.png", 4)
    (alb / "readme.txt").write_text("untouched")

def test_pipeline_all_steps_images(tmp_path):
    build_tree(tmp_path)
    opts = core.Options(strip=True, dedupe=True, reencode=True, rename=True, use_processes=False)
    seen = []
    results = core.run_pipeline(tmp_path, opts, progress=lambda s, d, t: seen.append(s))
    alb = tmp_path / "Album"
    assert sorted(p.name for p in alb.iterdir()) == ["Album (1).jpg", "Album (1).png", "Album (2).png", "readme.txt"]
    assert [p.name for p in (tmp_path / core.DUP_DIRNAME).iterdir()] == ["Album_duplicate_x_copy.png"]
    assert b"SecretCam" not in (alb / "Album (1).jpg").read_bytes()
    assert (alb / "readme.txt").read_text() == "untouched"
    assert any("3 of 3 files have a new content hash" in r for r in results)
    assert {"Hashing (before)", "Deduplication", "Renaming", "Hashing (after)"} <= set(seen)
    assert not list(tmp_path.rglob(core.TMP_PREFIX + "*"))

def test_cancel_stops_and_leaves_files_intact(tmp_path):
    build_tree(tmp_path)
    before = {p.name: p.read_bytes() for p in (tmp_path / "Album").iterdir()}
    stop = threading.Event()
    with pytest.raises(core.Cancelled):
        core.run_pipeline(tmp_path, core.Options(strip=True, rename=True, use_processes=False),
                          progress=lambda s, d, t: stop.set(), cancel=stop)
    after = {p.name: p.read_bytes() for p in (tmp_path / "Album").iterdir()}
    assert after == before and not list(tmp_path.rglob(core.TMP_PREFIX + "*"))

@needs_ffmpeg
def test_video_strip_and_padding(tmp_path):
    ff = shutil.which("ffmpeg")
    clip = tmp_path / "v.mp4"
    subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-metadata", "location=+01.35+103.81/", str(clip)], check=True)
    status, _ = core.strip_av(clip, ff)
    assert status == "changed" and b"103.81" not in clip.read_bytes()
    before = clip.read_bytes()
    status, _ = core.pad_av(clip, ff)
    assert status == "changed" and clip.read_bytes() != before

# ---------------------------------------------------------------- updater
def test_updater_snapshot_install(tmp_path, monkeypatch):
    app = tmp_path / "app"; app.mkdir()
    (app / "media_core.py").write_text("OLD = 1\n"); (app / "gone.py").write_text("x = 1\n")
    (app / "update_config.json").write_text(json.dumps({"github_token": ""}))
    (app / ".installed_files").write_text("media_core.py\ngone.py\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("repo-abc123/media_core.py", "NEW = 2\n")
        zf.writestr("repo-abc123/app.py", "print('hi')\n")
        zf.writestr("repo-abc123/update_config.json", "{\"github_token\": \"FROM-REPO\"}")
    for name, value in (("APP_DIR", app), ("CONFIG_FILE", app / "update_config.json"),
                        ("VERSION_FILE", app / ".installed_version"), ("MANIFEST_FILE", app / ".installed_files"),
                        ("BACKUP_DIR", app / ".update_backup")):
        monkeypatch.setattr(updater, name, value)
    monkeypatch.setattr(updater, "_get", lambda url, accept="": buf.getvalue())
    msg = updater.apply(updater.UpdateInfo(None, "abc123" * 6, "2026-11-12", "test"))
    assert (app / "media_core.py").read_text() == "NEW = 2\n" and (app / "app.py").exists()
    assert not (app / "gone.py").exists()                                   # deleted upstream -> backed up
    assert "FROM-REPO" not in (app / "update_config.json").read_text()      # local config never replaced
    assert list((app / ".update_backup").rglob("gone.py")) and "Installed" in msg

def test_updater_refuses_broken_snapshot(tmp_path, monkeypatch):
    app = tmp_path / "app"; app.mkdir(); (app / "media_core.py").write_text("OLD = 1\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("repo-x/media_core.py", "def broken(:\n")
    for name, value in (("APP_DIR", app), ("CONFIG_FILE", app / "update_config.json"),
                        ("VERSION_FILE", app / ".installed_version"), ("MANIFEST_FILE", app / ".installed_files"),
                        ("BACKUP_DIR", app / ".update_backup")):
        monkeypatch.setattr(updater, name, value)
    monkeypatch.setattr(updater, "_get", lambda url, accept="": buf.getvalue())
    with pytest.raises(updater.UpdateError):
        updater.apply(updater.UpdateInfo(None, "f" * 40, "", ""))
    assert (app / "media_core.py").read_text() == "OLD = 1\n"
