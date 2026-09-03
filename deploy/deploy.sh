#!/usr/bin/env bash
# Обновление бота на сервере до последней версии ветки. Вызывается из GitHub Actions
# или вручную: bash deploy/deploy.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/root/bot}"
BRANCH="${BRANCH:-new-branch}"
SERVICE="${SERVICE:-bot_raskhody}"

cd "$APP_DIR"

# limits.json — рабочее состояние бота, оно не должно теряться при обновлении кода
[ -f limits.json ] && cp limits.json /tmp/limits.json.bak

echo "==> Обновление кода: $BRANCH"
git fetch --quiet origin "$BRANCH"
git checkout --quiet -B "$BRANCH" "origin/$BRANCH"
git reset --quiet --hard "origin/$BRANCH"

[ -f limits.json ] || { [ -f /tmp/limits.json.bak ] && cp /tmp/limits.json.bak limits.json; } || true

echo "==> Зависимости"
[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q -r requirements.txt

echo "==> Перезапуск $SERVICE"
systemctl restart "$SERVICE"
sleep 3
systemctl --no-pager --lines=10 status "$SERVICE"
