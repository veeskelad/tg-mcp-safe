#!/usr/bin/env bash
# Новая сессия Telegram для tg-mcp-safe. Запускать в терминале: sudo deploy/rotate-session.sh [--qr [--light]]
# Без флага спросит телефон, код из Telegram и пароль 2FA; с --qr покажет QR-код для приложения. Новая строка сессии не печатается и не пишется
# на диск открытым текстом: из памяти Python сразу в systemd-creds encrypt.
# После проверки — завершить старую сессию: Telegram → Настройки → Устройства.
set -euo pipefail
CRED=${TG_CRED:-/etc/tg-mcp-safe/telegram.env.cred}
UNIT=${TG_UNIT:-tg-mcp-safe}
PY=${TG_PY:-/opt/tg-mcp-safe/venv/bin/python}
PROG=${TG_PROG:-/opt/tg-mcp-safe/src/deploy/new_session.py}
WAIT=${TG_WAIT:-15}
[[ $EUID -eq 0 ]] || { echo "запускать через sudo"; exit 1; }
[[ -f "$CRED" ]] || { echo "нет $CRED"; exit 1; }
NEW=$(mktemp "$(dirname "$CRED")/.new.XXXXXX"); trap 'rm -f "$NEW"' EXIT
# fd 3 программы → encrypt; её stdout → stderr (терминал).
systemd-creds decrypt --name=telegram.env "$CRED" - \
  | "$PY" -I "$PROG" "$@" 3>&1 1>&2 \
  | systemd-creds encrypt --name=telegram.env - "$NEW"
systemd-creds decrypt --name=telegram.env "$NEW" - | grep -q '^TELEGRAM_SESSION_STRING=.' \
  || { echo "новая сессия не записана, ничего не меняю"; exit 1; }
cp -p "$CRED" "$CRED.prev"
chmod 600 "$NEW"; mv "$NEW" "$CRED"
systemctl restart "$UNIT" || true; sleep "$WAIT"
if systemctl is-active -q "$UNIT"; then
  shred -u "$CRED.prev"
  echo "готово: $UNIT на новой сессии. Теперь завершите старую: Telegram → Настройки → Устройства"
else
  mv "$CRED.prev" "$CRED"; systemctl restart "$UNIT" || true
  echo "$UNIT не поднялся на новой сессии, вернул старую"; exit 1
fi
