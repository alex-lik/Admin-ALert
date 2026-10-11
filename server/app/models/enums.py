"""Автомат состояний алерта (задача 2.5).

    triggered → notifying(step N) → acked / escalated / resolved / expired

Все переходы должны быть явно перечислены: любое другое состояние — баг.
"""

from enum import StrEnum


class AlertState(StrEnum):
    TRIGGERED = "triggered"
    NOTIFYING = "notifying"
    ACKED = "acked"
    ESCALATED = "escalated"
    RESOLVED = "resolved"
    EXPIRED = "expired"


TERMINAL_STATES = frozenset({AlertState.ACKED, AlertState.RESOLVED, AlertState.EXPIRED})

ALLOWED_TRANSITIONS: dict[AlertState, frozenset[AlertState]] = {
    AlertState.TRIGGERED: frozenset({AlertState.NOTIFYING, AlertState.ACKED, AlertState.RESOLVED}),
    AlertState.NOTIFYING: frozenset(
        {
            AlertState.NOTIFYING,  # следующий шаг той же эскалации
            AlertState.ESCALATED,  # шаг эскалации завершён успешно
            AlertState.ACKED,
            AlertState.RESOLVED,
            AlertState.EXPIRED,  # исчерпан лимит повторов
        }
    ),
    AlertState.ESCALATED: frozenset(
        {AlertState.NOTIFYING, AlertState.RESOLVED, AlertState.EXPIRED}
    ),
    AlertState.ACKED: frozenset({AlertState.RESOLVED}),
    AlertState.RESOLVED: frozenset(),
    AlertState.EXPIRED: frozenset(),
}


def can_transition(current: AlertState, target: AlertState) -> bool:
    return target in ALLOWED_TRANSITIONS[current]
