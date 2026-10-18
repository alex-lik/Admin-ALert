"""HTTP-слой расписания дежурства (задача 2.10).

Кто дежурит — вопрос, который дежурный задаёт себе каждый раз, когда
просыпается ночью. Поэтому эндпоинт «кто сейчас» самый простой из всех и
не требует ничего, кроме токена.

Записи расписания тоже доступны любому админу, а не только с ролью
`admin`: в системе дежурства «админ» это не привилегия, а должность.
Права появятся, когда появится понятие «только ведущий инженер меняет
расписание», — заводить его раньше значит написать проверку, которую
придётся переписывать.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import CurrentAdmin, SessionDep
from app.models.entities import OnCallShift
from app.schemas.oncall import (
    CoverageOut,
    GapOut,
    RosterOut,
    RotationCreate,
    RotationOut,
    ShiftCreate,
    ShiftOut,
)
from app.services.alerts import coerce_aware, now
from app.services.escalation import why_fallback
from app.services.oncall import (
    ROLES,
    ScheduleError,
    coverage,
    create_shift,
    current_roster,
    delete_shift,
    ensure_horizon,
    rotate,
)

router = APIRouter()


@router.get("/roster", response_model=list[RosterOut])
async def roster(session: SessionDep, _admin: CurrentAdmin) -> list[RosterOut]:
    """Кто дежурит сейчас: основной и запасной."""
    result: list[RosterOut] = []

    for role in ROLES:
        current = await current_roster(session, role=role)
        if current is not None:
            result.append(
                RosterOut(
                    role=role,
                    shift_id=current.shift_id,
                    admin_id=current.admin_id,
                    admin_name=current.admin_name,
                    starts_at=current.starts_at,
                    ends_at=current.ends_at,
                )
            )
            continue

        result.append(
            RosterOut(
                role=role,
                shift_id=None,
                admin_id=None,
                admin_name=None,
                starts_at=None,
                ends_at=None,
                reason=await why_fallback(session, role),
            )
        )

    return result


@router.get("/shifts", response_model=list[ShiftOut])
async def list_shifts(
    session: SessionDep,
    _admin: CurrentAdmin,
    role: Annotated[str | None, Query(pattern="^(primary|backup)$")] = None,
    hours: Annotated[int, Query(ge=1, le=24 * 60)] = 168,
) -> list[ShiftOut]:
    """Смены на ближайшие `hours` часов."""
    moment = now()
    until = moment + timedelta(hours=hours)
    stmt = (
        select(OnCallShift)
        .where(OnCallShift.ends_at > moment, OnCallShift.starts_at < until)
        .order_by(OnCallShift.starts_at)
    )
    if role:
        stmt = stmt.where(OnCallShift.role == role)

    shifts = (await session.execute(stmt)).scalars().all()
    return [ShiftOut.model_validate(shift) for shift in shifts]


@router.post("/shifts", response_model=ShiftOut, status_code=status.HTTP_201_CREATED)
async def add_shift(
    session: SessionDep,
    _admin: CurrentAdmin,
    payload: ShiftCreate,
) -> ShiftOut:
    """Назначить смену вручную."""
    try:
        shift = await create_shift(
            session,
            admin_id=payload.admin_id,
            role=payload.role,
            starts_at=payload.starts_at,
            ends_at=payload.ends_at,
        )
    except ScheduleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await session.commit()
    return ShiftOut.model_validate(shift)


@router.post("/rotation", response_model=RotationOut, status_code=status.HTTP_201_CREATED)
async def add_rotation(
    session: SessionDep,
    _admin: CurrentAdmin,
    payload: RotationCreate,
) -> RotationOut:
    """Развернуть ротацию по кругу на указанный срок."""
    try:
        shifts = await rotate(
            session,
            admin_ids=payload.admin_ids,
            role=payload.role,
            starts_at=payload.starts_at,
            shift_hours=payload.shift_hours,
            days=payload.days,
        )
    except ScheduleError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await session.commit()

    return RotationOut(
        created=len(shifts),
        role=payload.role,
        from_at=coerce_aware(shifts[0].starts_at),
        to_at=coerce_aware(shifts[-1].ends_at),
    )


@router.delete("/shifts/{shift_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_shift(session: SessionDep, _admin: CurrentAdmin, shift_id: str) -> None:
    """Снять смену.

    После удаления в расписании появляется дыра — и это намеренно видно:
    `GET /oncall/coverage` её покажет, а эскалация в этот промежуток уйдёт
    во фолбэк с меткой «в расписании дыра».
    """
    try:
        await delete_shift(session, shift_id)
    except ScheduleError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    await session.commit()


@router.get("/coverage", response_model=CoverageOut)
async def shift_coverage(
    session: SessionDep,
    _admin: CurrentAdmin,
    role: Annotated[str, Query(pattern="^(primary|backup)$")] = "primary",
    hours: Annotated[int, Query(ge=1, le=24 * 60)] = 24,
) -> CoverageOut:
    """Дыры в расписании на ближайшие `hours` часов.

    Молчаливая дырка — худший вид поломки: система работает, ночью дежурит
    «первый по списку», и никто об этом не знает. Поэтому покрытие проверяется
    явно и показывается.
    """
    from_at = now()
    to_at = from_at + timedelta(hours=hours)
    raw_gaps = await coverage(session, role=role, from_at=from_at, to_at=to_at)

    return CoverageOut(
        from_at=from_at,
        to_at=to_at,
        gaps=[GapOut.model_validate(gap) for gap in raw_gaps],
        covered=not raw_gaps,
    )


@router.post("/horizon", response_model=dict[str, int])
async def extend_horizon(session: SessionDep, _admin: CurrentAdmin) -> dict[str, int]:
    """Досоздать ротацию до горизонта.

    Воркер делает это сам при каждом проходе; эндпоинт нужен для проверки
    вручную и для скрипта, который хочет убедиться, что расписание живо.
    """
    created = await ensure_horizon(session)
    await session.commit()
    return {"created": created}


@router.get("/whoami", response_model=dict[str, object])
async def whoami(session: SessionDep, admin: CurrentAdmin) -> dict[str, object]:
    """Дежурит ли сейчас этот админ и в какой роли.

    Это то, что приложение показывает в настройках: «вы сейчас на связи» или
    «вы запасной». Разница между «дежурный» и «запасной» меняет ожидания —
    запасного не будят первым.
    """
    result = await session.execute(
        select(OnCallShift)
        .where(
            OnCallShift.admin_id == admin.id,
            OnCallShift.starts_at <= now(),
            OnCallShift.ends_at > now(),
        )
        .order_by(OnCallShift.starts_at)
    )
    shifts = list(result.scalars())

    return {
        "admin_id": admin.id,
        "name": admin.name,
        "roles": [s.role for s in shifts],
        "on_call": bool(shifts),
        "current_shift": (
            {
                "role": shifts[0].role,
                "starts_at": coerce_aware(shifts[0].starts_at),
                "ends_at": coerce_aware(shifts[0].ends_at),
            }
            if shifts
            else None
        ),
        # Подсказка для клиента: кого система позовёт, если этот админ недоступен.
        "fallback_would_call": await _fallback_name(session, shifts),
    }


async def _fallback_name(session: SessionDep, shifts: list[OnCallShift]) -> str | None:
    """Кто дежурит следующим, если этот админ не ответит.

    Запасной дежужит параллельно, а не «после», поэтому ответ — он.
    """
    if not shifts:
        return None

    backup = await current_roster(session, role="backup")
    return backup.admin_name if backup is not None else "запасной не назначен"
