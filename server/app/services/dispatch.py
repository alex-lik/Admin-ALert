"""Отправка шага эскалации по каналам (план, задачи 2.16–2.18).

`escalation.py` знает, *когда* и *кому* пора пейджить. Здесь — *как*: собрать
сообщение, отправить во все активные устройства админа и честно доложить итог.

Что здесь, а что нет:
- здесь: data-only payload с приоритетом high, выбор Android-канала по профилю,
  отправка во все устройства, деактивация мёртвых токенов (план 2.19), агрегация
  итогов, уведомление «проблема устранена» (план 2.6);
- не здесь: Telegram и звонки. Они придут по той же схеме Dispatcher, но
  каналов этих пока нет — и вместо выдуманной доставки пишется `skipped` с
  объяснением.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Admin, Alert, Device
from app.models.enums import Severity
from app.services.alerts import now
from app.services.escalation import Target
from app.services.push import FcmClient, PushNotConfigured, PushResult, ServiceAccount

logger = logging.getLogger("admin-alert.dispatch")

SessionFactory = Callable[[], AsyncSession]


@dataclass(frozen=True)
class DeviceRef:
    """Устройство для отправки: только то, что нужно, чтобы позвать и гасить.

    Не ORM-сущность намеренно: сущность из чужой сессии, сконструированная
    вручную, — источник тихих ошибок (identity map, случайный flush). Здесь
    нужны три поля и ничего больше.
    """

    id: str
    token: str
    platform: str


# Профиль эскалации -> Android-канал уведомления. Идентификаторы должны
# совпадать с `ChannelId` в клиенте: сервер и приложение — две стороны одного
# имени, и опечатка здесь даст уведомление в канале по умолчанию.
PROFILE_CHANNELS = {
    "warning": "warning",
    "critical": "critical",
    "critical-vip": "critical-vip",
}
SEVERITY_CHANNELS = {
    Severity.WARNING: "warning",
    Severity.CRITICAL: "critical",
}


class PushDispatcher:
    """Реальный отправщик push. Каналы telegram/call пока честно помечаются skipped."""

    def __init__(
        self,
        session_factory: SessionFactory,
        fcm: FcmClient | None,
        *,
        apns_critical: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._fcm = fcm
        self._apns_critical = apns_critical

    @property
    def configured(self) -> bool:
        return self._fcm is not None

    @property
    def session_factory(self) -> SessionFactory:
        return self._session_factory

    async def aclose(self) -> None:
        """Закрыть HTTP-соединение, если отправщик его открыл."""
        if self._fcm is not None:
            await self._fcm.aclose()

    async def send(self, channel: str, alert: Alert, target: Target) -> tuple[str, str | None]:
        """Отправить шаг эскалации. Возвращает (outcome, note) — того вида, что ждёт аудит."""
        if channel != "push":
            return ("skipped", f"канал {channel}: отправка ещё не подключена")

        if self._fcm is None:
            # Не «delivered» и не молчание: в аудите должно быть видно, что
            # уведомление не ушло из-за отсутствия ключей Firebase (задача 1.7).
            return ("skipped", "FCM не настроен: нет ключей сервисного аккаунта")

        if target.admin_id is None:
            return ("skipped", "адресат не определён: пейджить некому")

        devices = await self._devices_of(target.admin_id)
        if not devices:
            return ("failed", "у адресата нет активных устройств")

        # Текст собираем из перечитанного алерта, а не из того, что принёс
        # вызывающий: связи у чужого объекта могут быть не прогружены.
        fresh = await self.load_for_notify(alert.id) or alert
        data = build_alert_payload(fresh, channel)
        results = await self._fan_out(devices, fresh, data)

        return await self._finish(results, target.label)

    async def send_resolved(self, alert: Alert, target: Target) -> tuple[str, str | None]:
        """Отправить «проблема устранена» одному адресату (план, задача 2.6)."""
        if self._fcm is None or target.admin_id is None:
            return ("skipped", "уведомление о resolved не настроено")

        devices = await self._devices_of(target.admin_id)
        if not devices:
            return ("skipped", f"у {target.label} нет активных устройств")

        data = build_alert_payload(alert, "push", resolved=True)
        results = await self._fan_out(devices, alert, data)

        return await self._finish(results, target.label, prefix="устранено")

    async def _fan_out(
        self, devices: list[DeviceRef], alert: Alert, data: dict[str, str]
    ) -> list[tuple[DeviceRef, PushResult]]:
        """Отправить одно и то же во все устройства адресата.

        Устройств у админа несколько (телефон, планшет, запасной). Отправляем во
        все: дежурный может быть не у того аппарата, который под рукой.
        """
        assert self._fcm is not None  # проверка вызывающего: send() уже отсеял
        results: list[tuple[DeviceRef, PushResult]] = []

        for device in devices:
            # iOS получает системный баннер вместе с данными, Android — только
            # данные и строит уведомление сам, в нужном канале (план 2.17).
            apns_alert = (
                {"title": "Проблема устранена", "body": _subtitle(alert)}
                if device.platform == "ios"
                else None
            )
            results.append(
                (
                    device,
                    await self._fcm.send_data(
                        device.token,
                        data,
                        collapse_key=alert.dedup_key,
                        apns_alert=apns_alert,
                        apns_critical=self._apns_critical,
                    ),
                )
            )
        return results

    async def load_for_notify(self, alert_id: str) -> Alert | None:
        """Перечитать алерт со связями для сборки текста уведомления.

        Отдельная сессия и `populate_existing`: вызывающий мог прийти с объектом,
        у которого связи не загружены, а ленивая загрузка на AsyncSession падает
        с MissingGreenlet ровно в момент отправки — то есть в самый неподходящий
        момент, когда алерт уже горит.
        """
        from sqlalchemy.orm import selectinload

        async with self._session_factory() as session:
            return (
                await session.execute(
                    select(Alert)
                    .where(Alert.id == alert_id)
                    .options(
                        selectinload(Alert.project),
                        selectinload(Alert.host),
                        selectinload(Alert.check),
                    )
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()

    async def _devices_of(self, admin_id: str) -> list[DeviceRef]:
        """Активные устройства админа.

        Проекционный запрос, а не сущности: из устройства нужны токен, платформа
        и id для деактивации, а тянуть в память всю строку на каждом шаге
        эскалации незачем.
        """
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(Device.id, Device.token, Device.platform).where(
                        Device.admin_id == admin_id, Device.is_active.is_(True)
                    )
                )
            ).all()
        return [DeviceRef(id=r.id, token=r.token, platform=r.platform) for r in rows]

    async def _finish(
        self,
        results: list[tuple[DeviceRef, PushResult]],
        target_label: str,
        *,
        prefix: str = "доставлено",
    ) -> tuple[str, str | None]:
        """Отключить мёртвые токены и свести отправки в один вердикт шага."""
        dead = [device.id for device, res in results if res.token_invalid]
        if dead:
            await self._deactivate(dead)

        return _aggregate(results, target_label, prefix=prefix)

    async def _deactivate(self, device_ids: list[str]) -> None:
        """Отключить устройства с мёртвым токеном (план, задача 2.19).

        Не удаляем строку: по ней видно, что устройство существовало. Когда админ
        зайдёт с того же телефона, он пришлёт тот же токен, регистрация снова
        включит устройство — иначе пришлось бы заводить новую запись.

        Отдельная транзакция: сессия воркера после шага коммитится, и деактивация
        не должна ехать в том же коммите — иначе сбой отправки утащил бы за собой
        состояние шага.
        """
        logger.info("отключаю устройства с недействительным push-токеном: %s", device_ids)
        async with self._session_factory() as session:
            await session.execute(
                update(Device).where(Device.id.in_(device_ids)).values(is_active=False)
            )
            await session.commit()


async def notify_resolved(dispatcher: PushDispatcher, alert_id: str) -> str:
    """Уведомить дежурных, что проблема устранена (план, задача 2.6).

    Отдельный сигнал: без него дежурный, который проснулся ночью, не узнает, что
    можно спать. Адресат шага тут уже неважен — алерт закрыт, сообщаем всем
    активным.

    Принимает id, а не объект: у вызывающего (HTTP-маршрут) сессия закрывается
    сразу после ответа, а связи у его алерта могли не прогрузиться. Перечитываем
    сами, со связями.

    Никогда не бросает. Ошибка отправки не должна превращать успешный resolve в
    500: проблема решена независимо от того, дошло ли уведомление.
    """
    if not dispatcher.configured:
        return "skipped"

    alert = await dispatcher.load_for_notify(alert_id)
    if alert is None:
        return "skipped"

    async with dispatcher.session_factory() as session:
        admins = list(
            (await session.execute(select(Admin).where(Admin.is_active.is_(True)))).scalars()
        )

    outcome = "skipped"
    notes: list[str] = []
    for admin in admins:
        try:
            result, note = await dispatcher.send_resolved(alert, Target(admin.id, admin.name))
        except Exception:  # noqa: BLE001 — уведомление не важнее самого факта resolve
            logger.exception("уведомление о resolved не отправлено: alert=%s", alert_id)
            continue
        outcome = _worse(outcome, result)
        if note:
            notes.append(note)

    logger.info("resolved %s: %s", alert_id, "; ".join(notes) or "без уведомлений")
    return outcome


def build_alert_payload(alert: Alert, channel: str, *, resolved: bool = False) -> dict[str, str]:
    """Собрать data-сообщение. FCM требует, чтобы все значения были строками.

    Поля подобраны под то, что умеет клиент: `channel` выбирает Android-канал,
    `alert_id` открывает карточку, `state` нужен локальной эскалации.
    """
    return {
        "type": "resolved" if resolved else "alert",
        "alert_id": alert.id,
        "channel": notification_channel(alert),
        "step_channel": channel,
        "project": alert.project.name if alert.project else "",
        "project_id": alert.project_id,
        "host": alert.host.name if alert.host else "",
        "check": alert.check.key if alert.check else "",
        "severity": str(alert.severity),
        "state": str(alert.state),
        "summary": alert.summary,
        "detail": _subtitle(alert),
        "dedup_key": alert.dedup_key,
        "occurrences": str(alert.occurrences),
        "sent_at": now().isoformat(),
    }


def notification_channel(alert: Alert) -> str:
    """Android-канал уведомления для алерта.

    Порядок: профиль (он кодирует агрессивность эскалации), потом severity.
    Неизвестный профиль не должен молча уводить алерт в канал `warning`, поэтому
    запасной вариант — канал по критичности, а не по умолчанию.
    """
    channel = PROFILE_CHANNELS.get(alert.profile_id)
    if channel:
        return channel
    return SEVERITY_CHANNELS.get(Severity(alert.severity), "critical")


def _subtitle(alert: Alert) -> str:
    project = alert.project.name if alert.project else "?"
    host = alert.host.name if alert.host else "?"
    return f"{project} · {host}"


def _aggregate(
    results: list[tuple[DeviceRef, PushResult]], target_label: str, *, prefix: str
) -> tuple[str, str | None]:
    """Свести отправку во все устройства в один вердикт шага."""
    delivered = sum(1 for _, res in results if res.outcome == "delivered")
    invalid = sum(1 for _, res in results if res.token_invalid)
    notes: list[str] = []

    if delivered == len(results):
        notes.append(f"{prefix} на {delivered} устройств")
    else:
        if delivered:
            notes.append(f"{prefix} {delivered} из {len(results)} устройств")
        if invalid:
            notes.append(f"{invalid} устройств с мёртвым токеном отключены")

    if delivered == 0:
        # Причину берём из ответа FCM: «мёртвый токен» и «сеть легла» — разные
        # ситуации с разными последствиями, и по аудиту их надо различать.
        detail = next((res.note for _, res in results if res.note), None)
        summary = "; ".join(notes) if notes else f"{target_label}: доставить не на что"
        return ("failed", f"{summary} ({detail})" if detail else summary)

    return ("delivered", f"{target_label}: {'; '.join(notes)}")


def _worse(left: str, right: str) -> str:
    """Правило приоритета итогов: неудача важнее успеха."""
    order = {"skipped": 0, "delivered": 1, "failed": 2}
    return right if order.get(right, 0) > order.get(left, 0) else left


def make_dispatcher(settings: object) -> PushDispatcher:
    """Собрать отправщик из настроек. Без ключей Firebase вернёт честный skipped."""
    from app.db import SessionLocal

    fcm: FcmClient | None = None
    path = getattr(settings, "fcm_service_account_file", None)
    if path:
        try:
            account = ServiceAccount.from_file(path, getattr(settings, "fcm_project_id", None))
            fcm = FcmClient(account)
        except (OSError, ValueError, KeyError, PushNotConfigured) as exc:
            # Неправильные ключи — ошибка конфигурации, а не повод молчать:
            # воркер продолжит работать и писать skipped с этим объяснением.
            logger.error("FCM не настроен (%s): %s", path, exc)

    return PushDispatcher(
        SessionLocal,
        fcm,
        apns_critical=bool(getattr(settings, "apns_critical_alerts", False)),
    )
