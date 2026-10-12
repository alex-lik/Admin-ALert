"""Воркер эскалации (задачи 2.7, 2.8).

Принцип: таймеры не живут в памяти. У каждого активного алерта есть next_action_at
в БД; воркер раз в N секунд выбирает просроченные записи, выполняет шаг и
планирует следующий. Поэтому перезапуск воркера не теряет эскалацию.

Вся логика — в `app.services.escalation` (там же тесты). Здесь только цикл.
"""

import asyncio

from app.config import get_settings

logger_name = "admin-alert.worker"


async def poll_once() -> int:
    """Один проход: вернуть количество обработанных алертов."""
    from app.db import SessionLocal
    from app.services import escalation

    settings = get_settings()
    async with SessionLocal() as session:
        return await escalation.poll_once(
            session,
            use_lock=settings.database_url.startswith("postgresql"),
            batch_size=settings.worker_batch_size,
            heartbeat_stale_seconds=settings.heartbeat_stale_seconds,
        )


async def run() -> None:
    settings = get_settings()
    interval = settings.worker_poll_interval_seconds
    while True:
        try:
            await poll_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — воркер не должен умирать из-за одного алерта
            import logging

            logging.getLogger(logger_name).exception("Ошибка в проходе воркера")
        await asyncio.sleep(interval)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
