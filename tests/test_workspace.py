import sys

import pytest

from review_brief.diff import parse_diff
from review_brief.workspace import WorkspaceError, materialize, run_tool
from tests.conftest import FIXTURE_REPO, SAMPLE_DIFF


def test_tools_never_see_our_api_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("OPEN_ROUTER_API_KEY", "sk-or-should-not-leak")
    result = run_tool(
        [sys.executable, "-c", "import os; print(os.environ.get('OPEN_ROUTER_API_KEY'))"], tmp_path
    )
    assert result.ok
    assert result.stdout.strip() == "None"


def test_tool_timeout_is_reported_not_raised(tmp_path):
    result = run_tool([sys.executable, "-c", "import time; time.sleep(5)"], tmp_path, timeout=0.5)
    assert result.timed_out and not result.ok


def test_materialize_keeps_base_and_applies_head():
    ws = materialize(FIXTURE_REPO, parse_diff(SAMPLE_DIFF.read_text()))
    try:
        assert "export_csv" not in (ws.base / "billing/invoice.py").read_text()
        assert "export_csv" in (ws.head / "billing/invoice.py").read_text()
    finally:
        ws.cleanup()
    assert not ws.root.exists()


def test_materialize_is_all_or_nothing():
    bad = "--- a/billing/invoice.py\n+++ b/billing/invoice.py\n@@ -1 +1 @@\n-no such line\n+x\n"
    with pytest.raises(WorkspaceError, match="does not match"):
        materialize(FIXTURE_REPO, parse_diff(bad))
