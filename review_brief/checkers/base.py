"""The checker interface.

Every review dimension (quality, safety, policy) is a Checker. A checker
reads the ReviewContext and returns a CheckResult: findings with evidence,
risk signals, questions for the LLM, and an honest list of what it could not
check. Future agents (a service-impact agent that knows production, a
security agent, ...) plug in behind this same interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from typing import Protocol

from review_brief.analysis import FunctionChange, function_changes
from review_brief.config import Policy
from review_brief.diff import ChangedFile
from review_brief.models import CheckResult
from review_brief.workspace import Workspace


@dataclass
class ReviewContext:
    files: list[ChangedFile]
    policy: Policy
    workspace: Workspace | None = None  # None in diff-only mode

    @property
    def full_mode(self) -> bool:
        return self.workspace is not None

    @property
    def python_files(self) -> list[ChangedFile]:
        return [f for f in self.files if f.path.endswith(".py") and f.new_text is not None]

    @cached_property
    def function_changes(self) -> list[FunctionChange]:
        return function_changes(self.files)


class Checker(Protocol):
    name: str

    def run(self, ctx: ReviewContext) -> CheckResult: ...
