#!/usr/bin/env python3
"""
Android Emulator Farm Manager
------------------------------
Запускает несколько Android-эмуляторов на «голой» Linux-машине (без Docker)
и следит за их доступностью, перезапуская упавшие/зависшие инстансы.

Использование:
    python3 emulator_manager.py create          # создать все AVD из конфига
    python3 emulator_manager.py start           # запустить все эмуляторы из конфига (с автосозданием AVD)
    python3 emulator_manager.py stop            # остановить все эмуляторы
    python3 emulator_manager.py watch           # запустить watchdog (бесконечный цикл)
    python3 emulator_manager.py status          # разовая проверка статуса

Требования:
    - Android SDK cmdline-tools, platform-tools и emulator в PATH
      (ANDROID_HOME/ANDROID_SDK_ROOT настроены)
    - AVD можно создать через avdmanager / Android Studio или через `python3 emulator_manager.py create`
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


# =============================================================================
# Датаклассы
# =============================================================================

@dataclass
class AvdSpec:
    """Описание AVD: что создавать (образ системы, профиль устройства, железо)."""
    name: str
    device: str = "pixel_6"
    system_image: str = ""
    sdcard: str = ""
    hw: dict = field(default_factory=dict)


@dataclass
class EmulatorSpec:
    """Описание экземпляра эмулятора: что запускать (порт, флаги, локаль, APK)."""
    name: str
    avd: str                         # имя AVD (ссылка на AvdSpec.name)
    port: int
    extra_args: str = ""
    writable_system: bool = False
    wipe_data: bool = False
    no_snapshot_save: bool = True
    locale: str = ""
    prop: dict = field(default_factory=dict)  # -prop key=value для эмулятора (ro.product.model и т.д.)
    pid: Optional[int] = None
    restart_count: int = field(default=0)

    @property
    def serial(self) -> str:
        return f"emulator-{self.port}"


# =============================================================================
# Утилиты
# =============================================================================

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


# =============================================================================
# EmulatorManager
# =============================================================================

class EmulatorManager:
    def __init__(self, config: dict):
        self.cfg = config
        self.wcfg = config.get("watchdog", {})
        self.avd_specs = [AvdSpec(**e) for e in config.get("avds", [])]
        self.specs = [EmulatorSpec(**e) for e in config.get("emulators", [])]
        self.log = setup_logging(BASE_DIR / self.wcfg.get("log_file", "logs/watchdog.log"))
        self.state_file = BASE_DIR / self.wcfg.get("state_file", "logs/state.json")
        self.poll_interval = int(self.wcfg.get("poll_interval_sec", 30))
        self.boot_timeout = int(self.wcfg.get("boot_timeout_sec", 180))
        self.adb_timeout = int(self.wcfg.get("adb_timeout_sec", 5))
        self.max_restart_attempts = int(self.wcfg.get("max_restart_attempts", 3))
        self.auto_create = bool(self.wcfg.get("auto_create", True))
        self.apk_dir = BASE_DIR / self.wcfg.get("apk_dir", "apks")
        self._stop = False
        signal.signal(signal.SIGINT, self._handle_sigterm)
        signal.signal(signal.SIGTERM, self._handle_sigterm)

    def _handle_sigterm(self, *_):
        self.log.info("Получен сигнал остановки, завершаю watchdog...")
        self._stop = True

    # ---------- низкоуровневые утилиты ----------

    def _run(self, cmd: str, timeout: Optional[int] = None,
             shell: bool = False) -> subprocess.CompletedProcess:
        """Выполняет команду. При shell=True используется bash для пайпов."""
        if shell:
            return subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, shell=True,
            )
        return subprocess.run(
            shlex.split(cmd), capture_output=True, text=True, timeout=timeout,
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

    # ---------- AVD: поиск и утилиты ----------

    def get_avd_spec(self, name: str) -> Optional[AvdSpec]:
        """Находит AvdSpec по имени."""
        for a in self.avd_specs:
            if a.name == name:
                return a
        return None

    def list_avds(self) -> dict[str, str]:
        """Возвращает {имя_AVD: путь} из вывода `avdmanager list avd`."""
        try:
            res = self._run("avdmanager list avd", timeout=self.adb_timeout)
        except subprocess.TimeoutExpired:
            self.log.warning("avdmanager list avd завис по таймауту")
            return {}
        avds = {}
        current_name = None
        for line in res.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("Name:"):
                current_name = stripped.split("Name:")[1].strip()
            elif stripped.startswith("Path:") and current_name:
                avds[current_name] = stripped.split("Path:")[1].strip()
        return avds

    def is_avd_exists(self, name: str) -> bool:
        """Проверяет, существует ли AVD с указанным именем."""
        return name in self.list_avds()

    def get_avd_config_path(self, name: str) -> Optional[Path]:
        """Возвращает путь к config.ini для AVD."""
        avds = self.list_avds()
        avd_dir = avds.get(name)
        if not avd_dir:
            return None
        return Path(avd_dir) / "config.ini"

    # ---------- AVD: создание ----------

    def is_system_image_installed(self, avd_spec: AvdSpec) -> bool:
        """Проверяет, установлен ли system image."""
        if not avd_spec.system_image:
            return False
        try:
            res = self._run("sdkmanager --list", timeout=self.adb_timeout * 3)
        except subprocess.TimeoutExpired:
            self.log.warning("sdkmanager --list завис по таймауту")
            return False
        return avd_spec.system_image in res.stdout

    def install_system_image(self, avd_spec: AvdSpec) -> bool:
        """Устанавливает system image через sdkmanager."""
        if not avd_spec.system_image:
            self.log.error(f"{avd_spec.name}: не указан system_image")
            return False
        self.log.info(f"{avd_spec.name}: устанавливаю system image {avd_spec.system_image}...")
        try:
            res = self._run(
                f"yes | sdkmanager --install {avd_spec.system_image}",
                timeout=600,
                shell=True,
            )
            ok = res.returncode == 0
            if ok:
                self.log.info(f"{avd_spec.name}: system image установлен")
            else:
                self.log.error(f"{avd_spec.name}: ошибка установки system image: {res.stderr}")
            return ok
        except subprocess.TimeoutExpired:
            self.log.error(f"{avd_spec.name}: таймаут установки system image")
            return False

    def patch_avd_config(self, avd_spec: AvdSpec) -> bool:
        """Патчит config.ini AVD кастомными параметрами из avd_spec.hw."""
        if not avd_spec.hw:
            return True

        config_path = self.get_avd_config_path(avd_spec.name)
        if not config_path or not config_path.exists():
            self.log.error(f"{avd_spec.name}: не найден config.ini для патча ({config_path})")
            return False

        self.log.info(f"{avd_spec.name}: патчу config.ini ({len(avd_spec.hw)} параметров)")
        lines = config_path.read_text(encoding="utf-8").splitlines()
        patched_keys = set()

        new_lines = []
        for line in lines:
            key = line.split("=")[0].strip() if "=" in line else ""
            if key in avd_spec.hw:
                new_lines.append(f"{key}={avd_spec.hw[key]}")
                patched_keys.add(key)
            else:
                new_lines.append(line)

        for key, value in avd_spec.hw.items():
            if key not in patched_keys:
                new_lines.append(f"{key}={value}")

        config_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        self.log.info(f"{avd_spec.name}: config.ini обновлён ({len(patched_keys)} замен, "
                       f"{len(avd_spec.hw) - len(patched_keys)} добавлено)")
        return True

    def create_avd(self, avd_spec: AvdSpec) -> bool:
        """Создаёт AVD, если он ещё не существует. Затем патчит config.ini."""
        if self.is_avd_exists(avd_spec.name):
            self.log.info(f"AVD {avd_spec.name} уже существует, пропускаю создание")
            return True

        if not avd_spec.system_image:
            self.log.error(f"{avd_spec.name}: не указан system_image")
            return False

        if not self.is_system_image_installed(avd_spec):
            if not self.install_system_image(avd_spec):
                return False

        self.log.info(f"{avd_spec.name}: создаю AVD "
                       f"(device={avd_spec.device}, image={avd_spec.system_image})")
        cmd = (f"yes '' | avdmanager create avd -n {avd_spec.name} "
               f'-k "{avd_spec.system_image}" -d {avd_spec.device} --force')
        if avd_spec.sdcard:
            cmd += f" -c {avd_spec.sdcard}"

        try:
            res = self._run(cmd, timeout=120, shell=True)
        except subprocess.TimeoutExpired:
            self.log.error(f"{avd_spec.name}: таймаут создания AVD")
            return False

        if res.returncode == 0 and self.is_avd_exists(avd_spec.name):
            self.log.info(f"{avd_spec.name}: AVD успешно создан")
            self.patch_avd_config(avd_spec)
            return True
        else:
            self.log.error(f"{avd_spec.name}: ошибка создания AVD: {res.stdout}\n{res.stderr}")
            return False

    def create_all(self) -> None:
        """Создаёт все AVD из секции avds."""
        if not self.avd_specs:
            self.log.warning("В конфиге нет секции 'avds', нечего создавать")
            return
        for avd_spec in self.avd_specs:
            self.create_avd(avd_spec)

    # ---------- локаль и APK ----------

    def set_locale(self, spec: EmulatorSpec) -> None:
        """Устанавливает системную локаль через setprop persist.sys.locale + framework restart."""
        if not spec.locale:
            return
        self.log.info(f"{spec.name}: устанавливаю локаль {spec.locale}")

        root_res = self._run(f"adb -s {spec.serial} root", timeout=self.adb_timeout)
        if "restarting adbd as root" in root_res.stdout or "restarting adbd as root" in root_res.stderr:
            self.log.info(f"{spec.name}: adbd перезапускается в root-режиме, ожидание...")
            time.sleep(5)
            self._run(f"adb -s {spec.serial} wait-for-device", timeout=30)

        self.log.info(f"{spec.name}: применяю persist.sys.locale={spec.locale}, перезапускаю framework")
        self._run(
            f"adb -s {spec.serial} shell 'setprop persist.sys.locale {spec.locale}; stop; sleep 5; start'",
            timeout=30,
        )
        time.sleep(8)
        self._run(f"adb -s {spec.serial} wait-for-device", timeout=60)
        self.log.info(f"{spec.name}: локаль {spec.locale} применена")

    def install_apks(self, spec: EmulatorSpec) -> None:
        """Устанавливает все APK из apk_dir на эмулятор."""
        if not self.apk_dir.exists():
            return
        apk_files = sorted(self.apk_dir.glob("*.apk"))
        if not apk_files:
            return
        self.log.info(f"{spec.name}: устанавливаю APK ({len(apk_files)} шт.) из {self.apk_dir}")
        for apk_file in apk_files:
            self.log.info(f"{spec.name}: устанавливаю {apk_file.name}")
            try:
                res = self._run(
                    f"adb -s {spec.serial} install -r {apk_file}",
                    timeout=120,
                )
                if res.returncode == 0 or "Success" in res.stdout:
                    self.log.info(f"{spec.name}: {apk_file.name} установлен успешно")
                else:
                    self.log.error(f"{spec.name}: ошибка установки {apk_file.name}: {res.stdout} {res.stderr}")
            except subprocess.TimeoutExpired:
                self.log.error(f"{spec.name}: таймаут установки {apk_file.name}")

    # ---------- запуск / остановка ----------

    def start_one(self, spec: EmulatorSpec) -> None:
        self.log.info(f"Запускаю эмулятор {spec.name} (AVD={spec.avd}, port={spec.port})")
        log_path = BASE_DIR / "logs" / f"{spec.name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        flags = [f"-avd {spec.avd}", f"-port {spec.port}"]
        if spec.writable_system:
            flags.append("-writable-system")
        if spec.wipe_data:
            flags.append("-wipe-data")
        if spec.no_snapshot_save:
            flags.append("-no-snapshot-save")
        for key, value in spec.prop.items():
            flags.append(f"-prop {key}={value}")
        if spec.extra_args:
            flags.append(spec.extra_args)

        cmd = f"emulator {' '.join(flags)}"
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
        if self._wait_for_device(spec):
            self.set_locale(spec)
            self.install_apks(spec)

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
            if self.auto_create:
                avd_spec = self.get_avd_spec(spec.avd)
                if avd_spec and not self.is_avd_exists(spec.avd):
                    self.log.info(f"{spec.name}: AVD {spec.avd} не найден, создаю...")
                    if not self.create_avd(avd_spec):
                        self.log.error(f"{spec.name}: не удалось создать AVD {spec.avd}, пропускаю запуск")
                        continue
            self.start_one(spec)
            time.sleep(2)

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
        # первый цикл: запустить все эмуляторы, которые ещё не запущены
        devices = self.adb_devices()
        for spec in self.specs:
            if spec.serial not in devices or devices[spec.serial] != "device":
                self.log.info(f"{spec.name} не запущен, запускаю...")
                if self.auto_create:
                    avd_spec = self.get_avd_spec(spec.avd)
                    if avd_spec and not self.is_avd_exists(spec.avd):
                        self.create_avd(avd_spec)
                self.start_one(spec)
                time.sleep(2)

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
            # спим с прерыванием по сигналу (проверка каждую секунду)
            for _ in range(self.poll_interval):
                if self._stop:
                    break
                time.sleep(1)
        self.log.info("Watchdog остановлен")

    def status(self) -> None:
        devices = self.adb_devices()
        for spec in self.specs:
            st = devices.get(spec.serial, "НЕ НАЙДЕН")
            print(f"{spec.name:10s} {spec.serial:16s} avd={spec.avd:25s} status={st}")


# =============================================================================
# CLI
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Android Emulator Farm Manager")
    parser.add_argument(
        "action", choices=["start", "stop", "watch", "status", "create"], help="Действие"
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
    elif args.action == "create":
        manager.create_all()


if __name__ == "__main__":
    main()
