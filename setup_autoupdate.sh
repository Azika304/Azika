#!/bin/bash
# Автообновление Azika: каждые 5 минут тянет файлы из GitHub и перезапускает сервисы только если что-то изменилось.
set -e
[ "$(id -u)" = 0 ] || { echo "Запусти от root (sudo bash)"; exit 1; }
URL=https://raw.githubusercontent.com/azika304/Azika/main/update_shop.sh
cat > /etc/systemd/system/azika-autoupdate.service <<UNIT
[Unit]
Description=Azika autoupdate from GitHub
After=network-online.target
[Service]
Type=oneshot
ExecStart=/bin/bash -c 'curl -fsSL $URL | bash'
UNIT
cat > /etc/systemd/system/azika-autoupdate.timer <<UNIT
[Unit]
Description=Azika autoupdate every 5 min
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Unit=azika-autoupdate.service
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now azika-autoupdate.timer
echo "Готово. Теперь достаточно загрузить файлы в GitHub — сервер обновится сам за ~5 минут."
