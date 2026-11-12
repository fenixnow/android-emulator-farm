"""Смоук-тесты отказоустойчивости watchdog: одиночная ошибка не роняет батч.

Запуск: python3 tests/test_emulator_manager.py
"""
import logging
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from emulator_manager import EmulatorManager  # noqa: E402


def _manager():
    m = EmulatorManager.__new__(EmulatorManager)
    m.log = logging.getLogger("test")
    m.adb_timeout = 5
    return m


def test_webview_a11y_flag_writes_both_files():
    """Команда пишет флаг в оба cmdline-файла на data-разделе; timeout не роняет."""
    m = _manager()
    spec = SimpleNamespace(locale="ru-RU", name="emu1", serial="emulator-5554")
    with patch.object(m, "_run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")) as run:
        m.apply_webview_a11y_flag(spec)
    cmd = run.call_args.args[0]
    assert "emulator-5554" in cmd
    assert "webview-chromium-command-line" in cmd and "webview-command-line" in cmd
    assert "force-renderer-accessibility" in cmd

    with patch.object(m, "_run", side_effect=subprocess.TimeoutExpired("adb", 10)):
        m.apply_webview_a11y_flag(spec)


def test_set_locale_survives_timeout():
    """adb root завис, getprop zygote тоже -> set_locale логирует error и молчит."""
    m = _manager()
    spec = SimpleNamespace(locale="ru-RU", name="emu1", serial="emulator-5554")
    with patch.object(m, "_run", side_effect=subprocess.TimeoutExpired("adb", 5)), \
         patch("emulator_manager.time.sleep"):
        m.set_locale(spec)


def test_set_locale_timeout_with_live_framework():
    """adb root завис, но framework жив (zygote running) -> warning, не error, молчит."""
    m = _manager()
    spec = SimpleNamespace(locale="ru-RU", name="emu1", serial="emulator-5554")

    def timeout_then_healthy(cmd, timeout=None, **kw):
        if "getprop" in cmd:
            return SimpleNamespace(stdout="running\n", returncode=0)
        raise subprocess.TimeoutExpired(cmd, timeout)

    with patch.object(m, "_run", side_effect=timeout_then_healthy), \
         patch("emulator_manager.time.sleep"):
        m.set_locale(spec)


def test_start_all_survives_single_failure():
    """Ошибка запуска одного эмулятора не останавливает батч."""
    m = _manager()
    m.specs = [SimpleNamespace(name=f"emu{i}", avd=f"a{i}", port=i, restart_count=0)
               for i in range(3)]
    m.auto_create = False
    started = []

    def flaky_start(spec):
        started.append(spec.name)
        if len(started) == 1:
            raise RuntimeError("adb завис")

    with patch.object(m, "start_one", side_effect=flaky_start), \
         patch("emulator_manager.time.sleep"):
        m.start_all()
    assert started == ["emu0", "emu1", "emu2"], started


def test_zombie_cleanup_deletes_only_old_sessions(tmp_path=None):
    """Старая running-сессия -> DELETE по API; свежая не трогается."""
    import sqlite3
    from unittest.mock import patch as _p

    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "df.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE Session (id TEXT, device_udid TEXT, start_time INTEGER,"
                " status TEXT, end_time INTEGER, failure_reason TEXT)")
    now_ms = int(time.time() * 1000)
    # "плагин" (фейковый DELETE) чистит свою запись — как в реальном плагине
    con.execute("INSERT INTO Session VALUES ('OLD', 'emu-1', ?, 'running', NULL, NULL)", (now_ms - 3600_000,))
    con.execute("INSERT INTO Session VALUES ('NEW', 'emu-1', ?, 'running', NULL, NULL)", (now_ms - 120_000,))
    con.commit()
    con.close()

    pw = tmp_path / "pass.txt"
    pw.write_text("secret\n")
    m = _manager()
    m.zombie_cfg = {"enabled": True, "db_path": str(db), "api_user": "pola",
                    "api_password_file": str(pw), "api_base": "http://x",
                    "max_age_min": 30}
    deleted = []

    def fake_api(method, path, data=None, token=None):
        if path == "/device-farm/api/auth/login":
            return 200, {"token": "jwt"}
        if method == "DELETE":
            sid = path.rsplit("/", 1)[1]
            deleted.append(sid)
            c = sqlite3.connect(db)
            c.execute("UPDATE Session SET status='failed', end_time=? WHERE id=?", (now_ms, sid))
            c.commit()
            c.close()
            return 404, None
        return None, None

    with _p.object(m, "_df_api", side_effect=fake_api):
        m.cleanup_zombie_sessions()
    assert deleted == ["OLD"], deleted


def test_zombie_cleanup_db_fallback(tmp_path=None):
    """DELETE не почистил запись -> cleanup закрывает её UPDATE-ом в БД."""
    import sqlite3
    from unittest.mock import patch as _p

    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "df.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE Session (id TEXT, device_udid TEXT, start_time INTEGER,"
                " status TEXT, end_time INTEGER, failure_reason TEXT)")
    con.execute("INSERT INTO Session VALUES ('OLD', 'emu-1', ?, 'running', NULL, NULL)",
                (int((time.time() - 3600) * 1000),))
    con.commit()
    con.close()

    pw = tmp_path / "pass.txt"
    pw.write_text("secret\n")
    m = _manager()
    m.zombie_cfg = {"enabled": True, "db_path": str(db), "api_user": "pola",
                    "api_password_file": str(pw), "api_base": "http://x",
                    "max_age_min": 30}

    def fake_api(method, path, data=None, token=None):
        if path == "/device-farm/api/auth/login":
            return 200, {"token": "jwt"}
        return 404, None  # DELETE не помогает

    with _p.object(m, "_df_api", side_effect=fake_api), \
         patch("emulator_manager.time.sleep"):
        m.cleanup_zombie_sessions()
    con = sqlite3.connect(db)
    status, end_time = con.execute("SELECT status, end_time FROM Session WHERE id='OLD'").fetchone()
    con.close()
    assert status == "failed" and end_time is not None, (status, end_time)


if __name__ == "__main__":
    import tempfile
    from pathlib import Path as _Path

    test_set_locale_survives_timeout()
    test_start_all_survives_single_failure()
    with tempfile.TemporaryDirectory() as td:
        test_zombie_cleanup_deletes_only_old_sessions(_Path(td) / "t1")
        test_zombie_cleanup_db_fallback(_Path(td) / "t2")
    print("OK: все тесты прошли")
