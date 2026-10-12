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

Релизы делаются вручную: `python tools/release.py bump --type <t>` → коммит →
тег `vX.Y.Z` → push. Автоматических сборок в проекте нет.

## [Unreleased]

## [0.2.0] - 2026-10-12

### Added
- Клиент: Expo SDK 57 + React Native 0.86, сборка через development build
  (`expo-dev-client`). Expo Go не подходит — нужны каналы с `bypassDnd`,
  точные будильники и Kotlin-модуль.
- `client/src/notifications/channels.ts` — каналы `warning` / `critical` /
  `critical-vip` с обходом DND, вибрацией, публичной видимостью на lock screen;
  категории `alarm`/`call` и `fullScreenAction`; локальная эскалация через
  `setAlarmClock` с шагами 1/2/3 мин и отменой при ack (задачи 3.6–3.8, 3.10).
- `client/src/notifications/fcm.ts` — получение FCM-токена, `onTokenRefresh`,
  регистрация устройства на сервере, фоновый обработчик сообщений (3.4).
- `client/App.tsx` — экран диагностики: разрешения, состояние каналов, токен,
  кнопки тестовых алертов (3.13).
- `client/app.json` — разрешения Android из плана (exact alarms, full-screen intent,
  игнорирование оптимизации батареи, foreground service).
- `client/plugins/withNotifeeMavenRepository.js` — config plugin, добавляющий
  maven-репозиторий Notifee при `expo prebuild`. Без него сборка падает с
  `Could not find any matches for app.notifee:core:+`: Android-ядро Notifee
  вкомпоновано в пакет и не лежит ни в Google Maven, ни в Maven Central.
- `make client-check` (типы + `expo-doctor`) и `make android` в корневом `Makefile`.

### Known limitations
- FCM не работает: Firebase-проект ещё не создан (задача 1.7), в `client/`
  лежит placeholder `google-services.json`. Он нужен только чтобы Gradle собрался,
  в репозиторий не коммитится. Экран диагностики честно показывает это в строке
  «FCM-токен».
- У каналов не настроен звук `USAGE_ALARM` (остальная часть задачи 3.7):
  сейчас `sound: 'default'` через `STREAM_NOTIFICATION`. Нужен ресурс в `res/raw`
  и config plugin — вместе с Kotlin-модулем из задачи 3.11.
- `expo-doctor` помечает `@notifee/react-native` как Unmaintained. Оставлено
  осознанно: альтернативы не дают контроля над каналами, `bypassDnd` и
  alarm-потоком, ради которых существует задача 3.7. Помечено исключением.
- `bypassDnd` не проверен: на эмуляторе он всегда `false`, в том числе у каналов
  Google Dialer. Проверяется на реальных устройствах (3.12, 5.3).

### Fixed
- `vibrationPattern` содержал нечётное число элементов, из-за чего Notifee
  отклонял канал и `critical` / `critical-vip` не создавались вообще. Теперь
  чётное число положительных значений. Поймано на устройстве, а не при сборке.
- `asForegroundService: true` в тестовых уведомлениях приводил к тому, что они
  показывались и тут же снимались системой. Флаг перенесён на момент появления
  нативного foreground service (задача 3.11).


## [0.1.0] - 2026-10-12

### Added
- Каркас монорепозитория: `server/` (FastAPI), `client/` (React Native), `desktop-agent/`, `docs/`.
- План проекта перенесён в `docs/plan.md` (5 блоков, чекбоксы) и загружен в Project Visor
  (проект `admin-alert`, 88 задач по разделам плана).
- `docs/architecture.md`: компоненты, жизненный цикл алерта, модель эскалации, исходы звонка.
- SemVer-версионирование: `VERSION`, `CHANGELOG.md`, Conventional Commits.
- `tools/release.py` — релизный инструмент без внешних зависимостей: `show`, `next`, `bump`, `sync`, `validate`.
- Сервер: `/health` и `/health/ready`, `POST /events` с валидацией по единой схеме события
  и ответом 202, автомат состояний алерта с проверкой переходов, каркас воркера на
  опросе `next_action_at`, async-подключение к БД, Alembic.
- `docker-compose.yml`: api, worker, db (postgres), redis.
- `Makefile` с командой `make check` — весь набор проверок вручную, вместо CI.
- Тесты: 15 штук на автомат состояний и HTTP-слой.

### Fixed
- `tools/release.py next`: падал с `TypeError` при разборе диапазона коммитов —
  `BUMP_TYPES | NO_BUMP_TYPES`, где `BUMP_TYPES` — словарь, а не множество.
- `tools/release.py bump`: в индекс попадали только `VERSION` и `CHANGELOG.md`,
  хотя менялись ещё `server/pyproject.toml` и `client/package.json`. Теперь
  индексируются ровно те файлы, которые изменил сам bump.
