"""Risk level and the LLM gate.

The risk level is the highest level among the signals, with every reason
listed. A summed score would rank PRs more finely, but nobody can explain why
a PR scored 7 rather than 6, and a reviewer needs to see why, not just how much.
"""

from __future__ import annotations

from review_brief.config import Policy
from review_brief.models import Risk, Signal, level_rank


def assess(signals: list[Signal]) -> Risk:
    if not signals:
        return Risk("low", ["No rule raised a risk signal."])
    ordered = sorted(signals, key=lambda s: -level_rank(s.level))
    reasons = list(dict.fromkeys(f"[{s.level}] {s.reason}" for s in ordered))
    return Risk(ordered[0].level, reasons)


def llm_gate(risk: Risk, policy: Policy) -> tuple[bool, str]:
    """Whether this PR is worth LLM calls at all."""
    threshold = policy.llm["min_risk"]
    if level_rank(risk.level) < level_rank(threshold):
        return False, f"risk {risk.level} is below llm.min_risk ({threshold})"
    return True, ""
