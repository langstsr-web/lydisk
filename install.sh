#!/usr/bin/env bash
# Регистрирует LYDisk в меню приложений текущего пользователя.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"

sed -e "s|^Exec=.*|Exec=python3 \"$DIR/lydisk.py\"|" \
    -e "s|^Icon=.*|Icon=$DIR/icons/lydisk.svg|" \
    "$DIR/lydisk.desktop" > "$APPS/lydisk.desktop"

update-desktop-database "$APPS" 2>/dev/null || true
echo "Готово: LYDisk добавлен в меню приложений."
echo "Запуск из терминала: python3 \"$DIR/lydisk.py\""
