#!/usr/bin/env bash
# Builds ffmpeg + ffprobe for Android (arm64) from the official source release, for the APK.
# Runs in GitHub Actions (Ubuntu, Android NDK preinstalled):  scripts/build_android_ffmpeg.sh <output dir>
#
# - Source: ffmpeg.org release tarball, checked against a pinned sha256 (the tarball's GPG signature was verified
#   against the FFmpeg release signing key FCF9 86EA 15E6 E293 A564 4F10 B432 2F04 D676 58D8 when it was pinned).
# - Only ffmpeg's own (native) codecs and formats, no external libraries: all decoders, demuxers and muxers, and
#   just the encoders the app uses (FFV1 video re-encode, FLAC audio re-encode, PNG frame grabs for video duplicate
#   detection, raw/PCM for verification hashes). LGPL; nothing GPL is enabled.
# - Output: <output dir>/ffmpeg and <output dir>/ffprobe (position-independent Android executables).
set -euo pipefail

VERSION=8.0.1
SHA256=05ee0b03119b45c0bdb4df654b96802e909e0a752f72e4fe3794f487229e5a41
API=24                      # Android 7.0, the minimum Flet supports
OUT=$(realpath -m "${1:?usage: build_android_ffmpeg.sh <output dir>}")

NDK=${ANDROID_NDK_LATEST_HOME:-${ANDROID_NDK_HOME:-}}
[ -d "$NDK" ] || { echo "Android NDK not found (ANDROID_NDK_LATEST_HOME / ANDROID_NDK_HOME)"; exit 1; }
TC=$NDK/toolchains/llvm/prebuilt/linux-x86_64

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl -fsSL --retry 3 -o "$work/ffmpeg.tar.xz" "https://ffmpeg.org/releases/ffmpeg-$VERSION.tar.xz"
echo "$SHA256  $work/ffmpeg.tar.xz" | sha256sum -c -
tar -xf "$work/ffmpeg.tar.xz" -C "$work"
cd "$work/ffmpeg-$VERSION"

./configure \
  --target-os=android --arch=aarch64 --cpu=armv8-a --enable-cross-compile \
  --cc="$TC/bin/aarch64-linux-android$API-clang" --cxx="$TC/bin/aarch64-linux-android$API-clang++" \
  --ar="$TC/bin/llvm-ar" --nm="$TC/bin/llvm-nm" --ranlib="$TC/bin/llvm-ranlib" --strip="$TC/bin/llvm-strip" \
  --sysroot="$TC/sysroot" \
  --enable-static --disable-shared --enable-pic \
  --disable-doc --disable-debug --disable-ffplay \
  --disable-network --disable-autodetect --disable-devices --disable-hwaccels --disable-indevs --disable-outdevs \
  --enable-zlib \
  --disable-encoders \
  --enable-encoder=ffv1,flac,png,rawvideo,wrapped_avframe,pcm_s16le,pcm_s24le,pcm_s32le,pcm_f32le,pcm_f64le \
  --extra-ldflags="-Wl,-z,max-page-size=16384"     # 16 KB pages: required by newer Android devices

make -j"$(nproc)" ffmpeg ffprobe
mkdir -p "$OUT"
"$TC/bin/llvm-strip" -o "$OUT/ffmpeg" ffmpeg
"$TC/bin/llvm-strip" -o "$OUT/ffprobe" ffprobe
file "$OUT/ffmpeg" "$OUT/ffprobe"
ls -la "$OUT"
