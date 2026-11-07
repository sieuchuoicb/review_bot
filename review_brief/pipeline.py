"""diff → workspace → checkers → risk → (LLM questions) → brief.

The pipeline never decides whether a PR should merge. It assembles evidence,
says what it could not check, and hands the decisions to a human.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from review_brief.checkers.base import Checker, ReviewContext
from review_brief.checkers.policy import PolicyChecker
from review_brief.checkers.quality import QualityChecker
from review_brief.checkers.safety import SafetyChecker
from review_brief.config import Policy
from review_brief.diff import ChangedFile, PatchError, approximate_changes, parse_diff
from review_brief.llm.answer import answer_question
from review_brief.llm.base import LLMClient
from review_brief.models import Brief, Finding, Question, level_rank
from review_brief.risk import assess, llm_gate
from review_brief.workspace import Workspace, WorkspaceError, materialize

DEFAULT_CHECKERS: list[Checker] = [QualityChecker(), SafetyChecker(), PolicyChecker()]
_DIMENSION_ORDER = {"safety": 0, "quality": 1, "policy": 2}


def review(
    diff_text: str,
    *,
    diff_source: str,
    repo: Path | None,
    policy: Policy,
    checkers: list[Checker] | None = None,
    llm: LLMClient | None = None,
    audit: list[dict] | None = None,
) -> Brief:
    """Build the brief. `audit`, if given, receives one transcript entry per LLM call."""
    checkers = DEFAULT_CHECKERS if checkers is None else checkers
    not_checked: list[str] = []
    notes: list[str] = [f"Policy: {policy.source}."]
    patches = parse_diff(diff_text)
    if not patches:
        notes.append("The diff contains no file changes.")

    workspace: Workspace | None = None
    if repo is None:
        not_checked.append("No repo given: ran diff-only checks (no lint, tests or caller analysis).")
    elif patches:
        try:
            workspace = materialize(repo, patches)
        except WorkspaceError as exc:
            not_checked.append(f"The diff did not apply to {repo}: {exc}. Ran diff-only checks instead.")

    try:
        files = _changed_files(patches, workspace, not_checked)
        ctx = ReviewContext(files=files, policy=policy, workspace=workspace)
        findings: list[Finding] = []
        questions: list[Question] = []
        signals = []
        for checker in checkers:
            try:
                result = checker.run(ctx)
            except Exception as exc:  # one broken checker must not hide the others' evidence
                not_checked.append(f"The {checker.name} checker crashed: {exc!r}.")
                continue
            findings += result.findings
            questions += result.questions
            signals += result.signals
            not_checked += result.not_checked
            notes += result.notes
    finally:
        if workspace is not None:
            workspace.cleanup()

    # Risk comes from proven signals only. LLM hypotheses never raise or lower it.
    risk = assess(signals)
    questions = _gate(questions, risk, policy, has_llm=llm is not None)
    findings.sort(key=lambda f: (-level_rank(f.severity), _DIMENSION_ORDER.get(f.dimension, 9), f.id))
    llm_info = _ask(llm, questions, findings, policy, audit)

    return Brief(
        diff_source=diff_source,
        repo=str(repo) if repo else None,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        mode="full" if workspace is not None else "diff-only",
        changed_files=[f.path for f in files],
        risk=risk,
        findings=findings,
        questions=questions,
        llm=llm_info,
        not_checked=not_checked,
        notes=notes,
    )


def _changed_files(patches, workspace: Workspace | None, not_checked: list[str]) -> list[ChangedFile]:
    if workspace is not None:
        not_checked += workspace.skipped
        return workspace.files
    files = []
    for p in patches:
        if p.is_binary or p.is_rename:
            not_checked.append(f"{p.path}: binary or rename changes are not analysed.")
            continue
        try:
            files.append(approximate_changes(p))
        except PatchError as exc:
            not_checked.append(str(exc))
    return files


def _gate(questions: list[Question], risk, policy: Policy, *, has_llm: bool) -> list[Question]:
    """Decide which rule-raised questions are worth an LLM call."""
    questions = sorted(questions, key=lambda q: -q.priority)
    allowed, reason = llm_gate(risk, policy)
    cap = int(policy.llm["max_questions"])
    for i, q in enumerate(questions):
        if not allowed:
            q.status, q.status_note = "not_asked", reason
        elif i >= cap:
            q.status, q.status_note = "not_asked", f"over llm.max_questions ({cap})"
        else:
            q.status, q.status_note = "pending", "" if has_llm else "no LLM configured"
    return questions


def _ask(
    llm: LLMClient | None,
    questions: list[Question],
    findings: list[Finding],
    policy: Policy,
    audit: list[dict] | None,
) -> dict:
    """Answer pending questions. Grounded concerns are appended to `findings` as hypotheses."""
    info: dict = {
        "provider": llm.provider if llm else None,
        "model": llm.model if llm else None,
        "calls": 0,
        "failed": 0,
        "dropped_claims": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": 0.0,
    }
    pending = [q for q in questions if q.status == "pending"]
    if llm is None or not pending:
        return info
    proven = [f"{f.location} {f.title}".strip() for f in findings]
    max_concerns = int(policy.llm["max_concerns_per_question"])
    # Questions are independent, so ask them in parallel; results are merged in priority order.
    with ThreadPoolExecutor(max_workers=min(4, len(pending))) as pool:
        outcomes = list(pool.map(lambda q: answer_question(llm, q, proven, max_concerns), pending))

    seen = {f.location for f in findings if f.evidence_level == "hypothesis"}
    for q, outcome in zip(pending, outcomes, strict=True):
        info["calls"] += 1
        info["failed"] += outcome.status == "failed"
        info["dropped_claims"] += outcome.dropped
        info["prompt_tokens"] += int(outcome.usage.get("prompt_tokens") or 0)
        info["completion_tokens"] += int(outcome.usage.get("completion_tokens") or 0)
        info["cost_usd"] = round(info["cost_usd"] + float(outcome.usage.get("cost") or 0), 6)
        q.status, q.status_note, q.dropped_claims = outcome.status, outcome.note, outcome.dropped
        if audit is not None:
            audit.append(outcome.transcript)
        for finding in outcome.findings:
            if finding.location in seen:  # two questions landed on the same line; keep the first
                continue
            seen.add(finding.location)
            findings.append(finding)
    return info
