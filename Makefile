.DEFAULT_GOAL := help
SHELL := bash
PY := python
SERVER := server
COMPOSE := docker compose

.PHONY: help version version-check bump-minor bump-patch lint fmt check test test-cov client-check android up down logs migrate revision ps clean

help: ## Показать список команд
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ── Версия ──
version: ## Показать текущую версию
	@$(PY) tools/release.py show

version-check: ## Проверить семантику и синхронность версий
	@$(PY) tools/release.py validate

bump-minor: ## Поднять версию на minor
	@$(PY) tools/release.py bump --type minor

bump-patch: ## Поднять версию на patch
	@$(PY) tools/release.py bump --type patch

# ── Качество ──
check: version-check lint test client-check ## Полный набор проверок перед коммитом (CI/CD нет)

lint: ## Линт сервера + проверка версии
	cd $(SERVER) && ruff check app tests
	cd $(SERVER) && ruff format --check app tests
	cd $(SERVER) && mypy app

client-check: ## Типы клиента + диагностика Expo
	cd client && npm run typecheck
	cd client && npx expo-doctor

fmt: ## Автоформат
	cd $(SERVER) && ruff format app tests && ruff check --fix app tests

test: ## Тесты
	cd $(SERVER) && pytest

test-cov: ## Тесты с покрытием
	cd $(SERVER) && pytest --cov=app --cov-report=term-missing --cov-report=html

# ── Локальный запуск ──
up: ## Поднять api, worker, db, redis
	$(COMPOSE) up --build

android: ## Собрать и запустить клиент на Android-устройстве/эмуляторе
	cd client && npx expo run:android

down: ## Остановить всё
	$(COMPOSE) down

logs: ## Логи сервисов
	$(COMPOSE) logs -f --tail=100

ps: ## Статус сервисов
	$(COMPOSE) ps

# ── Миграции ──
migrate: ## Применить миграции
	cd $(SERVER) && alembic upgrade head

revision: ## Создать ревизию: make revision m="сообщение"
	cd $(SERVER) && alembic revision -m "$(m)"

# ── Прочее ──
clean: ## Удалить кеши и артефакты
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf $(SERVER)/.pytest_cache $(SERVER)/.ruff_cache $(SERVER)/.mypy_cache htmlcov .coverage
