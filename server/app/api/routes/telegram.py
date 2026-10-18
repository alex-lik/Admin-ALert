"""Telegram-канал: webhook с нажатиями Ack/Snooze (задача 4.8).

Telegram шлёт сюда нажатия inline-кнопок. Два требования к этой точке входа,
и оба про безопасность:

1. **Аутентификация запроса.** Telegram умеет передавать секрет в заголовке
   `X-Telegram-Bot-Api-Secret-Token` — он задаётся при регистрации webhook.
   Без проверки любой желающий, узнав адрес, подделал бы нажатие.
2. **Аутентификация человека.** Нажатие может прийти из чата, который никому
   не привязан. Показывать алерт такому чату нельзя, а вот подтвердить —
   можно и нужно: кнопка «Принял» должна работать так же, как у дежурного, иначе
   она бесполезна как резервный путь. Поэтому действие применяется всегда, а в
   аудит пишется тот, кто нажал.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentAdmin, SessionDep
from app.models.entities import Admin, Alert
from app.models.enums import AlertState
from app.services import alerts as alerts_service
from app.services.dispatch import PushDispatcher, make_dispatcher
from app.services.telegram import (
    CallbackAction,
    TelegramClient,
    parse_callback,
)

logger = logging.getLogger("admin-alert.telegram")
router = APIRouter()

# Сколько минут предлагает кнопка откладывания. Значение одно на сервере и в
# разметке кнопки: разъедутся — и дежурный нажмёт «15 мин», а отложится иначе.
SNOOZE_MINUTES = 15


@router.post("/webhook")
async def telegram_webhook(
    request: Request,
    session: SessionDep,
    secret_header: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
) -> dict[str, object]:
    """Приём обновлений от Telegram.

    Отвечаем всегда 200 при успешной обработке, даже если нажатие было
    отклонено: не-200 Telegram считает ошибкой доставки и повторяет запрос,
    а повторное нажатие «Принял» испортило бы аудит.
    """
    settings = request.app.state.settings
    if settings.telegram_webhook_secret:
        # Сравнение постоянного времени: обычное `!=` позволяло бы подбирать
        # секрет побайтно по времени ответа.
        if secret_header is None or not hmac.compare_digest(
            secret_header, settings.telegram_webhook_secret
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="неверный секрет")

    update = await request.json()
    action = parse_callback(update)

    if action is None:
        # Не нажатие нашей кнопки: обычное сообщение, /start, чужая кнопка.
        # Это не ошибка — просто нечего делать.
        return {"ok": True, "handled": False}

    dispatcher = make_dispatcher(settings)
    try:
        return await _handle(action, session, dispatcher, settings)
    finally:
        await dispatcher.aclose()


async def _handle(
    action: CallbackAction,
    session: SessionDep,
    dispatcher: PushDispatcher,
    settings: object,
) -> dict[str, object]:
    """Применить действие кнопки и привести сообщение в порядок."""
    telegram = _telegram_of(dispatcher, settings)

    alert = (
        await session.execute(
            select(Alert)
            .where(Alert.id == action.alert_id)
            .options(
                selectinload(Alert.project),
                selectinload(Alert.host),
                selectinload(Alert.check),
                selectinload(Alert.acked_by),
                selectinload(Alert.steps),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()

    if alert is None:
        # Алерт удалён или id подделан. Подтверждать нечего, но «часики» на
        # кнопке снять надо — иначе она крутится полминуты.
        await telegram.answer_callback(action.callback_id, "Алерт не найден", show_alert=True)
        return {"ok": True, "handled": False, "reason": "алерт не найден"}

    admin = await _admin_for_chat(session, action.chat_id)
    who = admin.name if admin is not None else f"чат {action.chat_id}"

    if AlertState(alert.state) is AlertState.RESOLVED:
        # Позднее нажатие: алерт закрыли раньше, чем дежурный нажал кнопку.
        # Подтверждать нечего, но сообщение не должно остаться с живыми
        # кнопками — иначе следующее нажатие будет точно таким же бессмысленным.
        await telegram.answer_callback(action.callback_id, "Проблема уже устранена")
        await telegram.edit_alert(
            action.chat_id,
            action.message_id,
            f"✅ <b>Проблема устранена</b>\n<i>{_escape(alert.summary)}</i>",
        )
        return {"ok": True, "handled": True, "result": "already_resolved"}

    try:
        if action.action == "ack":
            return await _do_ack(action, alert, admin, who, session, telegram)
        return await _do_snooze(action, alert, admin, who, session, telegram)
    except alerts_service.AlertError as exc:
        # Лимит откладываний или уже устранённый алерт — это не сбой системы, а
        # ответ пользователю. Пишем его в чат, Telegram'у отвечаем успехом:
        # иначе он будет повторять тот же запрос и получит тот же отказ.
        await telegram.answer_callback(action.callback_id, str(exc)[:190], show_alert=True)
        await session.rollback()
        return {"ok": True, "handled": True, "result": "rejected", "reason": str(exc)}


async def _do_ack(
    action: CallbackAction,
    alert: Alert,
    admin: Admin | None,
    who: str,
    session: SessionDep,
    telegram: TelegramClient,
) -> dict[str, object]:
    stopped = await alerts_service.ack_alert(
        session, alert, admin.id if admin else "", who, "Подтверждено из Telegram"
    )
    await session.commit()

    await telegram.answer_callback(action.callback_id, "Принято, эскалация остановлена")
    await telegram.edit_alert(
        action.chat_id,
        action.message_id,
        f"✅ <b>Принято</b> ({_escape(who)})\n<i>{_escape(alert.summary)}</i>",
    )
    return {"ok": True, "handled": True, "result": "acked", "stopped": stopped}


async def _do_snooze(
    action: CallbackAction,
    alert: Alert,
    admin: Admin | None,
    who: str,
    session: SessionDep,
    telegram: TelegramClient,
) -> dict[str, object]:
    # Минуты берём серверные, а не из callback_data. Данные в кнопке приходят
    # от клиента, и подделав запрос, можно было бы выпросить откладывание на
    # любой срок. Лимит всё равно проверяется в snooze_alert, но доверять
    # тому, что нарисовано на кнопке, не стоит.
    until = await alerts_service.snooze_alert(session, alert, SNOOZE_MINUTES)
    await session.commit()

    stamp = until.strftime("%H:%M")
    await telegram.answer_callback(action.callback_id, f"Отложено до {stamp}")
    # Кнопки оставляем: алерт ещё не подтверждён, и «Принял» по-прежнему
    # нужен. Откладывание не отменяет эскалацию, а сдвигает её.
    await telegram.edit_alert(
        action.chat_id,
        action.message_id,
        f"⏳ <b>Отложено до {stamp}</b> ({_escape(who)})\n<i>{_escape(alert.summary)}</i>",
    )
    return {
        "ok": True,
        "handled": True,
        "result": "snoozed",
        "until": until.isoformat(),
    }


@router.post("/link")
async def link_chat(
    session: SessionDep,
    admin: CurrentAdmin,
    chat_id: str,
) -> dict[str, object]:
    """Привязать Telegram-чат к админу.

    Смысл эндпоинта не в записи chat_id, а в проверке: бот должен реально
    состоять в этом чате, иначе эскалация упрётся в 403 `bot was kicked` и
    шаг будет падать. Проверку делает клиент перед вызовом; сервер доверяет
    админу, который вошёл в систему по токену.
    """
    admin.telegram_chat_id = chat_id
    await session.commit()
    logger.info("админ %s привязал Telegram-чат %s", admin.name, chat_id)

    return {"admin_id": admin.id, "telegram_chat_id": chat_id}


@router.get("/status")
async def telegram_status(
    session: SessionDep, admin: CurrentAdmin, request: Request
) -> dict[str, object]:
    """Состояние канала для настроек клиента: что привязано и работает ли бот."""
    settings = request.app.state.settings
    dispatcher = make_dispatcher(settings)
    try:
        telegram = _telegram_of(dispatcher, settings)
        bot_ok = telegram.configured
        chat_reachable: bool | None = None
        detail: str | None = None

        if admin.telegram_chat_id and telegram.configured:
            result = await telegram.get_chat(admin.telegram_chat_id)
            chat_reachable = result.outcome == "delivered"
            if not chat_reachable:
                detail = result.note

        return {
            "bot_configured": bot_ok,
            "chat_id": admin.telegram_chat_id,
            "chat_reachable": chat_reachable,
            "detail": detail,
        }
    finally:
        await dispatcher.aclose()


def _telegram_of(dispatcher: PushDispatcher, settings: object) -> TelegramClient:
    """Клиент Telegram из диспетчера.

    Диспетчер — единственное место, где собираются транспорты, и второй раз
    создавать клиент здесь означало бы, что при неверном токене ошибка
    пойдёт в другое место, чем её ждёт воркер.
    """
    client = dispatcher.telegram_client
    if client is None:
        # Без токена webhook всё равно не должен падать: нажатие в чате,
        # привязанном ранее, обязано обрабатываться и отвечать дежурному.
        return TelegramClient(getattr(settings, "telegram_bot_token", None))
    return client


async def _admin_for_chat(session: SessionDep, chat_id: str) -> Admin | None:
    """Админ по привязанному чату или None, если чат никому не привязан."""
    result = await session.execute(
        select(Admin).where(Admin.telegram_chat_id == chat_id, Admin.is_active.is_(True))
    )
    return result.scalar_one_or_none()


def _escape(text: str) -> str:
    """Экранировать текст источника под HTML-режим Telegram.

    Имена хостов и проектов регулярно содержат `_` и `*`. Без экранирования
    чужое подчёркивание ломает разметку, и сообщение не отправится вовсе.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
