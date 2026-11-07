"""Policy: does the PR follow the company's rules?

The rules are code; policy.yaml decides which apply, to which paths, and at
what level. Nothing here needs an LLM.
"""

from __future__ import annotations

import re

from review_brief.analysis import is_test_file, iter_python_files
from review_brief.checkers.base import ReviewContext
from review_brief.config import glob_match
from review_brief.models import CheckResult, Finding, Signal

# (label, pattern, group holding the secret value)
SECRET_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    ("private key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"), 0),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), 0),
    ("API key (sk-...)", re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), 0),
    (
        "hardcoded credential",
        re.compile(r"""(?i)\b(?:api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*["']([^"'\s]{8,})["']"""),
        1,
    ),
]


def redact(text: str, value: str) -> str:
    return text.replace(value, value[:4] + "…[redacted]")


class PolicyChecker:
    name = "policy"

    def run(self, ctx: ReviewContext) -> CheckResult:
        res = CheckResult(self.name)
        self._sensitive_paths(ctx, res)
        self._secrets(ctx, res)
        self._new_functions_need_tests(ctx, res)
        return res

    def _sensitive_paths(self, ctx: ReviewContext, res: CheckResult) -> None:
        for rule in ctx.policy.sensitive_paths:
            pattern = rule["pattern"]
            matched = [f.path for f in ctx.files if glob_match(f.path, pattern)]
            if not matched:
                continue
            level = rule.get("level", "medium")
            approvals = rule.get("required_approvals")
            reason = rule.get("reason", "sensitive path")
            res.findings.append(
                Finding(
                    id=f"policy.sensitive-path:{pattern}",
                    dimension="policy",
                    title=f"Touches {pattern}: {reason}"
                    + (f" Needs {approvals} approving reviewers." if approvals else ""),
                    severity=level,
                    source="rule:sensitive-path",
                    file=matched[0],
                    detail="Files: " + ", ".join(matched),
                )
            )
            res.signals.append(Signal(level, f"touches sensitive path {pattern}", "policy"))

    def _secrets(self, ctx: ReviewContext, res: CheckResult) -> None:
        rule = ctx.policy.rule("secrets_in_diff")
        if not rule.get("enabled"):
            return
        level = rule.get("level", "high")
        for f in ctx.files:
            for line_no, text in sorted(f.added.items()):
                for label, pattern, group in SECRET_PATTERNS:
                    m = pattern.search(text)
                    if not m:
                        continue
                    res.findings.append(
                        Finding(
                            id=f"policy.secret:{f.path}:{line_no}",
                            dimension="policy",
                            title=f"Possible {label} committed in the diff",
                            severity=level,
                            source="rule:secrets-in-diff",
                            file=f.path,
                            line=line_no,
                            evidence=redact(text.strip(), m.group(group)),
                        )
                    )
                    res.signals.append(Signal(level, f"possible secret in {f.path}", "policy"))
                    break  # one finding per line

    def _new_functions_need_tests(self, ctx: ReviewContext, res: CheckResult) -> None:
        rule = ctx.policy.rule("new_public_function_requires_test")
        if not rule.get("enabled"):
            return
        if not ctx.full_mode:
            res.not_checked.append("Test-coverage policy: needs the repo (diff-only mode).")
            return
        assert ctx.workspace is not None
        globs = rule.get("test_globs", [])
        tests_text = "\n".join(
            path.read_text(errors="replace")
            for rel, path in iter_python_files(ctx.workspace.head)
            if any(glob_match(rel, g) for g in globs)
        )
        level = rule.get("level", "medium")
        for change in ctx.function_changes:
            fn = change.func
            if change.kind != "new" or not fn.is_public or is_test_file(change.file):
                continue
            if re.search(rf"\b{re.escape(fn.name)}\b", tests_text):
                continue
            res.findings.append(
                Finding(
                    id=f"policy.untested:{change.file}:{fn.qualname}",
                    dimension="policy",
                    title=f"New public function {fn.qualname}() has no test that references it",
                    severity=level,
                    source="rule:new-public-function-requires-test",
                    file=change.file,
                    line=fn.start,
                )
            )
            res.signals.append(Signal(level, f"new public function {fn.qualname} has no test", "policy"))
