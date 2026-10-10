"""Session authentication helpers and failed-login lockout."""
from __future__ import annotations

import time
from collections import defaultdict

MAX_ATTEMPTS = 5
WINDOW_SECONDS = 15 * 60

# ip -> list of failure timestamps (in-memory; resets on restart)
_failures: dict[str, list[float]] = defaultdict(list)


def verify_credentials(config, username: str, password: str) -> bool:
    return config.check_password(username, password)


def is_locked(ip: str) -> bool:
    now = time.time()
    _failures[ip] = [t for t in _failures[ip] if now - t < WINDOW_SECONDS]
    return len(_failures[ip]) >= MAX_ATTEMPTS


def record_failure(ip: str) -> None:
    _failures[ip].append(time.time())


def clear_failures(ip: str) -> None:
    _failures.pop(ip, None)
