"""Human triage: a reviewer accepts or rejects each finding.

Nothing reaches the PR without a person saying yes. Only accepted findings
go into the draft comment, and every decision is logged to decisions.jsonl.
Over time that log shows which rules and which LLM questions are noise,
which is the data needed to tune them or switch them off.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from review_brief.models import Finding

PROMPT = "  [a]ccept  [r]eject  [n]ote + accept  [s]kip  [q]uit > "


@dataclass
class Decision:
    finding_id: str
    decision: str  # accepted | rejected | skipped
    note: str
    reviewer: str
    decided_at: str
    source: str
    evidence_level: str
    dimension: str
    severity: str
    brief_generated_at: str


@dataclass
class TriageResult:
    decisions: list[Decision]
    decisions_path: Path
    comment_path: Path

    def count(self, decision: str) -> int:
        return sum(d.decision == decision for d in self.decisions)


def _show(n: int, total: int, f: Finding) -> str:
    kind = "PROVEN" if f.evidence_level == "proven" else "HYPOTHESIS (LLM, not verified)"
    lines = [
        "",
        f"[{n}/{total}] {kind} · {f.severity} · {f.dimension}",
        f"  {f.location or '-'}  {f.title}",
        f"  source: {f.source}",
    ]
    if f.evidence:
        lines.append(f"  evidence: {f.evidence.splitlines()[0][:200]}")
    lines += [f"  {line}" for line in f.detail.splitlines()[:6]]
    return "\n".join(lines)


def _ask(ask: Callable[[str], str], prompt: str) -> str | None:
    try:
        return ask(prompt).strip()
    except EOFError:
        return None


def run_triage(
    brief_path: Path,
    *,
    reviewer: str,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
) -> TriageResult:
    data = json.loads(brief_path.read_text())
    findings = [Finding.from_dict(f) for f in data["findings"]]
    findings.sort(key=lambda f: f.evidence_level != "proven")  # proven first, order kept otherwise
    generated_at = data.get("generated_at", "")

    say(f"Triage: {len(findings)} finding(s) · risk {data['risk']['level'].upper()} · reviewer {reviewer}")
    decisions: list[Decision] = []
    accepted: list[tuple[Finding, str]] = []
    for n, f in enumerate(findings, 1):
        say(_show(n, len(findings), f))
        while True:
            choice = _ask(ask, PROMPT)
            if choice is None or choice.lower() in {"a", "r", "n", "s", "q"}:
                break
            say("  please type a, r, n, s or q")
        if choice is None or choice.lower() == "q":
            break
        choice, note = choice.lower(), ""
        if choice == "r":
            note = _ask(ask, "  why reject? (optional, helps tune the rules) > ") or ""
        elif choice == "n":
            note = _ask(ask, "  note for the PR comment > ") or ""
        decision = {"a": "accepted", "n": "accepted", "r": "rejected", "s": "skipped"}[choice]
        if decision == "accepted":
            accepted.append((f, note))
        decisions.append(
            Decision(
                finding_id=f.id,
                decision=decision,
                note=note,
                reviewer=reviewer,
                decided_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                source=f.source,
                evidence_level=f.evidence_level,
                dimension=f.dimension,
                severity=f.severity,
                brief_generated_at=generated_at,
            )
        )

    decisions_path = brief_path.parent / "decisions.jsonl"
    with decisions_path.open("a") as fh:
        for d in decisions:
            fh.write(json.dumps(asdict(d)) + "\n")
    comment_path = brief_path.parent / "comment.md"
    comment_path.write_text(render_comment(accepted, data["risk"]["level"], reviewer))
    return TriageResult(decisions, decisions_path, comment_path)


def render_comment(accepted: list[tuple[Finding, str]], risk: str, reviewer: str) -> str:
    def item(f: Finding, note: str) -> list[str]:
        where = f"`{f.location}` " if f.location else ""
        out = [f"- {where}{f.title} _({f.source})_"]
        if f.evidence_level == "hypothesis" and f.detail:
            out.append(f"  - {f.detail.splitlines()[0]}")
        if note:
            out.append(f"  - **Reviewer:** {note}")
        return out

    proven = [(f, n) for f, n in accepted if f.evidence_level == "proven"]
    discuss = [(f, n) for f, n in accepted if f.evidence_level != "proven"]
    lines = ["## Review notes", "", f"Risk assessed by rules: **{risk.upper()}**", ""]
    if not accepted:
        lines.append("The reviewer accepted no findings.")
    if proven:
        lines += ["### Confirmed by tools", *[x for f, n in proven for x in item(f, n)], ""]
    if discuss:
        lines += ["### Raised for discussion", *[x for f, n in discuss for x in item(f, n)], ""]
    lines += [
        "---",
        f"_Advisory, drafted by review-brief. Each item above was accepted by {reviewer}. "
        "This comment is not an approval._",
    ]
    return "\n".join(lines) + "\n"
