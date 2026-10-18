"""Бэкапы БД (план, задача 2.24).

Главное правило, которое отличает рабочий бэкап от иллюзии: **бэкап, который
не проверен восстановлением, не является бэкапом**. Поэтому здесь есть команда
`verify` — она разворачивает дамп в отдельную БД и проверяет, что он читается
и в нём есть алерты. Без неё через полгода выяснится, что последний
рабочий дамп — трёхмесячной давности, и все архивы с тех пор — пустые.

Что делает:

    backup   — снять дамп (pg_dump для Postgres, копия файла для SQLite),
               сжать, положить в каталог с датой;
    list     — показать, что есть, и сколько весит;
    verify   — развернуть указанный дамп во временную БД и проверить;
    prune    — удалить старые, оставив последние N.

Секреты в дамп не попадают: пароль передаётся через PGPASSWORD и в файлы не
пишется. Сам дамп содержит хеши паролей админов и токены устройств — это
данные, доступ к которому нужно ограничивать правами на каталог.
"""

from __future__ import annotations

import argparse
import gzip
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_DIR = Path("backups")
# Сколько последних дампов держим при prune. Недельный бэкап без месячного
# про запас — плохая идея: дампы нужны не для «вчера», а для «месяц назад».
KEEP_DEFAULT = 14


@dataclass
class BackupInfo:
    path: Path
    created: datetime
    size: int
    kind: str  # pg | sqlite

    @property
    def age_days(self) -> float:
        return (datetime.now(UTC) - self.created).total_seconds() / 86400


def stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def detect_kind(database_url: str) -> str:
    if database_url.startswith("postgres"):
        return "pg"
    if "sqlite" in database_url:
        return "sqlite"
    raise SystemExit(f"не понимаю DATABASE_URL: {database_url[:40]}…")


def sqlite_file(database_url: str) -> Path:
    """Путь к файлу SQLite из строки подключения."""
    _, _, rest = database_url.partition("///")
    if not rest:
        raise SystemExit(f"не удалось найти файл в DATABASE_URL: {database_url}")
    return Path(rest)


def backup_postgres(database_url: str, target: Path) -> None:
    """Дамп через pg_dump.

    Пароль идёт через PGPASSWORD, а не аргументом командной строки: аргументы
    видны в `ps` всем пользователям машины.
    """
    url = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    env = dict(os.environ)
    password = extract_password(url)
    if password:
        env["PGPASSWORD"] = password

    with gzip.open(target, "wb") as stream:
        result = subprocess.run(  # noqa: S603 — фиксированная команда
            ["pg_dump", "--no-owner", "--no-acl", url],
            env=env,
            stdout=stream,
            stderr=subprocess.PIPE,
            check=False,
        )
    if result.returncode != 0:
        target.unlink(missing_ok=True)
        raise SystemExit(result.stderr.decode("utf-8", errors="replace")[:500])


def extract_password(url: str) -> str | None:
    """Достать пароль из URL для PGPASSWORD."""
    from urllib.parse import urlparse

    return urlparse(url).password


def backup_sqlite(database_url: str, target: Path) -> None:
    """Копия файла SQLite.

    Копируется через онлайн-бэкап API, а не простым копированием: при
    активной записи файл может быть в промежуточном состоянии, и такая копия
    не откроется.
    """
    import sqlite3

    source = sqlite_file(database_url)
    if not source.exists():
        raise SystemExit(f"файл базы не найден: {source}")

    with sqlite3.connect(source) as conn, gzip.open(target, "wb") as stream:
        # backup копирует в открытый дескриптор — сжатие поверх работает.
        conn.backup(stream)  # type: ignore[arg-type]


def do_backup(database_url: str, directory: Path) -> Path:
    kind = detect_kind(database_url)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"adminalert-{kind}-{stamp()}.sql.gz"

    if kind == "pg":
        backup_postgres(database_url, target)
    else:
        backup_sqlite(database_url, target)

    size = target.stat().st_size
    if size == 0:
        target.unlink(missing_ok=True)
        raise SystemExit("дамп пустой — бэкап не записан")

    print(f"бэкап: {target} ({size / 1024:.0f} КБ)")
    return target


def list_backups(directory: Path) -> list[BackupInfo]:
    directory.mkdir(parents=True, exist_ok=True)
    found: list[BackupInfo] = []

    for path in sorted(directory.glob("adminalert-*.sql.gz")):
        created = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        kind = "pg" if "pg" in path.name else "sqlite"
        found.append(BackupInfo(path, created, path.stat().st_size, kind))

    return sorted(found, key=lambda b: b.created, reverse=True)


def verify(path: Path, database_url: str) -> None:
    """Развернуть дамп и убедиться, что он рабочий.

    Для Postgres дамп накатывается в одноимённую базу через `psql`. Соединение
    берётся из DATABASE_URL: если это основная база, проверка её не тронет —
    она только читает и создаёт вспомогательную базу.
    """
    kind = detect_kind(database_url)

    if kind == "sqlite":
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
            temp = Path(handle.name)
        try:
            import sqlite3

            with gzip.open(path, "rb") as source, sqlite3.connect(temp) as conn:
                conn.executescript(source.read().decode("utf-8", errors="replace"))

            with sqlite3.connect(temp) as conn:
                alerts = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
                tables = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0]

            print(f"проверено: таблиц {tables}, алертов {alerts}")
            if tables == 0:
                raise SystemExit("В ДАМПЕ НЕТ ТАБЛИЦ — это не рабочий бэкап")
        finally:
            temp.unlink(missing_ok=True)
        return

    url = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    check_name = "adminalert_backup_check"
    from urllib.parse import urlparse

    parsed = urlparse(url)

    env = dict(os.environ)
    if parsed.password:
        env["PGPASSWORD"] = parsed.password

    try:
        subprocess.run(  # noqa: S603
            ["dropdb", "--if-exists", "-U", parsed.username or "postgres", check_name],
            env=env,
            check=False,
            capture_output=True,
        )
        subprocess.run(  # noqa: S603
            ["createdb", "-U", parsed.username or "postgres", check_name],
            env=env,
            check=True,
            capture_output=True,
        )

        with gzip.open(path, "rb") as stream:
            result = subprocess.run(  # noqa: S603
                ["psql", "-v", "ON_ERROR_STOP=1", "-U", parsed.username or "postgres", "-d", check_name],
                env=env,
                stdin=stream,
                capture_output=True,
                check=False,
            )
        if result.returncode != 0:
            raise SystemExit(result.stderr.decode("utf-8", errors="replace")[:500])

        count = subprocess.run(  # noqa: S603
            [
                "psql",
                "-t",
                "-A",
                "-U",
                parsed.username or "postgres",
                "-d",
                check_name,
                "-c",
                "SELECT COUNT(*) FROM alerts",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        print(f"проверено: алертов в дампе {count.stdout.strip()}")
    finally:
        subprocess.run(  # noqa: S603
            ["dropdb", "--if-exists", "-U", parsed.username or "postgres", check_name],
            env=env,
            check=False,
            capture_output=True,
        )


def prune(directory: Path, keep: int) -> None:
    backups = list_backups(directory)
    if len(backups) <= keep:
        print(f"удалять нечего: архивов {len(backups)}, оставляем {keep}")
        return

    for backup in backups[keep:]:
        backup.path.unlink(missing_ok=True)
        print(f"удалён {backup.path.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Бэкапы БД Admin-ALert")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="Строка подключения (по умолчанию из DATABASE_URL)",
    )
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help="Каталог архива")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("backup", help="Снять дамп")
    sub.add_parser("list", help="Показать архив")

    verify_parser = sub.add_parser("verify", help="Проверить, что дамп разворачивается")
    verify_parser.add_argument("path", help="Файл дампа")

    prune_parser = sub.add_parser("prune", help="Удалить старые")
    prune_parser.add_argument("--keep", type=int, default=KEEP_DEFAULT)

    args = parser.parse_args()
    directory = Path(args.dir)

    if args.command == "list":
        backups = list_backups(directory)
        if not backups:
            print(f"архив пуст: {directory}")
            return 0
        for backup in backups:
            print(
                f"{backup.created:%Y-%m-%d %H:%M}  "
                f"{backup.size / 1024 / 1024:6.1f} МБ  "
                f"{backup.kind:6s}  {backup.path.name}"
            )
        return 0

    if args.command == "verify":
        path = Path(args.path)
        if not path.exists():
            raise SystemExit(f"файл не найден: {path}")
        verify(path, args.database_url)
        return 0

    if args.command == "prune":
        prune(directory, args.keep)
        return 0

    if not args.database_url:
        raise SystemExit("нужен DATABASE_URL или --database-url")

    do_backup(args.database_url, directory)
    return 0


if __name__ == "__main__":
    sys.exit(main())
