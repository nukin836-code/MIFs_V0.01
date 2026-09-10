#!/data/data/com.termux/files/usr/bin/bash
#
# Поднимает Mini App backend (uvicorn + webapp_api.py) и пробрасывает его
# наружу через cloudflared quick tunnel (бесплатно, без аккаунта и домена —
# https://<случайные-слова>.trycloudflare.com). Дальше пишет свежий URL в
# .env как MINIAPP_URL, чтобы main.py при следующем запуске подхватил его
# и выставил кнопку меню бота (см. main.py: bot.set_chat_menu_button).
#
# ВАЖНО про quick tunnel: адрес меняется при каждом перезапуске этого
# скрипта. Это ок для личного использования — просто перезапускай бота
# после этого скрипта. Если нужен постоянный адрес, замени quick tunnel на
# именованный туннель с собственным доменом (cloudflared tunnel login /
# create / route dns) — тогда URL перестанет меняться и этот шаг вообще не
# понадобится.
#
# Установка зависимостей (один раз):
#   pkg install cloudflared tmux python
#   pip install -r requirements-webapp.txt
#
# Запуск:
#   bash start_miniapp.sh
# Остановка:
#   tmux kill-session -t mifki-api
#   tmux kill-session -t mifki-tunnel
#   tmux kill-session -t mifki-bot

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
TUNNEL_LOG="$PROJECT_DIR/cloudflared.log"
API_PORT="${MINIAPP_PORT:-8010}"

command -v cloudflared >/dev/null 2>&1 || { echo "❌ cloudflared не найден. Установи: pkg install cloudflared"; exit 1; }
command -v tmux >/dev/null 2>&1 || { echo "❌ tmux не найден. Установи: pkg install tmux"; exit 1; }

echo "▶ Останавливаю старые сессии backend'а и туннеля (если были)..."
tmux kill-session -t mifki-api 2>/dev/null || true
tmux kill-session -t mifki-tunnel 2>/dev/null || true
rm -f "$TUNNEL_LOG"

echo "▶ Запускаю backend (uvicorn) на 127.0.0.1:$API_PORT..."
tmux new-session -d -s mifki-api \
  "cd '$PROJECT_DIR' && uvicorn webapp_api:app --host 127.0.0.1 --port $API_PORT"

echo "▶ Запускаю cloudflared quick tunnel..."
tmux new-session -d -s mifki-tunnel \
  "cloudflared tunnel --url http://127.0.0.1:$API_PORT --logfile '$TUNNEL_LOG' --loglevel info"

echo "▶ Жду публичный адрес от cloudflared (до 30 секунд)..."
TUNNEL_URL=""
for _ in $(seq 1 30); do
    sleep 1
    if [ -f "$TUNNEL_LOG" ]; then
        TUNNEL_URL="$(grep -o 'https://[a-zA-Z0-9.-]*\.trycloudflare\.com' "$TUNNEL_LOG" | head -n1 || true)"
        [ -n "$TUNNEL_URL" ] && break
    fi
done

if [ -z "$TUNNEL_URL" ]; then
    echo "❌ Не удалось получить адрес туннеля за 30 секунд. Смотри лог: $TUNNEL_LOG"
    exit 1
fi

echo "✅ Приложение снаружи доступно по адресу: $TUNNEL_URL"

if [ -f "$ENV_FILE" ] && grep -q '^MINIAPP_URL=' "$ENV_FILE"; then
    sed -i "s#^MINIAPP_URL=.*#MINIAPP_URL=$TUNNEL_URL#" "$ENV_FILE"
else
    echo "MINIAPP_URL=$TUNNEL_URL" >> "$ENV_FILE"
fi
echo "▶ .env обновлён (MINIAPP_URL=$TUNNEL_URL)."

echo ""
echo "▶ Осталось перезапустить бота, чтобы кнопка меню обновилась на новый адрес:"
echo "    tmux kill-session -t mifki-bot 2>/dev/null"
echo "    tmux new-session -d -s mifki-bot \"cd '$PROJECT_DIR' && set -a && source .env && set +a && python main.py\""
