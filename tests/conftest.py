from pathlib import Path

import pytest

from review_brief.checkers.base import ReviewContext
from review_brief.config import Policy
from review_brief.diff import parse_diff
from review_brief.workspace import materialize

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_REPO = ROOT / "examples" / "billing-service"
SAMPLE_DIFF = ROOT / "sample_diff.txt"

DOCS_ONLY_DIFF = """\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-# billing-service (fixture)
+# billing-service (test fixture)
"""


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """Tests never see the developer's .env or API key, so they never call a real model."""
    for key in ("OPEN_ROUTER_API_KEY", "OPENROUTER_API_KEY", "LLM_MODEL_NAME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def policy() -> Policy:
    return Policy.load(ROOT / "policy.yaml")


@pytest.fixture
def sample_ctx(policy):
    ws = materialize(FIXTURE_REPO, parse_diff(SAMPLE_DIFF.read_text()))
    yield ReviewContext(files=ws.files, policy=policy, workspace=ws)
    ws.cleanup()


def make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return repo
