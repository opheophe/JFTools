"""The build version of the code that is actually running.

The Docker build writes ``app/VERSION`` next to this module from the ``VERSION``
build arg, which the publish workflow fills in from the git ref. That file is
preferred over the ``JFTOOLS_VERSION`` environment variable.

Why bother: Docker copies an image's ``ENV`` into the container once, when the
container is created. A tool that recreates a container from the old container's
config (Portainer's "Recreate" button, Watchtower, ...) therefore keeps the
*old* env value while the new image layers — and so new code — run underneath.
The reported version then lags the code by one release. A file travels with the
layers, so it can never drift that way.
"""
from __future__ import annotations

import os
from pathlib import Path

VERSION_FILE = Path(__file__).with_name("VERSION")


def _from_file() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def current_version() -> str:
    """The running build's version (``dev`` when not running a tagged image)."""
    for raw in (_from_file(), os.environ.get("JFTOOLS_VERSION", "")):
        value = (raw or "").strip().lstrip("vV")
        if value:
            return value
    return "dev"
