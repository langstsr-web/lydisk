"""Тесты автоперезапуска демона при отказе сервера в дельта-загрузке.

Запуск: python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lydisk  # noqa: E402
from lydisk import DeltaLogWatcher  # noqa: E402

MD = "Работа/Стройка/CRM/09_Единый_вид_элементов.md"
PNG = "Работа/Стройка/Макеты/Объекты/контакты/после/panel_ind__1440_light_2.png"

# Фрагменты настоящего core.log клиента 0.1.6.1080 (17.10.2026).
DELTA_500 = f"""\
61017-133933.999 PUT "{MD}" mime-type set to "" / cached
61017-133933.999 PUT "{MD}" looking for a delta from a04e06c8 to 171ba518
61017-133933.999 PUT "{MD}" delta
61017-133934.115 Queueing "ngs7dejtviil5at79iarpnkjf3" <pending "{MD}">
61017-133934.182 PUT "{MD}" 500
61017-133940.123 PUT "{MD}" looking for a delta from a04e06c8 to 171ba518
61017-133940.124 PUT "{MD}" delta
61017-133940.407 PUT "{MD}" 500
61017-133945.692 PROP "{MD}" lh=171ba518,rh=a04e06c8,in=46b1d064,id=3468b53f,e=6
61017-133945.700 Core ERROR  ("Server failure") at "{MD}"
""".splitlines()

FULL_GZIP_OK = f"""\
61017-134628.217 PUT "{MD}" looking for a delta from a04e06c8 to 171ba518
61017-134628.217 PUT "{MD}" gzip
61017-134628.740 PUT "{MD}" 0 / 77854
61017-134628.745 PUT "{MD}" receiving response
61017-134628.745 PUT "{MD}" 77854 / 77854
61017-134633.127 PUT "{MD}" OK
""".splitlines()

FULL_PLAIN_500 = f"""\
61017-134618.517 PUT "{PNG}" mime-type set to ""
61017-134618.517 PUT "{PNG}" looking for a delta from 80798e05 to 2c4db762
61017-134618.524 PUT "{PNG}"
61017-134619.648 PUT "{PNG}" 0 / 164747
61017-134619.649 PUT "{PNG}" 164747 / 164747
61017-134629.647 PUT "{PNG}" 500
61017-134647.358 HEAD "{PNG}" 500
""".splitlines()

PNG_DELTA_500 = f"""\
61017-133945.212 PUT "{PNG}" looking for a delta from 80798e05 to 2c4db762
61017-133945.215 PUT "{PNG}" delta
61017-133945.399 PUT "{PNG}" 500
""".splitlines()

HARDLINK_OK = [f'61017-135000.000 PUT "{PNG}" 201 Hardlink OK']

DAEMON_START = [
    "61017-134614.847 STATE version 1792233964295722 read with 93 incremental frames"]

NOISE = '61017-140000.000 MONITOR create "/home/u/Yandex.Disk" / "x/y.log"'


def stuck(lines) -> set:
    watcher = DeltaLogWatcher()
    watcher.feed(lines)
    return watcher.stuck


class FeedTest(unittest.TestCase):
    def test_delta_rejected_with_500_is_stuck(self):
        self.assertEqual(stuck(DELTA_500), {MD})

    def test_compressed_delta_rejected_is_stuck(self):
        lines = [f'61017-133945.215 PUT "{PNG}" delta gzip',
                 f'61017-133945.399 PUT "{PNG}" 500']
        self.assertEqual(stuck(lines), {PNG})

    def test_full_upload_after_rejected_delta_clears_it(self):
        self.assertEqual(stuck(DELTA_500 + FULL_GZIP_OK), set())

    def test_rejected_full_upload_is_not_delta_problem(self):
        self.assertEqual(stuck(FULL_PLAIN_500), set())

    def test_last_attempt_decides(self):
        self.assertEqual(stuck(PNG_DELTA_500 + FULL_PLAIN_500), set())
        self.assertEqual(stuck(FULL_PLAIN_500 + PNG_DELTA_500), {PNG})

    def test_hardlink_upload_clears_it(self):
        self.assertEqual(stuck(PNG_DELTA_500 + HARDLINK_OK), set())

    def test_progress_line_is_not_success(self):
        lines = PNG_DELTA_500 + [f'61017-133950.000 PUT "{PNG}" 201234 / 500000']
        self.assertEqual(stuck(lines), {PNG})

    def test_files_are_tracked_separately(self):
        self.assertEqual(stuck(DELTA_500 + PNG_DELTA_500 + FULL_PLAIN_500), {MD})

    def test_quotes_and_spaces_in_file_name(self):
        name = 'Документы/ООО "Курс" акт.pdf'
        lines = [f'61017-133945.215 PUT "{name}" delta',
                 f'61017-133945.399 PUT "{name}" 500']
        self.assertEqual(stuck(lines), {name})

    def test_daemon_start_forgets_old_failures(self):
        self.assertEqual(stuck(DELTA_500 + DAEMON_START), set())
        self.assertEqual(stuck(DAEMON_START + DELTA_500), {MD})

    def test_bare_daemon_start_marker(self):
        bare = ["61014-195803.820 STATE version 1791994851252568 read"]
        self.assertEqual(stuck(DELTA_500 + bare), set())

    def test_empty_log(self):
        self.assertEqual(stuck([]), set())


class PollTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.disk = self.tmp.name
        (Path(self.disk) / ".sync").mkdir()
        self.log = Path(self.disk) / ".sync" / "core.log"
        self.watcher = DeltaLogWatcher()

    def tearDown(self):
        self.tmp.cleanup()

    def append(self, text: str):
        with open(self.log, "a", encoding="utf-8") as f:
            f.write(text)

    def test_missing_log_is_ignored(self):
        self.watcher.poll(self.disk)
        self.assertEqual(self.watcher.stuck, set())

    def test_reads_only_new_lines(self):
        self.append("\n".join(DELTA_500) + "\n")
        self.watcher.poll(self.disk)
        self.assertEqual(self.watcher.stuck, {MD})
        self.append("\n".join(FULL_GZIP_OK) + "\n")
        self.watcher.poll(self.disk)
        self.assertEqual(self.watcher.stuck, set())

    def test_line_split_between_polls(self):
        line = f'61017-133934.182 PUT "{MD}" 500\n'
        self.append("\n".join(DELTA_500[:3]) + "\n" + line[:30])
        self.watcher.poll(self.disk)
        self.assertEqual(self.watcher.stuck, set())
        self.append(line[30:])
        self.watcher.poll(self.disk)
        self.assertEqual(self.watcher.stuck, {MD})

    def test_stuck_file_is_remembered_after_lots_of_log(self):
        # Клиент перестаёт повторять застрявший файл, а лог растёт дальше.
        self.append("\n".join(DELTA_500) + "\n")
        self.watcher.poll(self.disk)
        self.append((NOISE + "\n") * 30_000)  # > 2 МБ
        self.watcher.poll(self.disk)
        self.assertEqual(self.watcher.stuck, {MD})

    def test_rotation_keeps_state(self):
        self.append("\n".join(DELTA_500) + "\n")
        self.watcher.poll(self.disk)
        rotated = self.log.with_name("core.log.new")
        rotated.write_text(NOISE + "\n", encoding="utf-8")
        os.replace(rotated, self.log)
        self.watcher.poll(self.disk)
        self.assertEqual(self.watcher.stuck, {MD})
        self.append("\n".join(FULL_GZIP_OK) + "\n")
        self.watcher.poll(self.disk)
        self.assertEqual(self.watcher.stuck, set())


class _Settings:
    def __init__(self, **values):
        self.values = values

    def value(self, key, default=None, type=None):
        return self.values.get(key, default)


class MaybeHealDeltaTest(unittest.TestCase):
    """MainWindow._maybe_heal_delta без Qt: окно подменено простым объектом
    (настоящий MainWindow при создании запускает yandex-disk)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        disk = Path(self.tmp.name)
        (disk / ".sync").mkdir()
        self.log = disk / ".sync" / "core.log"
        self.log.write_text("\n".join(DELTA_500) + "\n", encoding="utf-8")
        self.tray = mock.Mock()
        self.win = SimpleNamespace(
            settings=_Settings(),
            tray=None,
            disk_dir=lambda: str(disk),
            statusBar=lambda: SimpleNamespace(showMessage=lambda *a: None),
            _on_command_done=lambda result: None,
            _delta_log=DeltaLogWatcher(),
            _delta_heal_after=0.0,
            _delta_heal_notified=False,
        )
        patcher = mock.patch.object(lydisk, "run_async")
        self.run_async = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def heal(self, state="error"):
        lydisk.MainWindow._maybe_heal_delta(self.win, state)

    def test_restarts_sync_on_error_with_stuck_delta(self):
        self.heal()
        self.run_async.assert_called_once()
        self.assertIs(self.run_async.call_args.args[0], lydisk.restart_sync_service)
        self.assertGreater(self.win._delta_heal_after,
                           time.monotonic() + lydisk.DELTA_HEAL_COOLDOWN - 5)

    def test_second_error_within_cooldown_does_not_restart(self):
        self.heal()
        self.heal()
        self.run_async.assert_called_once()

    def test_restarts_in_idle_too(self):
        # Клиент перестал повторять файл и считает, что всё синхронизировано.
        self.heal("idle")
        self.run_async.assert_called_once()

    def test_no_restart_while_working_or_offline(self):
        for state in ("busy", "index", "no internet access", "stopped", "paused"):
            self.heal(state)
        self.run_async.assert_not_called()

    def test_first_poll_starts_cooldown(self):
        self.win._delta_heal_after = None
        self.heal()
        self.run_async.assert_not_called()
        self.assertGreater(self.win._delta_heal_after,
                           time.monotonic() + lydisk.DELTA_HEAL_COOLDOWN - 5)

    def test_no_restart_when_disabled_in_settings(self):
        self.win.settings = _Settings(delta_heal=False)
        self.heal()
        self.run_async.assert_not_called()

    def test_no_restart_for_other_errors(self):
        self.log.write_text("\n".join(FULL_PLAIN_500) + "\n", encoding="utf-8")
        self.heal()
        self.run_async.assert_not_called()

    def test_tray_notification_only_once(self):
        self.win.tray = self.tray
        self.heal()
        self.win._delta_heal_after = 0.0
        self.heal()
        self.assertEqual(self.run_async.call_count, 2)
        self.tray.showMessage.assert_called_once()

    def test_no_tray_notification_when_notifications_off(self):
        self.win.tray = self.tray
        self.win.settings = _Settings(notify=False)
        self.heal()
        self.run_async.assert_called_once()
        self.tray.showMessage.assert_not_called()


class RestartSyncServiceTest(unittest.TestCase):
    UNIT_ACTIVE = mock.call("is-active", "--quiet", "yandex-disk.service")
    UNIT_RESTART = mock.call("restart", "yandex-disk.service")

    def test_uses_systemd_unit_when_it_runs_the_daemon(self):
        with mock.patch.object(lydisk, "_systemctl", return_value=True) as systemctl, \
             mock.patch.object(lydisk, "restart_daemon") as restart:
            rc, _ = lydisk.restart_sync_service()
        self.assertEqual(systemctl.call_args_list, [self.UNIT_ACTIVE, self.UNIT_RESTART])
        restart.assert_not_called()
        self.assertEqual(rc, 0)

    def test_reports_failed_unit_restart(self):
        with mock.patch.object(lydisk, "_systemctl", side_effect=[True, False]), \
             mock.patch.object(lydisk, "restart_daemon") as restart:
            rc, _ = lydisk.restart_sync_service()
        restart.assert_not_called()
        self.assertEqual(rc, 1)

    def test_restarts_daemon_directly_when_unit_inactive(self):
        # Юнит включён, но демон запущен в обход него (кнопкой, сменой флагов).
        with mock.patch.object(lydisk, "_systemctl", return_value=False) as systemctl, \
             mock.patch.object(lydisk, "restart_daemon", return_value=(0, "ok")) as restart:
            self.assertEqual(lydisk.restart_sync_service(), (0, "ok"))
        restart.assert_called_once_with()
        self.assertEqual(systemctl.call_args_list, [self.UNIT_ACTIVE])


class SettingsDialogTest(unittest.TestCase):
    def test_checkbox_saves_setting(self):
        from PyQt6.QtCore import QSettings
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])  # noqa: F841
        with tempfile.TemporaryDirectory() as tmp:
            settings = QSettings(os.path.join(tmp, "s.ini"), QSettings.Format.IniFormat)
            dialog = lydisk.SettingsDialog(settings, "")
            self.assertTrue(dialog.delta_heal_cb.isChecked())
            dialog.delta_heal_cb.setChecked(False)
            with mock.patch.object(lydisk, "set_autostart"):
                dialog.accept()
            self.assertFalse(settings.value("delta_heal", True, type=bool))


if __name__ == "__main__":
    unittest.main()
