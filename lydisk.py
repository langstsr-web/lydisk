#!/usr/bin/env python3
"""LYDisk — графическая оболочка для консольного клиента Яндекс Диска (yandex-disk).

Показывает состояние синхронизации в системном трее и в окне,
позволяет запускать/останавливать демон, получать публичные ссылки,
смотреть квоту диска и последние синхронизированные файлы.

Зависимости: python3-pyqt6, установленный пакет yandex-disk.
Запуск:      python3 lydisk.py            — открыть окно
             python3 lydisk.py --hidden   — запустить свёрнутым в трей
"""

import configparser
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import (
    QDir,
    QLockFile,
    QObject,
    QProcess,
    QProcessEnvironment,
    QRunnable,
    QSettings,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import QAction, QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QSystemTrayIcon,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "LYDisk"
APP_DIR = Path(__file__).resolve().parent

# Переопределяются переменными окружения только для тестов.
CLI = os.environ.get("LYDISK_CLI", "yandex-disk")
CONFIG_PATH = Path(
    os.environ.get("LYDISK_CONFIG", Path.home() / ".config/yandex-disk/config.cfg")
)

DISK_WEB_URL = "https://disk.yandex.ru"
DISK_TRASH_URL = "https://disk.yandex.ru/client/trash"

_XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
LINKS_PATH = _XDG_CONFIG / "LYDisk" / "links.json"
SYSTEMD_UNIT_PATH = _XDG_CONFIG / "systemd/user/yandex-disk.service"
SPECTACLE_RC = _XDG_CONFIG / "spectaclerc"
SCREENSHOTS_DIR_NAME = "Скриншоты"
SERVICEMENU_PATH = (
    Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    / "kio/servicemenus/lydisk.desktop"
)

# Состояние демона -> (подпись, цвет индикатора)
STATES = {
    "idle": ("Синхронизировано", "#2f9e5f"),
    "busy": ("Синхронизация…", "#1a73e8"),
    "index": ("Индексация…", "#1a73e8"),
    "paused": ("Синхронизация приостановлена", "#8a8a8a"),
    "error": ("Ошибка синхронизации", "#d93025"),
    "no internet access": ("Нет соединения с интернетом", "#e8710a"),
    "stopped": ("Служба синхронизации не запущена", "#8a8a8a"),
    "unknown": ("Состояние неизвестно", "#8a8a8a"),
}

DEB_INSTALL_CMD = """\
sudo mkdir -p /usr/share/keyrings
wget -qO- https://repo.yandex.ru/yandex-disk/YANDEX-DISK-KEY.GPG \\
  | sudo gpg --dearmor -o /usr/share/keyrings/yandex-disk.gpg
echo "deb [signed-by=/usr/share/keyrings/yandex-disk.gpg] http://repo.yandex.ru/yandex-disk/deb/ stable main" \\
  | sudo tee /etc/apt/sources.list.d/yandex-disk.list
sudo apt update && sudo apt install -y yandex-disk"""

RPM_INSTALL_CMD = """\
sudo rpm --import https://repo.yandex.ru/yandex-disk/YANDEX-DISK-KEY.GPG
sudo tee /etc/yum.repos.d/yandex-disk.repo > /dev/null <<'EOF'
[yandex-disk]
name=Yandex Disk
baseurl=https://repo.yandex.ru/yandex-disk/rpm/stable/$basearch/
enabled=1
gpgcheck=1
gpgkey=https://repo.yandex.ru/yandex-disk/YANDEX-DISK-KEY.GPG
EOF
sudo dnf install -y yandex-disk"""

TERMINALS = [
    ("konsole", ["-e"]),
    ("gnome-terminal", ["--"]),
    ("xfce4-terminal", ["-x"]),
    ("kitty", ["-e"]),
    ("alacritty", ["-e"]),
    ("xterm", ["-e"]),
]


# ---------------------------------------------------------------- CLI wrapper

def cli_available() -> bool:
    return shutil.which(CLI) is not None


def run_cli(*args: str, timeout: int = 60) -> tuple[int, str]:
    """Запустить yandex-disk с англоязычной локалью (для стабильного разбора вывода)."""
    env = dict(os.environ, LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8")
    try:
        proc = subprocess.run(
            [CLI, *args], capture_output=True, text=True, timeout=timeout, env=env
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except FileNotFoundError:
        return 127, "Клиент yandex-disk не найден"
    except subprocess.TimeoutExpired:
        return 124, "Команда выполняется слишком долго"


def read_config() -> dict:
    cfg = {}
    try:
        for line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                cfg[key.strip()] = value.strip().strip('"')
    except OSError:
        pass
    return cfg


def is_configured() -> bool:
    if not CONFIG_PATH.exists():
        return False
    auth = read_config().get("auth", str(CONFIG_PATH.parent / "passwd"))
    return Path(auth).expanduser().exists()


def get_exclude_dirs() -> list[str]:
    raw = read_config().get("exclude-dirs", "")
    return [d.strip() for d in raw.split(",") if d.strip()]


def set_exclude_dirs(dirs: list[str]):
    """Переписать строку exclude-dirs в config.cfg, сохранив остальное содержимое."""
    value = ",".join(dirs)
    lines = CONFIG_PATH.read_text(encoding="utf-8").splitlines()
    out, replaced = [], False
    for line in lines:
        if line.startswith("exclude-dirs="):
            if value and not replaced:
                out.append(f"exclude-dirs={value}")
                replaced = True
            continue  # пустое значение — строку убираем совсем
        out.append(line)
    if value and not replaced:
        out.append(f"exclude-dirs={value}")
    CONFIG_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


def ensure_config():
    """Создать config.cfg при необходимости и гарантировать proxy=no.

    Клиент 0.1.6.1080 сегфолтится в libproxy при proxy=auto (умолчание),
    поэтому proxy=no обязателен до первого сетевого обращения клиента.
    """
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = (CONFIG_PATH.read_text(encoding="utf-8").splitlines()
             if CONFIG_PATH.exists() else [])
    if not any(l.startswith("auth=") for l in lines):
        lines.append(f"auth={CONFIG_PATH.parent / 'passwd'}")
    if not any(l.strip() == "proxy=no" for l in lines):
        lines = [l for l in lines if not l.startswith("proxy=")]
        lines.append("proxy=no")
    CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def set_config_value(key: str, value: str | None):
    lines = (CONFIG_PATH.read_text(encoding="utf-8").splitlines()
             if CONFIG_PATH.exists() else [])
    lines = [l for l in lines if not l.startswith(f"{key}=")]
    if value is not None:
        lines.append(f"{key}={value}")
    CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def daemon_start_args() -> list[str]:
    """Аргументы запуска демона с учётом настроек LYDisk."""
    settings = QSettings(APP_NAME, APP_NAME)
    args = ["start"]
    if settings.value("read_only", False, type=bool):
        args.append("--read-only")
        if settings.value("overwrite", False, type=bool):
            args.append("--overwrite")
    return args


def restart_daemon() -> tuple[int, str]:
    """Остановить демон, дождаться остановки и запустить заново."""
    run_cli("stop")
    for _ in range(20):
        _, out = run_cli("status", timeout=10)
        if "daemon not started" in out.lower():
            break
        time.sleep(0.5)
    return run_cli(*daemon_start_args())


# ---------------------------------------------------- systemd-автозапуск демона

def systemd_unit_text() -> str:
    cli = shutil.which(CLI) or CLI
    flags = " ".join(daemon_start_args()[1:])
    return (
        "[Unit]\n"
        "Description=Яндекс Диск — фоновая синхронизация (yandex-disk)\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={cli} start --no-daemon{(' ' + flags) if flags else ''}\n"
        f"ExecStop={cli} stop\n"
        "Restart=on-failure\n"
        "RestartSec=10\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def systemd_autostart_enabled() -> bool:
    return SYSTEMD_UNIT_PATH.exists()


def _systemctl(*args) -> bool:
    try:
        return subprocess.run(["systemctl", "--user", *args],
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def set_systemd_autostart(enabled: bool) -> bool:
    if enabled:
        SYSTEMD_UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SYSTEMD_UNIT_PATH.write_text(systemd_unit_text(), encoding="utf-8")
        _systemctl("daemon-reload")
        run_cli("stop")  # иначе юнит упрётся в «daemon already started»
        return _systemctl("enable", "--now", "yandex-disk.service")
    _systemctl("disable", "--now", "yandex-disk.service")
    SYSTEMD_UNIT_PATH.unlink(missing_ok=True)
    _systemctl("daemon-reload")
    return True


def refresh_systemd_unit():
    """Перегенерировать юнит после смены флагов запуска и перезапустить его."""
    if systemd_autostart_enabled():
        SYSTEMD_UNIT_PATH.write_text(systemd_unit_text(), encoding="utf-8")
        _systemctl("daemon-reload")
        _systemctl("restart", "yandex-disk.service")


# ------------------------------------------------------- реестр публичных ссылок

def load_links() -> list[dict]:
    try:
        return json.loads(LINKS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def add_link_record(path: str, url: str):
    links = [l for l in load_links() if l.get("path") != path]
    links.insert(0, {
        "path": path,
        "url": url,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    LINKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    LINKS_PATH.write_text(
        json.dumps(links[:200], ensure_ascii=False, indent=1), encoding="utf-8")


def remove_link_record(path: str):
    links = [l for l in load_links() if l.get("path") != path]
    LINKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    LINKS_PATH.write_text(
        json.dumps(links, ensure_ascii=False, indent=1), encoding="utf-8")


# ----------------------------------------------------------------- уведомления

def notification_for(prev: str | None, new: str) -> tuple[str, str] | None:
    """Уведомление при смене состояния демона (или None)."""
    if prev is None or prev == new:
        return None
    if new == "error":
        return ("Ошибка синхронизации",
                "Яндекс Диск сообщил об ошибке — откройте LYDisk.")
    if new == "no internet access":
        return ("Нет соединения",
                "Синхронизация приостановлена: нет доступа к интернету.")
    if new == "idle" and prev in ("busy", "index", "no internet access"):
        return ("Синхронизация завершена", "Все файлы синхронизированы.")
    return None


def quota_percent(st: dict) -> int | None:
    used, total = size_to_bytes(st["used"]), size_to_bytes(st["total"])
    if used is None or not total:
        return None
    return round(used * 100 / total)


def quota_warning_state(percent: int, threshold: int,
                        warned: bool) -> tuple[bool, bool]:
    """(показать предупреждение сейчас, новое значение «уже предупреждали»).

    Гистерезис в 5% — чтобы уведомление не сыпалось каждые 5 секунд
    и повторялось только после заметного освобождения места."""
    if percent >= threshold and not warned:
        return True, True
    if percent < threshold - 5:
        return False, False
    return False, warned


# -------------------------------------------------------- выбор браузера для ссылок

def application_dirs() -> list[Path]:
    """Все каталоги с .desktop-файлами: системные, Flatpak, Snap и т.д.

    Основной источник — XDG_DATA_DIRS (дистрибутив сам прописывает туда
    каталоги Snap и Flatpak); запасной список — на случай запуска
    в окружении с урезанными переменными (systemd, cron)."""
    data_dirs = [os.environ.get("XDG_DATA_HOME",
                                str(Path.home() / ".local/share"))]
    data_dirs += (os.environ.get("XDG_DATA_DIRS")
                  or "/usr/local/share:/usr/share").split(":")
    data_dirs += [
        str(Path.home() / ".local/share/flatpak/exports/share"),
        "/var/lib/flatpak/exports/share",
        "/var/lib/snapd/desktop",
        "/usr/local/share",
        "/usr/share",
    ]
    result, seen = [], set()
    for d in data_dirs:
        if d and d not in seen:
            seen.add(d)
            result.append(Path(d) / "applications")
    return result


def _read_desktop_entry(path: Path) -> dict:
    cp = configparser.RawConfigParser(strict=False)
    cp.optionxform = str
    try:
        cp.read(path, encoding="utf-8")
        return dict(cp["Desktop Entry"])
    except (OSError, KeyError, configparser.Error):
        return {}


def installed_browsers() -> list[tuple[str, str]]:
    """[(имя, путь к .desktop)] для всех приложений-браузеров, включая flatpak."""
    browsers, seen_ids = [], set()
    for apps_dir in application_dirs():
        if not apps_dir.is_dir():
            continue
        for desktop in sorted(apps_dir.glob("*.desktop")):
            if desktop.name in seen_ids:
                continue
            entry = _read_desktop_entry(desktop)
            is_browser = (
                "x-scheme-handler/https" in entry.get("MimeType", "")
                or "WebBrowser" in entry.get("Categories", "")
            )
            if (is_browser and entry.get("Exec")
                    and entry.get("NoDisplay", "").lower() != "true"):
                seen_ids.add(desktop.name)
                browsers.append((entry.get("Name", desktop.stem), str(desktop)))
    return sorted(browsers, key=lambda b: b[0].casefold())


def desktop_exec_args(exec_line: str, url: str) -> list[str]:
    """Подставить URL в Exec= (включая flatpak-обёртки с @@u … @@)."""
    args, replaced = [], False
    for part in shlex.split(exec_line):
        if part in ("%u", "%U", "%f", "%F"):
            args.append(url)
            replaced = True
        elif part in ("@@u", "@@", "%i", "%c", "%k"):
            continue
        else:
            args.append(part)
    if not replaced:
        args.append(url)
    return args


def open_web(url: str):
    """Открыть веб-ссылку в браузере, выбранном в настройках LYDisk."""
    desktop = QSettings(APP_NAME, APP_NAME).value("browser_desktop", "")
    if desktop and Path(desktop).exists():
        exec_line = _read_desktop_entry(Path(desktop)).get("Exec", "")
        if exec_line:
            try:
                subprocess.Popen(desktop_exec_args(exec_line, url),
                                 start_new_session=True)
                return
            except OSError:
                pass
    QDesktopServices.openUrl(QUrl(url))


# ----------------------------------------------------------- REST API Яндекс Диска

API_FILE = _XDG_CONFIG / "LYDisk" / "api.json"
OAUTH_BASE = "https://oauth.yandex.ru"
API_BASE = "https://cloud-api.yandex.net/v1/disk"


def load_api_auth() -> dict:
    try:
        return json.loads(API_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_api_auth(auth: dict):
    API_FILE.parent.mkdir(parents=True, exist_ok=True)
    API_FILE.write_text(json.dumps(auth, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    API_FILE.chmod(0o600)  # токен — секрет


def api_connected() -> bool:
    return bool(load_api_auth().get("access_token"))


def _http_json(url: str, method: str = "GET", data: dict | None = None,
               headers: dict | None = None, timeout: int = 20):
    """HTTP-запрос → (код ответа, разобранный JSON). Сетевые ошибки → (0, …)."""
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(text) if text.strip() else {})
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(text)
        except ValueError:
            return e.code, {"message": text[:200]}
    except (OSError, ValueError) as e:
        return 0, {"message": f"Сеть недоступна: {e}"}


def refresh_api_token() -> bool:
    auth = load_api_auth()
    status, data = _http_json(OAUTH_BASE + "/token", "POST", {
        "grant_type": "refresh_token",
        "refresh_token": auth.get("refresh_token", ""),
        "client_id": auth.get("client_id", ""),
        "client_secret": auth.get("client_secret", ""),
    })
    if status == 200 and data.get("access_token"):
        auth["access_token"] = data["access_token"]
        auth["refresh_token"] = data.get("refresh_token",
                                         auth.get("refresh_token", ""))
        save_api_auth(auth)
        return True
    return False


def api_call(method: str, path: str, params: dict | None = None,
             _retry: bool = True):
    """Запрос к REST API Диска с автообновлением протухшего токена."""
    auth = load_api_auth()
    if not auth.get("access_token"):
        return 401, {"message": "API не подключён"}
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    status, data = _http_json(
        url, method, headers={"Authorization": "OAuth " + auth["access_token"]})
    if status == 401 and _retry and auth.get("refresh_token"):
        if refresh_api_token():
            return api_call(method, path, params, _retry=False)
    return status, data


def api_list_public(max_items: int = 500):
    """Серверный список всех опубликованных ресурсов."""
    items, offset = [], 0
    while True:
        status, data = api_call("GET", "/resources/public",
                                {"limit": 100, "offset": offset})
        if status != 200:
            return False, data.get("message", f"Ошибка API (HTTP {status})")
        batch = data.get("items", [])
        items.extend(batch)
        if len(batch) < 100 or len(items) >= max_items:
            return True, items
        offset += 100


def api_unpublish(cloud_path: str):
    status, data = api_call("PUT", "/resources/unpublish",
                            {"path": cloud_path})
    return status in (200, 202), data.get("message", f"HTTP {status}")


def api_trash_list(limit: int = 200):
    for root in ("/", "trash:/"):  # обозначение корня меняется между версиями API
        status, data = api_call("GET", "/trash/resources", {
            "path": root, "limit": limit, "sort": "-deleted"})
        if status == 200:
            return True, data.get("_embedded", {}).get("items", [])
    return False, data.get("message", f"Ошибка API (HTTP {status})")


def api_trash_restore(trash_path: str):
    status, data = api_call("PUT", "/trash/resources/restore",
                            {"path": trash_path})
    return status in (200, 201, 202), data.get("message", f"HTTP {status}")


def api_trash_delete(trash_path: str | None = None):
    """Удалить элемент навсегда; без пути — очистить корзину целиком."""
    params = {"path": trash_path} if trash_path else None
    status, data = api_call("DELETE", "/trash/resources", params)
    return status in (202, 204), data.get("message", f"HTTP {status}")


def human_size(nbytes) -> str:
    try:
        n = float(nbytes)
    except (TypeError, ValueError):
        return ""
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024


# ------------------------------------------------ скриншоты Spectacle в папку Диска

def _spectacle_config() -> configparser.RawConfigParser:
    cp = configparser.RawConfigParser()
    cp.optionxform = str  # KConfig различает регистр ключей
    try:
        cp.read(SPECTACLE_RC, encoding="utf-8")
    except (OSError, configparser.Error):
        pass
    return cp


def spectacle_save_location() -> str:
    return _spectacle_config().get("ImageSave", "imageSaveLocation", fallback="")


def set_spectacle_save_location(url: str | None):
    cp = _spectacle_config()
    if not cp.has_section("ImageSave"):
        cp.add_section("ImageSave")
    if url:
        cp.set("ImageSave", "imageSaveLocation", url)
    else:
        cp.remove_option("ImageSave", "imageSaveLocation")
    SPECTACLE_RC.parent.mkdir(parents=True, exist_ok=True)
    with open(SPECTACLE_RC, "w", encoding="utf-8") as f:
        cp.write(f, space_around_delimiters=False)


def screenshots_dir(disk_dir: str) -> Path:
    return Path(disk_dir) / SCREENSHOTS_DIR_NAME


def screenshots_to_disk_enabled(disk_dir: str) -> bool:
    loc = spectacle_save_location()
    if not loc:
        return False
    path = QUrl(loc).toLocalFile() if loc.startswith("file:") else loc
    return Path(path.rstrip("/")) == screenshots_dir(disk_dir)


# ------------------------------------------- режим для сервис-меню Dolphin (без GUI)

def _copy_to_clipboard_cli(text: str) -> bool:
    """Скопировать в буфер без GUI. На Wayland содержимое буфера живёт,
    пока жив владелец, поэтому нужен долгоживущий держатель: klipper или wl-copy."""
    candidates = [
        ["qdbus6", "org.kde.klipper", "/klipper",
         "org.kde.klipper.klipper.setClipboardContents", text],
        ["qdbus", "org.kde.klipper", "/klipper", "setClipboardContents", text],
        ["wl-copy", text],
    ]
    for cmd in candidates:
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, capture_output=True, timeout=5, check=True)
                return True
            except (OSError, subprocess.SubprocessError):
                continue
    return False


def _notify_cli(title: str, body: str):
    icon = str(APP_DIR / "icons" / "lydisk.svg")
    if shutil.which("notify-send"):
        subprocess.Popen(["notify-send", "-a", APP_NAME, "-i", icon, title, body])
    elif shutil.which("kdialog"):
        subprocess.Popen(["kdialog", "--title", APP_NAME,
                          "--passivepopup", f"{title}\n{body}", "6"])
    else:
        print(f"{title}: {body}")


def cli_publish(path: str, unpublish: bool = False) -> int:
    """lydisk.py --publish/--unpublish FILE — вызывается из меню Dolphin."""
    disk = read_config().get("dir", "")
    if disk and not os.path.abspath(path).startswith(disk):
        _notify_cli("Файл вне папки Диска",
                    "Публиковать можно только файлы из " + disk)
        return 1
    if unpublish:
        rc, out = run_cli("unpublish", path)
        remove_link_record(path)
        _notify_cli("Публикация отменена" if rc == 0 else "Ошибка",
                    os.path.basename(path) if rc == 0 else out[-200:])
        return rc
    rc, out = run_cli("publish", path)
    m = re.search(r"https?://\S+", out)
    if rc == 0 and m:
        url = m.group(0)
        copied = _copy_to_clipboard_cli(url)
        add_link_record(path, url)
        _notify_cli("Ссылка скопирована" if copied else "Публичная ссылка", url)
        return 0
    _notify_cli("Не удалось получить ссылку", out[-200:] or "ошибка")
    return 1


def install_service_menu():
    """Пункты «Яндекс Диск» в контекстном меню Dolphin."""
    icon = APP_DIR / "icons" / "lydisk.svg"
    script = APP_DIR / "lydisk.py"
    content = f"""[Desktop Entry]
Type=Service
X-KDE-ServiceTypes=KonqPopupMenu/Plugin
MimeType=all/all;
Actions=lydiskPublish;lydiskUnpublish;
X-KDE-Submenu=Яндекс Диск

[Desktop Action lydiskPublish]
Name=Скопировать публичную ссылку
Icon={icon}
Exec=python3 "{script}" --publish %f

[Desktop Action lydiskUnpublish]
Name=Отменить публикацию
Icon={icon}
Exec=python3 "{script}" --unpublish %f
"""
    try:
        SERVICEMENU_PATH.parent.mkdir(parents=True, exist_ok=True)
        if (not SERVICEMENU_PATH.exists()
                or SERVICEMENU_PATH.read_text(encoding="utf-8") != content):
            SERVICEMENU_PATH.write_text(content, encoding="utf-8")
            SERVICEMENU_PATH.chmod(0o755)  # KIO игнорирует неисполняемые меню
    except OSError:
        pass


def size_to_bytes(text: str) -> int | None:
    m = re.match(r"([\d.,]+)\s*([KMGT]?B)", text.strip(), re.IGNORECASE)
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    mult = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    return int(value * mult[m.group(2).upper()])


def parse_status(rc: int, out: str) -> dict:
    st = {
        "state": "unknown",
        "progress": "",
        "path": "",
        "total": "",
        "used": "",
        "available": "",
        "trash": "",
        "items": [],
        "raw": out,
    }
    if "daemon not started" in out.lower():
        st["state"] = "stopped"
        return st
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("Synchronization core status:"):
            st["state"] = s.split(":", 1)[1].strip()
        elif s.startswith("Sync progress:"):
            st["progress"] = s.split(":", 1)[1].strip()
        elif s.startswith("Path to"):
            m = re.search(r"'(.+)'", s)
            st["path"] = m.group(1) if m else ""
        elif s.startswith("Total:"):
            st["total"] = s.split(":", 1)[1].strip()
        elif s.startswith("Used:"):
            st["used"] = s.split(":", 1)[1].strip()
        elif s.startswith("Available:"):
            st["available"] = s.split(":", 1)[1].strip()
        elif s.startswith("Trash size:"):
            st["trash"] = s.split(":", 1)[1].strip()
        else:
            m = re.match(r"(file|directory): '(.+)'", s)
            if m:
                st["items"].append((m.group(1), m.group(2)))
    return st


# ------------------------------------------------------------ фоновые задачи

class _TaskSignals(QObject):
    done = pyqtSignal(object)


class _Task(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.setAutoDelete(False)
        self.fn = fn
        self.signals = _TaskSignals()

    def run(self):
        try:
            result = self.fn()
        except Exception as exc:  # не роняем поток пула
            result = (1, f"Ошибка: {exc}")
        self.signals.done.emit(result)


_ACTIVE_TASKS: set = set()


def run_async(fn, callback):
    task = _Task(fn)
    _ACTIVE_TASKS.add(task)

    def _finished(result, _task=task):
        _ACTIVE_TASKS.discard(_task)
        callback(result)

    task.signals.done.connect(_finished)
    QThreadPool.globalInstance().start(task)


# --------------------------------------------------------------------- иконки

def make_status_icon(color: str) -> QIcon:
    """Облако на цветном круге; цвет отражает состояние синхронизации."""
    size = 64
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(2, 2, size - 4, size - 4)
    p.setBrush(QColor("white"))
    p.drawEllipse(14, 30, 18, 18)
    p.drawEllipse(24, 20, 22, 22)
    p.drawEllipse(36, 30, 16, 16)
    p.drawRoundedRect(16, 36, 34, 12, 6, 6)
    p.end()
    return QIcon(pm)


def app_icon() -> QIcon:
    svg = APP_DIR / "icons" / "lydisk.svg"
    if svg.exists():
        return QIcon(str(svg))
    return make_status_icon("#1a73e8")


# ------------------------------------------------------------- вход в аккаунт

class AuthDialog(QDialog):
    """Вход в Яндекс по коду устройства: запускает `yandex-disk token`,
    показывает код и ссылку ya.ru/device, ждёт подтверждения."""

    CODE_RE = re.compile(
        r"(?:code|код)\W{0,3}[«'‘\"]?([A-Za-z0-9-]{4,16})[»'’\"]", re.IGNORECASE)
    GENERIC_CODE_RE = re.compile(r"[«'‘\"]([a-z0-9-]{6,12})[»'’\"]")
    URL_RE = re.compile(r"https?://[^\s'‘’«»\"]+")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Вход в Яндекс")
        self.resize(440, 280)
        self.proc = None
        self.buffer = ""
        self.code = ""
        self.url = "https://ya.ru/device"

        layout = QVBoxLayout(self)
        self.info_label = QLabel("Получение кода авторизации…")
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)

        self.code_label = QLabel("")
        self.code_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code_label.setStyleSheet(
            "font-size: 28px; font-weight: 700; letter-spacing: 3px;")
        self.code_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.code_label)

        row = QHBoxLayout()
        self.copy_btn = QPushButton("Скопировать код")
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(self.code))
        self.open_btn = QPushButton("Открыть ya.ru/device")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(lambda: open_web(self.url))
        row.addStretch()
        row.addWidget(self.copy_btn)
        row.addWidget(self.open_btn)
        row.addStretch()
        layout.addLayout(row)

        self.state_label = QLabel("")
        self.state_label.setWordWrap(True)
        self.state_label.setStyleSheet("color: #666;")
        layout.addWidget(self.state_label)
        layout.addStretch()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        self.retry_btn = buttons.addButton(
            "Получить новый код", QDialogButtonBox.ButtonRole.ActionRole)
        self.retry_btn.clicked.connect(self._start)
        self.retry_btn.setVisible(False)
        layout.addWidget(buttons)

        self._start()

    def _start(self):
        self.retry_btn.setVisible(False)
        self.code_label.setText("")
        self.copy_btn.setEnabled(False)
        self.open_btn.setEnabled(False)
        self.info_label.setText("Получение кода авторизации…")
        self.state_label.setText("")
        self.buffer = ""
        self.code = ""

        ensure_config()  # proxy=no до первого сетевого запроса клиента
        self.proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("LANG", "en_US.UTF-8")
        env.insert("LC_ALL", "en_US.UTF-8")
        self.proc.setProcessEnvironment(env)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._on_output)
        self.proc.finished.connect(self._on_finished)
        self.proc.start(CLI, ["token"])

    def _on_output(self):
        self.buffer += bytes(self.proc.readAllStandardOutput()).decode(
            "utf-8", "replace")
        m = self.URL_RE.search(self.buffer)
        if m and "device" in m.group(0):
            self.url = m.group(0)
        m = self.CODE_RE.search(self.buffer)
        if not m:  # формулировка без слова «код»: любой короткий токен в кавычках
            m = self.GENERIC_CODE_RE.search(self.URL_RE.sub("", self.buffer))
        if m and not self.code:
            self.code = m.group(1)
            self.code_label.setText(self.code)
            self.copy_btn.setEnabled(True)
            self.open_btn.setEnabled(True)
            self.info_label.setText(
                "Откройте страницу ya.ru/device (в браузере должен быть выполнен\n"
                "вход в ваш аккаунт Яндекса) и введите этот код:")
            self.state_label.setText(
                "Код действует около 5 минут. Ожидание подтверждения…")

    def _on_finished(self, *_):
        if is_configured():
            self.accept()
        else:
            tail = "\n".join(self.buffer.strip().splitlines()[-2:])
            self.info_label.setText("Войти не удалось.")
            self.state_label.setText(tail or "Клиент завершился без ответа.")
            self.copy_btn.setEnabled(False)
            self.open_btn.setEnabled(False)
            self.retry_btn.setVisible(True)

    def reject(self):
        if self.proc and self.proc.state() != QProcess.ProcessState.NotRunning:
            self.proc.kill()
        super().reject()


# ------------------------------------------------------- подключение REST API

class ApiConnectDialog(QDialog):
    """Подключение REST API Диска: свой OAuth-клиент + вход по коду устройства."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Подключение API Диска")
        self.resize(540, 460)
        self.device_code = ""
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_token)
        self._polling = False

        layout = QVBoxLayout(self)
        info = QLabel(
            "Для работы с API нужно один раз создать «приложение» в вашем "
            "аккаунте Яндекса (это бесплатно и занимает пару минут):<br><br>"
            "1. Откройте <a href='https://oauth.yandex.ru'>oauth.yandex.ru</a> "
            "→ «Создать приложение».<br>"
            "2. Платформа — <b>«Устройства»</b> (Device flow).<br>"
            "3. Доступы — раздел <b>«Яндекс.Диск REST API»</b>: отметьте чтение "
            "и запись (cloud_api:disk.read, cloud_api:disk.write, "
            "cloud_api:disk.info).<br>"
            "4. Скопируйте сюда ClientID и Client secret."
        )
        info.setWordWrap(True)
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setOpenExternalLinks(False)
        info.linkActivated.connect(open_web)
        layout.addWidget(info)

        form = QFormLayout()
        auth = load_api_auth()
        self.id_edit = QLineEdit(auth.get("client_id", ""))
        self.secret_edit = QLineEdit(auth.get("client_secret", ""))
        form.addRow("ClientID:", self.id_edit)
        form.addRow("Client secret:", self.secret_edit)
        layout.addLayout(form)

        self.start_btn = QPushButton("Получить код подтверждения")
        self.start_btn.clicked.connect(self._start)
        layout.addWidget(self.start_btn)

        self.code_label = QLabel("")
        self.code_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code_label.setStyleSheet(
            "font-size: 26px; font-weight: 700; letter-spacing: 3px;")
        self.code_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.code_label)

        self.open_btn = QPushButton("Открыть страницу подтверждения")
        self.open_btn.setEnabled(False)
        layout.addWidget(self.open_btn)

        self.state_label = QLabel("")
        self.state_label.setWordWrap(True)
        self.state_label.setStyleSheet("color: #666;")
        layout.addWidget(self.state_label)
        layout.addStretch()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _start(self):
        client_id = self.id_edit.text().strip()
        if not client_id or not self.secret_edit.text().strip():
            self.state_label.setText("Заполните ClientID и Client secret.")
            return
        self.start_btn.setEnabled(False)
        self.state_label.setText("Запрос кода…")
        run_async(
            lambda: _http_json(OAUTH_BASE + "/device/code", "POST",
                               {"client_id": client_id}),
            self._on_code,
        )

    def _on_code(self, result):
        status, data = result
        self.start_btn.setEnabled(True)
        if status != 200 or "user_code" not in data:
            self.state_label.setText(
                "Не удалось получить код: "
                + data.get("error_description", data.get("message", str(data))))
            return
        self.device_code = data["device_code"]
        url = data.get("verification_url", "https://ya.ru/device")
        self.code_label.setText(data["user_code"])
        self.open_btn.setEnabled(True)
        self.open_btn.clicked.connect(lambda: open_web(url))
        self.state_label.setText(
            f"Введите код на странице {url} и подтвердите доступ. "
            "Ожидание подтверждения…")
        self.poll_timer.start(int(data.get("interval", 5)) * 1000)

    def _poll_token(self):
        if self._polling:
            return
        self._polling = True
        run_async(
            lambda: _http_json(OAUTH_BASE + "/token", "POST", {
                "grant_type": "device_code",
                "code": self.device_code,
                "client_id": self.id_edit.text().strip(),
                "client_secret": self.secret_edit.text().strip(),
            }),
            self._on_token,
        )

    def _on_token(self, result):
        self._polling = False
        status, data = result
        if status == 200 and data.get("access_token"):
            self.poll_timer.stop()
            save_api_auth({
                "client_id": self.id_edit.text().strip(),
                "client_secret": self.secret_edit.text().strip(),
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token", ""),
            })
            self.accept()
        elif data.get("error") not in ("authorization_pending", None):
            self.poll_timer.stop()
            self.state_label.setText(
                "Ошибка: " + data.get("error_description", data["error"]))

    def reject(self):
        self.poll_timer.stop()
        super().reject()


# ---------------------------------------------------------- корзина (через API)

class TrashDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Корзина Диска")
        self.resize(700, 460)

        layout = QVBoxLayout(self)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Имя", "Размер", "Удалён"])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 340)
        layout.addWidget(self.tree)

        self.state_label = QLabel("")
        self.state_label.setStyleSheet("color: #666;")
        layout.addWidget(self.state_label)

        row = QHBoxLayout()
        restore_btn = QPushButton("Восстановить")
        restore_btn.clicked.connect(self._restore)
        delete_btn = QPushButton("Удалить навсегда")
        delete_btn.clicked.connect(self._delete)
        clear_btn = QPushButton("Очистить корзину")
        clear_btn.clicked.connect(self._clear)
        refresh_btn = QPushButton("Обновить")
        refresh_btn.clicked.connect(self.reload)
        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self.accept)
        for b in (restore_btn, delete_btn, clear_btn, refresh_btn):
            row.addWidget(b)
        row.addStretch()
        row.addWidget(close_btn)
        layout.addLayout(row)

        self.reload()

    def reload(self):
        self.state_label.setText("Загрузка…")
        run_async(api_trash_list, self._on_loaded)

    def _on_loaded(self, result):
        ok, items = result
        self.tree.clear()
        if not ok:
            self.state_label.setText(str(items))
            return
        style = self.style()
        for it in items:
            icon = style.standardIcon(
                QStyle.StandardPixmap.SP_DirIcon if it.get("type") == "dir"
                else QStyle.StandardPixmap.SP_FileIcon)
            row = QTreeWidgetItem([
                it.get("name", ""),
                human_size(it.get("size")),
                (it.get("deleted") or "").replace("T", " ")[:16],
            ])
            row.setIcon(0, icon)
            row.setData(0, ROLE_PATH, it.get("path", ""))
            self.tree.addTopLevelItem(row)
        self.state_label.setText(
            f"Объектов в корзине: {len(items)}" if items else "Корзина пуста")

    def _selected_path(self):
        item = self.tree.currentItem()
        return item.data(0, ROLE_PATH) if item else None

    def _restore(self):
        if path := self._selected_path():
            self.state_label.setText("Восстановление…")
            run_async(lambda: api_trash_restore(path), self._after_op)

    def _delete(self):
        path = self._selected_path()
        if not path:
            return
        name = self.tree.currentItem().text(0)
        if QMessageBox.question(
                self, APP_NAME,
                f"Удалить «{name}» безвозвратно?"
        ) == QMessageBox.StandardButton.Yes:
            self.state_label.setText("Удаление…")
            run_async(lambda: api_trash_delete(path), self._after_op)

    def _clear(self):
        if QMessageBox.question(
                self, APP_NAME,
                "Очистить корзину полностью?\nВсе файлы в ней будут "
                "удалены безвозвратно."
        ) == QMessageBox.StandardButton.Yes:
            self.state_label.setText("Очистка корзины…")
            run_async(lambda: api_trash_delete(None), self._after_op)

    def _after_op(self, result):
        ok, message = result
        if not ok:
            self.state_label.setText("Ошибка: " + str(message))
            return
        self.reload()


# ------------------------------------------------------------------ диагностика

class DiagnosticsDialog(QDialog):
    def __init__(self, disk_dir: str, restart_cb, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Диагностика")
        self.resize(720, 520)
        self.log_path = Path(disk_dir) / ".sync" / "cli.log"

        layout = QVBoxLayout(self)
        _, version = run_cli("-v", timeout=10)
        head = QLabel(
            f"{version.splitlines()[0] if version else 'Версия неизвестна'}\n"
            f"Конфигурация: {CONFIG_PATH}\nЖурнал: {self.log_path}"
        )
        head.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(head)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setStyleSheet("font-family: monospace; font-size: 11px;")
        layout.addWidget(self.view)

        row = QHBoxLayout()
        refresh_btn = QPushButton("Обновить")
        refresh_btn.clicked.connect(self.load_log)
        restart_btn = QPushButton("Перезапустить демона")
        restart_btn.clicked.connect(lambda: (restart_cb(), self.load_log()))
        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self.accept)
        row.addWidget(refresh_btn)
        row.addWidget(restart_btn)
        row.addStretch()
        row.addWidget(close_btn)
        layout.addLayout(row)

        self.load_log()

    def load_log(self):
        try:
            lines = self.log_path.read_text(
                encoding="utf-8", errors="replace").splitlines()
            self.view.setPlainText("\n".join(lines[-400:]))
            self.view.verticalScrollBar().setValue(
                self.view.verticalScrollBar().maximum())
        except OSError:
            self.view.setPlainText("Журнал ещё не создан.")


# ------------------------------------------------------- публичные ссылки (реестр)

class LinksDialog(QDialog):
    """Публичные ссылки: серверный список через API, иначе локальный реестр."""

    def __init__(self, unpublish_cb, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Публичные ссылки")
        self.resize(720, 440)
        self.unpublish_cb = unpublish_cb
        self.use_api = api_connected()

        layout = QVBoxLayout(self)
        self.head_label = QLabel(
            "Полный список опубликованного на Диске (через REST API)."
            if self.use_api else
            "Ссылки, опубликованные через LYDisk (локальный список).\n"
            "Подключите REST API в настройках, чтобы видеть все публикации.")
        layout.addWidget(self.head_label)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(
            ["Файл", "Ссылка", "Путь на Диске" if self.use_api else "Дата"])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 240)
        self.tree.setColumnWidth(1, 280)
        layout.addWidget(self.tree)

        self.state_label = QLabel("")
        self.state_label.setStyleSheet("color: #666;")
        layout.addWidget(self.state_label)

        row = QHBoxLayout()
        copy_btn = QPushButton("Копировать ссылку")
        copy_btn.clicked.connect(self._copy)
        open_btn = QPushButton("Открыть в браузере")
        open_btn.clicked.connect(self._open)
        unpub_btn = QPushButton("Отменить публикацию")
        unpub_btn.clicked.connect(self._unpublish)
        refresh_btn = QPushButton("Обновить")
        refresh_btn.clicked.connect(self.reload)
        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self.accept)
        for b in (copy_btn, open_btn, unpub_btn, refresh_btn):
            row.addWidget(b)
        row.addStretch()
        row.addWidget(close_btn)
        layout.addLayout(row)

        self.reload()

    def reload(self):
        if self.use_api:
            self.state_label.setText("Загрузка списка с сервера…")
            run_async(api_list_public, self._on_api_loaded)
            return
        self.tree.clear()
        for rec in load_links():
            item = QTreeWidgetItem([
                os.path.basename(rec.get("path", "")),
                rec.get("url", ""),
                rec.get("date", ""),
            ])
            item.setToolTip(0, rec.get("path", ""))
            item.setData(0, ROLE_PATH, rec.get("path", ""))
            self.tree.addTopLevelItem(item)
        self.state_label.setText(f"Записей: {self.tree.topLevelItemCount()}")

    def _on_api_loaded(self, result):
        ok, items = result
        self.tree.clear()
        if not ok:
            self.state_label.setText(str(items))
            return
        for it in items:
            row = QTreeWidgetItem([
                it.get("name", ""),
                it.get("public_url", ""),
                it.get("path", ""),
            ])
            row.setData(0, ROLE_PATH, it.get("path", ""))
            self.tree.addTopLevelItem(row)
        self.state_label.setText(
            f"Опубликовано объектов: {len(items)}" if items
            else "Публичных ссылок нет")

    def _current(self):
        return self.tree.currentItem()

    def _copy(self):
        if item := self._current():
            QApplication.clipboard().setText(item.text(1))

    def _open(self):
        if item := self._current():
            open_web(item.text(1))

    def _unpublish(self):
        item = self._current()
        if not item:
            return
        path = item.data(0, ROLE_PATH)
        if self.use_api:
            self.state_label.setText("Отмена публикации…")
            run_async(lambda: api_unpublish(path),
                      lambda res: (self.reload() if res[0] else
                                   self.state_label.setText(str(res[1]))))
        else:
            remove_link_record(path)
            self.unpublish_cb(path)
            self.reload()


# -------------------------------------------------------- выборочная синхронизация

ROLE_PATH = Qt.ItemDataRole.UserRole


class FolderSelectDialog(QDialog):
    """Выбор синхронизируемых папок: снятая галочка — папка в exclude-dirs.

    Дерево строится по локальной папке Диска (уровни подгружаются при
    раскрытии); папки, уже исключённые и не имеющие локальной копии,
    показываются как «фантомы» — их можно включить обратно.
    """

    def __init__(self, disk_dir: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Выбор папок для синхронизации")
        self.resize(480, 540)
        self.disk_root = Path(disk_dir)
        self.excludes = set(get_exclude_dirs())

        layout = QVBoxLayout(self)
        hint = QLabel(
            "Отмеченные папки синхронизируются с компьютером.\n"
            "Снимите галочку, чтобы исключить папку — её файлы останутся в облаке."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemExpanded.connect(self._populate_children)
        self.tree.itemChanged.connect(self._item_changed)
        layout.addWidget(self.tree)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._fill_level(None, "")

    # --- построение дерева

    def _is_excluded(self, rel: str) -> bool:
        return any(rel == e or rel.startswith(e + "/") for e in self.excludes)

    def _has_subdirs(self, rel: str) -> bool:
        try:
            return any(
                c.is_dir() and not c.name.startswith(".")
                for c in (self.disk_root / rel).iterdir()
            )
        except OSError:
            return False

    def _make_item(self, parent, rel: str, phantom: bool):
        name = rel.rsplit("/", 1)[-1]
        text = name + ("  (нет локальной копии)" if phantom else "")
        item = QTreeWidgetItem([text])
        item.setData(0, ROLE_PATH, rel)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(
            0,
            Qt.CheckState.Unchecked if self._is_excluded(rel)
            else Qt.CheckState.Checked,
        )
        item.setIcon(0, self.style().standardIcon(
            QStyle.StandardPixmap.SP_DirLinkIcon if phantom
            else QStyle.StandardPixmap.SP_DirIcon
        ))
        if parent is None:
            self.tree.addTopLevelItem(item)
        else:
            parent.addChild(item)
        if not phantom and self._has_subdirs(rel):
            item.addChild(QTreeWidgetItem(["…"]))  # заглушка для ленивой загрузки
        return item

    def _fill_level(self, parent_item, rel_parent: str):
        base = self.disk_root / rel_parent if rel_parent else self.disk_root
        try:
            names = sorted(
                (c.name for c in base.iterdir()
                 if c.is_dir() and not c.name.startswith(".")),
                key=str.casefold,
            )
        except OSError:
            names = []
        self.tree.blockSignals(True)
        for name in names:
            rel = f"{rel_parent}/{name}" if rel_parent else name
            self._make_item(parent_item, rel, phantom=False)
        # исключённые папки этого уровня, которых нет локально
        for e in sorted(self.excludes):
            e_parent = e.rsplit("/", 1)[0] if "/" in e else ""
            e_name = e.rsplit("/", 1)[-1]
            if e_parent == rel_parent and e_name not in names \
                    and not (self.disk_root / e).exists():
                self._make_item(parent_item, e, phantom=True)
        self.tree.blockSignals(False)

    def _populate_children(self, item):
        if item.childCount() == 1 and item.child(0).data(0, ROLE_PATH) is None:
            item.takeChildren()
            self._fill_level(item, item.data(0, ROLE_PATH))
            if item.checkState(0) == Qt.CheckState.Unchecked:
                self._set_children_enabled(item, False)

    # --- реакция на галочки

    def _item_changed(self, item, _column):
        self.tree.blockSignals(True)
        self._set_children_enabled(
            item, item.checkState(0) == Qt.CheckState.Checked
        )
        self.tree.blockSignals(False)

    def _set_children_enabled(self, item, enabled: bool):
        for i in range(item.childCount()):
            child = item.child(i)
            if child.data(0, ROLE_PATH) is None:
                continue
            child.setDisabled(not enabled)
            self._set_children_enabled(
                child,
                enabled and child.checkState(0) == Qt.CheckState.Checked,
            )

    # --- результат

    def result_excludes(self) -> list[str]:
        seen, result = set(), set()

        def walk(item, ancestor_excluded: bool):
            rel = item.data(0, ROLE_PATH)
            if rel is None:
                return
            seen.add(rel)
            unchecked = item.checkState(0) == Qt.CheckState.Unchecked
            if unchecked and not ancestor_excluded:
                result.add(rel)
            for i in range(item.childCount()):
                walk(item.child(i), ancestor_excluded or unchecked)

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i), False)

        # исключения на уровнях, которые не раскрывались, оставляем как есть,
        # если их не перекрыл исключённый предок
        for e in self.excludes:
            if e not in seen and not any(e.startswith(r + "/") for r in result):
                result.add(e)
        return sorted(result)


# ------------------------------------------------------------------ настройки

class SettingsDialog(QDialog):
    def __init__(self, settings: QSettings, disk_dir: str = "", parent=None):
        super().__init__(parent)
        self.settings = settings
        self.disk_dir = disk_dir
        self.setWindowTitle("Настройки LYDisk")

        form = QFormLayout(self)

        self.poll_spin = QSpinBox()
        self.poll_spin.setRange(2, 120)
        self.poll_spin.setSuffix(" с")
        self.poll_spin.setValue(int(settings.value("poll_interval", 5)))
        form.addRow("Интервал обновления статуса:", self.poll_spin)

        self.quota_spin = QSpinBox()
        self.quota_spin.setRange(50, 100)
        self.quota_spin.setSuffix(" %")
        self.quota_spin.setValue(int(settings.value("quota_warn_percent", 95)))
        form.addRow("Предупреждать о заполнении Диска от:", self.quota_spin)

        self.browser_combo = QComboBox()
        self.browser_combo.addItem("Браузер по умолчанию", "")
        current_browser = settings.value("browser_desktop", "")
        for name, desktop in installed_browsers():
            self.browser_combo.addItem(name, desktop)
            if desktop == current_browser:
                self.browser_combo.setCurrentIndex(
                    self.browser_combo.count() - 1)
        form.addRow("Открывать ссылки Диска в:", self.browser_combo)

        self.autostart_cb = QCheckBox("Запускать LYDisk при входе в систему")
        self.autostart_cb.setChecked(autostart_enabled())
        form.addRow(self.autostart_cb)

        self.daemon_cb = QCheckBox("Запускать синхронизацию при старте LYDisk")
        self.daemon_cb.setChecked(settings.value("start_daemon", True, type=bool))
        form.addRow(self.daemon_cb)

        self.tray_cb = QCheckBox("При закрытии окна сворачивать в трей")
        self.tray_cb.setChecked(settings.value("minimize_to_tray", True, type=bool))
        form.addRow(self.tray_cb)

        self.notify_cb = QCheckBox("Показывать уведомления о синхронизации")
        self.notify_cb.setChecked(settings.value("notify", True, type=bool))
        form.addRow(self.notify_cb)

        self.systemd_cb = QCheckBox(
            "Синхронизировать в фоне при входе в систему (systemd, без LYDisk)")
        self.systemd_cb.setChecked(systemd_autostart_enabled())
        form.addRow(self.systemd_cb)

        self.readonly_cb = QCheckBox(
            "Только чтение: скачивать изменения из облака, ничего не загружать")
        self.readonly_cb.setChecked(settings.value("read_only", False, type=bool))
        self.overwrite_cb = QCheckBox(
            "…и перезаписывать локальные изменения серверными")
        self.overwrite_cb.setChecked(settings.value("overwrite", False, type=bool))
        self.overwrite_cb.setEnabled(self.readonly_cb.isChecked())
        self.readonly_cb.toggled.connect(self.overwrite_cb.setEnabled)
        form.addRow(self.readonly_cb)
        form.addRow(self.overwrite_cb)

        self.screenshots_cb = QCheckBox(
            f"Сохранять скриншоты в Диск (папка «{SCREENSHOTS_DIR_NAME}»)")
        self._screenshots_was = (bool(self.disk_dir)
                                 and screenshots_to_disk_enabled(self.disk_dir))
        self.screenshots_cb.setChecked(self._screenshots_was)
        if not shutil.which("spectacle"):
            self.screenshots_cb.setEnabled(False)
            self.screenshots_cb.setToolTip(
                "Spectacle (программа скриншотов KDE) не найдена")
        elif not self.disk_dir:
            self.screenshots_cb.setEnabled(False)
        form.addRow(self.screenshots_cb)

        self.api_btn = QPushButton()
        self._update_api_btn()
        self.api_btn.clicked.connect(self._toggle_api)
        form.addRow("REST API Диска:", self.api_btn)

        cfg = read_config()
        if cfg.get("dir"):
            form.addRow("Папка Диска:", QLabel(cfg["dir"]))
        if cfg.get("exclude-dirs"):
            dirs = [d.strip() for d in cfg["exclude-dirs"].split(",") if d.strip()]
            text = ", ".join(dirs)
            if len(text) > 60:  # длинный список не должен растягивать окно
                text = text[:57].rstrip(" ,") + f"… (всего: {len(dirs)})"
            exclude_label = QLabel(text)
            exclude_label.setToolTip("\n".join(dirs))
            form.addRow("Исключены из синхронизации:", exclude_label)
        form.addRow("Файл конфигурации:", QLabel(str(CONFIG_PATH)))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _update_api_btn(self):
        self.api_btn.setText("Отключить (подключено)" if api_connected()
                             else "Подключить…")

    def _toggle_api(self):
        if api_connected():
            answer = QMessageBox.question(
                self, APP_NAME,
                "Отключить REST API? Сохранённый токен будет удалён.\n"
                "(Синхронизацию это не затрагивает.)",
            )
            if answer == QMessageBox.StandardButton.Yes:
                API_FILE.unlink(missing_ok=True)
        else:
            ApiConnectDialog(self).exec()
        self._update_api_btn()

    def accept(self):
        old_flags = daemon_start_args()
        self.settings.setValue("poll_interval", self.poll_spin.value())
        self.settings.setValue("quota_warn_percent", self.quota_spin.value())
        self.settings.setValue("browser_desktop",
                               self.browser_combo.currentData())
        self.settings.setValue("start_daemon", self.daemon_cb.isChecked())
        self.settings.setValue("minimize_to_tray", self.tray_cb.isChecked())
        self.settings.setValue("notify", self.notify_cb.isChecked())
        self.settings.setValue("read_only", self.readonly_cb.isChecked())
        self.settings.setValue("overwrite", self.overwrite_cb.isChecked())
        set_autostart(self.autostart_cb.isChecked())

        self.flags_changed = daemon_start_args() != old_flags
        self.systemd_changed = (
            self.systemd_cb.isChecked() != systemd_autostart_enabled()
        )
        self.systemd_enabled = self.systemd_cb.isChecked()
        self.screenshots_changed = (
            self.screenshots_cb.isChecked() != self._screenshots_was
        )
        self.screenshots_enabled = self.screenshots_cb.isChecked()
        super().accept()


def autostart_file() -> Path:
    return Path.home() / ".config/autostart/lydisk.desktop"


def autostart_enabled() -> bool:
    return autostart_file().exists()


def set_autostart(enabled: bool):
    path = autostart_file()
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={APP_NAME}\n"
            "Comment=Графическая оболочка Яндекс Диска\n"
            f'Exec=python3 "{APP_DIR / "lydisk.py"}" --hidden\n'
            f"Icon={APP_DIR / 'icons' / 'lydisk.svg'}\n"
            "X-GNOME-Autostart-enabled=true\n",
            encoding="utf-8",
        )
    else:
        path.unlink(missing_ok=True)


# --------------------------------------------------------------- главное окно

PAGE_MAIN, PAGE_MISSING, PAGE_SETUP = 0, 1, 2


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings(APP_NAME, APP_NAME)
        self.status = parse_status(1, "Error: daemon not started")
        self._status_pending = False

        self.setWindowTitle("LYDisk — Яндекс Диск")
        self.setWindowIcon(app_icon())
        self.resize(640, 540)
        self.setAcceptDrops(True)
        self._last_state = None
        self._quota_warned = False

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.stack.addWidget(self._build_main_page())     # PAGE_MAIN
        self.stack.addWidget(self._build_missing_page())  # PAGE_MISSING
        self.stack.addWidget(self._build_setup_page())    # PAGE_SETUP

        self._build_menu()
        self._build_tray()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(int(self.settings.value("poll_interval", 5)) * 1000)

        if (
            cli_available()
            and is_configured()
            and self.settings.value("start_daemon", True, type=bool)
        ):
            run_async(lambda: run_cli(*daemon_start_args()),
                      self._on_command_done)

        self.refresh()

    # ----- страницы

    def _build_main_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)

        header = QHBoxLayout()
        self.dot_label = QLabel("●")
        self.dot_label.setStyleSheet("font-size: 20px; color: #8a8a8a;")
        self.status_label = QLabel("Проверка состояния…")
        self.status_label.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet("color: #666;")
        header.addWidget(self.dot_label)
        header.addWidget(self.status_label)
        header.addStretch()
        header.addWidget(self.progress_label)
        layout.addLayout(header)

        buttons = QHBoxLayout()
        self.toggle_btn = QPushButton("Запустить")
        self.toggle_btn.clicked.connect(self.toggle_daemon)
        self.sync_btn = QPushButton("Синхронизировать")
        self.sync_btn.clicked.connect(
            lambda: self.run_command("Запуск синхронизации", "sync")
        )
        folder_btn = QPushButton("Открыть папку")
        folder_btn.clicked.connect(self.open_folder)
        web_btn = QPushButton("Диск в браузере")
        web_btn.clicked.connect(lambda: open_web(DISK_WEB_URL))
        folders_btn = QPushButton("Выбор папок")
        folders_btn.clicked.connect(self.choose_folders)
        self.share_btn = QPushButton("Поделиться…")
        share_menu = QMenu(self.share_btn)
        share_menu.addAction("Файлом…", lambda: self.publish_dialog(files=True))
        share_menu.addAction("Папкой…", lambda: self.publish_dialog(files=False))
        self.share_btn.setMenu(share_menu)
        settings_btn = QPushButton("Настройки")
        settings_btn.clicked.connect(self.open_settings)
        for b in (self.toggle_btn, self.sync_btn, folder_btn, web_btn,
                  folders_btn, self.share_btn, settings_btn):
            buttons.addWidget(b)
        layout.addLayout(buttons)

        quota_box = QGroupBox("Место на Диске")
        quota_layout = QVBoxLayout(quota_box)
        self.quota_bar = QProgressBar()
        self.quota_bar.setTextVisible(True)
        self.quota_label = QLabel("Нет данных")
        self.quota_label.setStyleSheet("color: #666;")
        self.quota_label.setTextFormat(Qt.TextFormat.RichText)
        self.quota_label.setOpenExternalLinks(False)
        self.quota_label.linkActivated.connect(open_web)
        quota_layout.addWidget(self.quota_bar)
        quota_layout.addWidget(self.quota_label)
        layout.addWidget(quota_box)

        recent_box = QGroupBox("Недавно синхронизированные")
        recent_layout = QVBoxLayout(recent_box)
        self.recent_list = QListWidget()
        self.recent_list.itemDoubleClicked.connect(self._open_item)
        self.recent_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.recent_list.customContextMenuRequested.connect(self._recent_menu)
        recent_layout.addWidget(self.recent_list)
        layout.addWidget(recent_box, stretch=1)

        self.statusBar().showMessage("Готово")
        return page

    def _build_missing_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        title = QLabel("Консольный клиент yandex-disk не установлен")
        title.setStyleSheet("font-size: 15px; font-weight: 600;")
        layout.addWidget(title)
        layout.addWidget(QLabel(
            "LYDisk — оболочка над официальным консольным клиентом Яндекса.\n"
            "Установите его командой ниже (скопируйте в терминал), затем нажмите «Проверить снова»."
        ))

        cmd_edit = QPlainTextEdit()
        cmd_edit.setReadOnly(True)
        if Path("/etc/apt/sources.list.d/yandex-disk.list").exists():
            # репозиторий уже подключён (например, deb-пакетом LYDisk)
            cmd_edit.setPlainText(
                "sudo apt update && sudo apt install -y yandex-disk")
        else:
            cmd_edit.setPlainText(
                DEB_INSTALL_CMD if shutil.which("apt") else RPM_INSTALL_CMD)
        layout.addWidget(cmd_edit)

        row = QHBoxLayout()
        copy_btn = QPushButton("Скопировать команду")
        copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(cmd_edit.toPlainText())
        )
        check_btn = QPushButton("Проверить снова")
        check_btn.clicked.connect(self.refresh)
        row.addWidget(copy_btn)
        row.addWidget(check_btn)
        row.addStretch()
        layout.addLayout(row)
        layout.addStretch()
        return page

    def _build_setup_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        title = QLabel("Клиент установлен, но ещё не настроен")
        title.setStyleSheet("font-size: 15px; font-weight: 600;")
        layout.addWidget(title)
        layout.addWidget(QLabel(
            "Нужно один раз войти в аккаунт Яндекса и выбрать папку\n"
            "для синхронизации. Вход происходит через браузер по коду —\n"
            "пароль вводить в LYDisk не требуется."
        ))

        row = QHBoxLayout()
        auth_btn = QPushButton("Войти в аккаунт Яндекса")
        auth_btn.clicked.connect(self.start_auth)
        terminal_btn = QPushButton("Настроить через терминал")
        terminal_btn.clicked.connect(self.open_setup_terminal)
        check_btn = QPushButton("Проверить снова")
        check_btn.clicked.connect(self.refresh)
        row.addWidget(auth_btn)
        row.addWidget(terminal_btn)
        row.addWidget(check_btn)
        row.addStretch()
        layout.addLayout(row)
        layout.addStretch()
        return page

    # ----- меню окна

    def _build_menu(self):
        bar = self.menuBar()

        m_disk = bar.addMenu("Диск")
        self.menu_toggle = m_disk.addAction("Запустить", self.toggle_daemon)
        m_disk.addAction("Синхронизировать",
                         lambda: self.run_command("Запуск синхронизации", "sync"))
        m_disk.addSeparator()
        m_disk.addAction("Открыть папку", self.open_folder)
        m_disk.addAction("Диск в браузере", lambda: open_web(DISK_WEB_URL))
        m_disk.addAction("Корзина", lambda: open_web(DISK_TRASH_URL))
        m_disk.addSeparator()
        m_disk.addAction("Выход", self.quit_app)

        m_tools = bar.addMenu("Инструменты")
        m_tools.addAction("Выбор папок…", self.choose_folders)
        m_tools.addAction("Публичные ссылки…", self.open_links)
        m_tools.addAction("Корзина…", self.open_trash)
        m_tools.addAction("Диагностика…", self.open_diagnostics)
        m_tools.addSeparator()
        m_tools.addAction("Настройки…", self.open_settings)

        m_account = bar.addMenu("Аккаунт")
        m_account.addAction("Сменить аккаунт…", self.change_account)

    # ----- трей

    def _build_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(make_status_icon("#8a8a8a"), self)
        menu = QMenu()
        menu.addAction("Показать окно", self.show_window)
        menu.addSeparator()
        self.tray_toggle = menu.addAction("Запустить", self.toggle_daemon)
        menu.addAction(
            "Синхронизировать",
            lambda: self.run_command("Запуск синхронизации", "sync"),
        )
        menu.addAction("Открыть папку", self.open_folder)
        menu.addAction("Выбор папок…", self.choose_folders)
        menu.addAction("Корзина", lambda: open_web(DISK_TRASH_URL))
        menu.addSeparator()
        menu.addAction("Выход", self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.isVisible():
                self.hide()
            else:
                self.show_window()

    def show_window(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def quit_app(self):
        QApplication.instance().quit()

    def closeEvent(self, event):
        if (
            self.tray
            and self.settings.value("minimize_to_tray", True, type=bool)
        ):
            event.ignore()
            self.hide()
            if not self.settings.value("tray_hint_shown", False, type=bool):
                self.tray.showMessage(
                    APP_NAME,
                    "Приложение продолжает работать в трее",
                    QSystemTrayIcon.MessageIcon.Information,
                    4000,
                )
                self.settings.setValue("tray_hint_shown", True)
        else:
            event.accept()
            self.quit_app()

    # ----- обновление состояния

    def refresh(self):
        if not cli_available():
            self.stack.setCurrentIndex(PAGE_MISSING)
            self._set_tray("#8a8a8a", "Клиент не установлен")
            return
        if not is_configured():
            self.stack.setCurrentIndex(PAGE_SETUP)
            self._set_tray("#8a8a8a", "Клиент не настроен")
            return
        self.stack.setCurrentIndex(PAGE_MAIN)
        if self._status_pending:
            return
        self._status_pending = True
        run_async(lambda: run_cli("status", timeout=20), self._on_status)

    def _on_status(self, result):
        self._status_pending = False
        rc, out = result
        self.status = parse_status(rc, out)
        self._apply_status()

    def _apply_status(self):
        st = self.status
        label, color = STATES.get(st["state"], STATES["unknown"])
        running = st["state"] != "stopped"

        self.dot_label.setStyleSheet(f"font-size: 20px; color: {color};")
        self.status_label.setText(label)
        self.progress_label.setText(st["progress"])

        self.toggle_btn.setText("Остановить" if running else "Запустить")
        self.sync_btn.setEnabled(running)
        self.share_btn.setEnabled(running)

        used, total = size_to_bytes(st["used"]), size_to_bytes(st["total"])
        if used is not None and total:
            percent = round(used * 100 / total)
            warn_now, self._quota_warned = quota_warning_state(
                percent,
                int(self.settings.value("quota_warn_percent", 95)),
                self._quota_warned,
            )
            if (warn_now and self.tray
                    and self.settings.value("notify", True, type=bool)):
                self.tray.showMessage(
                    "На Диске заканчивается место",
                    f"Занято {percent}% ({st['used']} из {st['total']}). "
                    "Освободите место или очистите корзину.",
                    QSystemTrayIcon.MessageIcon.Warning, 8000)
            self.quota_bar.setRange(0, 100)
            self.quota_bar.setValue(percent)
            self.quota_bar.setFormat(f"Занято {st['used']} из {st['total']} (%p%)")
            parts = []
            if st["available"]:
                parts.append(f"Свободно: {st['available']}")
            if st["trash"]:
                parts.append(
                    f'<a href="{DISK_TRASH_URL}">Корзина: {st["trash"]}</a>')
            self.quota_label.setText(" · ".join(parts) or " ")
        else:
            self.quota_bar.setRange(0, 100)
            self.quota_bar.setValue(0)
            self.quota_bar.setFormat("")
            self.quota_label.setText(
                "Нет данных — запустите синхронизацию" if not running else "Получение данных…"
            )

        self.recent_list.clear()
        style = self.style()
        for kind, rel_path in st["items"]:
            icon = style.standardIcon(
                QStyle.StandardPixmap.SP_DirIcon
                if kind == "directory"
                else QStyle.StandardPixmap.SP_FileIcon
            )
            item = QListWidgetItem(icon, rel_path)
            item.setToolTip(os.path.join(st["path"], rel_path))
            self.recent_list.addItem(item)

        self._set_tray(color, label)
        if self.tray:
            self.tray_toggle.setText("Остановить" if running else "Запустить")
        self.menu_toggle.setText("Остановить" if running else "Запустить")

        notif = notification_for(self._last_state, st["state"])
        self._last_state = st["state"]
        if (notif and self.tray
                and self.settings.value("notify", True, type=bool)):
            self.tray.showMessage(
                notif[0], notif[1],
                QSystemTrayIcon.MessageIcon.Information, 5000)

    def _set_tray(self, color: str, label: str):
        if self.tray:
            self.tray.setIcon(make_status_icon(color))
            self.tray.setToolTip(f"{APP_NAME} — {label}")

    # ----- команды

    def run_command(self, feedback: str, *args: str):
        self.statusBar().showMessage(f"{feedback}…")
        run_async(lambda: run_cli(*args), self._on_command_done)

    def _on_command_done(self, result):
        rc, out = result
        message = out.splitlines()[-1] if out else ("Готово" if rc == 0 else "Ошибка")
        self.statusBar().showMessage(message, 8000)
        self.refresh()

    def toggle_daemon(self):
        if self.status["state"] == "stopped":
            self.run_command("Запуск службы", *daemon_start_args())
        else:
            self.run_command("Остановка службы", "stop")

    def disk_dir(self) -> str:
        return self.status["path"] or read_config().get("dir", str(Path.home()))

    def open_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.disk_dir()))

    def _open_item(self, item: QListWidgetItem):
        QDesktopServices.openUrl(QUrl.fromLocalFile(item.toolTip()))

    def _recent_menu(self, pos):
        item = self.recent_list.itemAt(pos)
        if not item:
            return
        path = item.toolTip()
        menu = QMenu(self)
        menu.addAction("Открыть", lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(path)))
        menu.addAction("Открыть содержащую папку", lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(os.path.dirname(path))))
        menu.addSeparator()
        menu.addAction("Скопировать публичную ссылку",
                       lambda: self.publish(path))
        menu.addAction("Отменить публикацию",
                       lambda: self.unpublish(path))
        menu.exec(self.recent_list.mapToGlobal(pos))

    def publish_dialog(self, files: bool):
        if files:
            path, _ = QFileDialog.getOpenFileName(
                self, "Файл для публикации", self.disk_dir())
        else:
            path = QFileDialog.getExistingDirectory(
                self, "Папка для публикации", self.disk_dir())
        if path:
            self.publish(path)

    def publish(self, path: str):
        self.publish_many([path])

    def unpublish(self, path: str):
        remove_link_record(path)
        self.run_command("Отмена публикации", "unpublish", path)

    def publish_many(self, paths: list[str]):
        inside = [p for p in paths if p.startswith(self.disk_dir())]
        if not inside:
            QMessageBox.warning(
                self, APP_NAME,
                "Публиковать можно только файлы из папки Яндекс Диска:\n"
                + self.disk_dir(),
            )
            return

        def work():
            results = []
            for p in inside:
                rc, out = run_cli("publish", p)
                m = re.search(r"https?://\S+", out)
                results.append((p, m.group(0) if rc == 0 and m else None, out))
            return results

        self.statusBar().showMessage("Получение публичной ссылки…")
        run_async(work, self._on_published_many)

    def _on_published_many(self, results):
        ok = [(p, url) for p, url, _ in results if url]
        for p, url in ok:
            add_link_record(p, url)
        if ok:
            text = "\n".join(url for _, url in ok)
            QApplication.clipboard().setText(text)
            self.statusBar().showMessage("Ссылка скопирована в буфер обмена", 8000)
            QMessageBox.information(
                self, APP_NAME,
                "Публичная ссылка скопирована в буфер обмена:\n\n" + text,
            )
        failed = [out for _, url, out in results if not url]
        if failed:
            self.statusBar().showMessage(
                failed[-1].splitlines()[-1] if failed[-1]
                else "Не удалось получить ссылку", 8000)

    # ----- drag&drop: перетащили файл — получили ссылку

    def dragEnterEvent(self, event):
        if any(u.isLocalFile() for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls()
                 if u.isLocalFile()]
        if paths:
            self.publish_many(paths)

    def choose_folders(self):
        disk_dir = Path(self.disk_dir())
        if not disk_dir.is_dir():
            QMessageBox.warning(
                self, APP_NAME,
                "Папка Диска ещё не создана — запустите синхронизацию.",
            )
            return
        dialog = FolderSelectDialog(str(disk_dir), self)
        if not dialog.exec():
            return
        old = set(get_exclude_dirs())
        new = dialog.result_excludes()
        if set(new) == old:
            return
        set_exclude_dirs(new)

        newly_excluded = [p for p in new if p not in old]
        to_delete = [p for p in newly_excluded if (disk_dir / p).exists()]
        if to_delete:
            answer = QMessageBox.question(
                self, APP_NAME,
                "Удалить локальные копии исключённых папок?\n"
                "Файлы останутся в облаке.\n\n• " + "\n• ".join(to_delete),
            )
            if answer == QMessageBox.StandardButton.Yes:
                root = disk_dir.resolve()
                for p in to_delete:
                    target = (disk_dir / p).resolve()
                    if target != root and target.is_relative_to(root):
                        shutil.rmtree(target, ignore_errors=True)

        self.statusBar().showMessage("Перезапуск синхронизации с новыми настройками…")
        run_async(restart_daemon, self._on_command_done)

    # ----- вход в аккаунт и обслуживание

    def start_auth(self):
        dialog = AuthDialog(self)
        if not dialog.exec():
            return
        if not read_config().get("dir"):
            default = str(Path.home() / "Yandex.Disk")
            path = QFileDialog.getExistingDirectory(
                self, "Выберите папку для синхронизации", str(Path.home()))
            path = path or default
            Path(path).mkdir(parents=True, exist_ok=True)
            set_config_value("dir", path)
        self.run_command("Запуск синхронизации", *daemon_start_args())

    def change_account(self):
        answer = QMessageBox.question(
            self, APP_NAME,
            "Выйти из текущего аккаунта?\n"
            "Синхронизация будет остановлена, файлы останутся на компьютере.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        run_cli("stop")
        auth = read_config().get("auth", str(CONFIG_PATH.parent / "passwd"))
        Path(auth).expanduser().unlink(missing_ok=True)
        self.refresh()
        self.start_auth()

    def open_links(self):
        LinksDialog(self.unpublish, self).exec()

    def open_trash(self):
        if not api_connected():
            answer = QMessageBox.question(
                self, APP_NAME,
                "Управление корзиной (просмотр, восстановление, очистка)\n"
                "работает через REST API Диска, который ещё не подключён.\n\n"
                "Подключить сейчас?",
            )
            if answer == QMessageBox.StandardButton.Yes:
                ApiConnectDialog(self).exec()
            if not api_connected():
                return
        TrashDialog(self).exec()

    def open_diagnostics(self):
        DiagnosticsDialog(self.disk_dir(), self.restart_daemon_ui, self).exec()

    def restart_daemon_ui(self):
        self.statusBar().showMessage("Перезапуск демона…")
        run_async(restart_daemon, self._on_command_done)

    def apply_screenshots_setting(self, enable: bool):
        if not enable:
            prev = self.settings.value("spectacle_prev", "")
            set_spectacle_save_location(prev or None)
            self.statusBar().showMessage(
                "Скриншоты снова сохраняются в прежнюю папку", 8000)
            return
        target = screenshots_dir(self.disk_dir())
        excludes = get_exclude_dirs()
        if any(SCREENSHOTS_DIR_NAME == e
               or SCREENSHOTS_DIR_NAME.startswith(e + "/") for e in excludes):
            answer = QMessageBox.question(
                self, APP_NAME,
                f"Папка «{SCREENSHOTS_DIR_NAME}» сейчас исключена из синхронизации —\n"
                "скриншоты сохранялись бы только локально.\n\n"
                "Включить синхронизацию этой папки?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            set_exclude_dirs([e for e in excludes
                              if e != SCREENSHOTS_DIR_NAME])
            run_async(restart_daemon, self._on_command_done)
        target.mkdir(parents=True, exist_ok=True)
        prev = spectacle_save_location()
        if prev:
            self.settings.setValue("spectacle_prev", prev)
        set_spectacle_save_location(
            QUrl.fromLocalFile(str(target)).toString())
        self.statusBar().showMessage(
            f"Скриншоты будут сохраняться в {target}", 8000)

    def open_setup_terminal(self):
        for term, flag in TERMINALS:
            if shutil.which(term):
                subprocess.Popen([term, *flag, CLI, "setup"])
                return
        QMessageBox.warning(
            self, APP_NAME,
            f"Терминал не найден. Выполните вручную:\n\n{CLI} setup",
        )

    def open_settings(self):
        dialog = SettingsDialog(self.settings, self.disk_dir(), self)
        if not dialog.exec():
            return
        if dialog.screenshots_changed:
            self.apply_screenshots_setting(dialog.screenshots_enabled)
        self.timer.setInterval(
            int(self.settings.value("poll_interval", 5)) * 1000
        )
        if dialog.systemd_changed:
            self.statusBar().showMessage(
                "Настройка фоновой синхронизации (systemd)…")
            run_async(
                lambda: (0 if set_systemd_autostart(dialog.systemd_enabled)
                         else 1,
                         "Фоновая синхронизация настроена"
                         if dialog.systemd_enabled
                         else "Фоновая синхронизация отключена"),
                self._on_command_done,
            )
        elif dialog.flags_changed and self.status["state"] != "stopped":
            self.statusBar().showMessage(
                "Перезапуск с новыми параметрами…")
            run_async(
                lambda: (refresh_systemd_unit(), restart_daemon())[1]
                if systemd_autostart_enabled() else restart_daemon(),
                self._on_command_done,
            )


# ----------------------------------------------------------------------- main

def main():
    # режим сервис-меню Dolphin: публикация без запуска GUI и без Qt
    if len(sys.argv) >= 3 and sys.argv[1] in ("--publish", "--unpublish"):
        return cli_publish(sys.argv[2], unpublish=sys.argv[1] == "--unpublish")

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("lydisk")
    app.setWindowIcon(app_icon())

    lock = QLockFile(os.path.join(tempfile.gettempdir(),
                                  f"lydisk-{os.getuid()}.lock"))
    if not lock.tryLock(100):
        QMessageBox.information(None, APP_NAME, "LYDisk уже запущен.")
        return 0

    install_service_menu()

    window = MainWindow()
    if window.tray:
        app.setQuitOnLastWindowClosed(False)
    if "--hidden" not in sys.argv or not window.tray:
        window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
