"""Tests for media_core and updater.  Run:  python -m pytest -q tests
Video/audio tests are skipped when ffmpeg/ffprobe are not on PATH."""
import importlib.util, io, json, os, random, shutil, subprocess, sys, threading, time, zipfile
from pathlib import Path

import numpy as np
import imagehash
import pytest
from PIL import Image, ImageDraw, PngImagePlugin

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import media_core as core   # noqa: E402
import updater              # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not on PATH")

def load_v7():
    spec = importlib.util.spec_from_file_location("v7", ROOT / "legacy" / "parallel_rename_clean_deduplicate_media_v7.py")
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

def sample(fmt: str) -> bytes:
    buf = io.BytesIO()
    if fmt == "jpg": buf.write(jpeg_with_metadata(3, trailing=b"EXTRA"))
    elif fmt == "png": picture(4).save(buf, "PNG")
    elif fmt == "gif":
        frames = [picture(10 + k, 120, 90).convert("P") for k in range(3)]
        frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=[100, 200, 300], loop=0)
    elif fmt == "webp-lossy": picture(5).save(buf, "WEBP", quality=80)
    elif fmt == "webp-lossless-alpha": picture(5).convert("RGBA").save(buf, "WEBP", lossless=True, exact=True)
    elif fmt == "webp-extended":
        ex = Image.Exif(); ex[0x010F] = "cam"; picture(5).save(buf, "WEBP", quality=80, exif=ex.tobytes())
    elif fmt == "tif": picture(6).save(buf, "TIFF", compression="tiff_lzw")
    elif fmt == "bmp": picture(7).save(buf, "BMP")
    return buf.getvalue()

PAD_FORMATS = ["jpg", "png", "gif", "webp-lossy", "webp-lossless-alpha", "webp-extended", "tif", "bmp"]

@pytest.mark.parametrize("fmt", PAD_FORMATS)
def test_padding_changes_bytes_not_pixels(fmt):
    data = sample(fmt)
    ext = "." + fmt.split("-")[0]
    new = core.IMAGE_PADDERS[ext](data)
    assert new != data and core.same_pixels(data, new)
    assert core.exif_orientation(new) == core.exif_orientation(data)
    if fmt == "jpg":
        assert new.endswith(b"EXTRA")                 # embedded extra data after the image survives
    for _ in range(3):                                # padding again replaces the old padding: no growth
        again = core.IMAGE_PADDERS[ext](new)
        assert again != new and len(again) == len(new) and core.same_pixels(data, again)
        new = again

def test_reprocessing_does_not_inflate_files(tmp_path):
    """Someone feeds an app-processed folder into their own copy of the app: sizes must not keep growing."""
    build_tree(tmp_path)
    for name, fmt in (("t.tif", "tif"), ("b.bmp", "bmp"), ("w.webp", "webp-lossy"), ("g.gif", "gif")):
        (tmp_path / "Album" / name).write_bytes(sample(fmt))
    opts = core.Options(strip=True, reencode=True, unique_hash=True, hash_report=False, use_processes=False)
    core.run_pipeline(tmp_path, opts)
    first = {f.name: (f.stat().st_size, core.sha256_file(f)) for f in core.media_files(tmp_path)}
    for _ in range(2):
        core.run_pipeline(tmp_path, opts)
    again = {f.name: (f.stat().st_size, core.sha256_file(f)) for f in core.media_files(tmp_path)}
    assert {n: s for n, (s, _) in again.items()} == {n: s for n, (s, _) in first.items()}
    assert all(again[n][1] != first[n][1] for n in first)      # new random hash every time

def test_unique_padding_makes_hashes_differ_between_people(tmp_path):
    """Two people process the same photos: with padding no output hash is shared; without it, all are."""
    def outputs(where: Path, unique: bool) -> set:
        build_tree(where)
        core.run_pipeline(where, core.Options(strip=True, reencode=True, unique_hash=unique, hash_report=False,
                                              use_processes=False))
        return {core.sha256_file(f) for f in core.media_files(where)}
    plain_a, plain_b = outputs(tmp_path / "a", False), outputs(tmp_path / "b", False)
    assert plain_a == plain_b                         # deterministic: this is the problem padding solves
    uniq_a, uniq_b = outputs(tmp_path / "c", True), outputs(tmp_path / "d", True)
    assert not uniq_a & uniq_b and not uniq_a & plain_a
    # the tree holds an identical pair (x.png, x_copy.png): without padding they share a hash, with it they don't
    assert len(plain_a) == 3 and len(uniq_a) == 4

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
    once = clip.read_bytes()
    status, _ = core.pad_av(clip, ff)                 # the padding tag is replaced, not added again
    assert status == "changed" and clip.read_bytes() != once and len(clip.read_bytes()) == len(once)

# ---------------------------------------------------------------- updater
def fake_github(monkeypatch, files: dict, tamper: str = ""):
    """Serve `files` ({path under src/: bytes}) the way GitHub's tree API and raw file host would."""
    blobs = {p: updater._blob_sha(d) for p, d in files.items()}
    def get(url, accept=""):
        if url.endswith("/git/trees/COMMIT"):
            return json.dumps({"tree": [{"path": "src", "type": "tree", "sha": "SRCTREE"},
                                        {"path": "dist", "type": "tree", "sha": "DIST"}]}).encode()
        if "/git/trees/SRCTREE" in url:
            return json.dumps({"tree": [{"path": p, "type": "blob", "sha": b} for p, b in blobs.items()]}).encode()
        assert url.startswith("https://raw.githubusercontent.com/") and "/COMMIT/src/" in url, url
        rel = url.split("/COMMIT/src/", 1)[1]
        return files[rel] + (b"tampered" if rel == tamper else b"")
    monkeypatch.setattr(updater, "_get", get)

def point_updater_at(app: Path, monkeypatch):
    for name, value in (("APP_DIR", app), ("CONFIG_FILE", app / "update_config.json"),
                        ("VERSION_FILE", app / ".installed_version"), ("MANIFEST_FILE", app / ".installed_files"),
                        ("BACKUP_DIR", app / ".update_backup"), ("RELEASE_FILE", app / ".release_version")):
        monkeypatch.setattr(updater, name, value)

def test_updater_snapshot_install(tmp_path, monkeypatch):
    app = tmp_path / "app"; app.mkdir()
    (app / "media_core.py").write_text("OLD = 1\n"); (app / "gone.py").write_text("x = 1\n")
    (app / "update_config.json").write_text(json.dumps({"github_token": ""}))
    (app / ".installed_files").write_text("media_core.py\ngone.py\n")
    point_updater_at(app, monkeypatch)
    fake_github(monkeypatch, {"media_core.py": b"NEW = 2\n", "app.py": b"print('hi')\n", "assets/icon.png": b"PNG",
                              "update_config.json": b'{"github_token": "FROM-REPO"}'})
    msg = updater.apply(updater.UpdateInfo(None, "COMMIT", "2026-11-12", "test"))
    assert (app / "media_core.py").read_text() == "NEW = 2\n" and (app / "app.py").exists()
    assert (app / "assets" / "icon.png").read_bytes() == b"PNG"               # subfolders of src/ too
    assert not (app / "dist").exists()                                      # nothing outside src/ is fetched
    assert not (app / "gone.py").exists()                                   # deleted upstream -> backed up
    assert "FROM-REPO" not in (app / "update_config.json").read_text()      # local config never replaced
    assert list((app / ".update_backup").rglob("gone.py")) and "Installed" in msg
    assert (app / ".installed_version").read_text().strip() == "COMMIT"

def test_updater_refuses_broken_snapshot(tmp_path, monkeypatch):
    app = tmp_path / "app"; app.mkdir(); (app / "media_core.py").write_text("OLD = 1\n")
    point_updater_at(app, monkeypatch)
    fake_github(monkeypatch, {"media_core.py": b"def broken(:\n"})
    with pytest.raises(updater.UpdateError):
        updater.apply(updater.UpdateInfo(None, "COMMIT", "", ""))
    assert (app / "media_core.py").read_text() == "OLD = 1\n"

def test_updater_refuses_tampered_download(tmp_path, monkeypatch):
    app = tmp_path / "app"; app.mkdir(); (app / "media_core.py").write_text("OLD = 1\n")
    point_updater_at(app, monkeypatch)
    fake_github(monkeypatch, {"media_core.py": b"NEW = 2\n", "app.py": b"x = 1\n"}, tamper="app.py")
    with pytest.raises(updater.UpdateError, match="hash"):
        updater.apply(updater.UpdateInfo(None, "COMMIT", "", ""))
    assert (app / "media_core.py").read_text() == "OLD = 1\n" and not (app / "app.py").exists()

def test_blob_sha_matches_git():
    assert updater._blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"    # git hash-object

# ---------------------------------------------------------------- Android
def load_wheel_script():
    spec = importlib.util.spec_from_file_location("afw", ROOT / "scripts" / "android_ffmpeg_wheel.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod

def test_android_ffmpeg_wheel_installs_as_flet_would(tmp_path):
    """The wheel must carry the binaries as plain lib*.so (Flet moves those to the APK's native-library folder),
    under the names media_core looks for, and pip must accept it for Flet's Android target."""
    afw = load_wheel_script()
    assert afw.FILES == core.ANDROID_TOOL_FILES
    bins = tmp_path / "bin"; bins.mkdir()
    (bins / "ffmpeg").write_bytes(b"\x7fELF ffmpeg"); (bins / "ffprobe").write_bytes(b"\x7fELF ffprobe")
    wheel = afw.build_wheel(bins, tmp_path / "wheels")
    target = tmp_path / "site-packages"
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "--no-deps",
                        "--target", str(target), "--only-binary=:all:", "--platform", "android_24_arm64_v8a",
                        "--python-version", "3.14", "--implementation", "cp",
                        f"smp-android-ffmpeg @ {wheel.as_uri()}"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    pkg = target / "smp_android_ffmpeg"
    assert (pkg / "libsmp_ffmpeg.so").read_bytes() == b"\x7fELF ffmpeg"
    assert (pkg / "libsmp_ffprobe.so").read_bytes() == b"\x7fELF ffprobe"

def test_android_ffmpeg_wheel_is_added_to_pyproject(tmp_path):
    import tomllib
    afw = load_wheel_script()
    pp = tmp_path / "pyproject.toml"
    shutil.copy(ROOT / "pyproject.toml", pp)
    wheel = tmp_path / "w" / "smp_android_ffmpeg-8.0.1-py3-none-android_24_arm64_v8a.whl"
    afw.add_to_pyproject(pp, wheel); afw.add_to_pyproject(pp, wheel)          # idempotent
    android = tomllib.loads(pp.read_text())["tool"]["flet"]["android"]
    assert android["dependencies"] == [f"smp-android-ffmpeg @ {wheel.resolve().as_uri()}"]
    assert android["legacy_packaging"] is True and android["target_arch"] == ["arm64-v8a"]

def test_android_finds_bundled_ffmpeg(tmp_path, monkeypatch):
    libdir = tmp_path / "data" / "app" / "pkg-1" / "lib" / "arm64"; libdir.mkdir(parents=True)
    for f in ("libpython3.14.so", "libsmp_ffmpeg.so", "libsmp_ffprobe.so"):
        (libdir / f).write_bytes(b"x")
    maps = tmp_path / "maps"
    maps.write_text("7000000000-7000001000 r-xp 00000000 fd:05 1234   /system/lib64/libc.so\n"
                    "7000002000-7000003000 r--p 00000000 00:00 0 \n"
                    f"7000004000-7000005000 r-xp 00000000 fd:05 99   {(libdir / 'libpython3.14.so').as_posix()}\n")
    assert core.android_native_lib_dir(maps) == libdir
    monkeypatch.setattr(core, "ANDROID", True)
    monkeypatch.setattr(core, "android_native_lib_dir", lambda: libdir)
    monkeypatch.setattr(core, "tool_runs", lambda exe: (True, ""))
    t = core.find_tools()
    assert t.ffmpeg == str(libdir / "libsmp_ffmpeg.so") and t.ffprobe == str(libdir / "libsmp_ffprobe.so")
    monkeypatch.setattr(core, "tool_runs", lambda exe: (False, "would not start."))
    t = core.find_tools()                                    # present but not runnable -> treated as missing
    assert t.ffmpeg is None and t.ffprobe is None and t.blocked == ("ffmpeg", "ffprobe")

def fake_releases(monkeypatch, releases: list, compare_status: str = "behind"):
    """GitHub's release list (newest first), commit lookup and compare API."""
    def get(url, accept=""):
        if "/releases?" in url:
            return json.dumps(releases).encode()
        if "/compare/" in url:
            return json.dumps({"status": compare_status}).encode()
        if "/commits/" in url:
            return json.dumps({"commit": {"message": "Fix things\n\nmore"}}).encode()
        raise AssertionError(url)
    monkeypatch.setattr(updater, "_get", get)

def release(n: int, sha: str, apk: bool = True) -> dict:
    assets = [{"name": "SecureMediaProcessor-portable.zip", "browser_download_url": f"https://x/{n}.zip"}]
    if apk:
        assets.append({"name": "SecureMediaProcessor.apk", "browser_download_url": f"https://x/{n}.apk"})
    return {"tag_name": f"1.0.{n}", "target_commitish": sha, "published_at": f"2026-11-{n:02d}T10:00:00Z",
            "draft": False, "prerelease": False, "assets": assets}

def installed_copy(tmp_path, monkeypatch, sha=None, name=None) -> Path:
    app = tmp_path / "app"; app.mkdir()
    if sha: (app / ".installed_version").write_text(sha + "\n")
    if name: (app / ".release_version").write_text(name + "\n")
    point_updater_at(app, monkeypatch)
    monkeypatch.setattr(updater, "RELEASE_FILE", app / ".release_version")
    monkeypatch.setattr(updater, "is_git_checkout", lambda: False)
    return app

def test_version_shown_without_network(tmp_path, monkeypatch):
    installed_copy(tmp_path, monkeypatch, sha="a" * 40, name="1.0.6")
    monkeypatch.setattr(updater, "_get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("went online")))
    assert updater.describe_local() == "1.0.6"
    (updater.RELEASE_FILE).unlink()
    assert updater.describe_local() == "commit aaaaaaa"

def test_apk_version_comes_from_build_info(tmp_path, monkeypatch):
    """Flet's packager drops '.files' from the APK, so the Android build writes build_info.json."""
    app = installed_copy(tmp_path, monkeypatch)
    monkeypatch.setattr(updater, "BUILD_INFO_FILE", app / "build_info.json")
    assert updater.describe_local() == "unknown"
    (app / "build_info.json").write_text(json.dumps({"version": "1.0.4", "commit": "d" * 40}))
    assert updater.describe_local() == "1.0.4" and updater.local_version() == "d" * 40
    fake_releases(monkeypatch, [release(4, "d" * 40)])
    info = updater.check()
    assert info.up_to_date and info.local_name == "1.0.4"

def test_update_check_reports_this_and_latest_version(tmp_path, monkeypatch):
    installed_copy(tmp_path, monkeypatch, sha="a" * 40, name="1.0.6")
    fake_releases(monkeypatch, [release(7, "b" * 40), release(6, "a" * 40)])
    info = updater.check()
    assert (info.local_name, info.remote_name, info.remote) == ("1.0.6", "1.0.7", "b" * 40)
    assert not info.up_to_date and info.remote_message == "Fix things" and info.download_url == ""

def test_update_check_up_to_date_and_newer_than_release(tmp_path, monkeypatch):
    installed_copy(tmp_path, monkeypatch, sha="b" * 40)
    fake_releases(monkeypatch, [release(7, "b" * 40), release(6, "a" * 40)])
    info = updater.check()
    assert info.up_to_date and not info.local_ahead and info.local_name == "1.0.7"   # name found from the list
    monkeypatch.setattr(updater, "local_version", lambda: "c" * 40)          # a commit with no release yet
    fake_releases(monkeypatch, [release(7, "b" * 40)], compare_status="ahead")
    info = updater.check()
    assert info.up_to_date and info.local_ahead
    fake_releases(monkeypatch, [release(7, "b" * 40)], compare_status="behind")
    assert not updater.check().up_to_date

def test_snapshot_update_records_the_release_version(tmp_path, monkeypatch):
    app = installed_copy(tmp_path, monkeypatch, sha="a" * 40, name="1.0.6")
    fake_github(monkeypatch, {"app.py": b"x = 1\n"})
    msg = updater.apply(updater.UpdateInfo("a" * 40, "COMMIT", "", "", remote_name="1.0.7"))
    assert "1.0.7" in msg and updater.describe_local() == "1.0.7"
    assert (app / ".installed_version").read_text().strip() == "COMMIT"

def test_android_update_check_uses_latest_release_apk(tmp_path, monkeypatch):
    installed_copy(tmp_path, monkeypatch, sha="a" * 40, name="1.0.6")
    monkeypatch.setattr(updater, "ANDROID", True)
    fake_releases(monkeypatch, [release(8, "c" * 40, apk=False), release(7, "b" * 40), release(6, "a" * 40)])
    info = updater.check()                    # 1.0.8's Android build failed: the phone is offered 1.0.7
    assert (info.local_name, info.remote_name) == ("1.0.6", "1.0.7") and not info.up_to_date
    assert info.download_url == "https://x/7.apk"
    with pytest.raises(updater.UpdateError):          # code is never swapped inside the APK
        updater.apply(info)
    fake_releases(monkeypatch, [release(8, "c" * 40, apk=False)])
    with pytest.raises(updater.UpdateError, match="No release has an Android app"):
        updater.check()

def test_commit_keeps_going_when_dates_cannot_be_set(tmp_path, monkeypatch):
    """Some phone storage refuses os.utime; the verified file must still replace the original."""
    original = tmp_path / "a.png"; original.write_bytes(b"old")
    tmp = tmp_path / "t.png"; tmp.write_bytes(b"new")
    def refuse(*a, **k):
        raise PermissionError("Operation not permitted")
    monkeypatch.setattr(core.os, "utime", refuse)
    core.commit(tmp, original, original, original.stat())
    assert original.read_bytes() == b"new" and not tmp.exists()

# ---------------------------------------------------------------- settings / tools installer
def test_log_dir_setting(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "SETTINGS_FILE", tmp_path / "settings.json")
    assert core.log_dir() == core.DEFAULT_LOG_DIR
    core.set_log_dir(tmp_path / "mylogs")
    assert core.log_dir() == (tmp_path / "mylogs").resolve()
    assert core.default_log_path().parent == (tmp_path / "mylogs").resolve()
    assert core.log_inside_root(tmp_path / "media" / "logs", tmp_path / "media")
    assert not core.log_inside_root(tmp_path / "mylogs", tmp_path / "media")
    core.set_log_dir(None)
    assert core.log_dir() == core.DEFAULT_LOG_DIR

def _fake_ffmpeg_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe"):
            zf.writestr(f"ffmpeg-8.0.1-essentials_build/bin/{name}", b"MZ fake " + name.encode())
        zf.writestr("ffmpeg-8.0.1-essentials_build/README.txt", "docs")
    return buf.getvalue()

def _patch_download(monkeypatch, ti, blob: bytes, pinned_hash: str):
    monkeypatch.setattr(ti, "ZIP_SHA256", pinned_hash)
    monkeypatch.setattr(ti, "_get", lambda url, dest, progress=None: dest.write_bytes(blob))

@pytest.mark.skipif(sys.platform != "win32", reason="ffmpeg install is Windows-only")
def test_tools_installer_verifies_and_unpacks(tmp_path, monkeypatch):
    import hashlib, tools_installer as ti
    blob = _fake_ffmpeg_zip()
    _patch_download(monkeypatch, ti, blob, hashlib.sha256(blob).hexdigest())
    monkeypatch.setattr(core, "tool_runs", lambda exe: (True, ""))
    msg = ti.install_ffmpeg(target=tmp_path / "tools")
    assert sorted(p.name for p in (tmp_path / "tools").iterdir()) == ["ffmpeg.exe", "ffprobe.exe"]
    assert "8.0.1" in msg

@pytest.mark.skipif(sys.platform != "win32", reason="ffmpeg install is Windows-only")
def test_tools_installer_reports_windows_block(tmp_path, monkeypatch):
    """Smart App Control refusing the downloaded ffmpeg must be a clear message, not a crash."""
    import hashlib, tools_installer as ti
    blob = _fake_ffmpeg_zip()
    _patch_download(monkeypatch, ti, blob, hashlib.sha256(blob).hexdigest())
    def blocked(*a, **k):
        e = OSError("An Application Control policy has blocked this file"); e.winerror = 4551; raise e
    monkeypatch.setattr(core.subprocess, "run", blocked)
    core._RUN_CHECKS.clear()
    with pytest.raises(ti.InstallError, match="Smart App Control"):
        ti.install_ffmpeg(target=tmp_path / "tools")
    assert not (tmp_path / "tools").exists()
    core._RUN_CHECKS.clear()

def test_blocked_ffmpeg_counts_as_missing(monkeypatch):
    """A blocked ffmpeg on PATH would make v7's hashing move every video to Corrupted."""
    monkeypatch.setattr(core, "which", lambda name: f"C:/fake/{name}.exe")
    monkeypatch.setattr(core, "tool_runs", lambda exe: (False, "Windows blocked it.") if "ffmpeg" in exe else (True, ""))
    t = core.find_tools()
    assert t.ffmpeg is None and "ffmpeg" in t.blocked and t.ffprobe == "C:/fake/ffprobe.exe"

@pytest.mark.skipif(sys.platform != "win32", reason="ffmpeg install is Windows-only")
def test_tools_installer_rejects_bad_checksum(tmp_path, monkeypatch):
    import tools_installer as ti
    _patch_download(monkeypatch, ti, _fake_ffmpeg_zip(), "0" * 64)
    with pytest.raises(ti.InstallError, match="checksum"):
        ti.install_ffmpeg(target=tmp_path / "tools")
    assert not (tmp_path / "tools").exists()
