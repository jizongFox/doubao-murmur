#!/bin/sh
set -eu

# Build the Ubuntu 22.04 package from the existing Debian payload, plus
# application-private pure Python wheels tested on Jammy.
[ "$#" -eq 2 ] || { echo "Usage: $0 WHEEL_DIR OUTPUT_DIR" >&2; exit 2; }
wheel_dir=$(CDPATH= cd -- "$1" && pwd)
mkdir -p "$2"
out_dir=$(CDPATH= cd -- "$2" && pwd)
debian_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
temp=$(mktemp -d)
trap 'rm -rf "$temp"' EXIT HUP INT TERM
export LC_ALL=C
export DEB_VERSION=1.5.2-rc6+jammy.2e83f32

(
  cd "$wheel_dir"
  sha256sum -c <<'SUMS'
c3534038d42e0df2f1392a1b30a15a4ff5fdc2b86cfa94f072bf11b10a164398  python_xlib-0.33-py2.py3-none-any.whl
de099612311ad81e55d31ccbd83f43ea6bf4d87b48f9b6ea55a1fbcde0eee4e0  sounddevice-0.5.6-py3-none-any.whl
6abbd3e82c731c8e531714466acd5d87b5e88ac3243465337ba71d68e23ae7e3  websockets-16.1.1-py3-none-any.whl
SUMS
)

sh "$debian_dir/build.sh" "$temp"
package="doubao-murmur_${DEB_VERSION}_all.deb"
dpkg-deb --raw-extract "$temp/$package" "$temp/package"
site="$temp/package/usr/lib/doubao-murmur"
for wheel in python_xlib-0.33-py2.py3-none-any.whl sounddevice-0.5.6-py3-none-any.whl websockets-16.1.1-py3-none-any.whl; do
  unzip -oq "$wheel_dir/$wheel" -d "$site"
done

# Keep dependency licenses with the wheel contents. Apt manages the C
# libraries and the Python bindings that are available on Ubuntu 22.04.
find "$site" -type d -exec chmod 0755 {} +
find "$site" -type f -exec chmod 0644 {} +
size=$(find "$temp/package/usr" -type f -printf '%s\n' | awk '{ n += $1 } END { print int((n + 1023) / 1024) }')
sed -i \
  -e "s/^Installed-Size: .*/Installed-Size: $size/" \
  -e 's/^Depends: .*/Depends: python3 (>= 3.10), python3-gi, python3-gi-cairo, gir1.2-gtk-4.0, gir1.2-webkit-6.0, python3-cffi, python3-six, libportaudio2, xclip, xdotool/' \
  -e 's/^Recommends: .*/Recommends: x11-utils\nSuggests: wl-clipboard, ydotool/' \
  "$temp/package/DEBIAN/control"

dpkg-deb --root-owner-group -Zxz --build "$temp/package" "$out_dir/$package"
