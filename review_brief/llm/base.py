"""Provider-agnostic LLM interface.

A client only turns (system, user) text into reply text. Everything that
makes the answer trustworthy (prompt shape, parsing, grounding) lives in
llm/answer.py, so it is the same for every provider and for the mock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class LLMError(Exception):
    """The call failed. The pipeline records it and carries on."""


class LLMConfigError(LLMError):
    """The client cannot be built (missing key or model)."""


@dataclass
class LLMResponse:
    text: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    raw: Any = None  # full provider response, kept for the audit trail


class LLMClient(Protocol):
    provider: str
    model: str

    def complete(self, system: str, user: str, *, question_id: str) -> LLMResponse: ...
