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
    ],
)
def test_allowed_transitions(current: AlertState, target: AlertState) -> None:
    assert can_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (AlertState.RESOLVED, AlertState.NOTIFYING),  # resolve необратим
        (AlertState.EXPIRED, AlertState.NOTIFYING),  # лимит повторов исчерпан
        (AlertState.ACKED, AlertState.NOTIFYING),  # ack останавливает цепочку
    ],
)
def test_forbidden_transitions(current: AlertState, target: AlertState) -> None:
    assert not can_transition(current, target)


def test_terminal_states_have_no_outgoing_transitions() -> None:
    for state in TERMINAL_STATES:
        assert not can_transition(state, AlertState.NOTIFYING)
