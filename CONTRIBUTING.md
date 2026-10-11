# Contributing — Admin-ALert

## Ветки

| Ветка | Что в неё попадает | Прямые коммиты |
|---|---|---|
| `dev` | Вся новая разработка, рефакторинг, новые задачи | ✅ |
| `main` | Прод-конфиги, хотфиксы, результат релизов | ✅ только хотфикс |

- Новая задача → `feature/<номер-задачи>-<короткое-описание>` от `dev`.
- Работаешь над задачей из Project Visor → в названии ветки используй её номер (`2.7`).
- Открываешь PR в `dev`. PR в `main` — только подготовка релиза.
- Релизный merge `dev` → `main`: fast-forward либо merge-коммит, тэг ставит CI.
- Хотфикс в `main` обязательно **back-merge в `dev`** в тот же день, иначе next релиз его потеряет.

## Conventional Commits

Формат: `<type>(<scope>): <subject>`

```
feat(worker): шаг эскалации планируется через next_action_at
fix(push): не переотправлять алерт после ack
docs(adr): зафиксировать выбор FastAPI
refactor(events)!: dedup_key обязателен
```

**Types:** `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `build`, `ci`, `perf`, `style`.

**Scopes:** `server`, `worker`, `api`, `events`, `push`, `call`, `telegram`, `android`, `ios`,
`desktop`, `infra`, `docs`.

**Правила:**

- Subject — императив, до 72 символов, без точки в конце: `fix: не отправлять повтор`, а не
  `Исправил баг с повтором.`
- Breaking change — `!` после типа/скоупа **и** раздел `BREAKING CHANGE:` в теле коммита.
- Никаких секретов, токенов, паролей — ни в коммитах, ни в задачах Project Visor.

## Настройки GitHub (сделать один раз вручную)

`gh`/токена у агента нет, поэтому эти пункты нужно включить руками в
**Settings → Branches / Settings → Actions**:

**Settings → General → Branches**

- **Default branch: `main`** — открывать репозиторий по стабильной ветке.
- **Allow squash merging: off**, merge commits: on. Rebase merge: on.
  Squash ломает историю Conventional Commits, а rebase — историю релизов.

**Settings → Branches → Add rule**

| Правило | Настройки |
|---|---|
| `main` | Require PR to merge · **Do not allow bypassing** · Require status checks: `SemVer`, `Server lint & test`, `Conventional commits` · Require branches up to date |
| `dev` | Require PR to merge · Require status checks: `SemVer`, `Server lint & test`, `Conventional commits` |

**Settings → Actions → General**

- Workflow permissions: **Read and write** — иначе `release.yml` не сможет создать тег и GitHub Release.

**Settings → Secrets**

- `POSTGRES_PASSWORD` — для docker-compose.
- Остальные секреты (FCM, APNs, AMI, JWT) в GitHub Secrets **не выносить**, пока их
  не начал использовать CI.

Если `main` нужно защитить строже (только хотфиксы через отдельную ветку `hotfix/*`) —
это делается добавлением правила «Require PR to merge» с утверждёнными ревьюерами.

## Как это влияет на версию

| Коммит | SemVer |
|---|---|
| `fix:` | patch |
| `feat:` | minor |
| `!` или `BREAKING CHANGE:` | major |
| остальные | версия не меняется |

Проверить локально перед пушем:

```bash
python tools/release.py next $(git describe --tags --abbrev=0)..HEAD
python tools/release.py validate
```

## Pull Request

Чек-лист в [шаблоне PR](.github/PULL_REQUEST_TEMPLATE.md). Обязательно:

- [ ] `python tools/release.py validate` проходит
- [ ] Тесты проходят (`make test`)
- [ ] CHANGELOG обновлён, если изменение видно пользователю
- [ ] Для RN-клиента: проверка на реальном устройстве (Samsung/Xiaomi/Huawei), не только эмулятор

## Релиз

Ручной (экстренный):

```bash
python tools/release.py bump --type minor
git commit -m "chore(release): v0.2.0"
git tag -a v0.2.0 -m "v0.2.0"
git push origin main --tags
```

Обычный — автоматический: merge в `main` → CI считает бамп → коммит, тег, GitHub Release.

## Локальный запуск

```bash
make up        # docker compose
make test      # pytest
make lint      # ruff + проверка версии
make version   # показать версию
```
