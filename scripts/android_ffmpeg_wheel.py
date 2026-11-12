#!/usr/bin/env python3
"""
Packs the Android ffmpeg/ffprobe (scripts/build_android_ffmpeg.sh) into a wheel the APK build installs, and
adds that wheel to pyproject.toml's Android dependencies. Run by GitHub Actions only; the edit to pyproject.toml
is never committed.

    python scripts/android_ffmpeg_wheel.py <dir with ffmpeg + ffprobe> <wheel output dir> [--add-to-pyproject]

Why a wheel, and why the files are called lib*.so: Android only lets an app run programs from its native-library
folder (code in the app's data folder may not be executed). Flet's packager copies every plain .so file found in
the installed Python packages into that folder, and with legacy packaging (pyproject.toml) Android extracts it to
disk, executable. The app then finds them there (media_core.find_tools).
"""
import base64, hashlib, sys, zipfile
from pathlib import Path

NAME, VERSION = "smp_android_ffmpeg", "8.0.1"
TAG = "py3-none-android_24_arm64_v8a"          # the platform Flet builds the APK's Python packages for
FILES = {"ffmpeg": "libsmp_ffmpeg.so", "ffprobe": "libsmp_ffprobe.so"}   # must match media_core.ANDROID_TOOL_FILES

def build_wheel(bin_dir: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    wheel = out_dir / f"{NAME}-{VERSION}-{TAG}.whl"
    dist = f"{NAME}-{VERSION}.dist-info"
    entries = {f"{NAME}/__init__.py": b'"""ffmpeg and ffprobe for Android, shipped as native libraries (see the app)."""\n'}
    for tool, lib in FILES.items():
        entries[f"{NAME}/{lib}"] = (bin_dir / tool).read_bytes()
    entries[f"{dist}/METADATA"] = (f"Metadata-Version: 2.1\nName: {NAME.replace('_', '-')}\nVersion: {VERSION}\n"
                                   "Summary: ffmpeg/ffprobe (LGPL build) for the Secure Media Processor APK\n").encode()
    entries[f"{dist}/WHEEL"] = (f"Wheel-Version: 1.0\nGenerator: android_ffmpeg_wheel.py\nRoot-Is-Purelib: false\n"
                                f"Tag: {TAG}\n").encode()
    record = []
    for path, data in entries.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        record.append(f"{path},sha256={digest},{len(data)}")
    record.append(f"{dist}/RECORD,,")
    entries[f"{dist}/RECORD"] = ("\n".join(record) + "\n").encode()
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, data in entries.items():
            info = zipfile.ZipInfo(path, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = (0o755 if path.endswith(".so") else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return wheel

def add_to_pyproject(pyproject: Path, wheel: Path):
    """Adds `dependencies = ["smp-android-ffmpeg @ file:///..."]` to the [tool.flet.android] table."""
    text = pyproject.read_text(encoding="utf-8")
    header = "[tool.flet.android]\n"
    if header not in text:
        raise SystemExit(f"{pyproject} has no {header.strip()} table")
    if "smp-android-ffmpeg" in text:
        return
    line = f'dependencies = ["smp-android-ffmpeg @ {wheel.resolve().as_uri()}"]\n'
    pyproject.write_text(text.replace(header, header + line, 1), encoding="utf-8")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    w = build_wheel(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"built {w} ({w.stat().st_size / 1e6:.1f} MB)")
    if "--add-to-pyproject" in sys.argv:
        add_to_pyproject(Path(__file__).resolve().parents[1] / "pyproject.toml", w)
        print("added to pyproject.toml [tool.flet.android] dependencies")
