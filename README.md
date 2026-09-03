# bot_raskhody — Telegram-бот учёта расходов

Бот принимает сообщения вида `вчера хлеб 200` или `такси 350`, определяет категорию через ChadGPT,
записывает трату в Google Таблицу, показывает отчёты за 1/7/30 дней, следит за лимитами и
присылает автоотчёты по расписанию.

## ⚠️ Сначала — безопасность

В прежних коммитах этого **публичного** репозитория лежали токен Telegram, ключ ChadGPT и приватный SSH-ключ.
Они удалены из кода, но остались в истории git, поэтому их нужно считать скомпрометированными и перевыпустить:

1. **Telegram-токен** — в @BotFather: `/mybots` → бот → *API Token* → *Revoke current token*
   ([документация BotFather](https://core.telegram.org/bots/features#botfather)).
2. **Ключ ChadGPT** — перевыпустить в личном кабинете https://chadgpt.ru.
3. **SSH-ключ** — удалить старый публичный ключ из `~/.ssh/authorized_keys` на сервере и сгенерировать новый:
   `ssh-keygen -t ed25519 -C "github-actions" -f ~/.ssh/bot_deploy`.
4. **Ключ сервисного аккаунта Google** (`credentials.json`) — в репозиторий не попадал, но если он лежал в архиве
   рядом с кодом, лучше тоже перевыпустить
   ([создание и удаление ключей сервисного аккаунта](https://cloud.google.com/iam/docs/keys-create-delete)).
5. При желании очистить историю репозитория:
   [Removing sensitive data from a repository](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository).

## Переменные окружения

Все настройки берутся из файла `.env` (шаблон — `.env.example`). Файл `.env` и `credentials.json` в git не попадают.

| Переменная | Обязательна | Описание |
|---|---|---|
| `TELEGRAM_TOKEN` | да | токен бота от @BotFather |
| `CHAD_API_KEY` | нет | ключ ChadGPT; без него категория всегда «Другое», советов нет |
| `REPORT_CHAT_ID` | нет | chat_id для автоотчётов; без него автоотчёты выключены. Узнать: отправьте боту `/start` |
| `SHEET_NAME` | нет | название Google Таблицы (по умолчанию «Мои расходы») |
| `GOOGLE_CREDENTIALS_FILE` | нет | путь к JSON-ключу сервисного аккаунта (по умолчанию `credentials.json` рядом с `main.py`) |
| `LIMITS_FILE` | нет | где хранить лимиты (по умолчанию `limits.json` рядом с `main.py`) |
| `TIMEZONE` | нет | часовой пояс, по умолчанию `Asia/Krasnoyarsk` |
| `DAILY_REPORT_TIME` | нет | время ежедневного автоотчёта `ЧЧ:ММ`, по умолчанию `21:00` |

Google Таблица должна быть расшарена (права «Редактор») на email сервисного аккаунта из `credentials.json`
([инструкция gspread](https://docs.gspread.org/en/latest/oauth2.html#service-account)).

## Запуск локально

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # заполнить
cp ~/Downloads/credentials.json .
python main.py
```

Тесты: `python -m unittest discover -s tests -v`.

## Деплой на сервер (systemd)

Подходит для любого VPS с Ubuntu/Debian. Один раз от root:

```bash
apt-get update && apt-get install -y git
git clone --branch new-branch https://github.com/slmnsrrbk/bot_raskhody.git /root/bot
cd /root/bot
bash deploy/setup_server.sh          # ставит python, venv, зависимости и systemd-сервис
nano .env                            # вписать TELEGRAM_TOKEN, CHAD_API_KEY, REPORT_CHAT_ID
scp credentials.json root@СЕРВЕР:/root/bot/   # с локальной машины
systemctl restart bot_raskhody
journalctl -u bot_raskhody -f        # логи
```

Сервис `bot_raskhody` перезапускается при падении и стартует после перезагрузки сервера
(юнит: `deploy/bot_raskhody.service`). Обновить бота вручную: `bash deploy/deploy.sh`.

## Автодеплой через GitHub Actions

Workflow `.github/workflows/deploy.yml` при каждом push в `new-branch` (или `main`):
1. ставит зависимости, проверяет синтаксис и гоняет тесты;
2. по SSH запускает на сервере `deploy/deploy.sh` (обновляет код, зависимости, перезапускает сервис).

Чтобы включить, в настройках репозитория **Settings → Secrets and variables → Actions** задайте:

| Тип | Имя | Значение |
|---|---|---|
| Secret | `SSH_PRIVATE_KEY` | приватный ключ (содержимое `~/.ssh/bot_deploy`), публичная часть — в `authorized_keys` на сервере |
| Secret | `SERVER_HOST` | IP или домен сервера |
| Secret | `SERVER_USER` | пользователь SSH (по умолчанию `root`) |
| Variable | `DEPLOY_ENABLED` | `true` — без этого шаг деплоя пропускается, а проверки всё равно идут |
| Variable | `APP_DIR` | каталог на сервере (по умолчанию `/root/bot`) |

Документация: [secrets](https://docs.github.com/en/actions/security-for-github-actions/security-guides/using-secrets-in-github-actions),
[variables](https://docs.github.com/en/actions/learn-github-actions/variables),
[webfactory/ssh-agent](https://github.com/webfactory/ssh-agent).

## Деплой в Docker (альтернатива)

```bash
cp .env.example .env && nano .env
mkdir -p data && cp credentials.json .
docker compose up -d --build
docker compose logs -f
```

Лимиты хранятся в `./data/limits.json`, ключ Google монтируется только на чтение.

## Структура

```
main.py                      код бота
requirements.txt             зависимости (версии зафиксированы)
.env.example                 шаблон настроек
deploy/bot_raskhody.service  systemd-юнит
deploy/setup_server.sh       первичная настройка сервера
deploy/deploy.sh             обновление на сервере (использует GitHub Actions)
Dockerfile, docker-compose.yml
tests/                       юнит-тесты разбора сообщений
```
