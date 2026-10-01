#!/usr/bin/env bash
# pyclean-audio — builds dist/pyclean-audio-<version>-x86_64.AppImage, the Linux
# "one file, double-click it" deliverable (see packaging/README.md).
#
#   packaging/build_runtime.sh && packaging/linux/make-appimage.sh
#   VERSION=1.1.0 packaging/linux/make-appimage.sh
#   APPIMAGETOOL=/path/to/appimagetool packaging/linux/make-appimage.sh
#
# One self-contained file: the runtime tree plus an AppRun, a .desktop entry and
# the icon, packed by appimagetool. Nothing is installed: the user downloads the
# file, makes it executable, double-clicks it.
#
# appimagetool is downloaded, **pinned by version and sha256**, exactly like
# ffmpeg: an unverified tool must never end up inside a deliverable.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST="$ROOT/dist"
SRC="$DIST/pyclean-audio"
HERE="$ROOT/packaging/linux"
STAGE="$DIST/.appdir"

VERSION="${VERSION:-1.0.0}"
APPIMAGE="$DIST/pyclean-audio-${VERSION}-x86_64.AppImage"
# appimagetool 1.9.1 (a tagged release, not the rolling "continuous" asset).
APPIMAGETOOL="${APPIMAGETOOL:-https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage}"
APPIMAGETOOL_SHA256="${APPIMAGETOOL_SHA256:-ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0}"

say() { printf '\n=== %s\n' "$*"; }
die() { printf '\nBUILD FAILED: %s\n' "$*" >&2; exit 1; }

[ -d "$SRC/runtime/python" ] || die "$SRC is missing — run packaging/build_runtime.sh first"
[ -x "$SRC/pyclean-audio" ] || die "$SRC/pyclean-audio is missing or not executable"
command -v mksquashfs >/dev/null || die "mksquashfs is required (squashfs-tools)"
[ -f "$HERE/pyclean-audio.svg" ] || die "$HERE/pyclean-audio.svg is missing"

# appimagetool refuses to run as root (FUSE mounts are per-user, and squashing a
# 2.6 GB tree as root would leave a root-owned artefact behind). Say so instead
# of failing later with an obscure mount error.
if [ "$(id -u)" = "0" ]; then
  die "appimagetool cannot run as root. Build as a normal user (or in a non-root container, like the docker branch's Dockerfile), then copy the .AppImage out."
fi

# --- 1. the tool, pinned and verified ----------------------------------------
DL_TMP=""
cleanup() {
  [ -n "$DL_TMP" ] && rm -rf "$DL_TMP"
  rm -rf "$STAGE"
}
trap cleanup EXIT

if [ -x "$APPIMAGETOOL" ]; then
  TOOL="$APPIMAGETOOL"                      # a path given by the caller
elif [ -f "$APPIMAGETOOL" ]; then
  DL_TMP="$(mktemp -d)"
  cp "$APPIMAGETOOL" "$DL_TMP/appimagetool"
  TOOL="$DL_TMP/appimagetool"
else
  DL_TMP="$(mktemp -d)"
  say "appimagetool (pinned, sha256 verified)"
  curl -fsSL "$APPIMAGETOOL" -o "$DL_TMP/appimagetool" || die "cannot download $APPIMAGETOOL"
  if command -v sha256sum >/dev/null; then
    got="$(sha256sum "$DL_TMP/appimagetool" | cut -d" " -f1)"
  elif command -v shasum >/dev/null; then
    got="$(shasum -a 256 "$DL_TMP/appimagetool" | cut -d" " -f1)"
  elif command -v openssl >/dev/null; then
    got="$(openssl dgst -sha256 "$DL_TMP/appimagetool" | awk '{print $NF}')"
  else
    die "no sha256 tool (sha256sum, shasum, openssl) to verify appimagetool: install one, or pass APPIMAGETOOL=/path/to/a/tool/you/trust"
  fi
  [ "$got" = "$APPIMAGETOOL_SHA256" ] || die "appimagetool digest mismatch for $APPIMAGETOOL: got $got, expected $APPIMAGETOOL_SHA256. Fix the pin, or pass APPIMAGETOOL_SHA256=<digest> for another URL."
  TOOL="$DL_TMP/appimagetool"
fi
chmod +x "$TOOL"
"$TOOL" --version >/dev/null 2>&1 || die "$TOOL does not run"

# --- 2. the AppDir: the runtime tree + the three files an AppImage needs ------
say "AppDir"
chmod -R u+w "$STAGE" 2>/dev/null || true
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -a "$SRC" "$STAGE/pyclean-audio"

# AppRun: resolve our own directory (readlink -f, so the AppImage's own mount
# point and any symlink are followed) and delegate to the wrapper that already
# exists in the tree — its symlink loop is not duplicated here.
cat >"$STAGE/AppRun" <<'APPRUN'
#!/bin/sh
# pyclean-audio AppImage: start the bundled app and open its window.
# The real launcher is the tree's own `pyclean-audio` wrapper (it resolves
# symlinks, checks the interpreter and logs to the user's state directory).
here="$(CDPATH= cd -- "$(dirname -- "$(readlink -f -- "$0")")" && pwd -P)"
exec "$here/pyclean-audio/pyclean-audio" "$@"
APPRUN
chmod 0755 "$STAGE/AppRun"

# The icon. The SVG is the source of truth and appimagetool 1.9.1 embeds it as
# such; a rasteriser, when there is one, also produces the PNG that older
# appimagetool builds and some desktops expect. Neither is optional: the check
# after the build refuses an AppImage that carries no icon at all (appimagetool
# would only print a warning and carry on).
cp "$HERE/pyclean-audio.svg" "$STAGE/pyclean-audio.svg"
if [ -f "$HERE/pyclean-audio.png" ]; then
  cp "$HERE/pyclean-audio.png" "$STAGE/pyclean-audio.png"
  say "icon: the committed PNG"
elif command -v rsvg-convert >/dev/null; then
  rsvg-convert -w 256 -h 256 "$HERE/pyclean-audio.svg" -o "$STAGE/pyclean-audio.png"
  say "icon: SVG + rsvg-convert PNG"
elif command -v convert >/dev/null; then
  convert -background none "$HERE/pyclean-audio.svg" -resize 256x256 "$STAGE/pyclean-audio.png"
  say "icon: SVG + ImageMagick PNG"
else
  say "icon: the SVG alone (no rsvg-convert/convert here; appimagetool embeds SVG)"
fi

cat >"$STAGE/pyclean-audio.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Version=1.0
Name=pyclean-audio
GenericName=Audio restoration
Comment=Restore the audio of your recordings, locally
Exec=AppRun %F
TryExec=AppRun
Terminal=false
Categories=AudioVideo;Audio;AudioVideoEditing;
Keywords=audio;noise;restoration;denoise;mp3;wav;video;
MimeType=audio/x-wav;audio/mpeg;audio/flac;audio/x-flac;audio/ogg;audio/mp4;audio/x-m4a;video/mp4;video/x-matroska;video/webm;video/quicktime;
Icon=pyclean-audio
X-AppImage-Version=$VERSION
DESKTOP

# --- 3. pack it --------------------------------------------------------------
say "appimagetool"
rm -f "$APPIMAGE"
export ARCH="${ARCH:-x86_64}"
# No .desktop validation dependency: the entry above is hand-written and stable.
APPIMAGE_EXTRACT_AND_RUN=1 "$TOOL" --no-appstream "$STAGE" "$APPIMAGE" \
  || die "appimagetool failed"
[ -f "$APPIMAGE" ] || die "appimagetool produced nothing"

# An AppImage that does not mount is the one failure nobody notices until a user
# tries it, so check what we can without FUSE: the type, its own runtime magic,
# and that the launcher is inside.
say "checks"
file "$APPIMAGE" | grep -q "ELF" || die "the produced file is not an ELF AppImage: $APPIMAGE"
# The embedded runtime must answer, or the file is not runnable at all.
"$APPIMAGE" --appimage-offset >/dev/null 2>&1 \
  || die "the AppImage runtime does not answer (--appimage-offset): $APPIMAGE"
# A tree of 1.5 GB cannot squash below a few hundred MB; an empty or failed
# build leaves a ~15 MB file (the AppImage runtime alone).
SIZE_KB="$(du -k "$APPIMAGE" | cut -f1)"
[ "$SIZE_KB" -gt 102400 ] || die "the AppImage is only $SIZE_KB KB: the payload is missing"

# What the AppDir must contain, checked before packing — deterministic and free.
for want in AppRun pyclean-audio.desktop pyclean-audio.svg \
            pyclean-audio/pyclean-audio pyclean-audio/runtime/python; do
  [ -e "$STAGE/$want" ] || die "the AppDir has no $want"
done

# And what ended up inside: the squashfs starts at an offset, and a zstd payload
# is unreadable by an old unsquashfs, so both are handled explicitly.
if command -v unsquashfs >/dev/null; then
  OFFSET="$("$APPIMAGE" --appimage-offset 2>/dev/null || echo 0)"
  if ! listing="$(unsquashfs -o "$OFFSET" -l "$APPIMAGE" 2>/dev/null)"; then
    listing="$(unsquashfs -l "$APPIMAGE" 2>/dev/null || true)"
  fi
  if [ -n "$listing" ]; then
    for want in "pyclean-audio/pyclean-audio" "pyclean-audio/runtime/python" \
                "AppRun" "pyclean-audio.desktop" "pyclean-audio.svg"; do
      case "$listing" in
        *"$want"*) ;;
        *) die "the AppImage does not contain $want (an iconless AppImage shows up as a blank app-menu entry, so it is refused)" ;;
      esac
    done
  else
    echo "WARNING: this unsquashfs cannot read the archive (zstd?); the contents" >&2
    echo "WARNING: were NOT listed. The AppDir checks above did pass." >&2
  fi
else
  echo "WARNING: no unsquashfs here, the contents were not listed" >&2
fi

printf '\ndone — dist/%s (%s)\n' "$(basename "$APPIMAGE")" "$(du -h "$APPIMAGE" | cut -f1)"
echo "run:   chmod +x $(basename "$APPIMAGE") && ./$(basename "$APPIMAGE")"
echo "FUSE:  on a distribution without libfuse2, use --appimage-extract-and-run"
echo "       or APPIMAGE_EXTRACT_AND_RUN=1 (it extracts to \$TMPDIR and runs it)"