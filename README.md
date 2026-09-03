# bot_raskhody — Telegram-бот учёта расходов

Бот принимает сообщения вида `вчера хлеб 200` или `такси 350`, определяет категорию через ChadGPT,
сохраняет трату, показывает отчёты за 1/7/30 дней и следит за лимитами.

Данные хранятся в SQLite (`data.db`, общая база бота и мини-приложения). У каждого Telegram-аккаунта
свои траты и лимиты; чужие записи недоступны ни в боте, ни в приложении. Старые `expenses.json`/`limits.json`
переносятся первому пользователю (или `OWNER_ID` из `.env`).

В боте: добавление («такси 350», «вчера хлеб 200»), фото чека → позиции распознаются моделью и добавляются
(кнопка «↩️ Отменить весь чек»), отчёты за 1/7/30 дней, лимиты, удаление (кнопка «🗑 Удалить трату»,
«↩️ Отменить» сразу после добавления, `/undo`), выгрузка в Excel за период («📥 Выгрузка», `/export`),
автоотчёты каждому пользователю.

**Шифрование.** Название, сумма и категория каждой траты и лимиты хранятся в базе зашифрованными
(AES-256-GCM, [cryptography](https://cryptography.io/)); ключ каждого пользователя выводится из мастер-ключа
в файле `/root/bot/.data_key` (создаётся автоматически, права 600). В открытом виде в базе только id пользователя
и дата. **Сделайте резервную копию `.data_key`: без него данные не восстановить.** Действие `reset-data`
в workflow удаляет все данные и ключ.

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
`VPS_PASSWORD` — root-пароль сервера, `TELEGRAM_TOKEN` — токен бота от @BotFather (опционально `CHAD_API_KEY`).

## Локально

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python main.py
python -m unittest discover -s tests -v   # тесты
```

## Мини-приложение (Telegram Mini App)

`webapp.py` — небольшой API на aiohttp (порт 8080, только localhost) и страница `webapp/index.html`
в стиле iOS: траты по категориям и периодам, лимиты, добавление/редактирование/удаление.
Наружу выходит через nginx с сертификатом Let's Encrypt; домен задаётся переменной репозитория
`WEBAPP_DOMAIN` (по умолчанию `201.24.120.213.sslip.io`). Бот показывает кнопку «Открыть приложение»
и кнопку меню, если в `.env` есть `WEBAPP_URL`. Безопасность: каждый запрос к API подписан Telegram
([проверка initData](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app), срок годности сутки),
данные отдаются только их владельцу, есть ограничение частоты запросов (120 в минуту на пользователя),
защитные заголовки, API слушает только localhost, наружу — HTTPS через nginx.
