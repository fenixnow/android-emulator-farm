#!/bin/zsh
# Boot всех farm-симуляторов, которые не запущены.
# Запускается LaunchAgent'ом com.farm.simulators-keepalive каждую минуту.
export PATH=/usr/bin:/bin

xcrun simctl list devices | grep "(farm-" | while read -r line; do
  udid=$(echo "$line" | grep -oE "[0-9A-F]{8}-([0-9A-F]{4}-){3}[0-9A-F]{12}")
  if echo "$line" | grep -q "(Shutdown)"; then
    xcrun simctl boot "$udid" 2>/dev/null && echo "$(date '+%F %T') booted $udid"
  fi
done
