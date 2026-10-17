"""Отправка push в Firebase Cloud Messaging (план, задачи 2.16–2.18).

Транспорт, а не бизнес-логика: здесь ровно одна операция — отправить data-сообщение
на один токен. Кто отправляет, кого и с каким текстом — это `app.services.dispatch`.

Что здесь важно и почему:

* **HTTP v1, а не legacy.** Legacy API с «server key» закрыт Google в 2024 году.
  Живой путь — OAuth2: подписанный JWT из сервисного аккаунта обменивается на
  access token, дальше `POST /v1/projects/{project}/messages:send`.
* **Data-only для Android.** FCM сам рисует уведомление из `notification`, и оно
  не может попасть в нужный канал и с нужными категориями. Поэтому шлём только
  `data` с `priority: high`, а уведомление строит приложение (план 2.17).
  Без высокого приоритета data-only в фоне Android считает его фоновым и
  игнорирует.
* **Разбор ошибок — не формальность.** `UNREGISTERED`/`INVALID_ARGUMENT` означают,
  что токен мёртв: приложение удалено, сброшены данные или переустановлено. Такой
  токен надо деактивировать, иначе сервер будет пейджить в пустоту до вечности
  (план 2.19). Всё остальное — временная беда, её надо повторить, а не выкидывать.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 — это URL, не секрет
FCM_SEND_URI = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"

# Access token живёт час. Обновляем заранее, чтобы не упереться в истечение
# ровно в момент отправки.
TOKEN_TTL_SECONDS = 3600
TOKEN_REFRESH_MARGIN_SECONDS = 60

Outcome = Literal["delivered", "failed", "skipped", "invalid_token"]


@dataclass(frozen=True)
class PushResult:
    """Итог одной отправки. `token_invalid` — сигнал деактивировать устройство."""

    outcome: Outcome
    note: str | None = None
    token_invalid: bool = False


class PushNotConfigured(RuntimeError):
    """FCM не настроен: нет файла сервисного аккаунта или project_id."""


@dataclass(frozen=True)
class ServiceAccount:
    """Ключи сервисного аккаунта Firebase (задача 1.7)."""

    project_id: str
    client_email: str
    private_key: str
    token_uri: str = DEFAULT_TOKEN_URI

    @classmethod
    def from_file(cls, path: str | Path, project_id: str | None = None) -> ServiceAccount:
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
        resolved_project = project_id or data.get("project_id") or ""
        if not resolved_project:
            raise PushNotConfigured("в файле сервисного аккаунта нет project_id")
        return cls(
            project_id=resolved_project,
            client_email=data["client_email"],
            private_key=data["private_key"],
            token_uri=data.get("token_uri", DEFAULT_TOKEN_URI),
        )


class GoogleTokenProvider:
    """Access token для FCM: подписанный JWT -> обмен на токен -> кэш.

    Токен кэшируется до истечения: подписывать RSA-ключом на каждое сообщение
    было бы расточительством, а обменивать JWT на токен — по сети на каждое
    сообщение тем более.
    """

    def __init__(self, account: ServiceAccount) -> None:
        self._account = account
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    async def get(self, client: httpx.AsyncClient) -> str:
        now = time.time()
        if self._access_token is not None and now < self._expires_at:
            return self._access_token

        assertion = self._make_assertion(now)
        response = await client.post(
            self._account.token_uri,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
            timeout=15.0,
        )
        if response.status_code != 200:
            detail = response.text[:200]
            raise PushNotConfigured(f"Google отклонил JWT: HTTP {response.status_code} {detail}")

        payload = response.json()
        self._access_token = payload["access_token"]
        self._expires_at = (
            now + int(payload.get("expires_in", TOKEN_TTL_SECONDS)) - (TOKEN_REFRESH_MARGIN_SECONDS)
        )
        return self._access_token

    def _make_assertion(self, issued_at: float) -> str:
        """Собрать JWT (RS256) — ровно то, что требует Google.

        Стандартные библиотеки Python не умеют RS256, поэтому подпись делается
        через `cryptography`: это единственная причина, по которой пакет есть в
        зависимостях.
        """
        header = {"alg": "RS256", "typ": "JWT"}
        claims = {
            "iss": self._account.client_email,
            "scope": SCOPE,
            "aud": self._account.token_uri,
            "iat": int(issued_at),
            "exp": int(issued_at) + TOKEN_TTL_SECONDS,
        }

        def encode(part: dict[str, Any]) -> str:
            raw = json.dumps(part, separators=(",", ":")).encode("utf-8")
            return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

        signing_input = f"{encode(header)}.{encode(claims)}"

        # Сервисный аккаунт Firebase — это всегда RSA. Проверяем тип явно:
        # load_pem_private_key возвращает союз типов, и подпись PKCS1v15 годна
        # только для RSA. Без проверки неверный ключ упал бы позже и невнятно.
        private_key = serialization.load_pem_private_key(
            self._account.private_key.encode("utf-8"),  # noqa: S301 — свой ключ из своего файла
            password=None,
        )
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise PushNotConfigured("ключ сервисного аккаунта не RSA: FCM требует RS256")

        signature = private_key.sign(
            signing_input.encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return f"{signing_input}.{base64.urlsafe_b64encode(signature).decode('ascii').rstrip('=')}"


class FcmClient:
    """Отправка одного data-сообщения в FCM."""

    def __init__(
        self,
        account: ServiceAccount,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._account = account
        self._tokens = GoogleTokenProvider(account)
        # Свой клиент создаётся лениво: пока FCM не настроен, сеть трогать не
        # нужно, а открывать соединение на старте процесса незачем.
        self._http = http

    @property
    def project_id(self) -> str:
        return self._account.project_id

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=10.0)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
            self._http = None

    async def send_data(
        self,
        token: str,
        data: dict[str, str],
        *,
        ttl_seconds: int = 600,
        collapse_key: str | None = None,
        apns_alert: dict[str, str] | None = None,
        apns_critical: bool = False,
    ) -> PushResult:
        """Отправить data-сообщение. Никогда не бросает: сеть падает — это failed.

        Отправка лежит в пути HTTP-запроса алерта, поэтому исключение здесь
        превратилось бы в 500 у источника события из-за чужой проблемы.
        """
        message: dict[str, Any] = {
            "token": token,
            "data": data,
            "android": {
                # high — обязательное: data-only с обычным приоритетом в фоне
                # Android игнорирует как низкоприоритетное (план 2.17).
                "priority": "high",
                "ttl": f"{ttl_seconds}s",
            },
        }
        if collapse_key:
            # Перезаписывающий ключ: повторные шаги того же алерта заменяют друг
            # друга, а не копятся стопкой в шторке.
            message["android"]["collapse_key"] = collapse_key

        if apns_alert is not None:
            message["apns"] = _apns_block(apns_alert, critical=apns_critical)

        client = await self._client()
        try:
            access_token = await self._tokens.get(client)
            response = await client.post(
                FCM_SEND_URI.format(project_id=self._account.project_id),
                headers={"Authorization": f"Bearer {access_token}"},
                json={"message": message},
                timeout=10.0,
            )
        except (httpx.HTTPError, PushNotConfigured) as exc:
            return PushResult("failed", f"FCM недоступен: {type(exc).__name__}: {exc}")

        if response.status_code == 200:
            return PushResult("delivered", response.json().get("name"))

        return _classify_error(response)


def _apns_block(alert: dict[str, str], *, critical: bool) -> dict[str, Any]:
    """Блок APNs для того же data-сообщения.

    iOS не умеет data-only без `aps.alert` — системное уведомление не появится.
    Отдельная алярт-структура тут поэтому: текст дублируется, чтобы FCM не
    показывал системный баннер ДО того, как приложение построит своё.

    Про `critical: 1`: Apple требует entitlement Critical Alerts, а без него
    отправляет `BadPriority` и сообщение не доходит вовсе. Поэтому флаг
    включается настройкой и по умолчанию выключен — иначе «критичный» push
    молча пропадал бы на всех iPhone.
    """
    sound: Any = "default"
    if critical:
        sound = {"critical": 1, "name": "default", "volume": 1.0}

    aps: dict[str, Any] = {
        "alert": {"title": alert["title"], "body": alert["body"]},
        "sound": sound,
        "interruption-level": "time-sensitive",
    }
    return {
        "headers": {"apns-priority": "10", "apns-push-type": "alert"},
        "payload": {"aps": aps},
    }


def _classify_error(response: httpx.Response) -> PushResult:
    """Разобрать ответ FCM. Главное здесь — отличить мёртвый токен от сбоя сети.

    Ошибку деактивации устройства нельзя смешивать с временной: погасив токен из-за
    того, что Google мигрирует базы, мы отрежем дежурного от алертов.
    """
    try:
        error = response.json().get("error", {})
    except (ValueError, AttributeError):
        # Ответ без JSON (прокси, 502 от балансировщика). Текст режем, чтобы
        # HTML-страница не уехала в аудит целиком.
        return PushResult("failed", f"FCM HTTP {response.status_code}: {response.text[:200]}")

    status = str(error.get("status", ""))
    message = str(error.get("message", ""))[:200]
    error_codes = {
        d.get("errorCode")
        for d in error.get("details", [])
        if isinstance(d, dict) and d.get("errorCode")
    }

    # Токен отозван или невалиден: приложение удалено/переустановлено.
    if error_codes & {"UNREGISTERED", "INVALID_ARGUMENT", "SENDER_ID_MISMATCH"}:
        return PushResult(
            "invalid_token",
            f"{status or 'INVALID'}: {message}",
            token_invalid=True,
        )

    # 404 без errorCode тоже означает несуществующий токен: FCM так отвечает,
    # когда токен не существует вовсе, а не сломан.
    if response.status_code == 404 and status in {"NOT_FOUND", ""}:
        return PushResult("invalid_token", message or "токен не найден", token_invalid=True)

    if error_codes & {"QUOTA_EXCEEDED", "RESOURCE_EXHAUSTED"}:
        return PushResult("failed", f"{status}: {message} — исчерпана квота проекта")

    if response.status_code >= 500 or status in {"UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED"}:
        return PushResult("failed", f"{status or 'UNAVAILABLE'}: {message}")

    return PushResult("failed", f"{status or 'UNKNOWN'}: {message}")
