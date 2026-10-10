"""A minimal module/action registry.

Each module is a Python file that calls :func:`register_module` with a name and
a list of :class:`Action` objects. The dashboard renders every registered
module as a card of buttons; no UI changes are needed to add a feature.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

Handler = Callable[["Context"], Awaitable[dict]]


@dataclass
class Context:
    """Passed to every action handler; exposes the current connection settings."""

    settings: Any


@dataclass
class Action:
    id: str
    label: str
    description: str
    handler: Handler


@dataclass
class Module:
    id: str
    name: str
    description: str
    actions: list[Action] = field(default_factory=list)


_registry: dict[str, Module] = {}


def register_module(module: Module) -> Module:
    _registry[module.id] = module
    return module


def get_modules() -> list[Module]:
    return list(_registry.values())


def get_module(module_id: str) -> Module | None:
    return _registry.get(module_id)


def get_action(module_id: str, action_id: str) -> Action | None:
    module = _registry.get(module_id)
    if module is None:
        return None
    for action in module.actions:
        if action.id == action_id:
            return action
    return None


# ---- result shapes consumed by the frontend ---------------------------------


def table(columns: list[str], rows: list[list], title: str | None = None) -> dict:
    return {"type": "table", "columns": columns, "rows": rows, "title": title}


def kv(items: list[list], title: str | None = None) -> dict:
    """items is a list of [label, value] pairs."""
    return {"type": "kv", "items": items, "title": title}


def message(text: str, title: str | None = None, level: str = "info") -> dict:
    return {"type": "message", "text": text, "title": title, "level": level}
