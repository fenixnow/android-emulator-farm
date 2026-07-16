# Android Emulator Farm (native, без Docker)

Проект для запуска нескольких Android-эмуляторов напрямую на Linux-машине через
Android SDK (`emulator`, `adb`) — без дополнительного слоя виртуализации в виде
Docker — и сервис-watchdog, который следит за их доступностью и автоматически
перезапускает упавшие/зависшие инстансы.

## Структура проекта

```
android-emulator-farm/
├── config/
│   └── emulators.yaml        # список эмуляторов (avd, port, доп. флаги) + настройки watchdog
├── scripts/
│   └── emulator_manager.py   # основной Python-скрипт: start/stop/watch/status
├── systemd/
│   └── android-emulator-watchdog.service
├── logs/                     # логи и state.json (создаются автоматически)
├── requirements.txt
└── README.md
```

## Требования

- Linux-машина с установленным Android SDK: `cmdline-tools`, `platform-tools`, `emulator`
- Созданные AVD (через `avdmanager create avd ...` или Android Studio)
- Аппаратное ускорение `/dev/kvm` рекомендуется (проверка: `grep -E -c '(vmx|svm)' /proc/cpuinfo`)
- Python 3.9+

```bash
pip install -r requirements.txt
```

## Настройка

Отредактируйте `config/emulators.yaml`:

```yaml
emulators:
  - name: emu1
    avd: Pixel_5_API_33      # имя существующего AVD
    port: 5554                # ADB serial будет emulator-5554
    extra_args: "-no-window -no-audio -no-boot-anim -gpu swiftshader_indirect"

watchdog:
  poll_interval_sec: 30
  boot_timeout_sec: 180
  adb_timeout_sec: 5
  max_restart_attempts: 3
```

Создать AVD, если их ещё нет:

```bash
sdkmanager "system-images;android-33;google_apis;x86_64"
avdmanager create avd -n Pixel_5_API_33 -k "system-images;android-33;google_apis;x86_64" -d pixel_5
```

## Использование

```bash
# запустить все эмуляторы из конфига
python3 scripts/emulator_manager.py start

# посмотреть статус
python3 scripts/emulator_manager.py status

# запустить watchdog (бесконечный цикл проверки + авторестарт)
python3 scripts/emulator_manager.py watch

# остановить все эмуляторы
python3 scripts/emulator_manager.py stop
```

## Как работает watchdog

1. Раз в `poll_interval_sec` секунд для каждого эмулятора выполняется `adb devices`
   и проверяется статус serial (`device`, `offline`, отсутствует).
2. Дополнительно шлётся `adb shell echo ok` с таймаутом `adb_timeout_sec` — если
   команда не отвечает, эмулятор считается зависшим, даже если ADB показывает `device`.
3. При обнаружении проблемы — `adb emu kill` + повторный запуск процесса `emulator`
   с тем же AVD и портом, ожидание `sys.boot_completed=1`.
4. Счётчик перезапусков на инстанс ограничен `max_restart_attempts` — после
   превышения лимита эмулятор помечается как проблемный в логе (требуется ручное
   вмешательство), чтобы не уйти в бесконечный цикл рестартов при системной проблеме
   (например, нехватка памяти или сломанный AVD).
5. Текущее состояние всех инстансов пишется в `logs/state.json` после каждого цикла
   проверки — можно использовать для внешнего мониторинга/алертинга.

## Запуск как systemd-сервис

```bash
sudo cp -r . /opt/android-emulator-farm
sudo cp systemd/android-emulator-watchdog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now android-emulator-watchdog.service
sudo systemctl status android-emulator-watchdog.service
journalctl -u android-emulator-watchdog.service -f
```

Отредактируйте пути `ANDROID_HOME`/`WorkingDirectory` в `.service`-файле под свою
установку SDK перед копированием в `/etc/systemd/system`.

## Возможные доработки

- Добавить экспорт метрик в формате Prometheus (`restart_count`, `boot_time_sec`) поверх `logs/state.json`.
- Добавить алертинг (Telegram/Slack webhook) при превышении `max_restart_attempts`.
- Параллельный запуск эмуляторов вместо последовательного (с учётом лимита CPU/KVM).
