"""Воркер эскалации (задачи 2.7, 2.8).

Принцип: таймеры не живут в памяти. У каждого активного алерта есть next_action_at
в БД; воркер раз в N секунд выбирает просроченные записи, выполняет шаг и
планирует следующий. Поэтому перезапуск воркера не теряет эскалацию.
"""

import asyncio

from app.config import get_settings

logger_name = "admin-alert.worker"


async def poll_once() -> int:
    """Один проход: вернуть количество обработанных алертов.

    Выборка должна идти с блокировкой строк (SELECT ... FOR UPDATE SKIP LOCKED),
    иначе два воркера выполнят один шаг дважды (задача 2.8).
    """
    # TODO(2.7): выбрать алерты с next_action_at <= now, выполнить шаг, запланировать следующий.
    # TODO(2.9): по исчерпании лимита повторов перевести алерт в expired.
    return 0


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
