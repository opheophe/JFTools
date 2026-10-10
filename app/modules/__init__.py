"""Module auto-discovery: importing this package registers all modules."""
from . import jellyfin  # noqa: F401

__all__ = ["jellyfin"]
