"""Смоук-тесты отказоустойчивости watchdog: одиночная ошибка не роняет батч.

Запуск: python3 tests/test_emulator_manager.py
"""
import logging
import subprocess
import sys
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


if __name__ == "__main__":
    test_set_locale_survives_timeout()
    test_start_all_survives_single_failure()
    print("OK: оба теста прошли")
