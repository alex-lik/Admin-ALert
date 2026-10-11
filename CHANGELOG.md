# Changelog

Все значимые изменения в этом проекте документируются в этом файле.

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
версионирование — [Semantic Versioning](https://semver.org/lang/ru/).

Версия выводится автоматически из [Conventional Commits](https://www.conventionalcommits.org/ru/v1.0.0/):

| Тип коммита | Эффект на версию |
|---|---|
| `fix:` | patch `0.1.0` → `0.1.1` |
| `feat:` | minor `0.1.0` → `0.2.0` |
| `BREAKING CHANGE:` в теле коммита | major `0.1.0` → `1.0.0` |
| `docs:`, `chore:`, `refactor:`, `test:`, `build:`, `ci:` | версия не меняется |
| `!` после типа/скоупа: `feat(api)!: ...` | major |

Релизы собираются автоматически при merge в `main` (см. `.github/workflows/release.yml`).

## [Unreleased]

## [0.1.0] - 2026-10-12

### Added
- Каркас монорепозитория: `server/` (FastAPI), `client/` (React Native), `desktop-agent/`, `docs/`.
- План проекта перенесён в `docs/plan.md` (5 блоков, чекбоксы) и загружен в Project Visor
  (проект `admin-alert`, 88 задач по разделам плана).
- `docs/architecture.md`: компоненты, жизненный цикл алерта, модель эскалации, исходы звонка.
- SemVer-версионирование: `VERSION`, `CHANGELOG.md`, Conventional Commits (`.commitlintrc.yml`).
- `tools/release.py` — релизный инструмент без внешних зависимостей: `show`, `next`, `bump`, `sync`, `validate`.
- CI: `ci.yml` (SemVer-валидация, ruff, mypy, pytest, docker build, commitlint) и
  `release.yml` (авто-релиз по Conventional Commits в `main` → тег + GitHub Release + образ в GHCR).
- Сервер: `/health` и `/health/ready`, `POST /events` с валидацией по единой схеме события
  и ответом 202, автомат состояний алерта с проверкой переходов, каркас воркера на
  опросе `next_action_at`, async-подключение к БД, Alembic.
- `docker-compose.yml`: api, worker, db (postgres), redis.
- Тесты: 15 штук на автомат состояний и HTTP-слой.
