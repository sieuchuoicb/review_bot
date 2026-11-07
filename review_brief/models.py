"""Data model shared by the checkers, the pipeline, the report and triage.

The key distinction is `evidence_level`:
  proven      a tool or rule produced it; `evidence` holds the raw output
  hypothesis  the LLM produced it; it must cite a line it was shown
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

LEVELS = {"low": 1, "medium": 2, "high": 3}


def level_rank(level: str) -> int:
    return LEVELS.get(level, 0)


@dataclass
class Finding:
    id: str  # stable across runs: "<source>:<file>:<line>"
    dimension: str  # quality | safety | policy
    title: str
    severity: str = "medium"  # low | medium | high
    evidence_level: str = "proven"  # proven | hypothesis
    source: str = ""  # "ruff S110", "pytest", "rule:sensitive-path", "llm:<provider>/<model>"
    file: str | None = None
    line: int | None = None
    detail: str = ""
    evidence: str = ""

    @property
    def location(self) -> str:
        if not self.file:
            return ""
        return f"{self.file}:{self.line}" if self.line else self.file

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Finding:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


@dataclass
class Signal:
    """One input to the risk score."""

    level: str  # low | medium | high
    reason: str
    source: str


@dataclass
class ContextBlock:
    """A slice of code given to the LLM. Answers may only cite lines inside one."""

    label: str
    file: str
    start_line: int
    code: str
    version: str = "head"  # head (after the PR) | base (before)
    focus_line: int | None = None  # e.g. the call site inside a caller

    @property
    def end_line(self) -> int:
        return self.start_line + max(len(self.code.splitlines()), 1) - 1


@dataclass
class Question:
    """A narrow question a rule raised, for the LLM to answer."""

    id: str
    kind: str  # behaviour_change | new_method
    target: str  # e.g. "Invoice.total"
    file: str
    line: int
    trigger: str  # why the rule raised it, in plain words
    ask: str
    context: list[ContextBlock] = field(default_factory=list)
    priority: int = 0  # higher is asked first when capped
    status: str = "pending"  # pending | not_asked | answered | failed
    status_note: str = ""
    dropped_claims: int = 0


@dataclass
class CheckResult:
    checker: str
    findings: list[Finding] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    not_checked: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Risk:
    level: str
    reasons: list[str]


@dataclass
class Brief:
    diff_source: str
    repo: str | None
    generated_at: str
    mode: str  # full | diff-only
    changed_files: list[str]
    risk: Risk
    findings: list[Finding]
    questions: list[Question]
    llm: dict[str, Any]
    not_checked: list[str]
    notes: list[str]

    @property
    def proven(self) -> list[Finding]:
        return [f for f in self.findings if f.evidence_level == "proven"]

    @property
    def hypotheses(self) -> list[Finding]:
        return [f for f in self.findings if f.evidence_level == "hypothesis"]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
