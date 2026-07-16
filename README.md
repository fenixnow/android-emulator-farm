# Android Emulator Farm (native, без Docker)

Проект для запуска нескольких Android-эмуляторов напрямую на Linux-машине через
Android SDK (`emulator`, `adb`) — без дополнительного слоя виртуализации в виде
Docker — и сервис-watchdog, который следит за их доступностью и автоматически
перезапускает упавшие/зависшие инстансы.

## Структура проекта

```
android-emulator-farm/
├── config/
│   └── emulators.yaml        # AVD (что создавать) + эмуляторы (что запускать) + watchdog
├── scripts/
│   └── emulator_manager.py   # основной Python-скрипт: create/start/stop/watch/status
├── apks/                     # APK-файлы для установки на все эмуляторы (автоматически)
├── systemd/
│   └── android-emulator-watchdog.service
├── logs/                     # логи и state.json (создаются автоматически)
├── requirements.txt
└── README.md
```

## Требования

- Linux-машина с установленным Android SDK: `cmdline-tools`, `platform-tools`, `emulator`
- Аппаратное ускорение `/dev/kvm` рекомендуется (проверка: `grep -E -c '(vmx|svm)' /proc/cpuinfo`)
- Python 3.9+

```bash
pip install -r requirements.txt
```

## Конфигурация

Конфиг разделён на три секции:

### `avds` — что создавать

```yaml
avds:
  - name: medium_phone               # имя AVD
    device: pixel_6                  # базовый профиль для avdmanager
    system_image: "system-images;android-34;google_apis;x86_64"
    sdcard: "512M"
    hw:                              # кастомные параметры железа → патч config.ini
      hw.lcd.width: 1080
      hw.lcd.height: 2400
      hw.lcd.density: 420
      hw.ramSize: 2048
      vm.heapSize: 192
      disk.dataPartition.size: "4G"
      hw.keyboard: "yes"
```

- `device` — любой профиль из `avdmanager list device`
- `hw` — параметры, которые будут записаны в `config.ini` AVD после создания
- Один AVD могут использовать несколько эмуляторов (системный образ read-only)

### `emulators` — что запускать

```yaml
emulators:
  - name: emu1
    avd: medium_phone               # ссылка на имя из avds
    port: 5554
    extra_args: "-no-window -no-audio -no-boot-anim -gpu swiftshader_indirect"
    writable_system: false
    wipe_data: false
    no_snapshot_save: true
    locale: "ru-RU"                 # опционально: установить локаль после загрузки
```

### `watchdog` — мониторинг

```yaml
watchdog:
  poll_interval_sec: 30
  boot_timeout_sec: 180
  adb_timeout_sec: 5
  max_restart_attempts: 3
  auto_create: true                 # автосоздание AVD при start, если не существует
  apk_dir: "apks"                   # все .apk из этой папки устанавливаются автоматически
  log_file: logs/watchdog.log
  state_file: logs/state.json
```

## APK-файлы

Все `.apk`-файлы из папки `apks/` автоматически устанавливаются на каждый эмулятор после загрузки. Порядок установки — алфавитный.

```bash
# положить APK в папку
cp ~/Downloads/app.apk apks/
```

## Использование

```bash
# создать все AVD из конфига (установит system image при необходимости)
python3 scripts/emulator_manager.py create

# запустить все эмуляторы (с автосозданием AVD при auto_create: true)
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
   вмешательство).
5. Текущее состояние всех инстансов пишется в `logs/state.json` после каждого цикла
   проверки — можно использовать для внешнего мониторинга/алертинга.

## Как работает создание AVD

1. Проверяется, существует ли AVD (`avdmanager list avd`).
2. Если нет — проверяется/устанавливается system image (`sdkmanager --install`).
3. Создаётся AVD на базе стандартного профиля (`avdmanager create avd -d <device>`).
4. `config.ini` патчится кастомными параметрами из секции `hw`.

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

- Алертинг (Telegram/Slack webhook) при превышении `max_restart_attempts`.
- Параллельный запуск эмуляторов вместо последовательного.
