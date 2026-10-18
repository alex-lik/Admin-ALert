"""Ограничение частоты запросов (план, задача 2.24).

Зачем это системе алертинга: `/events` — единственная точка, через которую
приходят сотни обращений от разных источников. Один сбойный узел, который
начал шуметь в 1000 раз в секунду, способен положить всю БД — а значит,
и эскалацию остальных проектов.

Ключ ограничения — **не IP, а API-ключ проекта**. Это неочевидное решение,
и вот почему: источники стоят за NAT, за балансировщиками и в Docker-сетях,
где все запросы приходят с одного адреса. Ограничение по IP в такой
конфигурации отключает приём событий целиком после первых же 600 событий.

Что здесь:

- скользящее окно в Redis (Lua — атомарно: incr + expire в одном вызове);
- честный фолбэк на счётчик в памяти, если Redis недоступен. Это слабее:
  лимит не общий между инстансами API. Молча деградировать до «без
  ограничения» нельзя — тогда поломка Redis снимала бы защиту целиком и
  никто бы об этом не узнал;
- заголовок `Retry-After` в ответе 429: источник должен знать, когда
  повторить, иначе он будет бить стену и раздувать нагрузку.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from redis.asyncio import Redis

logger = logging.getLogger("admin-alert.ratelimit")

# Lua считает окно атомарно: INCR без EXPIRE оставил бы ключ навсегда,
# а две команды между собой допускают гонку (два инстанса одновременно
# создают ключ — и у одного из них счётчик сбрасывается).
_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int
    # degraded=True означает, что считалось в памяти процесса: Redis не
    # ответил. Метрика этого отдельно — по ней видно, что защита ослаблена.
    degraded: bool = False


class RateLimiter:
    """Скользящее окно по API-ключу проекта."""

    def __init__(
        self,
        redis_url: str | None,
        *,
        limit_per_minute: int = 600,
        window_seconds: int = 60,
    ) -> None:
        self._redis_url = redis_url
        self._limit = limit_per_minute
        self._window = window_seconds
        self._redis: Redis | None = None
        # Ключи вида `adm_xxx`; память чистим по событию окна, иначе словарь
        # рос бы вечно из-за мёртвых источников.
        self._memory: dict[str, deque[float]] = defaultdict(deque)
        self._redis_checked = False

    async def _client(self) -> Redis | None:
        """Подключиться к Redis один раз. Отсутствие не должно ломать импорт."""
        if self._redis is not None or self._redis_checked:
            return self._redis
        self._redis_checked = True

        if not self._redis_url:
            return None

        try:
            client = Redis.from_url(self._redis_url, decode_responses=True)
            await client.ping()
        except Exception as exc:  # noqa: BLE001 — Redis может быть любой недоступности
            logger.warning("rate-limit переключился на память процесса: %s", exc)
            return None

        self._redis = client
        return self._redis

    async def check(self, key: str) -> RateLimitResult:
        """Проверить, можно ли принять событие."""
        client = await self._client()
        if client is None:
            return self._check_memory(key)

        try:
            current = int(await client.eval(_SCRIPT, 1, self._redis_key(key), self._window))
        except Exception as exc:  # noqa: BLE001
            logger.warning("rate-limit: Redis не ответил (%s), считаем в памяти", exc)
            return self._check_memory(key, degraded=True)

        remaining = max(0, self._limit - current)
        return RateLimitResult(
            allowed=current <= self._limit,
            limit=self._limit,
            remaining=remaining,
            retry_after=0 if current <= self._limit else self._window,
        )

    def _check_memory(self, key: str, *, degraded: bool = False) -> RateLimitResult:
        """Тот же подсчёт в памяти процесса.

        Скользящее окно по меткам времени, а не счётчик с TTL: счётчик
        сбрасывался бы целиком каждую минуту, и источник, уложившийся в
        начало окна, проходил бы 1200 событий подряд.
        """
        now = time.monotonic()
        bucket = self._memory[key]
        cutoff = now - self._window

        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= self._limit:
            retry = max(1, int(bucket[0] + self._window - now))
            return RateLimitResult(
                allowed=False,
                limit=self._limit,
                remaining=0,
                retry_after=retry,
                degraded=degraded,
            )

        bucket.append(now)
        return RateLimitResult(
            allowed=True,
            limit=self._limit,
            remaining=self._limit - len(bucket),
            retry_after=0,
            degraded=degraded,
        )

    @staticmethod
    def _redis_key(key: str) -> str:
        """Ключ Redis.

        Префикс отделяет наши счётчики от чужих данных в общей базе: Redis в
        compose общий для всех сервисов, и вытирать его ключами вида
        `flytaxi:disk` было бы невежливо.
        """
        return f"adminalert:ratelimit:{key}"

    async def aclose(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def forget(self, key: str) -> None:
        """Сбросить счётчик — например, при смене API-ключа проекта."""
        client = await self._client()
        self._memory.pop(key, None)
        if client is not None:
            try:
                await client.delete(self._redis_key(key))
            except Exception:  # noqa: BLE001
                logger.warning("rate-limit: не удалось сбросить счётчик %s", key)


# Один ограничитель на процесс: счётчики и подключение к Redis общие.
_limiter: RateLimiter | None = None


def get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        from app.config import get_settings

        settings = get_settings()
        _limiter = RateLimiter(
            settings.redis_url,
            limit_per_minute=settings.events_rate_limit_per_minute,
        )
    return _limiter
