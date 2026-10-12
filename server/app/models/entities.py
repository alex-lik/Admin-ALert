"""Модели предметной области (план, задача 1.1).

Схема покрывает всё, из чего состоит эскалация:

    Project ─┬─< Check ─┬─< Alert >─ Admin (ack)
             │          │      ├─< AlertEvent   (аудит шагов)
    Host ────┘          │      ├─< EscalationStep
                        │      └─  Device[]  (кто и куда пейджится)
    Admin ──< OnCallShift >─ Admin
    EscalationProfile ──< EscalationStepDefinition

Ключевые решения:

* `Alert.next_action_at` — вся «память» таймеров живёт здесь, а не в
  процессе воркера. Рестарт воркера не теряет эскалацию (задача 1.4).
* `Alert.dedup_key` уникален среди активных алертов: повторное событие
  обновляет счётчик, а не создаёт новый алерт (задача 2.4).
* `AlertStep` хранит, что уже выполнено. По нему считается, какой шаг
  следующий, и рисуется история эскалации в клиенте (задача 2.22).
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import AlertState, Severity


class Project(TimestampMixin, Base):
    """Проект, за которым следят. `is_vip` включает агрессивный профиль."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    is_vip: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    # Профиль по умолчанию для алертов проекта. None — решает severity.
    default_profile: Mapped[str | None] = mapped_column(String(32))

    api_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    alerts: Mapped[list["Alert"]] = relationship(back_populates="project")
    checks: Mapped[list["Check"]] = relationship(back_populates="project")
    hosts: Mapped[list["Host"]] = relationship(back_populates="project")

    def __repr__(self) -> str:  # pragma: no cover - отладочное
        return f"<Project {self.id} vip={self.is_vip}>"


class Host(TimestampMixin, Base):
    """Хост или иной источник проверки: сервер, контейнер, десктоп."""

    __tablename__ = "hosts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # Небольшая заметка: роль хоста, стойка, что угодно — помогает админу
    # сориентироваться ночью.
    note: Mapped[str | None] = mapped_column(Text)

    # Хост принадлежит проекту; обратная сторона связи — Project.hosts.
    project: Mapped["Project"] = relationship(back_populates="hosts")
    checks: Mapped[list["Check"]] = relationship(back_populates="host")


class Check(TimestampMixin, Base):
    """Проверка: что именно наблюдаем. `key` совпадает с `check` во входящем событии."""

    __tablename__ = "checks"
    __table_args__ = (UniqueConstraint("project_id", "key", name="uq_check_project_key"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # На каком хосте проверка выполняется по умолчанию. Алерт хранит свой
    # host_id отдельно: одна и та же проверка бывает на разных хостах.
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False)
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    default_severity: Mapped[Severity] = mapped_column(
        String(16), default=Severity.WARNING, server_default="warning", nullable=False
    )
    runbook_url: Mapped[str | None] = mapped_column(Text)

    project: Mapped["Project"] = relationship(back_populates="checks")
    host: Mapped["Host"] = relationship(back_populates="checks")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Check {self.key}>"


class Admin(TimestampMixin, Base):
    """Админ-дежурный."""

    __tablename__ = "admins"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        String(16), default="admin", server_default="admin", nullable=False
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    # Хранится UTC. Часовой пояс нужен, чтобы вычислить смену дежурства.
    timezone: Mapped[str] = mapped_column(
        String(64), default="Europe/Kyiv", server_default="Europe/Kyiv", nullable=False
    )

    devices: Mapped[list["Device"]] = relationship(
        back_populates="admin", cascade="all, delete-orphan"
    )


class Device(TimestampMixin, Base):
    """Устройство админа с push-токеном (задача 2.2)."""

    __tablename__ = "devices"
    __table_args__ = (Index("ix_devices_last_seen_at", "last_seen_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    admin_id: Mapped[str] = mapped_column(
        ForeignKey("admins.id", ondelete="CASCADE"), nullable=False
    )

    # FCM или APNs токен. Уникален: один токен — одно устройство.
    token: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)  # android | ios
    platform_label: Mapped[str | None] = mapped_column(String(120))

    # Heartbeat: если устройство давно не отвечало, эскалация идёт на звонок
    # раньше (задача 2.21).
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Факт, что хотя бы одно уведомление доставлено — пригодится в метриках.
    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )

    admin: Mapped["Admin"] = relationship(back_populates="devices")


class OnCallShift(TimestampMixin, Base):
    """Смена дежурства (задача 2.10). Записи непрерывно покрывают время."""

    __tablename__ = "oncall_shifts"
    __table_args__ = (Index("ix_oncall_shifts_window", "starts_at", "ends_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    admin_id: Mapped[str] = mapped_column(
        ForeignKey("admins.id", ondelete="CASCADE"), nullable=False
    )
    # primary — дежурный, backup — подключается, если на основного нет ответа.
    role: Mapped[str] = mapped_column(
        String(16), default="primary", server_default="primary", nullable=False
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    admin: Mapped["Admin"] = relationship()


class EscalationProfile(TimestampMixin, Base):
    """Профиль эскалации (задача 1.2)."""

    __tablename__ = "escalation_profiles"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # warning | critical | ...
    description: Mapped[str | None] = mapped_column(Text)
    # Тихие часы применяются только там, где это осмысленно: для critical
    # эскалация не откладывается на ночь (задача 2.13).
    respects_quiet_hours: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    max_steps: Mapped[int] = mapped_column(Integer, default=8, server_default="8", nullable=False)

    steps: Mapped[list["EscalationStepDefinition"]] = relationship(
        back_populates="profile",
        cascade="all, delete-orphan",
        order_by="EscalationStepDefinition.step",
    )


class EscalationStepDefinition(TimestampMixin, Base):
    """Шаг внутри профиля: что, через сколько и кому."""

    __tablename__ = "escalation_step_definitions"
    __table_args__ = (UniqueConstraint("profile_id", "step", name="uq_step_profile_step"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("escalation_profiles.id", ondelete="CASCADE"), nullable=False
    )
    step: Mapped[int] = mapped_column(Integer, nullable=False)

    # push | telegram | call | snooze
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    # Через сколько секунд от начала эскалации.
    delay_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    # current | next | all — кому адресован шаг (задача 2.11).
    target: Mapped[str] = mapped_column(
        String(16), default="current", server_default="current", nullable=False
    )

    profile: Mapped["EscalationProfile"] = relationship(back_populates="steps")


class Alert(TimestampMixin, Base):
    """Активный или закрытый алерт."""

    __tablename__ = "alerts"
    __table_args__ = (
        # Главный индекс воркера: «дай просроченные таймеры».
        Index("ix_alerts_next_action_at", "next_action_at"),
        Index("ix_alerts_state", "state"),
        # Один активный алерт на dedup_key. Частичный индекс: закрытые алерты
        # с тем же ключом остаются в истории и не конфликтуют с новыми.
        #
        # Условие продублировано для Postgres и SQLite намеренно. Без
        # `sqlite_where` на SQLite получился бы обычный UNIQUE(dedup_key), и
        # resolved-алерт с тем же ключом заблокировал бы создание нового —
        # то есть после устранения проблемы повторное срабатывание
        # перестало бы доходить до дежурного вообще.
        #
        # Acked здесь считается активным: подтверждение означает «я занимаюсь»,
        # а не «проблемы больше нет». Новая эскалация начнётся после resolve.
        Index(
            "uq_alerts_active_dedup",
            "dedup_key",
            unique=True,
            postgresql_where=text("state NOT IN ('resolved', 'expired')"),
            sqlite_where=text("state NOT IN ('resolved', 'expired')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False)
    check_id: Mapped[str] = mapped_column(
        ForeignKey("checks.id", ondelete="CASCADE"), nullable=False
    )

    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False)
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("escalation_profiles.id", ondelete="RESTRICT"), nullable=False
    )

    severity: Mapped[Severity] = mapped_column(String(16), nullable=False)
    state: Mapped[AlertState] = mapped_column(
        String(16), default=AlertState.TRIGGERED, server_default="triggered", nullable=False
    )

    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    runbook_url: Mapped[str | None] = mapped_column(Text)
    labels: Mapped[str | None] = mapped_column(Text)  # JSON-строка

    occurrences: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Номер текущего шага: 0 — ещё ни один шаг не выполнен.
    current_step: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    # Сердце таймеров. NULL — таймеров нет (алерт закрыт или ждёт события).
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acked_by_id: Mapped[str | None] = mapped_column(ForeignKey("admins.id", ondelete="SET NULL"))
    # До какого момента действует snooze. NULL — snooze не активен.
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project: Mapped["Project"] = relationship(back_populates="alerts")
    host: Mapped["Host"] = relationship(foreign_keys=[host_id])
    check: Mapped["Check"] = relationship(foreign_keys=[check_id])
    profile: Mapped["EscalationProfile"] = relationship()
    acked_by: Mapped["Admin | None"] = relationship(foreign_keys=[acked_by_id])
    steps: Mapped[list["AlertStep"]] = relationship(
        back_populates="alert", cascade="all, delete-orphan", order_by="AlertStep.created_at"
    )

    @property
    def is_active(self) -> bool:
        return self.state not in (AlertState.RESOLVED, AlertState.EXPIRED, AlertState.ACKED)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Alert {self.id} {self.state} {self.severity}>"


class AlertStep(Base):
    """Выполненный шаг эскалации. Одновременно аудит и источник для истории (2.22)."""

    __tablename__ = "alert_steps"
    __table_args__ = (Index("ix_alert_steps_alert_id", "alert_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    alert_id: Mapped[str] = mapped_column(
        ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False
    )

    step: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    target_admin_id: Mapped[str | None] = mapped_column(
        ForeignKey("admins.id", ondelete="SET NULL")
    )
    target_label: Mapped[str | None] = mapped_column(String(200))

    # delivered | acked | no_answer | busy | failed | skipped
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    alert: Mapped["Alert"] = relationship(back_populates="steps")
    target_admin: Mapped["Admin | None"] = relationship()
