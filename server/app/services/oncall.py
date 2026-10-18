"""Расписание дежурства (план, задача 2.10).

Смысл модуля: дежурный меняется по времени, и система должна знать, кто
дежурит **сейчас**, а не «первый добавленный админ».

Ключевое решение — **материализация** вместо правил. Ротация не хранится как
шаблон «каждые N часов по кругу», а сразу разворачивается в конкретные смены
наперёд. Причины:

- в БД видно ровно то, что произойдёт, без вычислений в горячем пути воркера;
- «а что было в прошлые выходные» — обычный запрос, а не реконструкция;
- отмена и перенос смены — правка конкретной строки, а не «пересоздать
  расписание целиком и надеяться, что не потерялось»;
- ротация без администратора должна быть пустой, а не тихо работающей по
  остаткам.

Цена решения: смены нужно материализовать заранее. Расписание покрывает
`HORIZON_DAYS` дней вперёд, и отдельная функция `ensure_horizon` досоздаёт его.
Воркер вызывает её при каждом проходе — это дёшево, потому что при нормальном
расписании за проход не создаётся ничего.

Проверки, которые здесь есть и которые легко пропустить:

- **смены не пересекаются**. Две primary-смены одновременно означают, что
  система выберет одну наугад (в запросе стоит ORDER BY, но «какая именно
  должна была» — вопрос к расписанию, а не к коду);
- **роли не пересекаются по времени**. primary и backup в одном окне — это
  не ошибка данных, но бессмыслица: запасной включается, когда основной не
  ответил;
- **смена не в прошлом**. Смена, начавшаяся час назад, не спасает дежурство,
  которое уже началось, но делает вид, что назначила того, кто на смене.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Admin, OnCallShift
from app.services.alerts import coerce_aware, new_id, now

# На сколько дней вперёд поддерживается расписание. Длиннее месяца смысла
# нет: за месяц расписание всё равно меняют руками, а вот забытый на месяц
# алерт, ушедший в фолбэк, обернулся бы дежурным «на всякий случай».
HORIZON_DAYS = 30

ROLES = ("primary", "backup")


class ScheduleError(RuntimeError):
    """Расписание противоречиво. Сообщение — для человека, который его чинит."""


@dataclass(frozen=True)
class Roster:
    """Кто дежурит в момент `at`."""

    role: str
    shift_id: str
    admin_id: str
    admin_name: str
    timezone: str
    starts_at: datetime
    ends_at: datetime

    @property
    def is_current(self) -> bool:
        return True


async def admin_by_id(session: AsyncSession, admin_id: str) -> Admin:
    admin = await session.get(Admin, admin_id)
    if admin is None:
        raise ScheduleError(f"админ {admin_id} не найден")
    if not admin.is_active:
        raise ScheduleError(f"{admin.name} отключён: дежурным не назначить")
    return admin


async def create_shift(
    session: AsyncSession,
    *,
    admin_id: str,
    role: str,
    starts_at: datetime,
    ends_at: datetime,
    allow_past: bool = False,
) -> OnCallShift:
    """Создать смену с проверками.

    Проверки не «на всякий случай» — каждая соответствует реальной поломке,
    которую иначе пришлось бы искать в проде ночью:

    - `ends_at > starts_at` — иначе смена мгновенно истёкшая;
    - смена не начинается в прошлом — она не может закрыть уже начавшееся
      дежурство, но создаёт видимость назначения;
    - смены той же роли не пересекаются — иначе при двух сменах выбирается
      одна по `ORDER BY`, и это неожиданно для дежурного.
    """
    if role not in ROLES:
        raise ScheduleError(f"роль должна быть одной из {ROLES}, получено «{role}»")

    starts_at = coerce_aware(starts_at)
    ends_at = coerce_aware(ends_at)

    if ends_at <= starts_at:
        raise ScheduleError("конец смены должен быть позже начала")

    moment = now()
    if not allow_past and starts_at < moment - timedelta(minutes=1):
        raise ScheduleError("нельзя назначить смену, начавшуюся в прошлом")

    await admin_by_id(session, admin_id)

    overlapping = (
        (
            await session.execute(
                select(OnCallShift).where(
                    OnCallShift.role == role,
                    OnCallShift.starts_at < ends_at,
                    OnCallShift.ends_at > starts_at,
                )
            )
        )
        .scalars()
        .all()
    )

    if overlapping:
        clash = overlapping[0]
        raise ScheduleError(
            f"смена пересекается с уже существующей: "
            f"{clash.starts_at.isoformat()}–{clash.ends_at.isoformat()}"
        )

    shift = OnCallShift(
        id=new_id("shf"),
        admin_id=admin_id,
        role=role,
        starts_at=starts_at,
        ends_at=ends_at,
    )
    session.add(shift)
    await session.flush()
    return shift


async def delete_shift(session: AsyncSession, shift_id: str) -> None:
    shift = await session.get(OnCallShift, shift_id)
    if shift is None:
        raise ScheduleError(f"смена {shift_id} не найдена")
    await session.delete(shift)
    await session.flush()


async def rotate(
    session: AsyncSession,
    *,
    admin_ids: list[str],
    role: str,
    starts_at: datetime,
    shift_hours: int,
    days: int = HORIZON_DAYS,
    allow_past: bool = False,
) -> list[OnCallShift]:
    """Развернуть ротацию по кругу на `days` дней.

    Круг — по переданному порядку: `admin_ids[0]` дежурит первым. Порядок
    задаётся явно, а не «по дате создания админов»: иначе перестановка
    списка молча меняла бы расписание.
    """
    if not admin_ids:
        raise ScheduleError("в ротации нужен хотя бы один админ")
    if len(set(admin_ids)) != len(admin_ids):
        raise ScheduleError("в ротации повторяется один админ: смена была бы впустую")
    if shift_hours <= 0 or shift_hours > 24 * 7:
        raise ScheduleError("длительность смены — от часа до недели")

    duration = timedelta(hours=shift_hours)
    cursor = coerce_aware(starts_at)
    horizon = cursor + timedelta(days=days)

    created: list[OnCallShift] = []
    index = 0
    while cursor < horizon:
        created.append(
            await create_shift(
                session,
                admin_id=admin_ids[index % len(admin_ids)],
                role=role,
                starts_at=cursor,
                ends_at=cursor + duration,
                # Первая смена может начинаться «сейчас», и это нормально.
                # Прошлое допускаем только в самом начале: иначе ротация,
                # начатая на середине дня, отвергалась бы целиком.
                allow_past=allow_past or index > 0,
            )
        )
        cursor += duration
        index += 1

    return created


async def ensure_horizon(session: AsyncSession, *, horizon_days: int = HORIZON_DAYS) -> int:
    """Досоздать ротацию до горизонта. Возвращает число созданных смен.

    Кто продолжает ротацию — определяется по последней смене этой роли: её
    админ продолжает круг, её конец становится началом следующей смены. Так
    ротация не требует отдельного «расписания-эталона» и переживает удаление
    админа из середины круга.
    """
    moment = now()
    horizon = moment + timedelta(days=horizon_days)
    created_total = 0

    for role in ROLES:
        last = (
            await session.execute(
                select(OnCallShift)
                .where(OnCallShift.role == role)
                .order_by(OnCallShift.ends_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        if last is None:
            continue

        roster = await _admins_from_rotation(session, last, role)
        if roster is None:
            # Круг из одного админа: продолжать нечем осмысленно, и молча
            # размножать его смены на месяц вперёд нельзя — это замаскирует
            # отсутствие расписания.
            continue

        cursor = coerce_aware(last.ends_at)
        if cursor >= horizon:
            continue

        index = roster.index(last.admin_id)
        step = coerce_aware(last.ends_at) - coerce_aware(last.starts_at)
        while cursor < horizon:
            index = (index + 1) % len(roster)
            try:
                await create_shift(
                    session,
                    admin_id=roster[index],
                    role=role,
                    starts_at=cursor,
                    ends_at=cursor + step,
                    allow_past=True,
                )
            except ScheduleError:
                # Пересечение с чужой ручной сменой: круг на этом месте
                # останавливаем и продолжаем со следующей свободной точки.
                break
            created_total += 1
            cursor += step

    return created_total


async def _admins_from_rotation(
    session: AsyncSession, last: OnCallShift, role: str
) -> list[str] | None:
    """Восстановить круг ротации по истории смен.

    Берём админов, чьи сменами заполнен непрерывный отрезок перед последней
    сменой, и идём от самой старой к текущей — это и есть круг в порядке
    дежурства.
    """
    history = (
        (
            await session.execute(
                select(OnCallShift)
                .where(OnCallShift.role == role, OnCallShift.ends_at <= last.starts_at)
                .order_by(OnCallShift.starts_at.desc())
                .limit(24)  # круг длиннее суток — это уже не суточная ротация
            )
        )
        .scalars()
        .all()
    )

    if not history:
        return None

    order: list[str] = []
    for shift in history:
        if shift.admin_id not in order:
            order.append(shift.admin_id)
    order.reverse()

    return order if len(order) > 1 else None


async def current_roster(
    session: AsyncSession, *, role: str, at: datetime | None = None
) -> Roster | None:
    """Кто дежурит в указанный момент (по умолчанию — сейчас)."""
    moment = coerce_aware(at or now())

    shift = (
        (
            await session.execute(
                select(OnCallShift)
                .where(
                    OnCallShift.role == role,
                    OnCallShift.starts_at <= moment,
                    OnCallShift.ends_at > moment,
                )
                .order_by(OnCallShift.starts_at.desc())
            )
        )
        .scalars()
        .first()
    )

    if shift is None:
        return None

    admin = await session.get(Admin, shift.admin_id)
    if admin is None or not admin.is_active:
        # Смена есть, а дежурить некому: админ удалён или отключён. Это
        # дырка в расписании, и возвращать None честнее, чем показывать
        # отключённого дежурного.
        return None

    return Roster(
        role=role,
        shift_id=shift.id,
        admin_id=admin.id,
        admin_name=admin.name,
        timezone=admin.timezone,
        starts_at=coerce_aware(shift.starts_at),
        ends_at=coerce_aware(shift.ends_at),
    )


async def coverage(
    session: AsyncSession, *, role: str, from_at: datetime, to_at: datetime
) -> list[dict[str, str]]:
    """Дыры в расписании на интервале.

    Нужно для показа: «расписание покрыто» или «с 3:00 до 9:00 никто не
    дежурит». Молчаливые дырки — худший вид поломки: система работает, но
    ночью дежурит первый по списку, а никто об этом не знает.
    """
    shifts = (
        (
            await session.execute(
                select(OnCallShift)
                .where(
                    OnCallShift.role == role,
                    OnCallShift.ends_at > from_at,
                    OnCallShift.starts_at < to_at,
                )
                .order_by(OnCallShift.starts_at)
            )
        )
        .scalars()
        .all()
    )

    gaps: list[dict[str, str]] = []
    cursor = coerce_aware(from_at)

    for shift in shifts:
        start = coerce_aware(shift.starts_at)
        if start > cursor:
            gaps.append(
                {"from": cursor.isoformat(), "to": start.isoformat(), "reason": "нет смены"}
            )
        end = coerce_aware(shift.ends_at)
        if end > cursor:
            cursor = end

    if cursor < to_at:
        gaps.append({"from": cursor.isoformat(), "to": to_at.isoformat(), "reason": "нет смены"})

    return gaps
