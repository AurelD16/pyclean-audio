#!/usr/bin/env bash
# pyclean-audio — builds dist/pyclean-audio_<version>_amd64.deb from the runtime
# tree produced by packaging/build_runtime.sh (see packaging/README.md).
#
#   packaging/build_runtime.sh && packaging/linux/make-deb.sh
#   sudo dpkg -i dist/pyclean-audio_1.0.0_amd64.deb     # install
#
# The payload goes to /opt/pyclean-audio (a self-contained tree, like the
# Windows one), /usr/bin/pyclean-audio is a symlink to it and the .desktop entry
# starts it with Terminal=false: double-click in the file manager, or the
# software centre, and the page opens. No terminal, no Python to install.
#
# How the page opens depends on how the tree was built (packaging/README.md):
# dist/WEBVIEW = 1 -> an embedded window (pywebview), and the .deb then depends
# on the GTK/WebKit2GTK runtime; 0 -> the default browser, where the page's Quit
# button stops the app. The description below never promises what is not there.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST="$ROOT/dist"
SRC="$DIST/pyclean-audio"
VERSION="${VERSION:-1.0.0}"
PKG="pyclean-audio_${VERSION}_amd64.deb"
STAGE="$DIST/.deb"

# What the runtime tree was built with (written by build_runtime.sh).
[ -f "$DIST/WEBVIEW" ] || die "dist/WEBVIEW is missing — run packaging/build_runtime.sh first"
WEBVIEW="$(tr -d '[:space:]' <"$DIST/WEBVIEW")"

die() { printf '\nBUILD FAILED: %s\n' "$*" >&2; exit 1; }

[ -d "$SRC/runtime/python" ] || die "$SRC is missing — run packaging/build_runtime.sh first"
[ -x "$SRC/pyclean-audio" ] || die "$SRC/pyclean-audio is missing or not executable"
command -v dpkg-deb >/dev/null || die "dpkg-deb is required (Debian, Ubuntu)"
[ -f "$SRC/THIRD-PARTY-NOTICES.txt" ] || die "THIRD-PARTY-NOTICES.txt must ship with the payload"

chmod -R u+w "$STAGE" 2>/dev/null || true   # a previous run made it read-only
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" "$STAGE/opt" "$STAGE/usr/bin" "$STAGE/usr/share/applications"

# The tree is 2.6 GB of files: copy the directory, then fix its permissions.
cp -a "$SRC" "$STAGE/opt/pyclean-audio"
# __pycache__ is never shipped (build_runtime.sh strips it, this is belt and
# braces against a stale tree).
find "$STAGE/opt/pyclean-audio" -type d -name "__pycache__" -exec rm -rf {} +
chmod 0755 "$STAGE/opt/pyclean-audio/pyclean-audio"
find "$STAGE/opt/pyclean-audio" -type d -exec chmod 0755 {} +
# read-only for everyone: results, logs and the model cache are written to
# $XDG_DATA_HOME/pyclean-audio, never here.
chmod -R a-w "$STAGE/opt/pyclean-audio"
ln -s ../../opt/pyclean-audio/pyclean-audio "$STAGE/usr/bin/pyclean-audio"

INSTALLED_KB="$(du -sk "$STAGE/opt/pyclean-audio" | cut -f1)"

# Only the embedded window needs the GTK/WebKit2GTK *runtime* on the user's
# machine; browser mode needs nothing (that is the point of the default).
if [ "$WEBVIEW" = "1" ]; then
  DEPENDS="libwebkit2gtk-4.1-0, gir1.2-gtk-3.0, libc6, libstdc++6, libgomp1"
  WINDOW_LINE="$(printf '%s\n %s' \
    "The page opens in the application's own window (WebKit2GTK, a dependency" \
    "above); closing it quits pyclean-audio.")"
else
  DEPENDS="libc6, libstdc++6, libgomp1"
  WINDOW_LINE="$(printf '%s\n %s' \
    "The local page opens in your default browser, where its Quit button stops" \
    "the application.")"
fi

cat >"$STAGE/DEBIAN/control" <<CONTROL
Package: pyclean-audio
Version: $VERSION
Section: sound
Priority: optional
Architecture: amd64
Maintainer: pyclean-audio <https://github.com/AurelD16/pyclean-audio>
Installed-Size: $INSTALLED_KB
Homepage: https://github.com/AurelD16/pyclean-audio
Depends: $DEPENDS
Description: Local audio restoration for audio and video files
 pyclean-audio extends the bandwidth of degraded recordings up to 48 kHz
 (LavaSR v2) and, on the command line version, transcribes the cleaned audio.
 Everything runs on this machine: no cloud, no account, no API key.
 .
 $WINDOW_LINE
 .
 The transcription model is not bundled (~2.4 GB): the checkbox is disabled
 and the API answers 400.
 .
 The LavaSR weights (~115 MB) are downloaded from HuggingFace on first launch
 and cached under \$XDG_DATA_HOME/pyclean-audio. Results are ephemeral: they
 live in the same place, are purged after 6 hours and emptied at the next start.
CONTROL

# Nothing privileged: the payload is already executable, and the desktop cache
# is refreshed only when desktop-file-utils is installed.
cat >"$STAGE/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database -q /usr/share/applications || true
fi
exit 0
POSTINST
chmod 0755 "$STAGE/DEBIAN/postinst"

# uninstalling keeps the user's results and the model cache: they are outside
# /opt/pyclean-audio, so there is nothing to remove here.
cat >"$STAGE/DEBIAN/postrm" <<'POSTRM'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database -q /usr/share/applications || true
fi
exit 0
POSTRM
chmod 0755 "$STAGE/DEBIAN/postrm"

cat >"$STAGE/usr/share/applications/pyclean-audio.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Version=1.0
Name=pyclean-audio
GenericName=Audio restoration
Comment=Restore the audio of your recordings, locally
Exec=pyclean-audio %F
TryExec=pyclean-audio
Terminal=false
Categories=AudioVideo;Audio;AudioVideoEditing;
Keywords=audio;noise;restoration;denoise;mp3;wav;video;
MimeType=audio/x-wav;audio/mpeg;audio/flac;audio/x-flac;audio/ogg;audio/mp4;audio/x-m4a;video/mp4;video/x-matroska;video/webm;video/quicktime;
Icon=audio-x-generic
DESKTOP
chmod 0644 "$STAGE/usr/share/applications/pyclean-audio.desktop"

dpkg-deb --build --root-owner-group "$STAGE" "$DIST/$PKG" || die "dpkg-deb --build failed"
dpkg-deb --info "$DIST/$PKG" >/dev/null || die "the produced .deb is unreadable"
# the payload was made read-only on purpose: give the bits back before cleaning.
chmod -R u+w "$STAGE"
rm -rf "$STAGE"

printf '\ndone — dist/%s (%s)\n' "$PKG" "$(du -h "$DIST/$PKG" | cut -f1)"
if [ "$WEBVIEW" = "1" ]; then
  echo "window: embedded (dist/WEBVIEW=1) — the .deb depends on WebKit2GTK"
else
  echo "window: browser mode (dist/WEBVIEW=0) — the page opens in the default browser"
fi
echo "install: sudo dpkg -i dist/$PKG"
echo "remove:  sudo dpkg -r pyclean-audio      (results and model cache are kept)"