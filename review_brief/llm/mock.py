"""Canned-answer client for tests and evals: deterministic, offline, free.

It still goes through the same prompt, parse and grounding path as a real
model, so evals exercise everything except the model itself.

answers: {question_id: reply}, where reply is
  - a dict  -> sent back as JSON
  - a str   -> sent back verbatim (to test malformed replies)
  - {"__error__": "msg"} -> the call raises LLMError
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from review_brief.llm.base import LLMError, LLMResponse

NO_ANSWER = {"concerns": [], "no_concern_reason": "mock: no canned answer for this question"}


class MockLLM:
    provider = "mock"

    def __init__(self, answers: dict[str, Any] | None = None, model: str = "canned-answers"):
        self.answers = answers or {}
        self.model = model
        self.calls: list[dict[str, str]] = []

    @classmethod
    def from_file(cls, path: Path) -> MockLLM:
        return cls(json.loads(path.read_text()), model=f"canned:{path.parent.name}/{path.name}")

    def complete(self, system: str, user: str, *, question_id: str) -> LLMResponse:
        self.calls.append({"question_id": question_id, "system": system, "user": user})
        reply = self.answers.get(question_id, NO_ANSWER)
        if isinstance(reply, dict) and "__error__" in reply:
            raise LLMError(reply["__error__"])
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return LLMResponse(text=text, model=self.model)
