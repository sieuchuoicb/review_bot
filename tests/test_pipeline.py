import json

from review_brief import cli, pipeline, report
from review_brief.config import Policy
from review_brief.models import Signal
from review_brief.risk import assess, llm_gate
from tests.conftest import DOCS_ONLY_DIFF, FIXTURE_REPO, SAMPLE_DIFF


def run(diff_text, repo=FIXTURE_REPO, policy=None):
    return pipeline.review(
        diff_text, diff_source="test.diff", repo=repo, policy=policy or Policy.load(None)
    )


def test_risk_is_the_highest_signal_with_every_reason():
    risk = assess([Signal("medium", "a", "x"), Signal("high", "b", "y"), Signal("low", "c", "z")])
    assert risk.level == "high"
    assert risk.reasons[0] == "[high] b" and len(risk.reasons) == 3
    assert assess([]).level == "low"


def test_llm_gate_respects_min_risk():
    policy = Policy.load(None)
    assert llm_gate(assess([Signal("low", "a", "x")]), policy)[0] is False
    assert llm_gate(assess([Signal("medium", "a", "x")]), policy)[0] is True


def test_sample_diff_end_to_end(policy):
    brief = run(SAMPLE_DIFF.read_text(), policy=policy)
    assert brief.mode == "full" and brief.risk.level == "high"
    ids = {f.id for f in brief.findings}
    assert {
        "safety.test:tests/test_invoice.py::test_total_excludes_tax",
        "safety.callers:billing/invoice.py:Invoice.total",
        "quality.ruff:billing/invoice.py:35",
        "policy.sensitive-path:billing/**",
        "policy.untested:billing/invoice.py:Invoice.export_csv",
    } <= ids
    assert all(f.evidence_level == "proven" for f in brief.findings)
    assert [q.status for q in brief.questions] == ["pending", "pending"]
    assert brief.questions[0].kind == "behaviour_change"  # cross-file impact asked first
    look = [loc for loc, _ in report.where_to_look(brief)]
    assert "billing/reports.py:12" in look


def test_docs_only_change_is_low_risk_and_raises_no_questions(policy):
    brief = run(DOCS_ONLY_DIFF, policy=policy)
    assert brief.risk.level == "low"
    assert brief.findings == [] and brief.questions == []


def test_without_repo_it_degrades_loudly(policy):
    brief = run(SAMPLE_DIFF.read_text(), repo=None, policy=policy)
    assert brief.mode == "diff-only"
    assert any("No repo given" in item for item in brief.not_checked)
    assert any("Tests and caller analysis" in item for item in brief.not_checked)
    # Policy rules that only need the diff still run.
    assert "policy.sensitive-path:billing/**" in {f.id for f in brief.findings}


def test_diff_that_does_not_apply_falls_back_to_diff_only(policy):
    bad = "--- a/billing/invoice.py\n+++ b/billing/invoice.py\n@@ -1 +1 @@\n-no such line\n+x\n"
    brief = run(bad, policy=policy)
    assert brief.mode == "diff-only"
    assert any("did not apply" in item for item in brief.not_checked)


def test_too_many_questions_are_capped(policy):
    policy.data["llm"]["max_questions"] = 1
    brief = run(SAMPLE_DIFF.read_text(), policy=policy)
    assert [q.status for q in brief.questions] == ["pending", "not_asked"]
    assert "max_questions" in brief.questions[1].status_note


def test_markdown_is_advisory_and_separates_evidence(policy):
    md = report.to_markdown(run(SAMPLE_DIFF.read_text(), policy=policy))
    assert "Advisory only" in md
    assert md.index("## Proven by tools and rules") < md.index("## Needs your judgment")


def test_cli_review_json(capsys):
    code = cli.main(
        ["review", str(SAMPLE_DIFF), "--repo", str(FIXTURE_REPO), "--format", "json", "--no-save",
         "--llm", "none"]
    )
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["risk"]["level"] == "high"


def test_cli_missing_diff_is_a_usage_error(capsys):
    assert cli.main(["review", "does-not-exist.diff", "--no-save"]) == 2
