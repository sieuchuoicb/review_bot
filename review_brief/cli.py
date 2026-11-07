"""Command-line entry point.

    review-brief review DIFF [--repo PATH] [--llm auto|openrouter|mock|none]
    review-brief triage runs/<run>/brief.json
    review-brief eval [--llm mock|openrouter]

Exit codes: 0 done, 1 eval failures, 2 bad input or config. There is
deliberately no exit code that means "approved".
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from review_brief import evals, pipeline, report, triage
from review_brief.config import Policy
from review_brief.envfile import load_env
from review_brief.llm.base import LLMClient, LLMConfigError
from review_brief.llm.mock import MockLLM
from review_brief.llm.openrouter import OpenRouterClient


class UsageError(Exception):
    pass


def _policy(path: str | None) -> Policy:
    try:
        if path:
            return Policy.load(Path(path))
        local = Path("policy.yaml")
        return Policy.load(local if local.is_file() else None)
    except (OSError, ValueError) as exc:
        raise UsageError(f"cannot load policy: {exc}") from exc


def _llm(choice: str, mock_answers: str | None) -> LLMClient | None:
    has_key = bool(os.environ.get("OPEN_ROUTER_API_KEY") or os.environ.get("OPENROUTER_API_KEY"))
    if choice == "none" or (choice == "auto" and not has_key):
        return None
    if choice == "mock":
        return MockLLM.from_file(Path(mock_answers)) if mock_answers else MockLLM()
    try:
        return OpenRouterClient.from_env()
    except LLMConfigError as exc:
        raise UsageError(f"OpenRouter: {exc}") from exc


def _save(brief, out_dir: Path, diff_path: Path, audit: list[dict]) -> Path:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", diff_path.stem).strip("-") or "diff"
    run_dir = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "brief.json").write_text(report.to_json(brief))
    (run_dir / "brief.md").write_text(report.to_markdown(brief))
    if audit:
        with (run_dir / "llm_transcript.jsonl").open("w") as fh:
            for entry in audit:
                fh.write(json.dumps(entry, default=str) + "\n")
    return run_dir


def cmd_review(args: argparse.Namespace) -> int:
    diff_path = Path(args.diff)
    try:
        diff_text = diff_path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError(f"cannot read diff {diff_path}: {exc}") from exc
    policy = _policy(args.policy)
    llm = _llm(args.llm, args.mock_answers)

    audit: list[dict] = []
    brief = pipeline.review(
        diff_text,
        diff_source=str(diff_path),
        repo=Path(args.repo) if args.repo else None,
        policy=policy,
        llm=llm,
        audit=audit,
    )
    print(report.to_json(brief) if args.format == "json" else report.to_markdown(brief))
    if not args.no_save:
        run_dir = _save(brief, Path(args.out_dir), diff_path, audit)
        saved = "brief.json, brief.md" + (", llm_transcript.jsonl" if audit else "")
        print(f"Saved {run_dir}/ ({saved})", file=sys.stderr)
        print(f"Next: review-brief triage {run_dir}/brief.json", file=sys.stderr)
    return 0


def cmd_triage(args: argparse.Namespace) -> int:
    brief_path = Path(args.brief)
    if not brief_path.is_file():
        raise UsageError(f"no such brief: {brief_path}")
    result = triage.run_triage(brief_path, reviewer=args.reviewer or getpass.getuser())
    print(
        f"\nAccepted {result.count('accepted')}, rejected {result.count('rejected')}, "
        f"skipped {result.count('skipped')}."
    )
    print(f"Decisions logged to {result.decisions_path}")
    print(f"Draft PR comment (accepted items only): {result.comment_path}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    llm = None if args.llm == "mock" else _llm("openrouter", None)
    results = evals.run_all(Path(args.cases), _policy(args.policy), llm)
    if llm is not None:
        print(f"Live model: {llm.provider}/{llm.model} (hypothesis checks are file-level)\n")
    print(evals.format_results(results))
    return 0 if all(r.passed for r in results) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-brief",
        description="Evidence-driven PR review brief. Rules detect, the LLM interprets, humans decide.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    rev = sub.add_parser("review", help="build a review brief for a diff")
    rev.add_argument("diff", help="unified diff file")
    rev.add_argument("--repo", help="repo the diff applies to (omit for diff-only checks)")
    rev.add_argument("--policy", help="policy YAML (default: ./policy.yaml, else built-in defaults)")
    rev.add_argument(
        "--llm",
        choices=["auto", "openrouter", "mock", "none"],
        default="auto",
        help="auto = OpenRouter if OPEN_ROUTER_API_KEY is set, otherwise no LLM",
    )
    rev.add_argument("--mock-answers", help="canned replies JSON for --llm mock")
    rev.add_argument("--format", choices=["md", "json"], default="md")
    rev.add_argument("--out-dir", default="runs", help="where to save the brief (default: runs/)")
    rev.add_argument("--no-save", action="store_true", help="print only; do not write runs/")
    rev.set_defaults(func=cmd_review)

    tri = sub.add_parser("triage", help="accept or reject each finding; drafts the PR comment")
    tri.add_argument("brief", help="brief.json from a review run")
    tri.add_argument("--reviewer", help="name recorded with each decision (default: $USER)")
    tri.set_defaults(func=cmd_triage)

    ev = sub.add_parser("eval", help="run the eval cases")
    ev.add_argument("--cases", default="evals/cases")
    ev.add_argument("--policy", help="policy YAML (default: ./policy.yaml)")
    ev.add_argument("--llm", choices=["mock", "openrouter"], default="mock")
    ev.set_defaults(func=cmd_eval)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_env(Path(".env"))
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
