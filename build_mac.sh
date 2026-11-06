#!/bin/sh
# Сборка PDF Editor.app и .dmg.
#
#   ./build_mac.sh                  — под архитектуру этого Mac, macOS 12+
#   ./build_mac.sh arm64            — Apple Silicon (M1–M4…), macOS 12+
#   ./build_mac.sh x86_64           — Intel (на Apple Silicon собирается через Rosetta), macOS 12+
#   ./build_mac.sh arm64 11         — то же для macOS 11 Big Sur (Qt 6.7 вместо 6.8)
#   ./build_mac.sh x86_64 11
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

MACOS="${2:-12}"
case "$MACOS" in
  12) REQS=requirements-build.txt;         SUFFIX=$ARCH ;;
  11) REQS=requirements-build-macos11.txt; SUFFIX=$ARCH-macos11; LABEL=$LABEL-macOS11 ;;
  *) echo "Поддерживаются сборки для macOS 12 (по умолчанию) и 11"; exit 1 ;;
esac
export PDFEDITOR_MIN_MACOS="$MACOS.0"   # читается в pdfeditor.spec

UV="${UV:-uv}"
command -v "$UV" >/dev/null || { echo "Не найден uv. Установи: brew install uv"; exit 1; }

VENV="build/venv-$SUFFIX"
if [ ! -x "$VENV/bin/python" ]; then
  "$UV" python install "$PY"
  "$UV" venv "$VENV" --python "$PY"
fi
"$UV" pip install -q --only-binary :all: --python "$VENV" -r "$REQS"

[ -f assets/icon.icns ] || "$VENV/bin/python" tools/make_icon.py

DIST="dist/$SUFFIX"
arch "-$ARCH" "$VENV/bin/python" -m PyInstaller --noconfirm --clean \
  --distpath "$DIST" --workpath "build/work-$SUFFIX" pdfeditor.spec

APP="$DIST/PDF Editor.app"
codesign --force --deep --sign - "$APP"   # ad-hoc подпись, без неё arm64 не запустится

# Ни одна библиотека внутри не должна требовать macOS новее заявленной
NEWEST=$(find "$APP" -type f \( -perm +111 -o -name '*.so' -o -name '*.dylib' \) \
  -exec sh -c 'otool -l "$1" 2>/dev/null | grep -m1 minos' _ {} \; | awk '{print $2}' | sort -V | tail -1)
if [ "$(printf '%s\n%s\n' "$NEWEST" "$PDFEDITOR_MIN_MACOS" | sort -V | tail -1)" != "$PDFEDITOR_MIN_MACOS" ]; then
  echo "Ошибка: внутри есть библиотеки для macOS $NEWEST, а цель — $PDFEDITOR_MIN_MACOS"; exit 1
fi

VERSION=$(/usr/libexec/PlistBuddy -c "Print CFBundleShortVersionString" "$APP/Contents/Info.plist")
DMG="dist/PDF-Editor-$VERSION-$LABEL.dmg"
STAGE="build/dmg-$SUFFIX"
rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -quiet -volname "PDF Editor" -srcfolder "$STAGE" -fs HFS+ -format UDZO "$DMG"
rm -rf "$STAGE"

echo
echo "Готово (macOS $PDFEDITOR_MIN_MACOS+, самая новая библиотека требует $NEWEST):"
echo "  $APP ($(du -sh "$APP" | cut -f1))"
echo "  $DMG ($(du -h "$DMG" | cut -f1))"
