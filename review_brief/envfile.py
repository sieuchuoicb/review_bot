"""Minimal .env loader (stdlib only). Variables already in the environment win."""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path


def load_env(path: Path = Path(".env"), environ: MutableMapping[str, str] = os.environ) -> list[str]:
    """Load KEY=VALUE lines from `path`. Returns the keys it set."""
    if not path.is_file():
        return []
    loaded: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key not in environ:
            environ[key] = value
            loaded.append(key)
    return loaded
