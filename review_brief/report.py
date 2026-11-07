"""Render a Brief as Markdown (for people) or JSON (for triage and tooling)."""

from __future__ import annotations

import json

from review_brief.models import Brief, Finding, level_rank

_MAX_LOOK_FIRST = 5


def to_json(brief: Brief) -> str:
    return json.dumps(brief.to_dict(), indent=2)


def _finding_md(n: int, f: Finding) -> list[str]:
    where = f" `{f.location}`" if f.location else ""
    out = [f"{n}. **[{f.severity} · {f.dimension}]**{where} {f.title}  ", f"   _{f.source}_"]
    if f.evidence:
        out.append(f"   - evidence: `{f.evidence.splitlines()[0][:200]}`")
    for line in f.detail.splitlines()[:8]:
        out.append(f"   - {line}")
    return out


def where_to_look(brief: Brief) -> list[tuple[str, str]]:
    """Code locations, most urgent first: high proven, hypotheses, cross-file callers, the rest."""
    ranked: list[tuple[int, str, str]] = []
    for f in brief.findings:
        if not (f.file and f.line):
            continue
        if f.evidence_level == "hypothesis":
            score = 2
        else:
            score = 3 if f.severity == "high" else level_rank(f.severity) - 1
        ranked.append((score, f.location, f.title))
    for q in brief.questions:
        for block in q.context:
            if block.label.startswith("caller:") and block.file != q.file:
                line = block.focus_line or block.start_line
                ranked.append((2, f"{block.file}:{line}", f"{block.label[8:]} calls changed {q.target}"))
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for _, loc, why in sorted(ranked, key=lambda r: -r[0]):
        if loc not in seen:
            seen.add(loc)
            out.append((loc, why))
    return out[:_MAX_LOOK_FIRST]


def to_markdown(brief: Brief) -> str:
    llm = brief.llm
    asked = sum(1 for q in brief.questions if q.status in ("answered", "failed"))
    pending = sum(1 for q in brief.questions if q.status == "pending")
    if llm.get("provider"):
        llm_line = f"{llm['provider']}/{llm['model']}, {llm['calls']} call(s)"
        if llm.get("failed"):
            llm_line += f", {llm['failed']} failed"
        if llm.get("dropped_claims"):
            llm_line += f", {llm['dropped_claims']} uncited claim(s) dropped"
        if llm.get("cost_usd"):
            llm_line += f", ${llm['cost_usd']:.4f}"
    else:
        llm_line = "not configured" + (f" ({pending} question(s) pending)" if pending else "")

    lines = [
        "# PR Review Brief",
        "",
        f"**Risk: {brief.risk.level.upper()}** · diff `{brief.diff_source}` · repo `{brief.repo or '-'}` "
        f"· mode: {brief.mode} · LLM: {llm_line}",
        "",
        f"Changed: {', '.join(f'`{p}`' for p in brief.changed_files) or '-'}",
        "",
        "> Advisory only. This brief does not approve or merge anything; a human reviewer decides.",
        "",
        "## Why this risk level",
        *[f"- {r}" for r in brief.risk.reasons],
        "",
        "## Proven by tools and rules",
    ]
    proven = brief.proven
    if proven:
        for n, f in enumerate(proven, 1):
            lines += _finding_md(n, f)
    else:
        lines.append("Nothing. (See *Not checked* before reading that as good news.)")

    lines += ["", "## Needs your judgment (LLM hypotheses, not verified)"]
    hyps = brief.hypotheses
    if hyps:
        for n, f in enumerate(hyps, 1):
            lines += _finding_md(n, f)
    elif asked:
        lines.append("The LLM raised no concerns it could back with a cited line.")
    else:
        lines.append("None.")

    if brief.questions:
        lines += ["", "## Questions the rules raised for the LLM"]
        for q in brief.questions:
            note = f" ({q.status_note})" if q.status_note else ""
            dropped = f", {q.dropped_claims} uncited claim(s) dropped" if q.dropped_claims else ""
            lines.append(f"- [{q.status}{dropped}] `{q.target}` ({q.kind}): {q.trigger}{note}")

    look = where_to_look(brief)
    if look:
        lines += ["", "## Where to look first"]
        lines += [f"{n}. `{loc}` {why}" for n, (loc, why) in enumerate(look, 1)]

    if brief.not_checked:
        lines += ["", "## Not checked", *[f"- {item}" for item in brief.not_checked]]
    if brief.notes:
        lines += ["", "## Notes", *[f"- {item}" for item in brief.notes]]
    return "\n".join(lines) + "\n"
