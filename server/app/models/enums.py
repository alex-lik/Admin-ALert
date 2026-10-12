"""Автомат состояний алерта (задача 2.5).

    triggered → notifying(step N) → acked / escalated / resolved / expired

Все переходы должны быть явно перечислены: любое другое состояние — баг.
"""

from enum import StrEnum


class Severity(StrEnum):
    """Уровень алерта.

    Разница между ними не в формулировке, а в поведении:
    `warning` уважает тихие часы, `critical` — нет. Никто не должен
    проснуться из-за того, что ночью истёк сертификат.
    """

    WARNING = "warning"
    CRITICAL = "critical"


class AlertState(StrEnum):
    TRIGGERED = "triggered"
    NOTIFYING = "notifying"
    ACKED = "acked"
    ESCALATED = "escalated"
    RESOLVED = "resolved"
    EXPIRED = "expired"


# Состояния, из которых нет выхода. ACKED здесь нет намеренно: подтверждённый
# алерт переоткрывается передачей другому (reassign), поэтому он «закрыт,
# но не терминален». Воркер такие алерты не трогает — у них нет таймера.
TERMINAL_STATES = frozenset({AlertState.RESOLVED, AlertState.EXPIRED})

ALLOWED_TRANSITIONS: dict[AlertState, frozenset[AlertState]] = {
    AlertState.TRIGGERED: frozenset(
        {
            AlertState.NOTIFYING,
            AlertState.ACKED,
            AlertState.RESOLVED,
            # Профиль без шагов: эскалировать нечего, алерт сразу истёк.
            AlertState.EXPIRED,
        }
    ),
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
    AlertState.ACKED: frozenset(
        {
            AlertState.RESOLVED,
            # Передача алерта другому админу снимает чужое подтверждение:
            # новый владелец проблему ещё не видел и должен подтвердить сам.
            AlertState.NOTIFYING,
        }
    ),
    AlertState.RESOLVED: frozenset(),
    AlertState.EXPIRED: frozenset(),
}


def can_transition(current: AlertState, target: AlertState) -> bool:
    return target in ALLOWED_TRANSITIONS[current]
