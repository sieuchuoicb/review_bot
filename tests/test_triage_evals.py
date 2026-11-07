import json
import subprocess
import sys

from review_brief import cli, evals, pipeline, report, triage
from review_brief.config import Policy
from review_brief.llm.mock import MockLLM
from tests.conftest import FIXTURE_REPO, ROOT, SAMPLE_DIFF

ANSWERS = ROOT / "evals/cases/billing-tax-change/llm_answers.json"


def write_brief(tmp_path, policy):
    brief = pipeline.review(
        SAMPLE_DIFF.read_text(), diff_source="d", repo=FIXTURE_REPO, policy=policy,
        llm=MockLLM.from_file(ANSWERS),
    )
    path = tmp_path / "run" / "brief.json"
    path.parent.mkdir()
    path.write_text(report.to_json(brief))
    return path, brief


def scripted(answers):
    replies = iter(answers)
    return lambda prompt: next(replies)


def test_triage_only_accepted_findings_reach_the_comment(tmp_path, policy):
    path, brief = write_brief(tmp_path, policy)
    first, second, third = (f.title for f in brief.proven[:3])
    # accept, reject with a reason, accept with a note, then quit
    result = triage.run_triage(
        path, reviewer="sam", ask=scripted(["a", "r", "noise for billing", "n", "please add a test", "q"]),
        say=lambda _: None,
    )
    assert [d.decision for d in result.decisions] == ["accepted", "rejected", "accepted"]

    logged = [json.loads(line) for line in result.decisions_path.read_text().splitlines()]
    assert logged[1]["note"] == "noise for billing" and logged[1]["reviewer"] == "sam"
    assert all(entry["brief_generated_at"] == brief.generated_at for entry in logged)

    comment = result.comment_path.read_text()
    assert first in comment and third in comment and second not in comment
    assert "**Reviewer:** please add a test" in comment
    assert "not an approval" in comment


def test_triage_survives_end_of_input_and_rejects_bad_keys(tmp_path, policy):
    path, _ = write_brief(tmp_path, policy)
    replies = iter(["x", "s"])  # an invalid key, a skip, then end of input

    def ask(prompt):
        try:
            return next(replies)
        except StopIteration:
            raise EOFError from None

    result = triage.run_triage(path, reviewer="sam", ask=ask, say=lambda _: None)
    assert [d.decision for d in result.decisions] == ["skipped"]
    assert "accepted no findings" in result.comment_path.read_text()


def test_hypotheses_are_labelled_for_discussion_in_the_comment(tmp_path, policy):
    path, brief = write_brief(tmp_path, policy)
    n_proven = len(brief.proven)
    result = triage.run_triage(
        path, reviewer="sam", ask=scripted(["s"] * n_proven + ["a", "q"]), say=lambda _: None
    )
    comment = result.comment_path.read_text()
    assert "### Raised for discussion" in comment and "### Confirmed by tools" not in comment
    assert result.decisions[-1].evidence_level == "hypothesis"


def test_eval_cases_pass_with_the_mock_llm():
    results = evals.run_all(ROOT / "evals/cases", Policy.load(ROOT / "policy.yaml"))
    assert {r.name for r in results} == {"billing-tax-change", "docs-only", "secret-leak"}
    failures = {r.name: [d for d, ok in r.checks if not ok] for r in results if not r.passed}
    assert failures == {}
    assert all(r.unexpected == [] for r in results)


def test_eval_detects_a_missed_finding(policy):
    brief = pipeline.review("", diff_source="d", repo=None, policy=policy)
    result = evals.check_brief(brief, {"must_find": [{"id": "safety.test:x"}]}, "empty")
    assert not result.passed and result.found == 0


def test_cli_eval_exit_code(capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    assert cli.main(["eval"]) == 0
    assert "billing-tax-change" in capsys.readouterr().out


def test_cli_openrouter_without_config_is_a_clear_usage_error(capsys):
    code = cli.main(["review", str(SAMPLE_DIFF), "--llm", "openrouter", "--no-save"])
    assert code == 2
    assert "OPEN_ROUTER_API_KEY" in capsys.readouterr().err


def test_legacy_review_bot_entry_point_still_works(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(ROOT / "review_bot.py"), str(SAMPLE_DIFF), "--repo", str(FIXTURE_REPO),
         "--llm", "none", "--no-save"],
        capture_output=True, text=True, cwd=tmp_path, timeout=120,
        env={"PATH": "", "PYTHONPATH": str(ROOT)},
    )
    assert proc.returncode == 0, proc.stderr
    assert "# PR Review Brief" in proc.stdout and "Risk: HIGH" in proc.stdout
