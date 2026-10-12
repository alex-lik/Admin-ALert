"""Аутентификация админов (задача 2.2).

Пароли — PBKDF2-HMAC-SHA256 через `hashlib` из стандартной библиотеки.
`passlib`/`bcrypt` тянут нативные зависимости, а здесь нужна простая и
надёжная проверка, а не скорость. Формат хранения: `pbkdf2_sha256$<iter>$<salt>$<hash>`.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.entities import Admin

PBKDF2_ITERATIONS = 260_000
ALGORITHM = "pbkdf2_sha256"


class AuthError(RuntimeError):
    pass


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    salt_b64 = base64.b64encode(salt).decode()
    digest_b64 = base64.b64encode(digest).decode()
    return f"{ALGORITHM}${iterations}${salt_b64}${digest_b64}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations_raw, salt_raw, hash_raw = stored.split("$")
        if algorithm != ALGORITHM:
            return False
        iterations = int(iterations_raw)
        salt = base64.b64decode(salt_raw)
        expected = base64.b64decode(hash_raw)
    except (ValueError, TypeError):
        return False

    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    # Сравнение за постоянное время: иначе по задержке можно подобрать хеш.
    return hmac.compare_digest(actual, expected)


def _sign(payload: dict[str, object], secret: str) -> str:
    """Компактный подписанный токен: base64(payload).base64(hmac-sha256).

    Своего JWT-библиотеку не тянем: токен короткоживущий, формат
    непубличный и используется только внутри нашей связки api ↔ клиент.
    Полноценный JWT понадобится, как только появится сторонний потребитель.
    """
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return f"{body.decode()}.{_b64(signature).decode()}"


def _verify(token: str, secret: str) -> dict[str, object]:
    try:
        body_raw, sig_raw = token.split(".")
        body = body_raw.encode()
        expected = _b64(hmac.new(secret.encode(), body, hashlib.sha256).digest())
        if not hmac.compare_digest(expected, sig_raw.encode()):
            raise AuthError("подпись неверна")
        return json.loads(_unb64(body_raw))
    except AuthError:
        raise
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise AuthError("токен повреждён") from exc


def _b64(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_tokens(admin: Admin) -> tuple[str, str]:
    settings = get_settings()
    now = int(time.time())

    access = _sign(
        {
            "sub": admin.id,
            "role": admin.role,
            "typ": "access",
            # jti обязателен у обоих типов. Без него payload детерминирован,
            # и два вызова в одну секунду дают байт-в-байт одинаковый токен:
            # его нельзя ни отозвать, ни отличить от предыдущего выпуска.
            "jti": uuid.uuid4().hex,
            "exp": now + settings.access_token_ttl_seconds,
        },
        settings.jwt_secret,
    )
    refresh = _sign(
        {
            "sub": admin.id,
            "typ": "refresh",
            # jti здесь же позволяет вести чёрный список выданных refresh-токенов.
            "jti": uuid.uuid4().hex,
            "exp": now + settings.refresh_token_ttl_seconds,
        },
        settings.jwt_secret,
    )
    return access, refresh


def decode_token(token: str, expected_type: str = "access") -> dict[str, object]:
    settings = get_settings()
    payload = _verify(token, settings.jwt_secret)

    if payload.get("typ") != expected_type:
        raise AuthError(f"ожидался токен типа {expected_type}")

    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < int(time.time()):
        raise AuthError("токен истёк")

    return payload


async def get_admin_by_id(session: AsyncSession, admin_id: str) -> Admin | None:
    result = await session.execute(select(Admin).where(Admin.id == admin_id))
    return result.scalar_one_or_none()


async def authenticate(session: AsyncSession, email: str, password: str) -> Admin:
    result = await session.execute(select(Admin).where(Admin.email == email.lower()))
    admin = result.scalar_one_or_none()

    # Считаем хеш даже если админа нет: иначе по времени ответа можно
    # перебором выяснить, какие email зарегистрированы.
    stored = admin.password_hash if admin else hash_password("dummy-password-for-timing")
    password_ok = verify_password(password, stored)

    if admin is None or not password_ok:
        raise AuthError("неверный email или пароль")
    if not admin.is_active:
        raise AuthError("учётная запись отключена")

    return admin


async def register_admin(
    session: AsyncSession, email: str, name: str, password: str, role: str = "admin"
) -> Admin:
    admin = Admin(
        id=f"adm_{uuid.uuid4().hex[:16]}",
        email=email.lower(),
        name=name,
        password_hash=hash_password(password),
        role=role,
        timezone="Europe/Kyiv",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(admin)
    await session.flush()
    return admin


def token_expiry(seconds: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)
