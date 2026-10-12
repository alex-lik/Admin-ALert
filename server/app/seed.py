"""Начальные данные: профили эскалации (задача 1.2).

Запуск: `python -m app.seed` из каталога `server/` (читает те же переменные
окружения, что и приложение). Идемпотентно: можно запускать повторно,
существующие профили обновляются, шаги пересоздаются.

Профили:
- `warning` — предупреждение: один push, тихие часы уважаются;
- `critical` — push → telegram → звонок дежурному → звонок запасному;
- `critical-vip` — агрессивный: сразу всем, затем звонки без пауз на вежливость;
- `local-disk` — «диск заполнен»: частые push-напоминания, затем звонок.
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import SessionLocal
from app.models.entities import EscalationProfile, EscalationStepDefinition
from app.services.alerts import new_id

# (канал, адресат, задержка в секундах от начала эскалации)
StepSpec = tuple[str, str, int]

PROFILES: dict[str, dict] = {
    "warning": {
        "description": "Предупреждение: один push. Уважает тихие часы.",
        "respects_quiet_hours": True,
        "max_steps": 3,
        "steps": [("push", "current", 0)],
    },
    "critical": {
        "description": "Критичный: push → telegram → звонок → звонок запасному.",
        "respects_quiet_hours": False,
        "max_steps": 6,
        "steps": [
            ("push", "current", 0),
            ("telegram", "current", 120),
            ("call", "current", 300),
            ("call", "next", 600),
        ],
    },
    "critical-vip": {
        "description": "VIP: сразу всем, затем звонки. Пауз на вежливость нет.",
        "respects_quiet_hours": False,
        "max_steps": 8,
        "steps": [
            ("push", "current", 0),
            ("push", "all", 60),
            ("call", "current", 180),
            ("call", "next", 360),
        ],
    },
    "local-disk": {
        "description": "«Диск заполнен»: частые напоминания, затем звонок.",
        "respects_quiet_hours": False,
        "max_steps": 6,
        "steps": [
            ("push", "current", 0),
            ("push", "current", 60),
            ("push", "current", 120),
            ("call", "current", 180),
        ],
    },
}


async def seed_profiles(session: AsyncSession) -> list[str]:
    """Создать или обновить профили. Возвращает их идентификаторы.

    Сессию передаёт вызывающий: так функцию используют и тесты (своя
    in-memory база), и `main()` (боевая база из настроек).
    """
    for profile_id, spec in PROFILES.items():
        # selectinload обязателен: ленивое чтение profile.steps на
        # AsyncSession падает с MissingGreenlet — lazy load там запрещён.
        result = await session.execute(
            select(EscalationProfile)
            .where(EscalationProfile.id == profile_id)
            .options(selectinload(EscalationProfile.steps))
        )
        profile = result.scalar_one_or_none()

        if profile is None:
            profile = EscalationProfile(id=profile_id)
            session.add(profile)
            await session.flush()
            old_steps: list = []
        else:
            # Шаги пересоздаём целиком: их мало, а частичные обновления
            # дают рассинхрон нумерации, который потом больно отлаживать.
            old_steps = list(profile.steps)

        profile.description = spec["description"]
        profile.respects_quiet_hours = spec["respects_quiet_hours"]
        profile.max_steps = spec["max_steps"]

        for old in old_steps:
            await session.delete(old)
        await session.flush()

        steps: list[StepSpec] = spec["steps"]
        for number, (channel, target, delay) in enumerate(steps, start=1):
            session.add(
                EscalationStepDefinition(
                    id=new_id("esd"),
                    profile_id=profile_id,
                    step=number,
                    channel=channel,
                    delay_seconds=delay,
                    target=target,
                )
            )

    await session.commit()
    return list(PROFILES)


def main() -> None:
    async def _run() -> list[str]:
        async with SessionLocal() as session:
            return await seed_profiles(session)

    seeded = asyncio.run(_run())
    print(f"профили: {', '.join(seeded)}")


if __name__ == "__main__":
    main()
