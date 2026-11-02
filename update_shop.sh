#!/bin/bash
# Обновление ботов с телефона: скачивает свежие файлы из GitHub (azika304/Azika), проверяет синтаксис,
# делает копию старых и перезапускает нужные сервисы. Если файла нет на GitHub — пропускает.
BASE="${BASE:-https://raw.githubusercontent.com/azika304/Azika/main}"
TS=$(date +%Y%m%d_%H%M%S)
RESTART=""
# файл : папка : сервис
for item in "shop_bot.py:/opt/azika_shop:azika-shop" "support_bot.py:/opt/azika_shop:azika-shop-support" \
            "bot.py:/opt/azika:azika-bot" "admin_bot.py:/opt/azika_admin:azika-admin"; do
  f="${item%%:*}"; rest="${item#*:}"; d="${rest%%:*}"; svc="${rest#*:}"
  [ -d "$d" ] || continue
  if curl -fsSL "$BASE/$f?$TS" -o "/tmp/$f.new"; then
    if python3 -m py_compile "/tmp/$f.new" 2>/dev/null; then
      if [ -f "$d/$f" ] && cmp -s "/tmp/$f.new" "$d/$f"; then echo "= $f без изменений"; continue; fi
      mkdir -p "$d/backup"; [ -f "$d/$f" ] && cp "$d/$f" "$d/backup/$f.$TS"
      cp "/tmp/$f.new" "$d/$f"; echo "✅ $f обновлён (копия: $d/backup/$f.$TS)"
      case " $RESTART " in *" $svc "*) ;; *) RESTART="$RESTART $svc";; esac
    else
      echo "❌ $f: в файле ошибка, оставил старый"
    fi
  else
    echo "⚠️ $f не скачался с GitHub — пропускаю"
  fi
done
if [ -n "$RESTART" ]; then
  systemctl restart $RESTART 2>/dev/null
  sleep 4
  echo "--- перезапущено:$RESTART ---"
  for s in $RESTART; do echo "$s: $(systemctl is-active $s)"; done
else
  echo "Нечего перезапускать"
fi
