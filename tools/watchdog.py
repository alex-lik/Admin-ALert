"""Внешний watchdog: замечает, что сам сервис пейджинга перестал работать.

Задача 2.23, и это единственная её часть, которая не может быть написана
внутри системы: если API лежит, он не сможет сообщить, что он лежит.

Как работает:

    раз в N секунд шлёт запрос на /health/ready;
    если ответ не 200 или прошло больше `timeout` секунд — считает это
    аварией;
    об аварии сообщает через Telegram-бота (тот же токен, что у сервиса).

Особенность, которая определяет всю ценность этого скрипта: он **должен
работать без сети до Admin-ALert**. Поэтому состояние ведётся в файле, а
не в памяти процесса, а повторные уведомления подавляются — иначе при
длительном простое он завалит дежурного одинаковыми сообщениями и тот
перестанет их читать.

Запуск (cron/systemd):

    python tools/watchdog.py --once       # разовая проверка, для cron
    python tools/watchdog.py              # постоянно

Код возврата: 0 — всё в порядке, 1 — авария. Это позволяет cron-варианту
не рассылать письма и просто сигналить.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("watchdog")

DEFAULT_URL = "http://127.0.0.1:8080/api/v1/health/ready"
DEFAULT_STATE = Path("watchdog-state.json")

# Столько секунд аварии считается одним инцидентом. Пока авария длится,
# повторно не сообщаем — иначе дежурный получит 300 одинаковых сообщений
# за ночь и перестанет их открывать.
REPEAT_AFTER_SECONDS = 1800


@dataclass
class Verdict:
    ok: bool
    detail: str
    # Плохое состояние, для которого watchdog УЖЕ сообщал. По нему
    # решается, слать новое сообщение или молчать.
    should_alert: bool


def check(url: str, *, timeout: float = 5.0) -> Verdict:
    """Разовая проверка сервиса."""
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            elapsed = time.monotonic() - started
            body = response.read().decode("utf-8", errors="replace")

        if response.status != 200:
            return Verdict(False, f"HTTP {response.status}: {body[:200]}", True)

        try:
            payload = json.loads(body)
        except ValueError:
            return Verdict(False, f"ответ не JSON: {body[:200]}", True)

        backlog = payload.get("backlog", -1)
        return Verdict(
            True,
            f"ok, очередь воркера: {backlog}, проверка за {elapsed:.2f}с",
            False,
        )
    except urllib.error.HTTPError as exc:
        # 503 — это наш собственный ответ «процесс жив, но эскалация не
        # идёт». Тело полезно: там причина.
        body = exc.read().decode("utf-8", errors="replace")[:300]
        return Verdict(False, f"HTTP {exc.code}: {body}", True)
    except (urllib.error.URLError, OSError) as exc:
        # Сервис не отвечает вообще. Это самая частая и самая опасная авария:
        # ни один алерт не придёт, потому что некому его принять.
        return Verdict(False, f"сервис недоступен: {exc}", True)
    except Exception as exc:  # noqa: BLE001 — watchdog не должен падать сам
        return Verdict(False, f"непредвиденная ошибка проверки: {exc}", True)


def load_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        # Повреждённый файл состояния не должен мешать проверке: худшее, что
        # будет — одно лишнее сообщение об аварии.
        return {}


def save_state(path: Path, state: dict[str, object]) -> None:
    try:
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        logger.warning("состояние watchdog не сохранено: %s", exc)


def decide(verdict: Verdict, state: dict[str, object]) -> bool:
    """Нужно ли отправлять сообщение прямо сейчас."""
    if verdict.ok:
        return False

    last_detail = state.get("last_detail")
    last_sent = float(state.get("last_alert_at", 0))  # type: ignore[arg-type]
    now = time.time()

    # Другая поломка, чем в прошлый раз, — сообщить сразу: это новая авария,
    # а не продолжение старой.
    if last_detail != verdict.detail:
        return True

    return now - last_sent >= REPEAT_AFTER_SECONDS


def notify(token: str | None, chat_id: str | None, verdict: Verdict) -> str:
    """Отправить сообщение о аварии через Telegram."""
    if not token:
        return "нет TELEGRAM_BOT_TOKEN — сообщение не отправлено"
    if not chat_id:
        return "нет TELEGRAM_ALERT_CHAT_ID — некуда слать, сообщение не отправлено"

    text = f"🚨 Admin-ALert недоступен\n{verdict.detail}"

    try:
        payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        request = urllib.request.Request(  # noqa: S310
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            return (
                "уведомление отправлено"
                if response.status == 200
                else "Telegram ответил отказом"
            )
    except Exception as exc:  # noqa: BLE001 — сбой уведомления не должен ронять watchdog
        return f"уведомление не отправлено: {exc}"


def run_once(
    url: str,
    state_path: Path,
    token: str | None,
    chat_id: str | None,
    *,
    verbose: bool = True,
) -> int:
    """Одна проверка. Возвращает код: 0 — порядок, 1 — авария."""
    verdict = check(url)
    state = load_state(state_path)

    if verdict.ok:
        if state:
            # Авария закончилась — чисти состояние, чтобы следующая была
            # новой аварией, а не повтором.
            save_state(state_path, {})
        if verbose:
            print(f"OK: {verdict.detail}")
        return 0

    if verbose:
        print(f"АВАРИЯ: {verdict.detail}")

    if decide(verdict, state):
        result = notify(token, chat_id, verdict)
        save_state(
            state_path,
            {"last_detail": verdict.detail, "last_alert_at": time.time()},
        )
        if verbose:
            print(result)

    return 1


def run_loop(
    url: str,
    state_path: Path,
    token: str | None,
    chat_id: str | None,
    interval: float,
) -> None:
    logger.info("watchdog: проверка %s каждые %.0fс", url, interval)

    while True:
        try:
            run_once(url, state_path, token, chat_id)
        except Exception:  # noqa: BLE001 — watchdog переживает всё
            logger.exception("проверка не завершилась")

        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("watchdog остановлен")
            return


def main() -> int:
    parser = argparse.ArgumentParser(description="Внешний watchdog Admin-ALert")
    parser.add_argument("--url", default=DEFAULT_URL, help="Адрес /health/ready")
    parser.add_argument("--interval", type=float, default=30.0, help="Пауза между проверками")
    parser.add_argument("--state", default=str(DEFAULT_STATE), help="Файл состояния")
    parser.add_argument("--once", action="store_true", help="Одна проверка и выход")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_ALERT_CHAT_ID")
    state_path = Path(args.state)

    if args.once:
        return run_once(args.url, state_path, token, chat_id)

    run_loop(args.url, state_path, token, chat_id, args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
