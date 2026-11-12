"""
Installs ffmpeg + ffprobe for the app, per user, without admin rights and without touching PATH:
they go into %LOCALAPPDATA%\\SecureMediaProcessor\\tools, which media_core.find_tools() searches.

Source: BtbN FFmpeg-Builds (https://github.com/BtbN/FFmpeg-Builds), release branch 8.1, LGPL "shared"
build (smallest download, no GPL-only codecs - none are needed here: FFV1, FLAC and stream copy are all
built in). The download is checked against the sha256 that BtbN publishes in checksums.sha256.
Network access happens only when the user asks for the install.
"""
import hashlib, os, shutil, subprocess, sys, tempfile, urllib.error, urllib.request, zipfile
from pathlib import Path
from typing import Callable, Optional

import media_core as core

RELEASE = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest"
ZIP_NAME = "ffmpeg-n8.1-latest-win64-lgpl-shared-8.1.zip"
APPROX_DOWNLOAD_MB = 77
WANTED = (".exe", ".dll")
SKIP = ("ffplay.exe",)                                 # media player, not needed

class InstallError(Exception):
    pass

def supported() -> bool:
    return sys.platform == "win32"

def _get(url: str, dest: Optional[Path] = None, progress: Optional[Callable[[int, int], None]] = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "SecureMediaProcessor-tools"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            if dest is None:
                return r.read()
            total, done = int(r.headers.get("Content-Length") or 0), 0
            with open(dest, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk); done += len(chunk)
                    if progress: progress(done, total)
            return b""
    except (urllib.error.URLError, TimeoutError) as e:
        raise InstallError(f"Download failed: {getattr(e, 'reason', e)}")

def expected_sha256() -> str:
    for line in _get(f"{RELEASE}/checksums.sha256").decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == ZIP_NAME:
            return parts[0].lower()
    raise InstallError(f"{ZIP_NAME} is not listed in BtbN's checksums.sha256")

def install_ffmpeg(progress: Optional[Callable[[int, int], None]] = None, target: Path = None) -> str:
    """Download, verify and unpack ffmpeg/ffprobe. Returns a message; raises InstallError."""
    if not supported():
        raise InstallError("Automatic ffmpeg install is only available on Windows.")
    target = target or core.TOOLS_DIR
    want_hash = expected_sha256()
    with tempfile.TemporaryDirectory(prefix="smp_ffmpeg_") as tmp:
        archive = Path(tmp) / ZIP_NAME
        _get(f"{RELEASE}/{ZIP_NAME}", archive, progress)
        h = hashlib.sha256()
        with open(archive, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""): h.update(chunk)
        if h.hexdigest() != want_hash:
            raise InstallError("Downloaded file does not match BtbN's published checksum; nothing was installed.")
        staging = Path(tmp) / "bin"; staging.mkdir()
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                parts = Path(info.filename).parts
                name = parts[-1] if parts else ""
                if len(parts) >= 2 and parts[-2] == "bin" and name.lower().endswith(WANTED) and name.lower() not in SKIP:
                    (staging / name).write_bytes(zf.read(info))
        if not (staging / "ffmpeg.exe").exists() or not (staging / "ffprobe.exe").exists():
            raise InstallError("The download did not contain ffmpeg.exe and ffprobe.exe; nothing was installed.")
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        r = subprocess.run([str(staging / "ffmpeg.exe"), "-version"], capture_output=True, text=True, timeout=60, **flags)
        if r.returncode != 0:
            raise InstallError("The downloaded ffmpeg does not run on this PC; nothing was installed.")
        core.ensure_dir(target)
        for f in staging.iterdir():
            shutil.copy2(f, target / f.name)
    version = r.stdout.splitlines()[0] if r.stdout else "ffmpeg"
    return f"Installed {version.split(' Copyright')[0]} to {target}"
