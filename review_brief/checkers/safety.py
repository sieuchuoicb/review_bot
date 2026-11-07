"""Safety: will this change break something that depends on it?

Deterministic part (proven evidence):
  - tests run before and after the PR; a test that passed before and fails
    after is a regression this PR caused
  - changed public functions and every call site that depends on them
  - removed functions that are still called

LLM part: questions only. When a public function changes and has callers,
or a class gains a public method, the rule raises a narrow question with
exactly the code needed to answer it. The pipeline decides whether to ask.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from review_brief.analysis import (
    CallSite,
    FunctionChange,
    class_source,
    find_callers,
    index_functions,
    is_test_file,
    iter_python_files,
)
from review_brief.checkers.base import ReviewContext
from review_brief.checkers.quality import why_failed
from review_brief.models import CheckResult, ContextBlock, Finding, Question, Signal
from review_brief.workspace import run_tool

MAX_CALLERS_IN_CONTEXT = 5
MAX_CLASS_LINES = 150


@dataclass
class TestRun:
    results: dict[str, dict] = field(default_factory=dict)  # test id -> {status, file, line, message}
    error: str = ""


def run_pytest(root: Path, report: Path, timeout: float) -> TestRun:
    result = run_tool(
        [
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "-o", "junit_family=xunit1", f"--junitxml={report}",
        ],
        root,
        timeout=timeout,
        extra_env={"PYTHONPATH": str(root)},
    )
    if result.timed_out:
        return TestRun(error=f"timed out after {timeout:.0f}s")
    if result.error:
        return TestRun(error=result.error)
    if result.returncode == 5:  # no tests collected
        return TestRun()
    if not report.exists():
        return TestRun(error=f"pytest produced no report ({why_failed(result)})")
    return TestRun(results=_parse_junit(report))


def _parse_junit(report: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for case in ET.parse(report).getroot().iter("testcase"):
        file = case.get("file") or ""
        name = case.get("name", "?")
        test_id = f"{file}::{name}" if file else f"{case.get('classname', '')}::{name}"
        status, message = "passed", ""
        for tag in ("failure", "error", "skipped"):
            el = case.find(tag)
            if el is not None:
                status = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
                message = (el.get("message") or el.text or "").strip()
                break
        line = case.get("line")
        out[test_id] = {
            "status": status,
            "name": name,
            "file": file or None,
            "line": int(line) + 1 if line and line.isdigit() else None,  # junit lines are 0-based
            "message": message[:600],
        }
    return out


class SafetyChecker:
    name = "safety"

    def run(self, ctx: ReviewContext) -> CheckResult:
        res = CheckResult(self.name)
        if not ctx.full_mode:
            res.not_checked.append(
                "Tests and caller analysis: no repo to apply the diff to (diff-only mode)."
            )
            return res
        for change in ctx.function_changes:
            if not change.func.is_public:
                continue
            if change.kind == "modified":
                self._modified(ctx, change, res)
            elif change.kind == "new" and change.func.class_name:
                self._new_method(ctx, change, res)
            elif change.kind == "removed":
                self._removed(ctx, change, res)
        self._tests(ctx, res)
        return res

    # --- impact -----------------------------------------------------------

    def _modified(self, ctx: ReviewContext, change: FunctionChange, res: CheckResult) -> None:
        assert ctx.workspace is not None
        fn = change.func
        callers = find_callers(ctx.workspace.head, fn.name, exclude_qualname=fn.qualname)
        if not callers:
            return
        other_files = sorted({c.file for c in callers if c.file != change.file})
        res.findings.append(
            Finding(
                id=f"safety.callers:{change.file}:{fn.qualname}",
                dimension="safety",
                title=f"{fn.qualname}() changed; {len(callers)} call site(s) depend on it"
                + (f", including {', '.join(other_files)}" if other_files else ""),
                severity="medium",
                source="rule:changed-function-callers (AST, matched by name)",
                file=change.file,
                line=fn.start,
                detail="\n".join(f"{c.file}:{c.line} in {c.caller}: {c.code}" for c in callers),
            )
        )
        reason = f"public function {fn.qualname} changed and has {len(callers)} caller(s)"
        res.signals.append(Signal("medium", reason, "analysis"))

        context = []
        if change.base_func is not None:
            context.append(
                ContextBlock(f"before: {fn.qualname}", change.file, change.base_func.start,
                             change.base_func.source, version="base")
            )
        context.append(ContextBlock(f"after: {fn.qualname}", change.file, fn.start, fn.source))
        context.extend(_caller_blocks(ctx.workspace.head, callers, change.file))
        res.questions.append(
            Question(
                id=f"behaviour_change:{change.file}:{fn.qualname}",
                kind="behaviour_change",
                target=fn.qualname,
                file=change.file,
                line=fn.start,
                trigger=f"{fn.qualname}() changed and has {len(callers)} caller(s)"
                + (f" in {len(other_files)} other file(s)" if other_files else ""),
                ask=(
                    f"`{fn.qualname}` changed in this PR (before and after are shown). For each caller "
                    "shown, does the change break the caller's result or the assumptions it makes? "
                    "Report only concrete problems, and cite the caller line."
                ),
                context=context,
                priority=10 + 5 * len(other_files),
            )
        )

    def _new_method(self, ctx: ReviewContext, change: FunctionChange, res: CheckResult) -> None:
        fn = change.func
        head_file = next(f for f in ctx.files if f.path == change.file)
        found = class_source(head_file.new_text or "", fn.class_name or "")
        if found is None:
            return
        start, source = found
        if len(source.splitlines()) > MAX_CLASS_LINES:
            start, source = fn.start, fn.source
        res.questions.append(
            Question(
                id=f"new_method:{change.file}:{fn.qualname}",
                kind="new_method",
                target=fn.qualname,
                file=change.file,
                line=fn.start,
                trigger=f"new public method {fn.qualname}",
                ask=(
                    f"`{fn.qualname}` is new in this PR. Is it consistent with the rest of "
                    f"`{fn.class_name}`: its state, its invariants, and how other methods compute the "
                    "same values? Report only concrete problems, and cite the line."
                ),
                context=[ContextBlock(f"class {fn.class_name} (after the PR)", change.file, start, source)],
                priority=5,
            )
        )

    def _removed(self, ctx: ReviewContext, change: FunctionChange, res: CheckResult) -> None:
        assert ctx.workspace is not None
        fn = change.func
        callers = find_callers(ctx.workspace.head, fn.name)
        if not callers:
            return
        first = callers[0]
        res.findings.append(
            Finding(
                id=f"safety.removed-still-called:{change.file}:{fn.qualname}",
                dimension="safety",
                title=f"{fn.qualname}() was removed but is still called in {len(callers)} place(s)",
                severity="high",
                source="rule:removed-function-callers (AST, matched by name)",
                file=first.file,
                line=first.line,
                detail="\n".join(f"{c.file}:{c.line} in {c.caller}: {c.code}" for c in callers),
            )
        )
        res.signals.append(Signal("high", f"removed function {fn.qualname} is still called", "analysis"))

    # --- tests ------------------------------------------------------------

    def _tests(self, ctx: ReviewContext, res: CheckResult) -> None:
        ws = ctx.workspace
        assert ws is not None
        if not any(is_test_file(rel) for rel, _ in iter_python_files(ws.head)):
            res.not_checked.append("Tests: the repo has no test files.")
            return
        timeout = float(ctx.policy.tests["timeout_seconds"])
        base = run_pytest(ws.base, ws.root / "junit-base.xml", timeout)
        head = run_pytest(ws.head, ws.root / "junit-head.xml", timeout)
        if head.error:
            res.not_checked.append(f"Tests: could not run them after the PR ({head.error}).")
            return
        if base.error:
            res.notes.append(
                f"Tests: could not run them before the PR ({base.error}); failures are not attributed."
            )

        passed = sum(1 for r in head.results.values() if r["status"] == "passed")
        regressions = 0
        for test_id, r in head.results.items():
            if r["status"] not in ("failed", "error"):
                continue
            before = None if base.error else base.results.get(test_id, {}).get("status", "absent")
            if before in ("failed", "error"):
                res.notes.append(f"Tests: {test_id} already failed before this PR (not attributed to it).")
                continue
            if before == "passed":
                regressions += 1
                title = f"Test regression: {r['name']} passes before this PR and fails after it"
            elif before == "absent":
                title = f"New test fails: {r['name']}"
            else:
                title = f"Test fails after this PR: {r['name']} (result before the PR unknown)"
            res.findings.append(
                Finding(
                    id=f"safety.test:{test_id}",
                    dimension="safety",
                    title=title,
                    severity="high",
                    source="pytest (before vs after)",
                    file=r["file"],
                    line=r["line"],
                    evidence=r["message"],
                )
            )
            res.signals.append(Signal("high", f"test fails after the PR: {r['name']}", "pytest"))
        total = len(head.results)
        res.notes.append(f"Tests: {passed}/{total} pass after the PR; regressions it caused: {regressions}.")


def _caller_blocks(head: Path, callers: list[CallSite], changed_file: str) -> list[ContextBlock]:
    """The enclosing function of each caller, callers in other files first."""
    blocks: list[ContextBlock] = []
    seen: set[tuple[str, str]] = set()
    ordered = sorted(callers, key=lambda c: (c.file == changed_file, c.file, c.line))
    for call in ordered:
        key = (call.file, call.caller)
        if key in seen:
            continue
        seen.add(key)
        source = (head / call.file).read_text()
        fn = index_functions(source).get(call.caller)
        if fn is not None:
            start, snippet = fn.start, fn.source
        else:
            lines = source.splitlines()
            start = max(call.line - 3, 1)
            snippet = "\n".join(lines[start - 1 : call.line + 2])
        blocks.append(
            ContextBlock(f"caller: {call.caller}", call.file, start, snippet, focus_line=call.line)
        )
        if len(blocks) >= MAX_CALLERS_IN_CONTEXT:
            break
    return blocks
