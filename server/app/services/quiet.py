"""Тихие часы (план, задача 2.13).

Правило одно и оно несимметричное: **тихие часы уважает только `warning`**.
Для `critical` они игнорируются — ночь не должна отменять «упал сертификат»,
иначе система перестанет быть системой алертинга, а станет расписанием
«посмотри утром».

Что здесь происходит с алертом в тихие часы:

- шаг не выполняется вовсе — дежурного не будят;
- таймер сдвигается на момент окончания тихих часов;
- на следующий шаг после сдвига это тоже распространяется. Иначе цепочка
  `push (0с) → telegram (120с) → call (300с)` ночью разбудит на первом же шаге,
  а к утру доберётся до звонка — тихие часы получились бы только для первого
  шага.

Алерт при этом **не теряется и не истекает**: он ждёт своего утра и
эскалирует дальше. Молча терять предупреждения — значит обнаружить о них
через месяц по отсутствию отчёта.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Если часовой пояс админа неизвестен (опечатка в настройке), берём UTC.
# Это тихие часы по серверному времени, а не «игнорировать правило»: для
# warning лучше не разбудить, для critical правило всё равно не действует.
FALLBACK_TIMEZONE = "UTC"


@dataclass(frozen=True)
class QuietWindow:
    """Окно тихих часов в местном времени адресата.

    Границы включительны: тихо с 23:00 до 08:00. Окно может переходить через
    полночь (`start > end`) — обычный случай для ночного дежурства.
    """

    start_hour: int
    end_hour: int
    enabled: bool = True

    def __post_init__(self) -> None:
        for name, value in (("start_hour", self.start_hour), ("end_hour", self.end_hour)):
            if not 0 <= value <= 23:
                raise ValueError(f"{name} вне диапазона 0..23: {value}")

    @property
    def is_configured(self) -> bool:
        """Окно реально задано.

        `start == end` читается как «окно не задано», а не как «тихо круглосуточно».
        Так безопаснее: опечатка в конфиге гасит эскалацию warning совсем, и
        молча пропавший канал заметен куда меньше, чем молча пропавшие алерты.
        """
        return self.enabled and self.start_hour != self.end_hour

    def local_time(self, moment: datetime, timezone: str) -> datetime:
        """Перевести момент в местное время адресата."""
        return moment.astimezone(zone_of(timezone))

    def is_quiet(self, moment: datetime, timezone: str) -> bool:
        """Сейчас тихие часы по времени этого адресата?"""
        if not self.is_configured:
            return False

        local = self.local_time(moment, timezone)

        if self.start_hour < self.end_hour:
            # Окно внутри суток: 02:00–06:00.
            return self.start_hour <= local.hour < self.end_hour

        # Окно через полночь: 23:00–08:00.
        return local.hour >= self.start_hour or local.hour < self.end_hour

    def ends_at(self, moment: datetime, timezone: str) -> datetime:
        """Во что переводить таймер алерта.

        Если сейчас не тихо, возвращается тот же момент: переносить нечего, и
        вызывающий код не должен запоминать, тихо сейчас или нет. Иначе
        вернётся конец ближайшего тихого окна — в местном времени адресата.
        """
        if not self.is_quiet(moment, timezone):
            return moment

        tz = zone_of(timezone)
        local = moment.astimezone(tz)

        # Момент окончания сегодняшнего окна в местном времени.
        candidate = local.replace(hour=self.end_hour, minute=0, second=0, microsecond=0)
        if candidate <= local:
            candidate += timedelta(days=1)

        return candidate.astimezone(moment.tzinfo or candidate.tzinfo)

    def describe(self) -> str:
        if not self.is_configured:
            return "тихие часы не заданы"
        return f"тихие часы {self.start_hour:02d}:00–{self.end_hour:02d}:00"


def zone_of(timezone: str) -> ZoneInfo:
    """Часовой пояс или UTC, если он неизвестен.

    Строки таймзон приходят из БД, и там они задаются руками через API.
    Неизвестная зона не должна ронять воркер посреди ночи.
    """
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo(FALLBACK_TIMEZONE)


def window_from_settings(settings: object) -> QuietWindow:
    """Собрать окно из настроек приложения."""
    return QuietWindow(
        start_hour=int(getattr(settings, "quiet_hours_start", 23)),
        end_hour=int(getattr(settings, "quiet_hours_end", 8)),
        enabled=bool(getattr(settings, "quiet_hours_enabled", True)),
    )


def as_time(value: datetime) -> time:
    """Время суток из момента — для компактных логов и подписей."""
    return value.timetz()
