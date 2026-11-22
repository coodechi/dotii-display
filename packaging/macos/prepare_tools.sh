#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DOWNLOADS="$ROOT/.codex/macos-tools-downloads"
BUILD="$ROOT/.codex/macos-tools-build"
OUTPUT="$ROOT/.codex/macos-tools"
FFMPEG_VERSION="8.1.2"
FFMPEG_SHA256="464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c"
FFMPEG_ARCHIVE="ffmpeg-$FFMPEG_VERSION.tar.xz"

[[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]] || {
  echo "This script requires Apple Silicon macOS" >&2
  exit 1
}

mkdir -p "$DOWNLOADS"
rm -rf "$BUILD" "$OUTPUT"
mkdir -p "$BUILD" "$OUTPUT/licenses"

curl --fail --location --retry 3 \
  "https://ffmpeg.org/releases/$FFMPEG_ARCHIVE" \
  --output "$DOWNLOADS/$FFMPEG_ARCHIVE"
FFMPEG_ACTUAL="$(shasum -a 256 "$DOWNLOADS/$FFMPEG_ARCHIVE" | awk '{print $1}')"
[[ "$FFMPEG_ACTUAL" == "$FFMPEG_SHA256" ]] || {
  echo "FFmpeg SHA-256 verification failed" >&2
  exit 1
}
tar -xJf "$DOWNLOADS/$FFMPEG_ARCHIVE" -C "$BUILD"
FFMPEG_SOURCE="$BUILD/ffmpeg-$FFMPEG_VERSION"
FFMPEG_PREFIX="$BUILD/ffmpeg-install"
(
  cd "$FFMPEG_SOURCE"
  MACOSX_DEPLOYMENT_TARGET=13.0 ./configure \
    --prefix=/ \
    --arch=arm64 \
    --cc=clang \
    --disable-debug \
    --disable-doc \
    --disable-ffplay \
    --disable-ffprobe \
    --disable-gpl \
    --disable-nonfree \
    --disable-shared \
    --enable-static \
    --enable-ffmpeg \
    --enable-securetransport \
    --extra-cflags="-mmacosx-version-min=13.0" \
    --extra-ldflags="-mmacosx-version-min=13.0"
  make -j"$(sysctl -n hw.logicalcpu)"
  make DESTDIR="$FFMPEG_PREFIX" install
)
mkdir -p "$OUTPUT/ffmpeg/bin"
/bin/cp "$FFMPEG_PREFIX/bin/ffmpeg" "$OUTPUT/ffmpeg/bin/ffmpeg"
/bin/cp "$FFMPEG_SOURCE/COPYING.LGPLv2.1" "$OUTPUT/licenses/FFmpeg-COPYING.LGPLv2.1"

[[ "$(lipo -archs "$OUTPUT/ffmpeg/bin/ffmpeg")" == "arm64" ]]
"$OUTPUT/ffmpeg/bin/ffmpeg" -hide_banner -version | grep -F "ffmpeg version $FFMPEG_VERSION"
"$OUTPUT/ffmpeg/bin/ffmpeg" -hide_banner -protocols | grep -E '^  tls$'
"$OUTPUT/ffmpeg/bin/ffmpeg" -hide_banner -demuxers | grep -E '^ D +rtsp '
"$OUTPUT/ffmpeg/bin/ffmpeg" -hide_banner -decoders | grep -E '^ V.* h264 +'
"$OUTPUT/ffmpeg/bin/ffmpeg" -hide_banner -encoders | grep -E '^ V[^ ]* mjpeg +'
for filter in fps scale crop; do
  "$OUTPUT/ffmpeg/bin/ffmpeg" -hide_banner -filters | grep -E "^ .. ${filter} +"
done

echo "$OUTPUT"
