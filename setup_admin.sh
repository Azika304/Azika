#!/bin/bash
# Установка админ-бота @azikaadmin_bot. Запускать на сервере: bash setup_admin.sh
# Токен вводится здесь же (вводимые символы не видны) — в чат его не отправляй!
set -e
[ "$(id -u)" = 0 ] || { echo "Запусти от root"; exit 1; }
[ -x /opt/azika/venv/bin/python ] || { echo "Нет /opt/azika/venv — сначала должен стоять Azika VPN"; exit 1; }
[ -f /opt/azika_shop/shop.db ] || echo "⚠️ Не вижу /opt/azika_shop/shop.db — проверь, что магазин уже запускался"
SRC="$(cd "$(dirname "$0")" && pwd)"
mkdir -p /opt/azika_admin
if [ -f "$SRC/admin_bot.py" ]; then cp "$SRC/admin_bot.py" /opt/azika_admin/admin_bot.py; fi
[ -f /opt/azika_admin/admin_bot.py ] || { echo "Нет admin_bot.py рядом со скриптом"; exit 1; }

if [ ! -s /etc/azika_admin.env ] || ! grep -q '^ADMIN_BOT_TOKEN=.\+' /etc/azika_admin.env; then
  read -r -s -p "Токен @azikaadmin_bot из BotFather (символы не видны): " TOK </dev/tty; echo
  [ -n "$TOK" ] || { echo "Пустой токен"; exit 1; }
  printf 'ADMIN_BOT_TOKEN=%s\nVPN_DB=/opt/azika/azika_vpn.db\n' "$TOK" > /etc/azika_admin.env
  chmod 600 /etc/azika_admin.env
fi

cat > /etc/systemd/system/azika-admin.service <<SVC
[Unit]
Description=Azika Admin bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/azika_admin
EnvironmentFile=/etc/azika_shop.env
EnvironmentFile=/etc/azika_admin.env
ExecStart=/opt/azika/venv/bin/python /opt/azika_admin/admin_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVC
systemctl daemon-reload
systemctl enable azika-admin >/dev/null 2>&1
systemctl restart azika-admin
sleep 4
systemctl is-active azika-admin
journalctl -u azika-admin -n 6 --no-pager | cut -c1-170
echo
echo "Готово. Открой @azikaadmin_bot и нажми /start (доступ только у ADMIN_ID из /etc/azika_shop.env)."
