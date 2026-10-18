"""Telegram-канал: отправка алерта с кнопками Ack/Snooze (задача 4.8).

Это дублирующий канал. Он нужен там, где push не сработал: телефон в кармане,
приложение убито, FCM не доставил. И он же — резервное место для подтверждения:
подтвердить алерт можно прямо из чата, не открывая приложение.

Что здесь, а что нет:
- здесь: клиент Bot API (sendMessage с inline-клавиатурой), разбор обновлений
  от webhook, обработка нажатий Ack/Snooze, привязка чата к админу;
- не здесь: userbot и голосовые звонки (задача 4.9). Обычный бот не умеет
  звонить, и обходить это через userbot — отдельное решение, а не деталь
  реализации.

Токен бота — секрет: он даёт право писать от имени бота всем, кто его знает.
Поэтому он только из окружения и только на сервере.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger("admin-alert.telegram")

API_BASE = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT_SECONDS = 10.0

# Telegram ограничивает callback_data 64 байтами. Наш payload — короткий
# префикс действия плюс id алерта, поэтому укладываемся с запасом.
ACK_PREFIX = "ack:"
SNOOZE_PREFIX = "snooze:"

SNOOZE_MINUTES = 15


class TelegramNotConfigured(RuntimeError):
    """Токен бота не задан."""


@dataclass(frozen=True)
class TelegramResult:
    outcome: str  # delivered | failed | skipped
    note: str | None = None
    message_id: int | None = None


@dataclass(frozen=True)
class CallbackAction:
    """Разобранное нажатие inline-кнопки."""

    action: str  # ack | snooze
    alert_id: str
    chat_id: str
    message_id: int
    user_id: int
    # Идентификатор нажатия. Без него нельзя снять «часики» на кнопке:
    # Telegram ждёт answerCallbackQuery именно по нему.
    callback_id: str


class TelegramClient:
    """Тонкая обёртка над Bot API. Никогда не бросает — только возвращает итог."""

    def __init__(self, token: str | None, *, http: httpx.AsyncClient | None = None) -> None:
        self._token = token
        self._http = http

    @property
    def configured(self) -> bool:
        return bool(self._token)

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
            self._http = None

    async def _call(self, method: str, payload: dict[str, object]) -> TelegramResult:
        if not self._token:
            return TelegramResult("skipped", "Telegram не настроен: нет токена бота")

        client = await self._client()
        try:
            response = await client.post(
                API_BASE.format(token=self._token, method=method),
                json=payload,
                timeout=TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            return TelegramResult("failed", f"Telegram недоступен: {type(exc).__name__}: {exc}")

        if response.status_code != 200:
            return TelegramResult(
                "failed", f"Telegram HTTP {response.status_code}: {response.text[:200]}"
            )

        body = response.json()
        if not body.get("ok"):
            # Telegram возвращает 200 с ok:false — причина описана словами,
            # и она единственная полезная вещь в ответе.
            return TelegramResult("failed", f"Telegram: {body.get('description', 'без описания')}")

        result = body.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        return TelegramResult("delivered", None, message_id)

    async def send_alert(
        self,
        chat_id: str,
        *,
        title: str,
        body: str,
        alert_id: str,
        step: int,
        occurrences: int = 1,
        resolved: bool = False,
    ) -> TelegramResult:
        """Отправить алерт с кнопками Ack/Snooze.

        `parse_mode=HTML` — чтобы выделить заголовок жирным и показать номер
        шага приглушённым. Текст приходит от источника алерта, поэтому всё,
        что не наша разметка, экранируется в `_render_alert_text`.
        """
        keyboard: list[list[dict[str, str]]] = []
        if not resolved:
            keyboard.append(
                [
                    {"text": "✅ Принял", "callback_data": f"{ACK_PREFIX}{alert_id}"},
                    {
                        "text": f"⏳ {SNOOZE_MINUTES} мин",
                        "callback_data": f"{SNOOZE_PREFIX}{SNOOZE_MINUTES}:{alert_id}",
                    },
                ]
            )

        payload: dict[str, object] = {
            "chat_id": chat_id,
            "text": _render_alert_text(
                title=title,
                body=body,
                step=step,
                occurrences=occurrences,
                resolved=resolved,
            ),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            # Сообщение в чате не должно пропеть как личное: это алерт, а не
            # болтовня. Плюс админ может вести переписку в том же чате.
            "disable_notification": False,
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}

        return await self._call("sendMessage", payload)

    async def edit_alert(self, chat_id: str, message_id: int, text: str) -> TelegramResult:
        """Переписать сообщение после нажатия — убрать кнопки, показать итог.

        Кнопки после нажатия обязаны исчезнуть: повторное нажатие «Принял» на
        уже подтверждённом алерте породило бы вторую запись в аудите.
        """
        return await self._call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": []},
            },
        )

    async def answer_callback(
        self, callback_id: str, text: str, *, show_alert: bool = False
    ) -> TelegramResult:
        """Ответить на нажатие. Без этого у кнопки крутится часики 30 секунд."""
        return await self._call(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_id,
                "text": text,
                "show_alert": show_alert,
            },
        )

    async def get_chat(self, chat_id: str) -> TelegramResult:
        """Проверить, что чат существует и бот в нём состоит."""
        return await self._call("getChat", {"chat_id": chat_id})


def _escape(text: str) -> str:
    """Экранировать текст источника под HTML-режим Telegram.

    Опасны только `&`, `<`, `>` — именно их HTML-парсер считает разметкой.
    Подчёркивания и звёздочки экранировать не нужно: они относятся к
    Markdown-режиму, а мы используем HTML. Суммы в именах хостов вида
    `disk_*_used` приходят от источника как есть и остаются читаемыми.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_alert_text(
    *, title: str, body: str, step: int, occurrences: int, resolved: bool
) -> str:
    """Собрать текст сообщения.

    Порядок — от ответа к контексту: дежурный читает в темноте и одним
    взглядом решает, нажимать ли «Принял». Поэтому «Принял» и суть проблемы
    в первой строке, а хост и номер шага — ниже.
    """
    if resolved:
        return f"✅ <b>Проблема устранена</b>\n{_escape(title)}\n<i>{_escape(body)}</i>"

    header = f"🚨 <b>{_escape(title)}</b>"
    if occurrences > 1:
        header = f"{header} <i>(повтор {occurrences})</i>"

    lines = [header, _escape(body)]

    # Номер шага нужен дежурному: по нему видно, что эскалация уже дошла до
    # звонка, и «Принял» надо нажать скорее.
    lines.append(f"<i>шаг эскалации {step}</i>")
    return "\n".join(lines)


def parse_callback(data: dict[str, object]) -> CallbackAction | None:
    """Разобрать обновление с нажатием inline-кнопки.

    Возвращает None на всём, что не является нажатием нашей кнопки: бот получает
    и обычные сообщения, и `/start`, и нажатия на чужие кнопки. Молча игнорировать
    их — правильное поведение, а не ошибка.
    """
    # В callback_query сообщение лежит ВНУТРИ него, а не на верхнем уровне
    # обновления — верхний уровень используется для обычных входящих сообщений.
    callback = data.get("callback_query")
    if not isinstance(callback, dict):
        return None

    payload = callback.get("data")
    callback_id = callback.get("id")
    if not isinstance(payload, str) or not isinstance(callback_id, str):
        return None

    message = callback.get("message")
    if not isinstance(message, dict):
        # Нажатие на inline-кнопку в сообщении, которое бот уже удалил, —
        # допустимая ситуация: чата для ответа просто нет.
        return None

    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None

    chat_id = chat.get("id")
    message_id = message.get("message_id")
    sender = callback.get("from")
    if chat_id is None or message_id is None or not isinstance(sender, dict):
        return None

    if payload.startswith(ACK_PREFIX):
        action, alert_id = "ack", payload[len(ACK_PREFIX) :]
    elif payload.startswith(SNOOZE_PREFIX):
        _, rest = payload.split(":", 1)
        minutes, _, alert_id = rest.partition(":")
        if not minutes.isdigit() or not alert_id:
            return None
        action = "snooze"
    else:
        return None

    return CallbackAction(
        action=action,
        alert_id=alert_id,
        chat_id=str(chat_id),
        message_id=int(message_id),
        user_id=int(sender.get("id", 0)),
        callback_id=callback_id,
    )
