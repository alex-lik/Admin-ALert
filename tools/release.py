#!/usr/bin/env python3
"""SemVer-релизный инструмент Admin-ALert.

Без внешних зависимостей — работает на чистом Python 3.11+.

Команды:
    show                      Текущая версия из файла VERSION.
    next <range>              Тип бампа (major|minor|patch|none) по коммитам в диапазоне.
    bump --type <t> [--date] Поднять версию, обновить CHANGELOG и манифесты.
    sync                      Синхронизировать версию в манифесты и VERSION.
    validate                  Проверить семантику VERSION и синхронность манифестов.

Диапазон для `next` — любой revspec, который понимает git, например:
    v0.1.0..HEAD
    $(git describe --tags --abbrev=0)..HEAD
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "VERSION"
CHANGELOG_FILE = ROOT / "CHANGELOG.md"
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$")

# Conventional Commits: [scope][!]: type
COMMIT_PATTERN = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scope>[a-z0-9._/-]+)\))?(?P<breaking>!)?: (?P<subject>.+)$"
)
BUMP_TYPES = {"feat": "minor", "fix": "patch"}
NO_BUMP_TYPES = {"docs", "chore", "refactor", "test", "build", "ci", "style", "perf"}

# type -> заголовок секции CHANGELOG (Keep a Changelog)
SECTION_BY_TYPE = {
    "feat": "Added",
    "fix": "Fixed",
    "perf": "Performance",
    "refactor": "Changed",
    "docs": "Documentation",
    "test": "Tests",
    "build": "Build",
    "ci": "CI",
    "chore": "Chore",
    "style": "Chore",
}


class ReleaseError(RuntimeError):
    pass


def read_version() -> str:
    if not VERSION_FILE.exists():
        raise ReleaseError(f"Не найден файл версии: {VERSION_FILE}")
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not VERSION_PATTERN.match(version):
        raise ReleaseError(f"Версия '{version}' не соответствует SemVer (X.Y.Z)")
    return version


def write_version(version: str) -> None:
    VERSION_FILE.write_text(f"{version}\n", encoding="utf-8")


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def parse_commit(message: str) -> dict | None:
    """Разобрать subject коммита. Возвращает None для merge-коммитов и мусора."""
    subject = message.splitlines()[0].strip()
    if subject.startswith("Merge "):
        return None
    match = COMMIT_PATTERN.match(subject)
    if not match:
        return None
    return {
        "type": match.group("type"),
        "scope": match.group("scope"),
        "breaking": bool(match.group("breaking")) or "BREAKING CHANGE:" in message,
        "subject": subject,
    }


def commit_range(revspec: str) -> list[dict]:
    raw = git("log", "--format=%H%x1f%B%x1e", revspec)
    commits = []
    for chunk in raw.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        _sha, _, body = chunk.partition("\x1f")
        parsed = parse_commit(body)
        if parsed:
            commits.append(parsed)
    return commits


def determine_bump(commits: list[dict]) -> str:
    if any(c["breaking"] for c in commits):
        return "major"
    types = {c["type"] for c in commits}
    if "feat" in types:
        return "minor"
    if "fix" in types:
        return "patch"
    return "none"


def next_version(current: str, bump: str) -> str:
    major, minor, patch = (int(part) for part in current.split("."))
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ReleaseError(f"Неизвестный тип бампа: {bump}")


def latest_tag() -> str | None:
    try:
        tag = git("describe", "--tags", "--abbrev=0")
    except ReleaseError:
        return None
    return tag.lstrip("v") or None


def cmd_show(_args: argparse.Namespace) -> int:
    print(read_version())
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    revspec = args.range or f"{latest_tag() or '0.0.0'}..HEAD"
    commits = commit_range(revspec)
    if not commits:
        print("none")
        return 0
    known_types = set(BUMP_TYPES) | NO_BUMP_TYPES
    unknown = [c["subject"] for c in commits if c["type"] not in known_types]
    if unknown:
        for subject in unknown:
            print(f"warning: нераспознанный тип коммита: {subject}", file=sys.stderr)
    print(determine_bump(commits))
    return 0


def cmd_bump(args: argparse.Namespace) -> int:
    current = read_version()
    version = next_version(current, args.type)
    date = args.date or dt.date.today().isoformat()

    if not CHANGELOG_FILE.exists():
        raise ReleaseError(f"Не найден {CHANGELOG_FILE}")
    changelog = CHANGELOG_FILE.read_text(encoding="utf-8")
    if f"## [{version}]" in changelog:
        raise ReleaseError(f"Версия {version} уже есть в CHANGELOG.md")

    entry = f"## [{version}] - {date}\n"
    changelog = changelog.replace("## [Unreleased]\n", f"## [Unreleased]\n\n{entry}", 1)
    CHANGELOG_FILE.write_text(changelog, encoding="utf-8")
    write_version(version)
    manifests = sync_manifests(version)

    # Индексируем ровно то, что изменили, чтобы bump не тянул за собой чужие правки.
    changed = [VERSION_FILE, CHANGELOG_FILE, *(ROOT / path for path in manifests)]
    git("add", *(str(path) for path in changed))
    print(version)
    return 0


def sync_manifests(version: str) -> list[str]:
    """Синхронизировать версию в манифесты, если они существуют."""
    touched = []
    pyproject = ROOT / "server" / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8")
        new_text = re.sub(r'^version = ".*"$', f'version = "{version}"', text, count=1, flags=re.M)
        if new_text != text:
            pyproject.write_text(new_text, encoding="utf-8")
            touched.append(str(pyproject.relative_to(ROOT)))
    for manifest in (ROOT / "client" / "package.json", ROOT / "desktop-agent" / "package.json"):
        if manifest.exists():
            text = manifest.read_text(encoding="utf-8")
            new_text = re.sub(r'"version": ".*?"', f'"version": "{version}"', text, count=1)
            if new_text != text:
                manifest.write_text(new_text, encoding="utf-8")
                touched.append(str(manifest.relative_to(ROOT)))
    return touched


def cmd_sync(_args: argparse.Namespace) -> int:
    version = read_version()
    for path in sync_manifests(version):
        print(f"updated {path}")
    print(version)
    return 0


def cmd_validate(_args: argparse.Namespace) -> int:
    problems = []
    try:
        version = read_version()
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    pyproject = ROOT / "server" / "pyproject.toml"
    if pyproject.exists():
        match = re.search(r'^version = "(.*)"$', pyproject.read_text(encoding="utf-8"), re.M)
        if match and match.group(1) != version:
            problems.append(f"server/pyproject.toml: версия {match.group(1)} != VERSION {version}")

    if CHANGELOG_FILE.exists():
        changelog = CHANGELOG_FILE.read_text(encoding="utf-8")
        if f"## [{version}]" not in changelog and f"## [Unreleased]" not in changelog:
            problems.append(f"CHANGELOG.md: нет секции для версии {version}")
    else:
        problems.append("CHANGELOG.md не найден")

    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"ok: {version}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="release.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("show", help="показать текущую версию").set_defaults(func=cmd_show)

    p_next = sub.add_parser("next", help="тип бампа по коммитам в диапазоне")
    p_next.add_argument("range", nargs="?", help="revspec, например v0.1.0..HEAD")
    p_next.set_defaults(func=cmd_next)

    p_bump = sub.add_parser("bump", help="поднять версию и обновить CHANGELOG")
    p_bump.add_argument("--type", required=True, choices=["major", "minor", "patch"])
    p_bump.add_argument("--date", help="дата релиза YYYY-MM-DD")
    p_bump.set_defaults(func=cmd_bump)

    sub.add_parser("sync", help="синхронизировать версию в манифесты").set_defaults(func=cmd_sync)
    sub.add_parser("validate", help="проверить семантику и синхронность версий").set_defaults(
        func=cmd_validate
    )

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
