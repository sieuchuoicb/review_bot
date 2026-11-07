"""Code quality: syntax and lint. Deterministic only; no LLM.

Ruff results are kept only when they land on lines this PR added, so the
author is not blamed for issues that were already there. Ruff runs with
--isolated so the policy file, not whatever config sits in the repo, decides
the rule set.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

from review_brief.checkers.base import ReviewContext
from review_brief.diff import ChangedFile
from review_brief.models import CheckResult, Finding, Signal, level_rank
from review_brief.workspace import ToolResult, run_tool


def why_failed(result: ToolResult) -> str:
    if result.timed_out:
        return "timed out"
    if result.error:
        return result.error
    tail = (result.stderr or result.stdout).strip().splitlines()
    return f"exit {result.returncode}: {tail[-1] if tail else 'no output'}"


class QualityChecker:
    name = "quality"

    def run(self, ctx: ReviewContext) -> CheckResult:
        res = CheckResult(self.name)
        if not ctx.full_mode:
            res.not_checked.append("Syntax and lint: no repo to apply the diff to (diff-only mode).")
            return res

        lintable: list[ChangedFile] = []
        for f in ctx.python_files:
            try:
                ast.parse(f.new_text or "", filename=f.path)
            except SyntaxError as exc:
                res.findings.append(
                    Finding(
                        id=f"quality.syntax:{f.path}:{exc.lineno}",
                        dimension="quality",
                        title=f"Syntax error: {exc.msg}",
                        severity="high",
                        source="python ast",
                        file=f.path,
                        line=exc.lineno,
                        evidence=(exc.text or "").strip(),
                    )
                )
                res.signals.append(Signal("high", f"syntax error in {f.path}", "python ast"))
            else:
                lintable.append(f)

        if lintable:
            self._ruff(ctx, lintable, res)
        return res

    def _ruff(self, ctx: ReviewContext, files: list[ChangedFile], res: CheckResult) -> None:
        assert ctx.workspace is not None
        head = ctx.workspace.head
        cmd = [
            sys.executable, "-m", "ruff", "check", "--isolated", "--no-cache", "--exit-zero",
            "--output-format", "json", "--select", ",".join(ctx.policy.ruff["select"]),
            *[f.path for f in files],
        ]
        result = run_tool(cmd, head, timeout=60)
        if not result.ok or result.returncode != 0:
            res.not_checked.append(f"Lint: ruff did not run ({why_failed(result)}).")
            return
        try:
            items = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            res.not_checked.append("Lint: could not read ruff's JSON output.")
            return

        by_path = {f.path: f for f in files}
        head_resolved = head.resolve()
        grouped: dict[tuple[str, int], list[dict]] = {}
        ignored = 0
        for item in items:
            rel = Path(item["filename"]).resolve().relative_to(head_resolved).as_posix()
            row = item["location"]["row"]
            if rel in by_path and row in by_path[rel].added:
                grouped.setdefault((rel, row), []).append(item)
            else:
                ignored += 1

        for (rel, row), hits in sorted(grouped.items()):
            codes = [h.get("code") or "syntax" for h in hits]
            severity = max((ctx.policy.ruff_severity(c) for c in codes), key=level_rank)
            res.findings.append(
                Finding(
                    id=f"quality.ruff:{rel}:{row}",
                    dimension="quality",
                    title=" / ".join(dict.fromkeys(h["message"] for h in hits)),
                    severity=severity,
                    source="ruff " + ", ".join(codes),
                    file=rel,
                    line=row,
                    detail="\n".join(f"{c}: {h['message']}" for c, h in zip(codes, hits, strict=True)),
                    evidence=by_path[rel].added[row].strip(),
                )
            )
            if level_rank(severity) >= level_rank("medium"):
                res.signals.append(
                    Signal(severity, f"lint {', '.join(codes)} on a changed line ({rel}:{row})", "ruff")
                )
        if ignored:
            res.notes.append(f"Lint: ignored {ignored} ruff issue(s) on lines this PR did not touch.")
