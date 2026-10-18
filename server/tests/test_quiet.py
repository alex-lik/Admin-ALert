"""Тихие часы: уважает только warning (план, задача 2.13).

Правило несимметричное, и в этом весь смысл: `critical` ночью эскалирует как
обычно, `warning` ждёт утра. Если бы тихие часы действовали на всех, ночью
терялись бы падения сертификатов, а система перестала бы быть системой
алертинга.

Проверяются три вещи, и все три — реальные способы сломать правило:

1. граница окна (тихо ровно в 23:00, не тихо в 08:00);
2. переход через полночь — обычное ночное дежурство;
3. часовой пояс адресата, а не сервера: дежурный в Киеве и администратор во
   Франкфурте имеют разное «23:00».
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.quiet import QuietWindow, zone_of

# 2026-10-18 — всё ещё летнее время (переход на зимнее в ЕС 25 октября),
# поэтому Киев смещён на +3. Смещение считаем через zoneinfo, а не константой:
# летнее время меняется, и зашитая цифра рано или поздно соврёт.
WINTER = "Europe/Kyiv"
KYIV = zone_of(WINTER)


def at(hour: int, minute: int = 0, day: int = 18) -> datetime:
    """Момент в UTC, соответствующий указанному киевскому времени."""
    local = datetime(2026, 10, day, hour, minute, tzinfo=KYIV)
    return local.astimezone(UTC)


NIGHT = QuietWindow(start_hour=23, end_hour=8)


# ───────────────────────────── Границы окна ─────────────────────────────


def test_quiet_starts_exactly_at_start_hour() -> None:
    """Ровно в 23:00 уже тихо. Минута погоды не должна решать судьбу алерта."""
    assert NIGHT.is_quiet(at(23, 0), WINTER) is True


def test_not_quiet_exactly_at_end_hour() -> None:
    """Ровно в 08:00 уже не тихо — дежурный встал и ждёт алерты."""
    assert NIGHT.is_quiet(at(8, 0), WINTER) is False


def test_midnight_is_quiet() -> None:
    assert NIGHT.is_quiet(at(0, 30), WINTER) is True


def test_daytime_is_not_quiet() -> None:
    assert NIGHT.is_quiet(at(14, 0), WINTER) is False


def test_evening_before_start_is_not_quiet() -> None:
    assert NIGHT.is_quiet(at(22, 59), WINTER) is False


def test_morning_before_end_is_still_quiet() -> None:
    """За минуту до конца окна тихо — иначе последняя минута ночи будила бы."""
    assert NIGHT.is_quiet(at(7, 59), WINTER) is True


# ───────────────────────────── Окно внутри суток ─────────────────────────────


def test_window_inside_single_day() -> None:
    """Ночная смена 02:00–06:00 не переходит через полночь."""
    window = QuietWindow(start_hour=2, end_hour=6)

    assert window.is_quiet(at(1, 0), WINTER) is False
    assert window.is_quiet(at(2, 0), WINTER) is True
    assert window.is_quiet(at(5, 59), WINTER) is True
    assert window.is_quiet(at(6, 0), WINTER) is False


# ───────────────────────────── Часовые пояса ─────────────────────────────


def test_timezone_of_recipient_is_respected() -> None:
    """Один и тот же момент — тихо для одного адресата и не тихо для другого.

    22:00 UTC — это 01:00 в Киеве (тихо) и 00:00 в Лос-Анджелесе (тихо), но
    23:00 UTC — уже 02:00 по Тбилиси, где тихие часы могут быть иначе. Суть в
    том, что окно считается по часам адресата, а не по часам сервера.
    """
    moment = datetime(2026, 10, 18, 22, 0, tzinfo=UTC)

    # Тихие часы 00:00–06:00: в 01:00 по Киеву тихо, в 15:00 по Лос-Анджелесе нет.
    window = QuietWindow(start_hour=0, end_hour=6)

    assert window.is_quiet(moment, WINTER) is True
    assert window.is_quiet(moment, "America/Los_Angeles") is False


def test_unknown_timezone_falls_back_to_utc() -> None:
    """Опечатка в зоне не должна ронять воркер посреди ночи."""
    moment = datetime(2026, 10, 18, 22, 0, tzinfo=UTC)
    window = QuietWindow(start_hour=21, end_hour=23)

    assert window.is_quiet(moment, "Не/Такая/Зона") is True


def test_zone_of_returns_fallback() -> None:
    assert str(zone_of("выдумка")) == "UTC"


# ───────────────────────────── Окончание окна ─────────────────────────────


def test_ends_at_is_today_end_when_inside_window() -> None:
    """Окончание сегодняшнего окна — сегодня, даже если момент за полночь."""
    window = QuietWindow(start_hour=0, end_hour=6)

    ends = window.ends_at(at(2, 0), WINTER)

    assert ends.astimezone(zone_of(WINTER)).hour == 6
    assert ends.astimezone(zone_of(WINTER)).day == 18


def test_ends_at_crosses_midnight() -> None:
    """В 23:30 тихо до 08:00 следующего дня — таймер ставится на завтра."""
    window = QuietWindow(start_hour=23, end_hour=8)

    local_end = window.ends_at(at(23, 30), WINTER).astimezone(zone_of(WINTER))

    assert local_end.hour == 8
    assert local_end.day == 19


def test_ends_at_when_outside_window_is_now() -> None:
    """Вне тихих часов переносить нечего: возвращаем тот же момент."""
    window = QuietWindow(start_hour=23, end_hour=8)

    assert window.ends_at(at(12, 0), WINTER) == at(12, 0)


# ───────────────────────────── Настройки окна ─────────────────────────────


def test_same_start_and_end_means_not_configured() -> None:
    """Начало равно концу — это «окно не задано», а не «тихо круглосуточно»."""
    window = QuietWindow(start_hour=8, end_hour=8)

    assert window.is_configured is False
    assert window.is_quiet(at(3, 0), WINTER) is False


def test_disabled_window_never_quiet() -> None:
    window = QuietWindow(start_hour=23, end_hour=8, enabled=False)

    assert window.is_quiet(at(2, 0), WINTER) is False


@pytest.mark.parametrize("hour", [-1, 24, 99])
def test_invalid_hour_is_rejected(hour: int) -> None:
    """Опечатка в конфиге должна падать на старте, а не молча гасить эскалацию."""
    with pytest.raises(ValueError, match="вне диапазона"):
        QuietWindow(start_hour=hour, end_hour=8)


def test_window_from_settings() -> None:
    from app.services.quiet import window_from_settings

    class Settings:
        quiet_hours_start = 22
        quiet_hours_end = 7
        quiet_hours_enabled = True

    window = window_from_settings(Settings())

    assert (window.start_hour, window.end_hour) == (22, 7)
    assert window.describe() == "тихие часы 22:00–07:00"


def test_describe_when_not_configured() -> None:
    assert QuietWindow(start_hour=8, end_hour=8).describe() == "тихие часы не заданы"
