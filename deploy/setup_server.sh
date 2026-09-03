#!/usr/bin/env bash
# Первичная настройка сервера (Ubuntu/Debian). Запускать один раз от root:
#   bash <(curl -fsSL https://raw.githubusercontent.com/slmnsrrbk/bot_raskhody/new-branch/deploy/setup_server.sh)
# или, если репозиторий уже склонирован: bash deploy/setup_server.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/root/bot}"
REPO_URL="${REPO_URL:-https://github.com/slmnsrrbk/bot_raskhody.git}"
BRANCH="${BRANCH:-new-branch}"
SERVICE="bot_raskhody"

echo "==> Установка системных пакетов"
apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip

if [ ! -d "$APP_DIR/.git" ]; then
  echo "==> Клонирование $REPO_URL в $APP_DIR"
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"

echo "==> Виртуальное окружение и зависимости"
[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "!!  Создан $APP_DIR/.env — заполните TELEGRAM_TOKEN, CHAD_API_KEY, REPORT_CHAT_ID"
fi
if [ ! -f credentials.json ]; then
  echo "!!  Положите JSON-ключ сервисного аккаунта Google в $APP_DIR/credentials.json"
fi

echo "==> systemd-сервис $SERVICE"
sed "s#/root/bot#$APP_DIR#g" deploy/bot_raskhody.service > "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable "$SERVICE"

if [ -f .env ] && [ -f credentials.json ] && grep -q '^TELEGRAM_TOKEN=.\+' .env; then
  systemctl restart "$SERVICE"
  sleep 3
  systemctl --no-pager --lines=5 status "$SERVICE" || true
else
  echo "==> Сервис включён, но не запущен: сначала заполните .env и credentials.json, затем:"
  echo "    systemctl restart $SERVICE && journalctl -u $SERVICE -f"
fi
