#!/usr/bin/env bash
# Установка/обновление бота на сервере. Запускается GitHub Actions или вручную от root:
#   bash deploy/install.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/root/bot}"
BRANCH="${BRANCH:-claude/bot-deployment-t9a0e5}"
REPO_URL="https://github.com/slmnsrrbk/bot_raskhody.git"
SERVICE="bot_raskhody"
WEB_SERVICE="bot_raskhody-web"
WEBAPP_DOMAIN="${WEBAPP_DOMAIN:-}"
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
sed "s#/root/bot#$APP_DIR#g" deploy/bot_raskhody-web.service > "/etc/systemd/system/$WEB_SERVICE.service"
systemctl daemon-reload
systemctl enable -q "$SERVICE" "$WEB_SERVICE"

if [ -n "$WEBAPP_DOMAIN" ] && command -v nginx >/dev/null; then
  echo "==> nginx + HTTPS для $WEBAPP_DOMAIN"
  SITE=/etc/nginx/sites-available/raskhody
  if [ ! -f "$SITE" ] || ! grep -q "server_name $WEBAPP_DOMAIN;" "$SITE"; then
    sed "s#__DOMAIN__#$WEBAPP_DOMAIN#g" deploy/nginx-raskhody.conf > "$SITE"
  fi
  ln -sf "$SITE" /etc/nginx/sites-enabled/raskhody
  nginx -t
  systemctl reload nginx
  if ! grep -q "listen 443" "$SITE"; then
    command -v certbot >/dev/null || apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
    ok=0
    for attempt in 1 2 3 4; do
      # ждём, если в этот момент работает плановое обновление сертификатов
      for i in $(seq 1 12); do pgrep -x certbot >/dev/null || break; sleep 5; done
      if certbot --nginx --non-interactive --agree-tos --register-unsafely-without-email --redirect -d "$WEBAPP_DOMAIN"; then
        ok=1; break
      fi
      echo "…попытка $attempt не удалась, повтор через 15 с"; sleep 15
    done
    if [ "$ok" = 1 ]; then
      echo "==> Сертификат получен, https://$WEBAPP_DOMAIN"
    else
      echo "!!  certbot не смог выпустить сертификат для $WEBAPP_DOMAIN (см. вывод выше)"
    fi
  else
    echo "==> HTTPS уже настроен"
  fi
  grep -q '^WEBAPP_URL=' .env 2>/dev/null && sed -i "s#^WEBAPP_URL=.*#WEBAPP_URL=https://$WEBAPP_DOMAIN#" .env || echo "WEBAPP_URL=https://$WEBAPP_DOMAIN" >> .env
fi
echo "==> Готово"
