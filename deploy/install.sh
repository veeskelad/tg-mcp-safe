#!/usr/bin/env bash
# Установка/обновление tg-mcp-safe. Запуск: sudo deploy/install.sh [--move-credentials SRC]
# Значения секретов не печатает: файл переносится целиком.
set -euo pipefail
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX=/opt/tg-mcp-safe
CONF="$SRC_DIR/deploy/site.conf"
[[ -f "$CONF" ]] || { echo "нет $CONF (см. deploy/site.conf.example)"; exit 1; }
# shellcheck source=/dev/null
source "$CONF"
DL="$DOWNLOAD_DIR"

id tgmcp >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin tgmcp
command -v setfacl >/dev/null || apt-get install -y -qq acl

# Код и venv принадлежат root: процессы пользователя не подменят их без sudo.
install -d -m 0755 "$PREFIX" "$PREFIX/src"
rsync -a --delete --exclude .venv --exclude .git --exclude __pycache__ --exclude .tasks --exclude .pytest_cache "$SRC_DIR/" "$PREFIX/src/"
chown -R root:root "$PREFIX/src"
UV_PROJECT_ENVIRONMENT="$PREFIX/venv" UV_PYTHON_DOWNLOADS=never "$UV" sync --project "$PREFIX/src" --frozen --no-dev --no-editable --python /usr/bin/python3.12 -q
chown -R root:root "$PREFIX/venv"

# Чтение разрешённых папок для tgmcp — через ACL, права остальных не меняются.
for r in "${ROOTS[@]}"; do
  setfacl -R -m u:tgmcp:rX "$r"
  find "$r" -type d -exec setfacl -m d:u:tgmcp:rX {} +
done
install -d -m 2770 -o tgmcp -g "$DOWNLOAD_GROUP" "$DL"

install -d -m 0700 -o root -g root /etc/tg-mcp-safe
CRED=/etc/tg-mcp-safe/telegram.env.cred
if [[ "${1:-}" == "--move-credentials" ]]; then
  # Шифруем и удаляем открытый файл: на диске остаётся только шифротекст.
  systemd-creds encrypt --name=telegram.env "$2" "$CRED"
  chmod 0600 "$CRED"
  shred -u "$2"
  echo "credentials: зашифрованы в $CRED, исходный файл удалён"
fi
[[ -f "$CRED" ]] || { echo "нет $CRED: запустите с --move-credentials <telegram.env>"; exit 1; }

sed -e "s#@ROOTS@#${ROOTS[*]}#g" -e "s#@DOWNLOAD_DIR@#$DL#g" "$SRC_DIR/deploy/tg-mcp-safe.service.in" > /etc/systemd/system/tg-mcp-safe.service
chmod 0644 /etc/systemd/system/tg-mcp-safe.service
systemctl daemon-reload
echo "готово: systemctl start tg-mcp-safe"
