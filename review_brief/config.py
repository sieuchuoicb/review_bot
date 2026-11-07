"""Company review policy: which rules apply, where, and how strictly.

The rules themselves live in code (review_brief/checkers). This file only
loads the knobs from policy.yaml so the team can tune behaviour without a
code change, and without putting policy into a prompt.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "sensitive_paths": [],
    "rules": {
        "new_public_function_requires_test": {
            "enabled": True,
            "level": "medium",
            "test_globs": ["tests/**/*.py", "**/test_*.py", "**/*_test.py"],
        },
        "secrets_in_diff": {"enabled": True, "level": "high"},
    },
    "ruff": {
        "select": ["E9", "F", "B", "BLE", "S110"],
        "severity": {"E9": "high", "F821": "high", "S": "medium", "BLE": "medium", "B": "medium"},
        "default_severity": "low",
    },
    "tests": {"timeout_seconds": 120},
    "llm": {"min_risk": "medium", "max_questions": 4, "max_concerns_per_question": 3},
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class Policy:
    data: dict[str, Any]
    source: str

    @classmethod
    def load(cls, path: Path | None) -> Policy:
        if path is None:
            return cls(copy.deepcopy(DEFAULTS), "built-in defaults")
        raw = yaml.safe_load(path.read_text()) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: policy must be a YAML mapping")
        return cls(_merge(DEFAULTS, raw), str(path))

    def rule(self, name: str) -> dict[str, Any]:
        return self.data["rules"].get(name, {"enabled": False})

    @property
    def sensitive_paths(self) -> list[dict[str, Any]]:
        return self.data["sensitive_paths"]

    @property
    def ruff(self) -> dict[str, Any]:
        return self.data["ruff"]

    @property
    def tests(self) -> dict[str, Any]:
        return self.data["tests"]

    @property
    def llm(self) -> dict[str, Any]:
        return self.data["llm"]

    def ruff_severity(self, code: str) -> str:
        """Longest matching code prefix wins (F821 beats F)."""
        table = self.ruff["severity"]
        for size in range(len(code), 0, -1):
            if code[:size] in table:
                return table[code[:size]]
        return self.ruff["default_severity"]


@lru_cache(maxsize=256)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def glob_match(path: str, pattern: str) -> bool:
    """Path glob where * stays inside one directory and ** crosses directories."""
    return bool(_glob_regex(pattern).match(path))
