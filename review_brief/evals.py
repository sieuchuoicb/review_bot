"""Eval runner: does the pipeline still find what it should, and nothing else?

Each case under evals/cases/<name>/ has an expected.yaml:

    description: ...
    diff: diff.patch                # relative to the case dir
    repo: ../../../examples/billing-service   # optional
    llm_answers: llm_answers.json   # optional canned replies for the mock LLM
    expect:
      risk: high
      llm_calls: 2
      dropped_claims: 1
      max_findings: 8               # noise budget
      must_find:
        - id: <finding id>
        - hypothesis_at: <file>:<line>
        - hypothesis_in: <file>
      must_not_find:
        - id_prefix: <prefix>
      absent_text: [<string that must not appear in the brief, e.g. a secret>]

By default cases run with the mock LLM, so they are deterministic and free.
With a live model (--llm openrouter), line-exact hypothesis checks relax to
file-level and dropped_claims is not checked: the same cases then measure
whether a given model finds the issues at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from review_brief import pipeline, report
from review_brief.config import Policy
from review_brief.llm.base import LLMClient
from review_brief.llm.mock import MockLLM
from review_brief.models import Brief, Finding


@dataclass
class CaseResult:
    name: str
    checks: list[tuple[str, bool]] = field(default_factory=list)
    expected: int = 0
    found: int = 0
    unexpected: list[str] = field(default_factory=list)
    risk: str = ""
    llm_calls: int = 0

    @property
    def passed(self) -> bool:
        return all(ok for _, ok in self.checks)


def _matches(rule: dict[str, Any], f: Finding, live: bool) -> bool:
    if "id" in rule:
        return f.id == rule["id"]
    if "id_prefix" in rule:
        return f.id.startswith(rule["id_prefix"])
    if f.evidence_level != "hypothesis":
        return False
    if "hypothesis_at" in rule:
        file, _, line = rule["hypothesis_at"].rpartition(":")
        return f.file == file and (live or str(f.line) == line)
    if "hypothesis_in" in rule:
        return f.file == rule["hypothesis_in"]
    raise ValueError(f"unknown must_find rule: {rule}")


def _describe(rule: dict[str, Any]) -> str:
    key, value = next(iter(rule.items()))
    return f"{key} {value}"


def check_brief(brief: Brief, expect: dict[str, Any], name: str, live: bool = False) -> CaseResult:
    res = CaseResult(name, risk=brief.risk.level, llm_calls=brief.llm["calls"])

    def equal(label: str, want: object, got: object) -> None:
        res.checks.append((f"{label} == {want} (got {got})", want == got))

    if "risk" in expect:
        equal("risk", expect["risk"], brief.risk.level)
    if "llm_calls" in expect:
        equal("llm calls", expect["llm_calls"], brief.llm["calls"])
    if "dropped_claims" in expect and not live:
        equal("uncited claims dropped", expect["dropped_claims"], brief.llm["dropped_claims"])

    must = expect.get("must_find", [])
    matched: set[str] = set()
    res.expected = len(must)
    for rule in must:
        hits = [f for f in brief.findings if _matches(rule, f, live)]
        matched.update(f.id for f in hits)
        res.found += bool(hits)
        res.checks.append((f"finds {_describe(rule)}", bool(hits)))
    for rule in expect.get("must_not_find", []):
        hits = [f.id for f in brief.findings if _matches(rule, f, live)]
        res.checks.append((f"does not find {_describe(rule)}" + (f" (got {hits})" if hits else ""), not hits))
    if "max_findings" in expect:
        n, cap = len(brief.findings), expect["max_findings"]
        res.checks.append((f"at most {cap} findings (got {n})", n <= cap))
    rendered = report.to_json(brief) + report.to_markdown(brief)
    for text in expect.get("absent_text", []):
        res.checks.append(("secret value is not in the output", text not in rendered))
    res.unexpected = [f.id for f in brief.findings if f.id not in matched]
    return res


def run_case(case_dir: Path, policy: Policy, llm: LLMClient | None = None) -> CaseResult:
    spec = yaml.safe_load((case_dir / "expected.yaml").read_text())
    live = llm is not None
    if llm is None:
        answers = case_dir / spec["llm_answers"] if spec.get("llm_answers") else None
        llm = MockLLM.from_file(answers) if answers else MockLLM()
    repo = (case_dir / spec["repo"]).resolve() if spec.get("repo") else None
    brief = pipeline.review(
        (case_dir / spec["diff"]).read_text(),
        diff_source=f"{case_dir.name}/{spec['diff']}",
        repo=repo,
        policy=policy,
        llm=llm,
    )
    return check_brief(brief, spec.get("expect", {}), case_dir.name, live=live)


def run_all(cases_dir: Path, policy: Policy, llm: LLMClient | None = None) -> list[CaseResult]:
    return [run_case(d, policy, llm) for d in sorted(cases_dir.iterdir()) if (d / "expected.yaml").is_file()]


def format_results(results: list[CaseResult]) -> str:
    lines = [f"{'case':<22} {'result':<6} {'recall':<7} {'unexpected':<10} {'risk':<6} llm calls"]
    for r in results:
        recall = f"{r.found}/{r.expected}" if r.expected else "-"
        verdict = "PASS" if r.passed else "FAIL"
        lines.append(
            f"{r.name:<22} {verdict:<6} {recall:<7} {len(r.unexpected):<10} {r.risk:<6} {r.llm_calls}"
        )
    for r in results:
        failed = [desc for desc, ok in r.checks if not ok]
        if failed:
            lines += ["", f"{r.name} failed:", *[f"  - {d}" for d in failed]]
        if r.unexpected:
            lines += ["", f"{r.name} unexpected findings (not in must_find):"]
            lines += [f"  - {u}" for u in r.unexpected]
    return "\n".join(lines)
