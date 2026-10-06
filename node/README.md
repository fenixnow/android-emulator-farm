# Mac mini — нода фермы (remote execution)

Машина: `user@87.228.10.20` (arm64, macOS 27.0.1), хостнейм `Mac-mini-user.local`.
Роль: **node** (iOS-симуляторы). Hub: Ubuntu `135.106.142.247:4723` (ферма `appium-server.service`).

## Файлы

- `node-config.json` — шаблон конфига Appium-ноды (`platform: ios`). **Секреты
  (`accessKey`, `token`) хранятся только на маке** (`~/device-farm-node/node-config.json`),
  в git не попадают.
- `com.appium.device-farm-node.plist` — LaunchAgent ноды (автозапуск + KeepAlive).
- `boot-farm-simulators.sh` + `com.farm.simulators-keepalive.plist` — держат все
  farm-симуляторы запущенными (проверка раз в 60 сек, бут Shutdown-симуляторов).
- Учётка ноды на хабе: юзер `mac-node` (admin). Пароль у Андрея.

## Установлено на маке

- Xcode Command Line Tools 27.0 (git 2.54.0)
- Homebrew 7.0.8 (`/opt/homebrew`)
- Node.js 24.21.0 (`/opt/homebrew/opt/node@24`)
- Xcode 27.0 + iOS 27.0 runtime, CLT 27.0
- Appium 3.8.0 + плагин `appium-device-farm@11.3.2` (как на хабе) + драйверы
  `uiautomator2@8.1.0`, `xcuitest@12.15.0`
- ffmpeg 9.0.2, applesimutils (tap wix/brew, требует `brew trust`), mjpeg-consumer
- 4 симулятора «iPhone 16 Pro (farm-1..4)» (iPhone17,1, iOS 27.0)

## Запуск

```bash
appium server -ka 800 --use-plugins=device-farm --config ~/device-farm-node/node-config.json -pa /wd/hub
```

## Как подключается нода

Поля `hub` + `accessKey` + `token` в секции `plugin.device-farm` — нода регистрируется
на хабе (на хабе включена `enableAuthentication: true`). Дашборд устройств: на хабе
`http://135.106.142.247:4723/device-farm`.

