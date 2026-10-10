"""Session authentication helpers, local-IP detection, and failed-login lockout."""
from __future__ import annotations

import ipaddress
import time
from collections import defaultdict

MAX_ATTEMPTS = 5
WINDOW_SECONDS = 15 * 60

# ip -> list of failure timestamps (in-memory; resets on restart)
_failures: dict[str, list[float]] = defaultdict(list)


def verify_credentials(config, username: str, password: str) -> bool:
    return config.check_password(username, password)


def is_local_ip(ip: str | None) -> bool:
    """True for localhost and private/LAN addresses (IPv4 and IPv6)."""
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private or addr.is_link_local


def is_locked(ip: str) -> bool:
    now = time.time()
    _failures[ip] = [t for t in _failures[ip] if now - t < WINDOW_SECONDS]
    return len(_failures[ip]) >= MAX_ATTEMPTS


def record_failure(ip: str) -> None:
    _failures[ip].append(time.time())


def clear_failures(ip: str) -> None:
    _failures.pop(ip, None)
