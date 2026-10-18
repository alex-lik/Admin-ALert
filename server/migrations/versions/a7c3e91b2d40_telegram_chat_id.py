"""telegram_chat_id у админа

Revision ID: a7c3e91b2d40
Revises: 32be529fa9bd
Create Date: 2026-10-18 10:12:00.000000

Задача 4.8: Telegram-канал эскалации. Колонка nullable — у админа, который
Telegram ещё не привязал, канала нет, и это не поломка: шаг через Telegram
честно пишет skipped. Не пустая строка и не 0, потому что «нет чата» и
«чат с таким id» — разные состояния, и пустая строка сделала бы их
неразличимыми в разметке бота.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c3e91b2d40"
down_revision: str | None = "32be529fa9bd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("admins", sa.Column("telegram_chat_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("admins", "telegram_chat_id")
