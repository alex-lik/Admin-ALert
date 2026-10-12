"""Юнит-тесты: автомат состояний (задача 5.1, каркас под полное покрытие)."""

import pytest

from app.models.enums import TERMINAL_STATES, AlertState, can_transition


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (AlertState.TRIGGERED, AlertState.NOTIFYING),
        (AlertState.NOTIFYING, AlertState.NOTIFYING),
        (AlertState.NOTIFYING, AlertState.ESCALATED),
        (AlertState.NOTIFYING, AlertState.ACKED),
        (AlertState.NOTIFYING, AlertState.EXPIRED),
        (AlertState.ESCALATED, AlertState.RESOLVED),
        (AlertState.ACKED, AlertState.RESOLVED),
        # Передача другому снимает чужое подтверждение: новый владелец
        # должен подтвердить сам, поэтому возврат в notifying легален.
        (AlertState.ACKED, AlertState.NOTIFYING),
        # Профиль без шагов: эскалировать нечего, алерт сразу истёк.
        (AlertState.TRIGGERED, AlertState.EXPIRED),
    ],
)
def test_allowed_transitions(current: AlertState, target: AlertState) -> None:
    assert can_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (AlertState.RESOLVED, AlertState.NOTIFYING),  # resolve необратим
        (AlertState.EXPIRED, AlertState.NOTIFYING),  # лимит повторов исчерпан
    ],
)
def test_forbidden_transitions(current: AlertState, target: AlertState) -> None:
    assert not can_transition(current, target)


def test_terminal_states_have_no_outgoing_transitions() -> None:
    for state in TERMINAL_STATES:
        assert not can_transition(state, AlertState.NOTIFYING)
