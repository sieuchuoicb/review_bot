"""Tolerant unified-diff parser and applier.

Real-world diffs are often hand-edited or truncated, like the sample in this
repo: its hunk-header line counts are wrong and its blank context lines have
no leading space. So we never trust the header counts. A hunk is located by
matching its context and removed lines against the file, using the header's
line number only as a hint for where to look first.

The applier also records the exact line numbers of added lines in the new
file. Every finding is anchored to those numbers, so they have to be right.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")


class PatchError(Exception):
    """The diff cannot be applied to the repo as given."""


@dataclass
class Hunk:
    old_start: int  # 1-based hint from the header
    new_start: int
    section: str
    lines: list[tuple[str, str]] = field(default_factory=list)  # (op, text), op in " -+"


@dataclass
class FilePatch:
    old_path: str | None  # None for a new file
    new_path: str | None  # None for a deleted file
    hunks: list[Hunk] = field(default_factory=list)
    is_binary: bool = False
    is_rename: bool = False

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or "?"


@dataclass
class ChangedFile:
    """One file after the patch is applied (or approximated, in diff-only mode)."""

    path: str
    status: str  # "modified" | "added" | "deleted"
    old_text: str | None
    new_text: str | None
    added: dict[int, str]  # new-file line number -> text
    removed: dict[int, str]  # old-file line number -> text
    exact: bool = True  # False when line numbers come from hunk headers only


def _strip_prefix(raw: str) -> str | None:
    path = raw.split("\t", 1)[0].strip()
    if path == "/dev/null":
        return None
    if path[:2] in ("a/", "b/"):
        path = path[2:]
    return path


def parse_diff(text: str) -> list[FilePatch]:
    patches: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    lines = text.splitlines()

    def start_file() -> FilePatch:
        fp = FilePatch(old_path=None, new_path=None)
        patches.append(fp)
        return fp

    i = 0
    while i < len(lines):
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else ""

        if line.startswith("diff --git "):
            current, hunk = start_file(), None
            parts = line.split(" ")
            if len(parts) >= 4:
                current.old_path = _strip_prefix(parts[2])
                current.new_path = _strip_prefix(parts[3])
        # A "--- " line is a file header only when "+++ " follows; otherwise it
        # is a removed line that happens to start with "--".
        elif line.startswith("--- ") and nxt.startswith("+++ "):
            if current is None or current.hunks:
                current = start_file()
            current.old_path = _strip_prefix(line[4:])
            current.new_path = _strip_prefix(nxt[4:])
            hunk = None
            i += 1
        elif line.startswith("@@") and current is not None and (m := _HUNK_RE.match(line)):
            hunk = Hunk(old_start=int(m.group(1)), new_start=int(m.group(3)), section=m.group(5))
            current.hunks.append(hunk)
        elif current is not None and line.startswith(("Binary files ", "GIT binary patch")):
            current.is_binary = True
        elif current is not None and line.startswith(("rename from ", "rename to ", "similarity index")):
            current.is_rename = True
        elif current is not None and line.startswith("new file mode"):
            current.old_path = None
        elif current is not None and line.startswith("deleted file mode"):
            current.new_path = None
        elif hunk is not None:
            if line.startswith("\\"):  # "\ No newline at end of file"
                pass
            elif line == "":
                hunk.lines.append((" ", ""))  # blank context line missing its leading space
            elif line[0] in " -+":
                hunk.lines.append((line[0], line[1:]))
            else:
                hunk = None  # trailing junk; stop reading this hunk
        i += 1

    return [p for p in patches if p.hunks or p.is_binary or p.is_rename]


def safe_relpath(path: str) -> str:
    """Reject paths that could escape the workspace (absolute or containing ..)."""
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise PatchError(f"refusing unsafe path in diff: {path!r}")
    return str(p)


def _locate(old: list[str], block: list[str], start: int, hint: int) -> int | None:
    """Index where `block` occurs in `old` at or after `start`, nearest to `hint`."""
    if not block:
        return max(start, min(hint, len(old)))
    for normalize in (lambda s: s, str.rstrip):
        target = [normalize(b) for b in block]
        hits = [
            p
            for p in range(start, len(old) - len(block) + 1)
            if [normalize(x) for x in old[p : p + len(block)]] == target
        ]
        if hits:
            return min(hits, key=lambda p: abs(p - hint))
    return None


def _trim_trailing_blank_context(lines: list[tuple[str, str]]) -> list[tuple[str, str]]:
    out = list(lines)
    while out and out[-1] == (" ", ""):
        out.pop()
    return out


def apply_patch(old_text: str | None, fp: FilePatch) -> ChangedFile:
    """Apply one file's hunks. Raises PatchError if any hunk does not fit."""
    path = safe_relpath(fp.path)

    if fp.old_path is None:  # new file
        new_lines = [t for h in fp.hunks for op, t in h.lines if op != "-"]
        return ChangedFile(
            path=path,
            status="added",
            old_text=None,
            new_text="\n".join(new_lines) + "\n",
            added={n: t for n, t in enumerate(new_lines, start=1)},
            removed={},
        )

    if old_text is None:
        raise PatchError(f"{path}: file does not exist in the repo")

    old = old_text.splitlines()
    if fp.new_path is None:  # deleted file
        return ChangedFile(
            path=path,
            status="deleted",
            old_text=old_text,
            new_text=None,
            added={},
            removed={n: t for n, t in enumerate(old, start=1)},
        )

    out: list[str] = []
    added: dict[int, str] = {}
    removed: dict[int, str] = {}
    cursor = 0
    for hunk in fp.hunks:
        hunk_lines = hunk.lines
        block = [t for op, t in hunk_lines if op != "+"]
        pos = _locate(old, block, cursor, hunk.old_start - 1)
        if pos is None:
            hunk_lines = _trim_trailing_blank_context(hunk.lines)
            block = [t for op, t in hunk_lines if op != "+"]
            pos = _locate(old, block, cursor, hunk.old_start - 1)
        if pos is None:
            raise PatchError(
                f"{path}: hunk '@@ -{hunk.old_start} +{hunk.new_start} @@' does not match the file "
                "(context or removed lines not found)"
            )
        out.extend(old[cursor:pos])
        i = pos
        for op, text in hunk_lines:
            if op == " ":
                out.append(old[i])
                i += 1
            elif op == "-":
                removed[i + 1] = old[i]
                i += 1
            else:
                out.append(text)
                added[len(out)] = text
        cursor = i
    out.extend(old[cursor:])

    return ChangedFile(
        path=path,
        status="modified",
        old_text=old_text,
        new_text="\n".join(out) + "\n",
        added=added,
        removed=removed,
    )


def approximate_changes(fp: FilePatch) -> ChangedFile:
    """Diff-only mode: line numbers come from hunk headers, which may be wrong."""
    added: dict[int, str] = {}
    removed: dict[int, str] = {}
    for hunk in fp.hunks:
        old_no, new_no = hunk.old_start, hunk.new_start
        for op, text in hunk.lines:
            if op == " ":
                old_no += 1
                new_no += 1
            elif op == "-":
                removed[old_no] = text
                old_no += 1
            else:
                added[new_no] = text
                new_no += 1
    status = "added" if fp.old_path is None else "deleted" if fp.new_path is None else "modified"
    return ChangedFile(
        path=safe_relpath(fp.path),
        status=status,
        old_text=None,
        new_text=None,
        added=added,
        removed=removed,
        exact=False,
    )
