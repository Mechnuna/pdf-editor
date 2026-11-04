#!/bin/sh
# Сборка PDF Editor.app и .dmg для macOS 12+.
#
#   ./build_mac.sh            — под архитектуру этого Mac
#   ./build_mac.sh arm64      — Apple Silicon (M1–M4…)
#   ./build_mac.sh x86_64     — Intel (на Apple Silicon собирается через Rosetta)
#
# Нужен uv (brew install uv). Python берётся портативный (python-build-standalone),
# а не из Homebrew: тот собран под текущую macOS и не запустится на старых.
set -e
cd "$(dirname "$0")"

ARCH="${1:-$(uname -m)}"
case "$ARCH" in
  arm64)  PY=cpython-3.12-macos-aarch64-none; LABEL=AppleSilicon ;;
  x86_64) PY=cpython-3.12-macos-x86_64-none;  LABEL=Intel ;;
  *) echo "Неизвестная архитектура: $ARCH (нужно arm64 или x86_64)"; exit 1 ;;
esac
UV="${UV:-uv}"
command -v "$UV" >/dev/null || { echo "Не найден uv. Установи: brew install uv"; exit 1; }

VENV="build/venv-$ARCH"
if [ ! -x "$VENV/bin/python" ]; then
  "$UV" python install "$PY"
  "$UV" venv "$VENV" --python "$PY"
fi
"$UV" pip install -q --only-binary :all: --python "$VENV" -r requirements-build.txt

[ -f assets/icon.icns ] || "$VENV/bin/python" tools/make_icon.py

DIST="dist/$ARCH"
arch "-$ARCH" "$VENV/bin/python" -m PyInstaller --noconfirm --clean \
  --distpath "$DIST" --workpath "build/work-$ARCH" pdfeditor.spec

APP="$DIST/PDF Editor.app"
codesign --force --deep --sign - "$APP"   # ad-hoc подпись, без неё arm64 не запустится

VERSION=$(/usr/libexec/PlistBuddy -c "Print CFBundleShortVersionString" "$APP/Contents/Info.plist")
DMG="dist/PDF-Editor-$VERSION-$LABEL.dmg"
STAGE="build/dmg-$ARCH"
rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -quiet -volname "PDF Editor" -srcfolder "$STAGE" -fs HFS+ -format UDZO "$DMG"
rm -rf "$STAGE"

echo
echo "Готово:"
echo "  $APP ($(du -sh "$APP" | cut -f1))"
echo "  $DMG ($(du -h "$DMG" | cut -f1))"
