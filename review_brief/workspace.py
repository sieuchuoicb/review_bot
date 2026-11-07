"""Materialise the PR on disk and run tools against it.

Two throwaway copies of the repo are made: base (before the PR) and head
(after). Comparing tool results across the two is what lets us say "this PR
caused it" instead of "this is broken".

Running tests means executing code from the PR, so tools run with a timeout
and a stripped environment: no API keys or tokens from our process reach the
PR's code. That is a minimum, not a sandbox. In production this belongs in a
container with no network and no credentials (see README).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from review_brief.diff import ChangedFile, FilePatch, PatchError, apply_patch, safe_relpath

_IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", "runs"
)
_SAFE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "TMPDIR")


class WorkspaceError(Exception):
    pass


@dataclass
class Workspace:
    root: Path
    base: Path
    head: Path
    files: list[ChangedFile]
    skipped: list[str] = field(default_factory=list)

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def materialize(repo: Path, patches: list[FilePatch]) -> Workspace:
    """Copy the repo twice and apply the patch to the head copy. All or nothing."""
    if not repo.is_dir():
        raise WorkspaceError(f"repo path is not a directory: {repo}")

    root = Path(tempfile.mkdtemp(prefix="review-brief-"))
    base, head = root / "base", root / "head"
    try:
        shutil.copytree(repo, base, ignore=_IGNORE)
        shutil.copytree(repo, head, ignore=_IGNORE)
        files: list[ChangedFile] = []
        skipped: list[str] = []
        for fp in patches:
            if fp.is_binary or fp.is_rename:
                kind = "binary" if fp.is_binary else "rename"
                skipped.append(f"{fp.path}: {kind} changes are not analysed")
                continue
            rel = safe_relpath(fp.path)
            base_file = base / rel
            old_text = base_file.read_text() if fp.old_path and base_file.is_file() else None
            changed = apply_patch(old_text, fp)
            target = head / rel
            if changed.new_text is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(changed.new_text)
            files.append(changed)
        return Workspace(root=root, base=base, head=head, files=files, skipped=skipped)
    except (PatchError, OSError, UnicodeDecodeError) as exc:
        shutil.rmtree(root, ignore_errors=True)
        raise WorkspaceError(str(exc)) from exc


@dataclass
class ToolResult:
    cmd: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    error: str = ""
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode is not None and not self.timed_out and not self.error


def run_tool(
    cmd: list[str], cwd: Path, timeout: float = 120, extra_env: dict[str, str] | None = None
) -> ToolResult:
    env = {k: os.environ[k] for k in _SAFE_ENV_KEYS if k in os.environ}
    env["HOME"] = str(cwd)  # keep tools from reading or writing the real home dir
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra_env or {})
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        return ToolResult(
            cmd, None, _text(exc.stdout), _text(exc.stderr), timed_out=True,
            duration_s=time.monotonic() - started,
        )
    except OSError as exc:
        return ToolResult(cmd, None, "", "", error=str(exc), duration_s=time.monotonic() - started)
    return ToolResult(
        cmd, proc.returncode, proc.stdout, proc.stderr, duration_s=time.monotonic() - started
    )


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value
