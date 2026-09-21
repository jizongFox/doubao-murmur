#!/bin/sh
set -eu

DEBIAN_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
LINUX_DIR=$(dirname "$DEBIAN_DIR")
REPO_DIR=$(dirname "$LINUX_DIR")
OUT_DIR=${1:-"$LINUX_DIR/dist"}

if ! command -v dpkg-deb >/dev/null 2>&1; then
    echo "dpkg-deb is required (sudo apt install dpkg-dev)" >&2
    exit 1
fi

APP_VERSION=$(sed -n 's/^version = "\([^"]*\)"/\1/p' "$LINUX_DIR/pyproject.toml" | head -n 1)
DEB_VERSION=${DEB_VERSION:-"${APP_VERSION}+wayland3"}
PACKAGE_ROOT=$(mktemp -d)
trap 'rm -rf "$PACKAGE_ROOT"' EXIT HUP INT TERM
chmod 0755 "$PACKAGE_ROOT"

install -d \
    "$PACKAGE_ROOT/DEBIAN" \
    "$PACKAGE_ROOT/usr/bin" \
    "$PACKAGE_ROOT/usr/lib/doubao-murmur" \
    "$PACKAGE_ROOT/usr/share/applications" \
    "$PACKAGE_ROOT/usr/share/doc/doubao-murmur" \
    "$PACKAGE_ROOT/usr/share/icons/hicolor/scalable/apps" \
    "$PACKAGE_ROOT/usr/share/metainfo"

cp -a "$LINUX_DIR/src/doubao_murmur" "$PACKAGE_ROOT/usr/lib/doubao-murmur/"
find "$PACKAGE_ROOT/usr/lib/doubao-murmur" \
    \( -name __pycache__ -o -name '*.pyc' \) -exec rm -rf {} +

install -m 0755 "$DEBIAN_DIR/doubao-murmur" "$PACKAGE_ROOT/usr/bin/doubao-murmur"
install -m 0644 "$LINUX_DIR/flatpak/com.doubao.Murmur.desktop" \
    "$PACKAGE_ROOT/usr/share/applications/com.doubao.Murmur.desktop"
install -m 0644 "$LINUX_DIR/flatpak/com.doubao.Murmur.svg" \
    "$PACKAGE_ROOT/usr/share/icons/hicolor/scalable/apps/com.doubao.Murmur.svg"
install -m 0644 "$LINUX_DIR/flatpak/com.doubao.Murmur.metainfo.xml" \
    "$PACKAGE_ROOT/usr/share/metainfo/com.doubao.Murmur.metainfo.xml"
install -m 0644 "$REPO_DIR/LICENSE" "$PACKAGE_ROOT/usr/share/doc/doubao-murmur/copyright"

INSTALLED_SIZE=$(du -sk "$PACKAGE_ROOT/usr" | cut -f1)
cat > "$PACKAGE_ROOT/DEBIAN/control" <<EOF
Package: doubao-murmur
Version: $DEB_VERSION
Architecture: all
Maintainer: Doubao Murmur contributors <noreply@github.com>
Installed-Size: $INSTALLED_SIZE
Section: utils
Priority: optional
Homepage: https://github.com/jizongFox/doubao-murmur
Depends: python3 (>= 3.11), python3-gi, gir1.2-gtk-4.0, gir1.2-webkit-6.0, python3-websockets, python3-sounddevice, python3-xlib, wl-clipboard, ydotool
Recommends: xclip, xdotool, x11-utils
Description: Voice-to-text input using Doubao ASR
 Native Linux client with a global hotkey, recording overlay, clipboard copy,
 and automatic paste support for Wayland and X11 desktops.
EOF

mkdir -p "$OUT_DIR"
OUTPUT="$OUT_DIR/doubao-murmur_${DEB_VERSION}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$PACKAGE_ROOT" "$OUTPUT"
echo "$OUTPUT"
