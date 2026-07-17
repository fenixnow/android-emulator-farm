# Android Emulator Farm (native, без Docker)

Проект для запуска нескольких Android-эмуляторов напрямую на Linux-машине через
Android SDK (`emulator`, `adb`) — без дополнительного слоя виртуализации в виде
Docker — и сервис-watchdog, который следит за их доступностью и автоматически
перезапускает упавшие/зависшие инстансы.

## Возможности

- **Создание AVD** — автоматическая установка system image, создание AVD, патч `config.ini` под кастомное железо
- **Запуск эмуляторов** — 6 эмуляторов (2 типа устройств × 3 экземпляра), настройка локали `ru-RU`
- **Watchdog** — мониторинг здоровья, авторестарт упавших, `state.json` для внешнего мониторинга
- **Установка APK** — все `.apk` из папки `apks/` ставятся автоматически на каждый эмулятор
- **Device Farm** — дашборд `http://<host>:4723/device-farm`, управление устройствами через Appium
- **Плановый перезапуск** — cron-расписание внутри watchdog (сброс памяти, например в 5 утра)
- **Systemd-сервисы** — watchdog + appium как службы с автозапуском

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

### Эмуляторы + watchdog

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

**Правка и перезапуск сервиса:**

```bash
sudo nano /etc/systemd/system/android-emulator-watchdog.service
sudo systemctl daemon-reload
sudo systemctl restart android-emulator-watchdog.service
sudo systemctl status android-emulator-watchdog.service
journalctl -u android-emulator-watchdog.service -f
```

### Appium server + Device Farm

```bash
# 1. Установить Node.js
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.3/install.sh | bash
source ~/.bashrc
nvm install 22

# 2. Установить Appium глобально (от root с текущим PATH — попадёт в /usr/local/bin/)
sudo env "PATH=$PATH" npm install -g appium

# 3. Установить драйвер uiautomator2 и плагин Device Farm
appium driver install uiautomator2
appium plugin install --source=npm appium-device-farm

# 4. Создать пользователя и директорию
sudo useradd -r -s /bin/false appium
sudo mkdir -p /opt/appium /var/log/appium
sudo chown -R appium:appium /opt/appium /var/log/appium

# 5. Установить systemd-юнит
sudo cp systemd/appium-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now appium-server.service
sudo systemctl status appium-server.service
journalctl -u appium-server.service -f
```

Дашборд Device Farm: `http://<host>:4723/device-farm`

**Управление плагинами и драйверами:**

```bash
appium driver list                    # список драйверов
appium driver install uiautomator2    # установить
appium driver uninstall uiautomator2  # удалить
appium driver update uiautomator2     # обновить

appium plugin list                              # список плагинов
appium plugin install --source=npm appium-device-farm
appium plugin uninstall device-farm
appium plugin update device-farm
```

**Конфиг `config/appium-config.json`:**

```json
{
  "server": {
    "address": "0.0.0.0",
    "port": 4723,
    "base-path": "/wd/hub",
    "allow-cors": true,
    "session-override": true,
    "log-level": "info",
    "log": "/var/log/appium/appium-farm.log",
    "use-plugins": ["device-farm"],
    "plugin": {
      "device-farm": {
        "platform": "android"
      }
    }
  }
}
```

**Файл `systemd/appium-server.service`:**

```ini
[Unit]
Description=Appium Server for Android Test Automation Farm
After=android-emulator-watchdog.service
Wants=android-emulator-watchdog.service

[Service]
Type=simple
User=appium
Group=appium
WorkingDirectory=/opt/appium

Environment=ANDROID_HOME=/opt/android-sdk
Environment=ANDROID_SDK_ROOT=/opt/android-sdk
Environment=JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
Environment=PATH=/usr/local/bin:/usr/bin:/bin:/opt/android-sdk/emulator:/opt/android-sdk/platform-tools:/opt/android-sdk/cmdline-tools/latest/bin

ExecStart=/usr/local/bin/appium server --config /opt/android-emulator-farm/config/appium-config.json

Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

**Правка и перезапуск сервиса:**

```bash
# отредактировать unit-файл
sudo nano /etc/systemd/system/appium-server.service

# применить изменения и перезапустить
sudo systemctl daemon-reload
sudo systemctl restart appium-server.service

# проверить статус и логи
sudo systemctl status appium-server.service
journalctl -u appium-server.service -f
```

**Важно:** При подключении нескольких эмуляторов к одному Appium задавайте уникальный
`systemPort` в capabilities для каждого устройства (8200, 8201, …) во избежание конфликтов.

## Ежедневный перезапуск эмуляторов

Эмуляторы постепенно выедают память. Watchdog сам перезапускает их по cron-расписанию (поле `restart_cron`):

```yaml
watchdog:
  restart_cron: "0 5 * * *"     # каждый день в 5 утра ("" = отключено)
```

При срабатывании:
1. `stop_all()` — мягкая остановка через `adb emu kill`
2. `pkill -9 -f qemu-system-x86_64` — жёсткая зачистка зависших
3. `start_all()` — чистый старт

## Возможные доработки

- Алертинг (Telegram/Slack webhook) при превышении `max_restart_attempts`.
- Параллельный запуск эмуляторов вместо последовательного.
