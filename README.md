# bot_raskhody — Telegram-бот учёта расходов

Бот принимает сообщения вида `вчера хлеб 200` или `такси 350`, определяет категорию через ChadGPT,
сохраняет трату, показывает отчёты за 1/7/30 дней и следит за лимитами.

Траты хранятся в файле `expenses.json` рядом с `main.py` (Google Таблица пока отключена),
лимиты — в `limits.json`.

## Запуск на сервере

Один раз от root:

```bash
apt-get update && apt-get install -y git
git clone --branch claude/bot-deployment-t9a0e5 https://github.com/slmnsrrbk/bot_raskhody.git /root/bot
bash /root/bot/deploy/install.sh
systemctl restart bot_raskhody
journalctl -u bot_raskhody -f
```

Сервис `bot_raskhody` перезапускается при падении и стартует после перезагрузки.
Обновить до последней версии: `bash /root/bot/deploy/install.sh && systemctl restart bot_raskhody`.

## Деплой через GitHub Actions

Workflow `.github/workflows/deploy.yml` запускается вручную (Actions → Deploy to VPS → Run workflow)
и по SSH ставит/обновляет бота на сервере. Нужен один секрет в Settings → Secrets and variables → Actions:
`VPS_PASSWORD` — root-пароль сервера.

## Локально

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python main.py
python -m unittest discover -s tests -v   # тесты
```
