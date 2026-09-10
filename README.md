# Gymflex — Telegram-бот для тренировок в зале

Индивидуальный лог в личке (кнопки вместо текста) + напоминания и сводка в общий чат друзей.

## Возможности MVP

- Онбординг: имя, код для сводки (`И` / `К`), вес, стаж → фаза прогрессии (медовый месяц / средний / плато)
- Общая программа и график недели — только админ редактирует, остальные read-only (`/program`, `/today`)
- В день тренировки: лог упражнения кнопками (вес ±шаг, повторы, подходы, сложность)
- Автопредложение следующего рабочего веса
- Ретроспектива после тренировки в личке
- Утреннее напоминание и вечерняя сводка в чат в формате:

```
#деньспины
Верхняя тяга
И 12×55×4 легко
К 12×50×4 норм
```

## Быстрый старт

1. Создай бота у [@BotFather](https://t.me/BotFather), скопируй токен.
2. Узнай свой Telegram id (например [@userinfobot](https://t.me/userinfobot)).
3. Установи зависимости:

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
pip install -r requirements.txt
```

4. Скопируй `.env.example` → `.env` и заполни:

```env
BOT_TOKEN=...
ADMIN_TELEGRAM_IDS=123456789
TIMEZONE=Europe/Moscow
REMINDER_HOUR=8
RECAP_HOUR=22
```

Несколько админов: `ADMIN_TELEGRAM_IDS=111,222`

5. Запуск (локально):

```bash
python -m app.main
```

Или через Docker Compose:

```bash
# один раз: перенести существующую БД в том data/
# copy gymflex.db data\gymflex.db   # Windows
# cp gymflex.db data/gymflex.db     # Linux/macOS

docker compose up -d --build
docker compose logs -f bot
```

Только бот (без нейросети): `docker compose up -d`.

Опциональный ИИ-коуч (Ollama + Qwen, CPU, ~≥4 GB RAM):

```bash
docker compose --profile nn up -d --build
docker compose --profile nn exec ollama ollama pull qwen2.5:1.5b-instruct
```

При лагах сервера снять нагрузку нейросети (бот продолжает работать):

```bash
docker compose --profile nn stop
```

В боте: меню «ИИ-разбор» и строка статуса в профиле (`онлайн` / `офлайн` / `выключена`).
`NN_ENABLED=false` в `.env` отключает вызовы без остановки контейнеров.

6. Напиши боту `/start` в личке, пройди онбординг.
7. В админке создай шаблоны дней, упражнения, назначь график на неделю.
8. Добавь бота в групповой чат (права писать сообщения). Privacy Mode можно оставить включённым — бот не читает ваши сообщения, только сам пишет.

## Команды

| Команда | Кто | Что |
|---------|-----|-----|
| `/start` | все | онбординг / меню |
| `/today` | все | что сегодня по графику |
| `/program` | все | программа (read-only) |
| `/profile` | все | профиль и фаза |
| `/weight` | все | обновить вес тела |
| `/experience` | все | обновить стаж |
| `/workout` | все | начать/продолжить тренировку |
| `/admin` | админ | шаблоны, график, часы, юзеры |

## Как логировать

1. «Тренировка» → выбрать упражнение
2. Вес кнопками (`±2.5`, прошлый вес) или «Ввести вес»
3. Повторы → подходы → сложность (Легко / Норм / Тяжело / Отказ)
4. Так по всем упражнениям → «Закончить тренировку»
5. Вечером в чат уходит общая сводка

## Деплой (Docker Compose)

```bash
cd /path/to/gymflex
cp .env.example .env   # заполнить BOT_TOKEN и ADMIN_TELEGRAM_IDS
# при миграции с хоста: cp gymflex.db data/gymflex.db
docker compose up -d --build
```

- БД и логи: том `./data` → `/data` в контейнере (`gymflex.db`, `logs/gymflex.log`)
- Рестарт: `restart: unless-stopped`
- Логи: `docker compose logs -f bot`
- Рестарт бота: `docker compose restart bot`
- ИИ-коуч: `docker compose --profile nn up -d --build` → `http://nn:8000` (Ollama внутри сети). Снять нагрузку: `docker compose --profile nn stop`
- Модель по умолчанию: `qwen2.5:1.5b-instruct` (на 2 GB RAM лучше `qwen2.5:0.5b` через `OLLAMA_MODEL`)

Альтернатива без Docker — systemd (`deploy/gymflex.service`).

## Стек

Python 3.12+, aiogram 3, SQLAlchemy 2 + SQLite (aiosqlite), APScheduler, Docker Compose (`bot` + опциональный профиль `nn` / Ollama).
