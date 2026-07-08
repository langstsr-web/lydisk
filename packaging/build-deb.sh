#!/usr/bin/env bash
# Сборка deb-пакета LYDisk.
# Результат: build/lydisk_<версия>_all.deb
#
# Пакет ставит GUI в /usr/share/lydisk (+ команда lydisk и ярлык в меню)
# и добавляет репозиторий Яндекса, чтобы консольный клиент ставился так:
#   sudo apt update && sudo apt install yandex-disk
set -euo pipefail

VERSION="1.1.0"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$DIR/build/lydisk_${VERSION}_all"

rm -rf "$DIR/build"
mkdir -p \
    "$BUILD/DEBIAN" \
    "$BUILD/usr/share/lydisk/icons" \
    "$BUILD/usr/bin" \
    "$BUILD/usr/share/applications" \
    "$BUILD/usr/share/icons/hicolor/scalable/apps" \
    "$BUILD/usr/share/keyrings" \
    "$BUILD/etc/apt/sources.list.d" \
    "$BUILD/usr/share/doc/lydisk"

# --- файлы приложения
install -m 644 "$DIR/lydisk.py" "$BUILD/usr/share/lydisk/lydisk.py"
install -m 644 "$DIR/icons/lydisk.svg" "$BUILD/usr/share/lydisk/icons/lydisk.svg"
install -m 644 "$DIR/icons/lydisk.svg" \
    "$BUILD/usr/share/icons/hicolor/scalable/apps/lydisk.svg"
install -m 644 "$DIR/README.md" "$BUILD/usr/share/doc/lydisk/README.md"

cat > "$BUILD/usr/bin/lydisk" <<'EOF'
#!/bin/sh
exec python3 /usr/share/lydisk/lydisk.py "$@"
EOF
chmod 755 "$BUILD/usr/bin/lydisk"

cat > "$BUILD/usr/share/applications/lydisk.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=LYDisk
GenericName=Яндекс Диск
Comment=Графическая оболочка для консольного клиента Яндекс Диска
Exec=lydisk
Icon=lydisk
Terminal=false
Categories=Network;FileTransfer;Qt;
Keywords=yandex;disk;диск;яндекс;синхронизация;
EOF

# --- репозиторий Яндекса (ключ + список источников)
if [ -f /usr/share/keyrings/yandex-disk.gpg ]; then
    cp /usr/share/keyrings/yandex-disk.gpg "$BUILD/usr/share/keyrings/"
else
    wget -qO- https://repo.yandex.ru/yandex-disk/YANDEX-DISK-KEY.GPG \
        | gpg --dearmor > "$BUILD/usr/share/keyrings/yandex-disk.gpg"
fi
chmod 644 "$BUILD/usr/share/keyrings/yandex-disk.gpg"

echo "deb [signed-by=/usr/share/keyrings/yandex-disk.gpg] http://repo.yandex.ru/yandex-disk/deb/ stable main" \
    > "$BUILD/etc/apt/sources.list.d/yandex-disk.list"

# --- метаданные пакета
cat > "$BUILD/DEBIAN/control" <<EOF
Package: lydisk
Version: $VERSION
Section: net
Priority: optional
Architecture: all
Depends: python3 (>= 3.10), python3-pyqt6
Recommends: yandex-disk
Maintainer: Stanislav <gronowesuzanne@mail.com>
Description: GUI for the Yandex Disk console client
 LYDisk is a PyQt6 wrapper around the official yandex-disk
 console client: tray icon with sync status, quota, selective
 sync, public links, Dolphin integration, notifications.
 .
 The package also installs the Yandex apt repository so the
 console client can be installed with:
 apt update && apt install yandex-disk
EOF

echo "/etc/apt/sources.list.d/yandex-disk.list" > "$BUILD/DEBIAN/conffiles"

dpkg-deb --build --root-owner-group "$BUILD" \
    "$DIR/build/lydisk_${VERSION}_all.deb"
echo
echo "Готово: $DIR/build/lydisk_${VERSION}_all.deb"
echo "Установка: sudo apt install ./build/lydisk_${VERSION}_all.deb"
echo "Затем клиент: sudo apt update && sudo apt install yandex-disk"
