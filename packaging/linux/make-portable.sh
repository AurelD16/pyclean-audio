#!/usr/bin/env bash
# pyclean-audio — builds a portable .tar.gz (extract, run) for distributions
# that are not Debian/Ubuntu: no package manager, no root, no .desktop entry.
#
#   packaging/build_runtime.sh && packaging/linux/make-portable.sh
#   tar -xzf dist/pyclean-audio-1.0.0-linux-x86_64.tar.gz
#   ./pyclean-audio/pyclean-audio
#
# The archive contains the same tree as the .deb (one directory,
# `pyclean-audio/`), so both are the same install with a different wrapper.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST="$ROOT/dist"
SRC="$DIST/pyclean-audio"
VERSION="${VERSION:-1.0.0}"
ARCH="${ARCH:-linux-x86_64}"
TARBALL="$DIST/pyclean-audio-${VERSION}-${ARCH}.tar.gz"

die() { printf '\nBUILD FAILED: %s\n' "$*" >&2; exit 1; }

[ -d "$SRC/runtime/python" ] || die "$SRC is missing — run packaging/build_runtime.sh first"
[ -x "$SRC/pyclean-audio" ] || die "$SRC/pyclean-audio is missing or not executable"
[ -f "$SRC/THIRD-PARTY-NOTICES.txt" ] || die "THIRD-PARTY-NOTICES.txt must ship with the payload"
command -v tar >/dev/null || die "tar is required"

tar --sort=name --owner=0 --group=0 --numeric-owner \
    -czf "$TARBALL" -C "$DIST" pyclean-audio || die "tar failed"
tar -tzf "$TARBALL" >/dev/null || die "the produced archive is unreadable"

printf '\ndone — dist/%s (%s)\n' "$(basename "$TARBALL")" "$(du -h "$TARBALL" | cut -f1)"
echo "install: tar -xzf $(basename "$TARBALL") && ./pyclean-audio/pyclean-audio"
echo "note:    the app writes to \$XDG_DATA_HOME/pyclean-audio, not to the extracted folder"