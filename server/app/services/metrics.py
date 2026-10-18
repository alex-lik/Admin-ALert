"""Метрики для Prometheus (план, задача 2.23).

Зачем они: если сам сервис пейджинга упал, узнать об этом должен
**независимый** watchdog. Следить за тем, что внутри, некому — некому и
принимать алерты. Поэтому метрики читаются снаружи, а в этом модуле нет
ни строки, которая может уронить приём события.

Что отслеживается и почему именно это:

- `alerts_active` по состояниям — сколько алертов висит. Всплеск активных
  означает шторм или поломку источника.
- `worker_backlog` — алерты, чей таймер просрочен. **Это главная метрика
  исправности**: если очередь растёт, эскалация не работает, и никакие
  алерты об этом не сообщат.
- `steps_total{channel,outcome}` — что реально доставляется. Нулевой поток
  `delivered` при ненулевом числе шагов означает, что каналы молчат.
- `events_total{status}` — приём событий, включая отклонённые по лимиту.
- `ratelimit_degraded` — счётчик ситуаций, когда Redis не ответил и лимит
  считался в памяти процесса. Метрика в том, что защита ослаблена.

Формат текстовый, без `prometheus_client`: зависимость ради двадцати строк
кода не стоит, а формат `/metrics` стабилен и понимается любым сборщиком.
"""

from __future__ import annotations

import threading
from collections import defaultdict


class Metrics:
    """Счётчики процесса. Потокобезопасно: пишет воркер, читает HTTP."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Счётчики: имя -> метки -> значение. Имена с _total — монотонные.
        self._counters: dict[str, dict[tuple[tuple[str, str], ...], float]] = defaultdict(dict)
        # Одноразовые значения: имя -> метки -> число.
        self._gauges: dict[str, dict[tuple[tuple[str, str], ...], float]] = defaultdict(dict)

    def increment(
        self, name: str, labels: dict[str, str] | None = None, amount: float = 1.0
    ) -> None:
        key = _key(labels)
        with self._lock:
            bucket = self._counters[name]
            bucket[key] = bucket.get(key, 0.0) + amount

    def set_gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        key = _key(labels)
        with self._lock:
            self._gauges[name][key] = value

    # ── События источников ──

    def event_accepted(self, status: str, created: bool) -> None:
        """Событие принято. `duplicate` отделён: повторы — это нормальная
        работа дедупликации, а не новый инцидент."""
        self.increment(
            "adminalert_events_total",
            {"status": str(status), "new": "true" if created else "false"},
        )

    def event_rate_limited(self, degraded: bool) -> None:
        """Запрос отклонён по лимиту.

        Вызывается именно при отказе, а не на каждом запросе: иначе счётчик
        показывал бы не «сколько отклонено», а «сколько всего», и всплеск
        лимита был бы не виден в мониторинге.
        """
        self.increment("adminalert_events_total", {"status": "rate_limited"})
        if degraded:
            # Отдельный счётчик: Redis не ответил, лимит считается в памяти
            # процесса и не общий между инстансами.
            self.increment("adminalert_ratelimit_degraded_total")

    # ── Шаги эскалации ──

    def step(self, channel: str, outcome: str) -> None:
        self.increment("adminalert_steps_total", {"channel": channel, "outcome": outcome})

    def alert_created(self, profile: str) -> None:
        self.increment("adminalert_alerts_created_total", {"profile": profile})

    def alert_state_changed(self, state: str) -> None:
        self.increment("adminalert_alert_transitions_total", {"state": state})

    def alert_deferred_by_quiet_hours(self) -> None:
        """Алерт перенесён тихими часами. Растущее число означает, что ночью
        накопилась масса предупреждений — полезно видеть заранее."""
        self.increment("adminalert_quiet_hours_deferred_total")

    # ── Исправность воркера ──

    def worker_poll(self, processed: int, due: int) -> None:
        self.set_gauge("adminalert_worker_due", due)
        self.set_gauge("adminalert_worker_processed_last", processed)

    def render(self) -> str:
        """Текстовое представление в формате Prometheus."""
        with self._lock:
            counters = {k: dict(v) for k, v in self._counters.items()}
            gauges = {k: dict(v) for k, v in self._gauges.items()}

        lines: list[str] = []

        for name, samples in sorted(counters.items()):
            lines.append(f"# HELP {name} Счётчик {name}.")
            lines.append(f"# TYPE {name} counter")
            for labels, value in sorted(samples.items()):
                lines.append(f"{name}{_render_labels(labels)} {value:g}")

        for name, samples in sorted(gauges.items()):
            lines.append(f"# HELP {name} Текущее значение {name}.")
            lines.append(f"# TYPE {name} gauge")
            for labels, value in sorted(samples.items()):
                lines.append(f"{name}{_render_labels(labels)} {value:g}")

        return "\n".join(lines) + "\n"


def _key(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((labels or {}).items()))


def _render_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    parts = [f'{k}="{_escape(v)}"' for k, v in labels]
    return "{" + ",".join(parts) + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


# Один экземпляр на процесс: счётчики переживают запросы.
metrics = Metrics()
