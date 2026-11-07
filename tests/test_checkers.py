import pytest

from review_brief.checkers.base import ReviewContext
from review_brief.checkers.policy import PolicyChecker
from review_brief.checkers.quality import QualityChecker
from review_brief.checkers.safety import SafetyChecker
from review_brief.config import glob_match
from review_brief.diff import apply_patch, parse_diff
from review_brief.workspace import materialize
from tests.conftest import make_repo


def test_quality_reports_bare_except_on_the_changed_line(sample_ctx):
    res = QualityChecker().run(sample_ctx)
    [finding] = res.findings
    assert finding.location == "billing/invoice.py:35"
    assert finding.source == "ruff S110, BLE001"
    assert finding.evidence_level == "proven"


def test_quality_ignores_lint_on_untouched_lines(tmp_path, policy):
    repo = make_repo(tmp_path, {"app.py": "import os\n\n\ndef f():\n    return 1\n"})
    diff = "--- a/app.py\n+++ b/app.py\n@@ -5 +5,2 @@\n     return 1\n+import sys\n"
    ws = materialize(repo, parse_diff(diff))
    try:
        res = QualityChecker().run(ReviewContext(ws.files, policy, ws))
    finally:
        ws.cleanup()
    assert [f.location for f in res.findings] == ["app.py:6"]  # new unused import only
    assert any("ignored 1 ruff issue" in n for n in res.notes)  # pre-existing `import os`


def test_quality_reports_syntax_error_instead_of_linting(tmp_path, policy):
    repo = make_repo(tmp_path, {"app.py": "x = 1\n"})
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = (\n"
    ws = materialize(repo, parse_diff(diff))
    try:
        res = QualityChecker().run(ReviewContext(ws.files, policy, ws))
    finally:
        ws.cleanup()
    assert res.findings[0].title.startswith("Syntax error")
    assert res.signals[0].level == "high"


def test_safety_proves_the_regression_by_comparing_before_and_after(sample_ctx):
    res = SafetyChecker().run(sample_ctx)
    tests = [f for f in res.findings if f.id.startswith("safety.test:")]
    assert [f.id for f in tests] == ["safety.test:tests/test_invoice.py::test_total_excludes_tax"]
    assert tests[0].title.startswith("Test regression")
    assert "110" in tests[0].evidence


def test_safety_finds_callers_across_files(sample_ctx):
    res = SafetyChecker().run(sample_ctx)
    callers = next(f for f in res.findings if f.id == "safety.callers:billing/invoice.py:Invoice.total")
    assert "billing/reports.py:12 in monthly_revenue" in callers.detail


def test_safety_raises_narrow_questions_with_the_code_to_answer_them(sample_ctx):
    res = SafetyChecker().run(sample_ctx)
    by_kind = {q.kind: q for q in res.questions}
    assert set(by_kind) == {"behaviour_change", "new_method"}

    behaviour = by_kind["behaviour_change"]
    labels = [b.label for b in behaviour.context]
    assert labels[:2] == ["before: Invoice.total", "after: Invoice.total"]
    assert "caller: monthly_revenue" in labels
    assert behaviour.context[0].version == "base"

    new_method = by_kind["new_method"]
    assert new_method.target == "Invoice.export_csv"
    assert "total_override" in new_method.context[0].code  # the class state it must respect


def test_safety_flags_removed_function_that_is_still_called(tmp_path, policy):
    repo = make_repo(
        tmp_path,
        {
            "lib.py": "def helper():\n    return 1\n",
            "app.py": "from lib import helper\n\n\ndef run():\n    return helper()\n",
        },
    )
    diff = "--- a/lib.py\n+++ b/lib.py\n@@ -1,2 +1 @@\n-def helper():\n-    return 1\n+X = 1\n"
    ws = materialize(repo, parse_diff(diff))
    try:
        res = SafetyChecker().run(ReviewContext(ws.files, policy, ws))
    finally:
        ws.cleanup()
    [finding] = [f for f in res.findings if f.id.startswith("safety.removed-still-called")]
    assert finding.severity == "high" and finding.location == "app.py:5"


def test_policy_flags_sensitive_path_and_untested_function(sample_ctx):
    res = PolicyChecker().run(sample_ctx)
    ids = {f.id for f in res.findings}
    assert "policy.sensitive-path:billing/**" in ids
    assert "policy.untested:billing/invoice.py:Invoice.export_csv" in ids


def test_policy_flags_secrets_and_redacts_them(sample_ctx):
    secret = "sk-REPLACE-ME-1234567890"  # the pattern review_bot.py shipped with
    diff = f'--- /dev/null\n+++ b/cfg.py\n@@ -0,0 +1 @@\n+api_key = "{secret}"\n'
    files = [*sample_ctx.files, apply_patch(None, parse_diff(diff)[0])]
    res = PolicyChecker().run(ReviewContext(files, sample_ctx.policy, sample_ctx.workspace))
    [finding] = [f for f in res.findings if f.id.startswith("policy.secret")]
    assert finding.severity == "high"
    assert secret not in finding.evidence and "[redacted]" in finding.evidence


@pytest.mark.parametrize(
    "path, pattern, expected",
    [
        ("billing/invoice.py", "billing/**", True),
        ("billing/sub/x.py", "billing/**", True),
        ("billingx/a.py", "billing/**", False),
        ("test_a.py", "**/test_*.py", True),
        ("a/b/test_a.py", "**/test_*.py", True),
        ("a/b.py", "*.py", False),
    ],
)
def test_glob_match(path, pattern, expected):
    assert glob_match(path, pattern) is expected
