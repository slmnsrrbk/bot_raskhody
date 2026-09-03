#!/usr/bin/env bash
# Установка/обновление бота на сервере. Запускается GitHub Actions или вручную от root:
#   bash deploy/install.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/root/bot}"
BRANCH="${BRANCH:-claude/bot-deployment-t9a0e5}"
REPO_URL="https://github.com/slmnsrrbk/bot_raskhody.git"
SERVICE="bot_raskhody"
export DEBIAN_FRONTEND=noninteractive

echo "==> Системные пакеты"
if command -v apt-get >/dev/null; then
  apt-get update -qq
  apt-get install -y -qq git python3 python3-venv python3-pip >/dev/null
elif command -v dnf >/dev/null; then
  dnf install -y -q git python3 python3-pip
elif command -v yum >/dev/null; then
  yum install -y -q git python3 python3-pip
fi

echo "==> Код ($BRANCH)"
if [ -d "$APP_DIR/.git" ]; then
  # limits.json и expenses.json — данные бота, их сохраняем
  cd "$APP_DIR"
  git fetch -q origin "$BRANCH"
  git checkout -q -B "$BRANCH" "origin/$BRANCH"
  git reset -q --hard "origin/$BRANCH"
else
  git clone -q --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
  cd "$APP_DIR"
fi

echo "==> Python $(python3 --version 2>&1)"
if [ ! -x venv/bin/python ] || ! venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  rm -rf venv
  python3 -m venv venv
fi
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt

echo "==> systemd"
sed "s#/root/bot#$APP_DIR#g" deploy/bot_raskhody.service > "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable -q "$SERVICE"
echo "==> Готово"
