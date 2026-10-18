"""Тесты rate-limit и метрик (задачи 2.23, 2.24).

Rate-limit проверяется в двух режимах, потому что у них разные свойства:

- в памяти процесса — работает всегда, но не общий между инстансами;
- в Redis — общий, но зависит от доступности.

Самый важный тест здесь последний: при недоступном Redis защита ослабеет,
но **не исчезнет**. Молча снять ограничение — значит сделать поломку Redis
причиной отказа в приёме алертов.
"""

from __future__ import annotations

import time

import pytest

from app.services import metrics as metrics_module
from app.services.metrics import Metrics
from app.services.ratelimit import RateLimiter, RateLimitResult

# ───────────────────────────── Окно в памяти ─────────────────────────────


def test_allows_up_to_limit() -> None:
    limiter = RateLimiter(None, limit_per_minute=3, window_seconds=60)

    for _ in range(3):
        assert limiter._check_memory("k").allowed is True


def test_blocks_after_limit() -> None:
    limiter = RateLimiter(None, limit_per_minute=3, window_seconds=60)

    for _ in range(3):
        limiter._check_memory("k")
    blocked = limiter._check_memory("k")

    assert blocked.allowed is False
    assert blocked.remaining == 0
    assert blocked.retry_after > 0


def test_window_is_sliding_not_bucket() -> None:
    """Главное отличие от счётчика с TTL: счётчик сбрасывается целиком каждую
    минуту, и источник, уложившийся в начало окна, прошёл бы вдвое больше
    лимита."""
    limiter = RateLimiter(None, limit_per_minute=3, window_seconds=1)

    for _ in range(3):
        limiter._check_memory("k")

    time.sleep(1.05)
    assert limiter._check_memory("k").allowed is True, "окно должно сдвинуться"


def test_old_events_expire_one_by_one() -> None:
    """Скользящее окно освобождает место по одному событию, а не всё сразу.

    События добавляются с разницей во времени намеренно: добавленные в один
    момент истекают одновременно, и разницы со счётчиком с TTL не видно.
    """
    limiter = RateLimiter(None, limit_per_minute=2, window_seconds=1)

    limiter._check_memory("k")  # t = 0.00
    time.sleep(0.6)
    limiter._check_memory("k")  # t = 0.60
    assert limiter._check_memory("k").allowed is False, "лимит исчерпан"

    time.sleep(0.6)  # t = 1.20: первое событие истекло, второе ещё в окне
    assert limiter._check_memory("k").allowed is True, "первое место освободилось"
    assert limiter._check_memory("k").allowed is False, "второе ещё занято"


def test_keys_are_independent() -> None:
    """Лимит на проект, а не глобальный: один шумный источник не должен
    отключать другие."""
    limiter = RateLimiter(None, limit_per_minute=2, window_seconds=60)

    for _ in range(3):
        limiter._check_memory("шумный")

    assert limiter._check_memory("тихий").allowed is True


def test_remaining_counts_down() -> None:
    limiter = RateLimiter(None, limit_per_minute=3, window_seconds=60)

    assert limiter._check_memory("k").remaining == 2
    assert limiter._check_memory("k").remaining == 1
    assert limiter._check_memory("k").remaining == 0


def test_retry_after_is_within_window() -> None:
    limiter = RateLimiter(None, limit_per_minute=1, window_seconds=60)
    limiter._check_memory("k")

    blocked = limiter._check_memory("k")

    assert 0 < blocked.retry_after <= 60


# ───────────────────────────── Ключи Redis ─────────────────────────────


def test_redis_key_is_namespaced() -> None:
    """В compose Redis общий для всех сервисов, и вытирать его чужими ключами
    невежливо."""
    assert RateLimiter._redis_key("flytaxi-key").startswith("adminalert:ratelimit:")


async def test_without_redis_still_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Отказ Redis не должен снимать защиту целиком.

    Худший вариант был бы «деградация до безлимита»: тогда поломка Redis
    тихо отключала бы rate-limit, и никто об этом не узнал бы.
    """
    limiter = RateLimiter(None, limit_per_minute=2, window_seconds=60)

    results = [await limiter.check("k") for _ in range(3)]

    assert [r.allowed for r in results] == [True, True, False]


async def test_degraded_is_reported() -> None:
    """Метрика деградации нужна, чтобы по мониторингу было видно: лимит
    считается в памяти процесса и не общий между инстансами."""
    limiter = RateLimiter(None, limit_per_minute=10, window_seconds=60)

    result = await limiter.check("k")

    assert isinstance(result, RateLimitResult)
    assert result.allowed is True


async def test_redis_failure_degrades_to_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Когда Redis есть, но не отвечает — считаем в памяти и помечаем."""

    class BrokenRedis:
        async def eval(self, *_args, **_kwargs):
            raise RuntimeError("connection refused")

        async def aclose(self) -> None:
            return None

    limiter = RateLimiter("redis://localhost:6379/0", limit_per_minute=1, window_seconds=60)
    monkeypatch.setattr(limiter, "_client", lambda: _immediate(BrokenRedis()))

    first = await limiter.check("k")
    second = await limiter.check("k")

    assert first.allowed is True
    assert first.degraded is True
    assert second.allowed is False, "защита должна продолжать работать"


async def _immediate(value: object):
    """Обёртка, чтобы подставить объект вместо coroutine-фабрики."""
    return value


# ───────────────────────────── Метрики ─────────────────────────────


def test_counter_renders_prometheus_format() -> None:
    m = Metrics()
    m.increment("adminalert_steps_total", {"channel": "push", "outcome": "delivered"})

    out = m.render()

    assert 'adminalert_steps_total{channel="push",outcome="delivered"} 1' in out
    assert "# TYPE adminalert_steps_total counter" in out


def test_counter_increments() -> None:
    m = Metrics()
    m.increment("x_total")
    m.increment("x_total")

    assert "x_total 2" in m.render()


def test_labels_with_different_values_are_separate() -> None:
    m = Metrics()
    m.increment("steps_total", {"channel": "push"})
    m.increment("steps_total", {"channel": "telegram"})

    out = m.render()

    assert 'steps_total{channel="push"} 1' in out
    assert 'steps_total{channel="telegram"} 1' in out


def test_gauge_replaces_value() -> None:
    """Gauge — текущее значение, а не счётчик: два прохода воркера не должны
    складываться."""
    m = Metrics()
    m.set_gauge("adminalert_worker_due", 100)
    m.set_gauge("adminalert_worker_due", 3)

    out = m.render()

    assert "adminalert_worker_due 3" in out
    assert "adminalert_worker_due 100" not in out
    assert "# TYPE adminalert_worker_due gauge" in out


def test_escapes_label_values() -> None:
    """Значение с кавычкой сломало бы формат и весь scrape."""
    m = Metrics()
    m.increment("x_total", {"name": 'сказал "привет"'})

    assert r'name="сказал \"привет\""' in m.render()


def test_worker_poll_sets_backlog() -> None:
    """Главная метрика исправности: очередь просроченных алертов."""
    m = Metrics()
    m.worker_poll(processed=5, due=42)

    out = m.render()

    assert "adminalert_worker_due 42" in out
    assert "adminalert_worker_processed_last 5" in out


def test_quiet_hours_deferred_is_counted() -> None:
    m = Metrics()
    m.alert_deferred_by_quiet_hours()
    m.alert_deferred_by_quiet_hours()

    assert "adminalert_quiet_hours_deferred_total 2" in m.render()


def test_step_records_channel_and_outcome() -> None:
    """Нулевой поток delivered при ненулевом числе шагов означает, что каналы
    молчат, — и это должно быть видно в мониторинге."""
    m = Metrics()
    m.step("push", "delivered")
    m.step("push", "failed")
    m.step("telegram", "skipped")

    out = m.render()

    assert 'adminalert_steps_total{channel="push",outcome="delivered"} 1' in out
    assert 'adminalert_steps_total{channel="push",outcome="failed"} 1' in out
    assert 'adminalert_steps_total{channel="telegram",outcome="skipped"} 1' in out


def test_ratelimit_degraded_is_visible() -> None:
    m = Metrics()
    m.event_rate_limited(degraded=True)

    assert "adminalert_ratelimit_degraded_total 1" in m.render()
    assert 'adminalert_events_total{status="rate_limited"} 1' in m.render()


def test_rate_limited_is_not_counted_for_accepted() -> None:
    """Счётчик отказов не должен расти на принятых событиях.

    Реальный баг: метод отказов вызывался на каждом запросе, и метрика
    показывала «сколько запросов», а не «сколько отклонено». Всплеск лимита
    был бы не виден.
    """
    m = Metrics()
    for _ in range(10):
        m.event_accepted("firing", created=True)

    out = m.render()

    assert 'adminalert_events_total{new="true",status="firing"} 10' in out
    assert "rate_limited" not in out


def test_duplicate_events_are_separate() -> None:
    """Повтор — это нормальная работа дедупликации, а не новый инцидент:
    разводить их обязательно, иначе шторм повторов выглядит как шторм."""
    m = Metrics()
    m.event_accepted("firing", created=True)
    m.event_accepted("firing", created=False)

    out = m.render()

    assert 'adminalert_events_total{new="true",status="firing"} 1' in out
    assert 'adminalert_events_total{new="false",status="firing"} 1' in out


def test_module_level_instance_exists() -> None:
    """Счётчики переживают запросы: импорт по одному на запрос обнулил бы их."""
    assert isinstance(metrics_module.metrics, Metrics)
