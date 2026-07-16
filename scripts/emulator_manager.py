#!/usr/bin/env python3
"""
Android Emulator Farm Manager
------------------------------
Запускает несколько Android-эмуляторов на «голой» Linux-машине (без Docker)
и следит за их доступностью, перезапуская упавшие/зависшие инстансы.

Использование:
    python3 emulator_manager.py start           # запустить все эмуляторы из конфига
    python3 emulator_manager.py stop            # остановить все эмуляторы
    python3 emulator_manager.py watch           # запустить watchdog (бесконечный цикл)
    python3 emulator_manager.py status          # разовая проверка статуса

Требования:
    - Android SDK cmdline-tools, platform-tools и emulator в PATH
      (ANDROID_HOME/ANDROID_SDK_ROOT настроены)
    - AVD-образы созданы заранее через avdmanager / Android Studio
    - pip install pyyaml
"""

import argparse
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    sys.exit("Нужен пакет pyyaml: pip install pyyaml")

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "emulators.yaml"


@dataclass
class EmulatorSpec:
    name: str
    avd: str
    port: int
    extra_args: str = ""
    pid: Optional[int] = None
    restart_count: int = field(default=0)

    @property
    def serial(self) -> str:
        return f"emulator-{self.port}"


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(log_file: Path) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("emulator_manager")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


class EmulatorManager:
    def __init__(self, config: dict):
        self.cfg = config
        self.wcfg = config.get("watchdog", {})
        self.specs = [EmulatorSpec(**e) for e in config["emulators"]]
        self.log = setup_logging(BASE_DIR / self.wcfg.get("log_file", "logs/watchdog.log"))
        self.state_file = BASE_DIR / self.wcfg.get("state_file", "logs/state.json")
        self.poll_interval = int(self.wcfg.get("poll_interval_sec", 30))
        self.boot_timeout = int(self.wcfg.get("boot_timeout_sec", 180))
        self.adb_timeout = int(self.wcfg.get("adb_timeout_sec", 5))
        self.max_restart_attempts = int(self.wcfg.get("max_restart_attempts", 3))
        self._stop = False
        signal.signal(signal.SIGINT, self._handle_sigterm)
        signal.signal(signal.SIGTERM, self._handle_sigterm)

    def _handle_sigterm(self, *_):
        self.log.info("Получен сигнал остановки, завершаю watchdog...")
        self._stop = True

    # ---------- низкоуровневые утилиты ----------

    def _run(self, cmd: str, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            shlex.split(cmd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def adb_devices(self) -> dict:
        """Возвращает {serial: status} по данным `adb devices`."""
        try:
            res = self._run("adb devices", timeout=self.adb_timeout)
        except subprocess.TimeoutExpired:
            self.log.warning("adb devices завис по таймауту")
            return {}
        devices = {}
        for line in res.stdout.strip().splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                devices[parts[0]] = parts[1]
        return devices

    def is_fully_booted(self, spec: EmulatorSpec) -> bool:
        """Проверяет sys.boot_completed через adb shell."""
        try:
            res = self._run(
                f"adb -s {spec.serial} shell getprop sys.boot_completed",
                timeout=self.adb_timeout,
            )
            return res.stdout.strip() == "1"
        except subprocess.TimeoutExpired:
            return False

    # ---------- запуск / остановка ----------

    def start_one(self, spec: EmulatorSpec) -> None:
        self.log.info(f"Запускаю эмулятор {spec.name} (AVD={spec.avd}, port={spec.port})")
        log_path = BASE_DIR / "logs" / f"{spec.name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = f"emulator -avd {spec.avd} -port {spec.port} {spec.extra_args}".strip()
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write(f"\n--- start {datetime.now().isoformat()} ---\n")
            proc = subprocess.Popen(
                shlex.split(cmd),
                stdout=lf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        spec.pid = proc.pid
        self.log.info(f"{spec.name} запущен, pid={proc.pid}, ожидаю появления в adb devices")
        self._wait_for_device(spec)

    def _wait_for_device(self, spec: EmulatorSpec) -> bool:
        deadline = time.time() + self.boot_timeout
        while time.time() < deadline:
            devices = self.adb_devices()
            if devices.get(spec.serial) == "device" and self.is_fully_booted(spec):
                self.log.info(f"{spec.name} ({spec.serial}) успешно загрузился")
                return True
            time.sleep(5)
        self.log.error(f"{spec.name} ({spec.serial}) не загрузился за {self.boot_timeout}s")
        return False

    def stop_one(self, spec: EmulatorSpec) -> None:
        self.log.info(f"Останавливаю {spec.name} ({spec.serial})")
        try:
            self._run(f"adb -s {spec.serial} emu kill", timeout=self.adb_timeout)
        except subprocess.TimeoutExpired:
            pass
        if spec.pid:
            try:
                os.killpg(os.getpgid(spec.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass

    def start_all(self) -> None:
        for spec in self.specs:
            self.start_one(spec)
            time.sleep(2)  # небольшая пауза между запусками, чтобы не перегружать KVM/CPU

    def stop_all(self) -> None:
        for spec in self.specs:
            self.stop_one(spec)

    # ---------- watchdog ----------

    def check_health(self, spec: EmulatorSpec) -> bool:
        """True если эмулятор жив и отвечает."""
        devices = self.adb_devices()
        status = devices.get(spec.serial)
        if status != "device":
            self.log.warning(f"{spec.name} ({spec.serial}) статус в adb: {status!r}")
            return False
        try:
            res = self._run(f"adb -s {spec.serial} shell echo ok", timeout=self.adb_timeout)
            return res.returncode == 0 and "ok" in res.stdout
        except subprocess.TimeoutExpired:
            self.log.warning(f"{spec.name} ({spec.serial}) не отвечает на adb shell (timeout)")
            return False

    def restart(self, spec: EmulatorSpec) -> None:
        spec.restart_count += 1
        if spec.restart_count > self.max_restart_attempts:
            self.log.error(
                f"{spec.name}: превышен лимит перезапусков ({self.max_restart_attempts}). "
                f"Требуется вмешательство оператора."
            )
            return
        self.log.warning(
            f"Перезапускаю {spec.name} (попытка {spec.restart_count}/{self.max_restart_attempts})"
        )
        self.stop_one(spec)
        time.sleep(3)
        self.start_one(spec)

    def dump_state(self) -> None:
        state = {
            "timestamp": datetime.now().isoformat(),
            "emulators": [
                {
                    "name": s.name,
                    "serial": s.serial,
                    "pid": s.pid,
                    "restart_count": s.restart_count,
                }
                for s in self.specs
            ],
        }
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    def watch(self) -> None:
        self.log.info(
            f"Watchdog запущен: poll={self.poll_interval}s, "
            f"boot_timeout={self.boot_timeout}s, max_restarts={self.max_restart_attempts}"
        )
        while not self._stop:
            for spec in self.specs:
                healthy = self.check_health(spec)
                if healthy:
                    if spec.restart_count:
                        self.log.info(f"{spec.name} восстановился, сбрасываю счётчик перезапусков")
                    spec.restart_count = 0
                else:
                    self.restart(spec)
            self.dump_state()
            time.sleep(self.poll_interval)
        self.log.info("Watchdog остановлен")

    def status(self) -> None:
        devices = self.adb_devices()
        for spec in self.specs:
            st = devices.get(spec.serial, "НЕ НАЙДЕН")
            print(f"{spec.name:10s} {spec.serial:16s} avd={spec.avd:25s} status={st}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Android Emulator Farm Manager")
    parser.add_argument(
        "action", choices=["start", "stop", "watch", "status"], help="Действие"
    )
    parser.add_argument(
        "--config", default=str(CONFIG_PATH), help="Путь к YAML-конфигу"
    )
    args = parser.parse_args()

    config = load_config(Path(args.config))
    manager = EmulatorManager(config)

    if args.action == "start":
        manager.start_all()
    elif args.action == "stop":
        manager.stop_all()
    elif args.action == "watch":
        manager.watch()
    elif args.action == "status":
        manager.status()


if __name__ == "__main__":
    main()
