# Admin-ALert

Система алертинга и эскалации для админов: доводит критичный сбой инфраструктуры
до человека за секунды и заставляет его **отреагировать** (ack), а не просто увидеть пуш.

```
Alertmanager / Uptime Kuma / Zabbix / curl
                  │
                  ▼  POST /events  (единый JSON)
        ┌──────────────────────────┐
        │   Server (FastAPI)       │   алерты, дедуп, эскалация, аудит
        │   Worker  (очередь)      │   next_action_at → шаг эскалации
        └──────┬───────────┬───────┘
               │           │
      ┌────────▼─────┐  ┌──▼──────────┐  ┌────────────┐
      │ React Native │  │  Telegram   │  │ SIP / звонок│
      │ Android/iOS  │  │  бот (ack)  │  │ Asterisk AMI│
      └──────────────┘  └─────────────┘  └─────────────┘
```

## Состав репозитория

| Путь | Что внутри | Состояние |
|---|---|---|
| `server/` | FastAPI, модели, воркер эскалации, адаптеры, `POST /events` | каркас |
| `client/` | React Native приложение админа (FCM/APNs + Notifee) | каркас |
| `desktop-agent/` | Агент для сценария «диск заполнен» | каркас |
| `docs/` | План, архитектура, ADR | живой документ |
| `tools/release.py` | SemVer + Conventional Commits | рабочий |
| `.github/` | шаблон PR | рабочий |
| `Makefile` | команды, включая `make check` (замена CI) | рабочий |

## План работ

Источник правды — [`docs/plan.md`](docs/plan.md). 5 блоков, чекбоксы:

1. **Решения и подготовка** — модель данных, стек, Firebase/APNs, доступ телефонии.
2. **Сервер** — ядро эскалации, маршрутизация, push, операционка.
3. **Клиент на React Native** — Android (критичное), iOS, desktop-агент.
4. **Звонки и интеграции** — SIP через AMI, Telegram, адаптеры источников.
5. **Тестирование и запуск** — тесты, защита от усталости, пилот.

Каждый пункт плана — задача в Project Visor (проект `admin-alert`).

## Ветки и релизы

| Ветка | Назначение | Прямые коммиты |
|---|---|---|
| `dev` | Основная разработка, всё новое | ✅ |
| `main` | Только проверенное: прод-конфиги, хотфиксы, релизы | ✅ (хотфиксы) |

Поток: feature-ветка → PR в `dev` → merge → `dev` → `main` (релиз).

```
feature/2.7-worker ──► dev ──► main ──► v0.2.0
                        ▲
              hotfix ────┘  (прямо в main, потом back-merge в dev)
```

## Версионирование (SemVer + Conventional Commits)

Текущая версия — [`VERSION`](VERSION) (в `CHANGELOG.md` она же).

```bash
python tools/release.py show
python tools/release.py validate
python tools/release.py next v0.1.0..HEAD   # major|minor|patch|none
python tools/release.py bump --type minor   # обновит VERSION, CHANGELOG, манифесты
```

| Коммит | Бамп |
|---|---|
| `fix: не отправлять повтор после ack` | patch |
| `feat: SIP-звонок по профилю critical` | minor |
| `refactor!: новая схема события` | major |
| `docs: ADR про стек` | нет |

Релиз делается руками: `bump` → коммит → тег `vX.Y.Z` → push. Полная процедура —
в [CONTRIBUTING.md](CONTRIBUTING.md#релиз).

Автоматических сборок и деплоя пока нет. Перед коммитом прогоняется
`make check` — это весь набор проверок, которые делал бы CI:
SemVer-валидация, ruff, ruff format, mypy и pytest.

Настройки GitHub, которые нужно включить руками (ветки, секреты) —
в [CONTRIBUTING.md](CONTRIBUTING.md#настройки-github-сделать-один-раз-вручную).

## Быстрый старт (сервер)

```bash
cp .env.example .env
docker compose up --build          # api, worker, db, redis
curl http://localhost:8080/health
```

Локально без Docker:

```bash
cd server
python -m venv .venv && .venv/Scripts/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
pytest
```

## Документы

- [`docs/plan.md`](docs/plan.md) — план с чекбоксами
- [`docs/architecture.md`](docs/architecture.md) — компоненты и решения
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — Conventional Commits, ветки, релиз-процесс
- [`CHANGELOG.md`](CHANGELOG.md) — история релизов
