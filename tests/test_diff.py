import pytest

from review_brief.diff import PatchError, apply_patch, parse_diff
from tests.conftest import FIXTURE_REPO, SAMPLE_DIFF


def test_parses_sample_despite_wrong_counts_and_bare_blank_lines():
    patches = parse_diff(SAMPLE_DIFF.read_text())
    assert [p.path for p in patches] == ["billing/invoice.py"]
    first, second = patches[0].hunks
    assert first.lines[-1] == (" ", "")  # blank context line with no leading space
    assert sum(op == "+" for op, _ in second.lines) == 9


def test_applies_sample_and_maps_added_lines_exactly():
    fp = parse_diff(SAMPLE_DIFF.read_text())[0]
    changed = apply_patch((FIXTURE_REPO / "billing/invoice.py").read_text(), fp)
    assert changed.status == "modified"
    assert set(changed.removed) == {14, 32, 33}
    assert set(changed.added) == {14, 32, 33, 34, 35, 36, 38, 39, 40, 41}
    new_lines = changed.new_text.splitlines()
    for line_no, text in changed.added.items():
        assert new_lines[line_no - 1] == text
    assert changed.added[35].strip() == "except Exception:"


def test_hunk_that_does_not_match_raises():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n-not in the file\n+new\n"
    with pytest.raises(PatchError, match="does not match"):
        apply_patch("something else\n", parse_diff(diff)[0])


@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd"])
def test_rejects_paths_that_escape_the_repo(path):
    diff = f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-a\n+b\n".replace("a//", "/")
    with pytest.raises(PatchError, match="unsafe path"):
        apply_patch("a\n", parse_diff(diff)[0])


def test_new_and_deleted_files():
    diff = (
        "diff --git a/new.py b/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/new.py\n"
        "@@ -0,0 +1,2 @@\n+x = 1\n+y = 2\n"
        "diff --git a/old.py b/old.py\ndeleted file mode 100644\n--- a/old.py\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-z = 3\n"
    )
    new, old = parse_diff(diff)
    added = apply_patch(None, new)
    assert added.status == "added" and added.added == {1: "x = 1", 2: "y = 2"}
    deleted = apply_patch("z = 3\n", old)
    assert deleted.status == "deleted" and deleted.new_text is None


def test_removed_line_starting_with_dashes_is_not_a_file_header():
    diff = "--- a/notes.md\n+++ b/notes.md\n@@ -1,2 +1,1 @@\n keep\n--- old rule\n"
    changed = apply_patch("keep\n-- old rule\n", parse_diff(diff)[0])
    assert changed.new_text == "keep\n"
