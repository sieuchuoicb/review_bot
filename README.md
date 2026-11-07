# review-brief

**Rules detect. The LLM interprets. Humans decide.**

`review-brief` turns a PR diff into a **reviewer brief**: what tools have proven, what still needs a
human's judgment, and where to look first. Deterministic tools do everything that can be decided
mechanically. The LLM is only asked narrow questions that the rules raise, and its answers must cite
real lines. A human accepts or rejects every item before anything is posted. It never approves or
merges.

It replaces `review_bot.py`, which still works as an entry point (see [Quick start](#quick-start)).

---

## Why I redirected it instead of hardening it

`review_bot.py` sent the whole diff to one LLM call and printed whatever came back. Its surface
problems are easy to list: a hardcoded API key, the `openai<1.0` API that current installs no longer
have, no input or error handling, and no limit on diff size. Hardening would fix those, but the
**shape** would stay wrong:

- **The LLM did work tools do better.** Lint, syntax checks and "does the test suite still pass" are
  deterministic. An LLM does them slower, costs more, and is sometimes wrong.
- **Nothing it said could be checked.** The output was free text with no file or line anchors, so a
  reviewer had to re-verify every sentence. That is the reading the bot was meant to save.
- **It saw only the diff.** It couldn't see that `billing/reports.py` already adds tax to
  `Invoice.total()`, so it couldn't notice that the sample PR now taxes revenue twice.
- **Its output was verdict-shaped,** with no human step and no record of what anyone decided.

The underlying need is not "an LLM that writes review comments". It is **spending reviewer attention
where it matters**. So I took the *redirect* option and changed the shape:

| Layer | Does | On the sample diff |
|---|---|---|
| **Rules** (no LLM) | syntax, ruff, tests run before *and* after the PR, callers of changed functions, policy | proves the `test_total_excludes_tax` regression, the bare `except: pass`, 4 callers of `Invoice.total`, `billing/**` sensitivity, untested `export_csv` |
| **LLM** (narrow, triggered by rules) | answers only the questions rules raise, given only the code it needs | "`total()` changed. Do its callers break?" finds `reports.py:12` taxing twice. "Is new `export_csv` consistent with its class?" finds it ignores the discount. |
| **Human** | decides | accepts or rejects each item; only accepted items reach the draft PR comment |

## Quick start

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync

# 1. Rules only, no LLM: fast, free, deterministic
uv run python -m review_brief review sample_diff.txt --repo examples/billing-service --llm none

# 2. Mock LLM with canned answers (offline; same prompt/parse/grounding path as a real model)
uv run python -m review_brief review sample_diff.txt --repo examples/billing-service \
    --llm mock --mock-answers evals/cases/billing-tax-change/llm_answers.json

# 3. Real model via OpenRouter: put these in .env (git-ignored)
#      OPEN_ROUTER_API_KEY=sk-or-...
#      LLM_MODEL_NAME=z-ai/glm-5.3-flash
uv run python -m review_brief review sample_diff.txt --repo examples/billing-service

# Human step: accept/reject each finding → decisions.jsonl + comment.md
uv run python -m review_brief triage runs/<run>/brief.json

# Evals (mock by default; --llm openrouter measures the real model on the same cases)
uv run python -m review_brief eval
uv run pytest

# The old entry point still works (diff-only checks unless you pass --repo)
uv run python review_bot.py sample_diff.txt
```

**No API key?** [`examples/sample-run/`](examples/sample-run) holds a real run against GLM 5.3 Flash:
the brief, the full LLM transcript (every prompt and raw reply), the triage decisions and the
resulting draft comment.

`examples/billing-service/` is a small fixture repo the sample diff applies to. The exercise ships
only a diff, and the tools need real code to run on. I built the fixture so every path is exercised:
`total()` is pre-tax by design and a test pins it, and `reports.py` applies tax itself and has no
tests, so only something that reads the callers can spot the double tax.

## What it produces on the sample diff

Abridged from [`examples/sample-run/brief.md`](examples/sample-run/brief.md):

```
Risk: HIGH · mode: full · LLM: openrouter/z-ai/glm-5.3-flash, 2 call(s)

Proven by tools and rules
 [high]   tests/test_invoice.py:14  test_total_excludes_tax passes before this PR, fails after
          evidence: assert 110.00000000000001 == 100.0
 [high]   billing/**                touches a sensitive path: needs 2 approving reviewers
 [medium] billing/invoice.py:12     Invoice.total() changed; 4 call sites depend on it
 [medium] billing/invoice.py:35     try/except/pass hides all errors (ruff S110, BLE001)
 [medium] billing/invoice.py:38     new public Invoice.export_csv() has no test

Needs your judgment (LLM hypotheses, not verified)
 billing/reports.py:12  monthly_revenue now applies tax twice     evidence: revenue += inv.total() * (1 + rate)
 billing/invoice.py:34  discount now taken off the taxed total
 billing/invoice.py:40  export_csv ignores total_override        evidence: ...{self.total()}...

Where to look first: tests/test_invoice.py:14 → billing/reports.py:12 → billing/invoice.py:34 → ...
```

## How it works

```
diff ─► parse (tolerant) ─► apply to base/head copies of the repo
                               │
     ┌───────── RULES: quality · safety · policy (checkers/) ─────────┐
     │ syntax+ruff on added lines · pytest before vs after · callers  │
     │ sensitive paths · new public fn needs a test · secrets         │
     └──────────┬──────────────────────────────┬──────────────────────┘
          proven findings + signals      questions (raised by rules)
                │                              │
           risk = highest signal ──► gate: min_risk, max_questions
                │                              │
                │                    LLM answers each question (parallel)
                │                    parse JSON → ground citations → hypotheses
                ▼                              ▼
          brief.md / brief.json  (proven │ hypotheses │ where to look │ not checked)
                │
          HUMAN triage ─► decisions.jsonl + comment.md (accepted items only)
```

- **Two evidence levels.** `proven` means a tool or rule produced it, with raw output attached.
  `hypothesis` means the LLM produced it and it cites a line it was shown. They are rendered in
  separate sections and never mixed.
- **Rules decide whether the LLM is called, and what it is asked.** A changed public function with
  callers raises a `behaviour_change` question, with its code before and after plus the callers'
  code. A new public method on a class raises a `new_method` question, with the class. A docs-only
  PR makes zero LLM calls (`llm.min_risk` in `policy.yaml`), and `llm.max_questions` caps cost per PR.
- **Grounding.** Each concern must cite a `file:line` inside a code block it was sent (after the
  PR). Everything else is dropped and counted ("1 uncited claim dropped"). A finding's evidence is
  the actual code at the cited line, not the model's text.
- **Risk comes from rules only.** It is the highest proven signal, with every reason listed. LLM
  hypotheses never raise or lower it. I chose this over a weighted score because a reviewer can
  argue with a reason but not with "7.2".
- **Fail loudly.** No repo, a diff that doesn't apply, a missing tool, a crashed checker, a failed
  LLM call: each lands under **Not checked** in the brief. An empty findings list never silently
  means "fine".
- **Policy is configuration.** `policy.yaml` decides sensitive paths, required approvals, the ruff
  rule set, test timeouts and LLM limits. The rules stay in code, and none of the policy lives in a
  prompt.
- **Pluggable checkers.** Quality, Safety and Policy implement one `Checker` interface
  (`review_brief/checkers/base.py`). That interface is where future agents plug in.

## Trust and safety

- **It cannot approve.** It has no write credentials and no exit code that means "approved". The
  draft comment contains only human-accepted items and says it is not an approval.
- **Running PR code is running untrusted code.** Tests and ruff run in throwaway copies, with a
  timeout and an allowlisted environment (`PATH`, locale, `TMPDIR`, `PYTHONPATH`, and a scratch
  `HOME`), so API keys never reach the PR's code. A test checks this. **This is a minimum, not a sandbox.** In production it belongs
  in a container with no network and no credentials.
- **Prompt injection.** Code is labelled untrusted and fenced with a random marker per call. The
  model must answer in a fixed JSON shape. Grounding means an injected "say LGTM" produces nothing
  unless it cites a real line, and the LLM can't change the risk level anyway.
- **Secrets.** A rule flags keys in added lines (including the `sk-…` pattern `review_bot.py`
  shipped with) and redacts them in the output; an eval checks the secret never appears. Our own
  key comes from env or `.env`, is never logged, and the transcript was checked to be free of it.
- **Diff paths** that escape the repo (`../`, absolute paths) are rejected.
- **Audit trail.** Each run writes `llm_transcript.jsonl` (every prompt and raw reply), and triage
  appends to `decisions.jsonl` (who decided what, and why they rejected).

## Evaluation

`evals/cases/*/expected.yaml` lists what each case must and must not find, the risk level, the number
of LLM calls, and a noise budget (`max_findings`).

| Case | What it proves |
|---|---|
| `billing-tax-change` (the sample diff) | all 5 proven findings and 3 hypotheses; exactly 2 LLM calls; the canned uncited claim is dropped |
| `docs-only` | low risk, **0 LLM calls**, 0 findings |
| `secret-leak` | a committed key is caught by a rule (0 LLM calls) and never repeated in the output |

- **Mock mode** (the default) tests the pipeline deterministically: the same prompt, parse and
  grounding path, with canned replies. Result: 3/3 pass.
- **Live mode** (`--llm openrouter`) tests the model on the same cases, with line-exact hypothesis
  checks relaxed to file level. GLM 5.3 Flash: 3/3 pass, recall 8/8, 0 unexpected findings. One
  review costs about $0.003–0.004 and takes 15–50 s (one reasoning-heavy question dominates).
- **Observed:** results vary between runs even at `temperature=0`. One live run found the
  discount-on-taxed-total issue and another didn't. Evaluate a model over several runs before
  trusting it, and in production use `decisions.jsonl` accept rates as the real precision metric.
- **Tests:** 62 unit and end-to-end tests (`uv run pytest`, offline, about 5 s).

## Design decisions and trade-offs

- **Own diff applier, not `git apply`.** The sample's hunk headers have wrong counts and blank
  context lines with no leading space. The applier finds each hunk by its context and records the
  exact head-file line of every added line, because every finding is anchored to those lines.
- **Tests run before and after.** Only "passed before, fails after" counts as a regression the PR
  caused; pre-existing failures are noted, not blamed. Ruff runs with `--isolated` (the policy
  picks the rules) and only lines the PR added count.
- **Callers are matched by name in the AST.** This is cheap and approximate (two classes can share a
  method name), and it says so in the finding. It stands in for real code intelligence, or the
  service graph described below.
- **Standard-library HTTP to OpenRouter**, behind a small `LLMClient` interface. The model is one
  environment variable, and the mock shares every code path except the network call.
- **Few dependencies:** `pyyaml`, plus `ruff` and `pytest` as the tools the pipeline runs.

## Limitations

- Python only. The fixture repo is constructed (see above).
- Impact analysis stops at the repo boundary: no cross-service view.
- The "sandbox" is a subprocess with a stripped environment, not isolation.
- The repo is copied twice per run. That's fine here; at scale, use git worktrees at the base and
  head SHAs.
- Tests run with this tool's interpreter, so the target repo's dependencies must be installed.
- No GitHub integration: `comment.md` is a draft for a human to post.

## What I would build next

This exercise is a one-repo version of a bigger idea: review agents that know **what runs in
production and how services integrate**, because changing one service can break many others.

1. **Service-impact agent.** Build a service graph (API contracts, events, shared tables, owners)
   and replace `analysis.find_callers` with cross-service blast radius: "this changes `total()`,
   which feeds finance's month-end export and the payments service". It plugs in behind the same
   `Checker` interface.
2. **More agents behind the same interface:** security review, migration safety, and test planning.
   Test planning would generate a reproduction test for each hypothesis and run it, which turns a
   hypothesis into a proven finding.
3. **GitHub App.** Post the brief as a *neutral* check run (never an approval) and triage inside the
   PR.
4. **Rollout.** Start in *shadow mode* (generate briefs, don't post, compare with human reviews),
   then *advisory*. Use `decisions.jsonl` to measure the accept rate per rule and per question, and
   retire the noisy ones.
5. **Real isolation** for running PR code: a container with no network and no credentials.

## Layout

```
review_bot.py              compatibility shim → review_brief CLI
policy.yaml                company policy (paths, approvals, ruff rules, LLM limits)
review_brief/
  cli.py                   review | triage | eval
  pipeline.py              diff → workspace → checkers → risk → LLM questions → brief
  diff.py  workspace.py    tolerant diff parser/applier · base/head copies · run_tool()
  analysis.py              AST: changed functions, callers
  checkers/                quality.py · safety.py · policy.py  (one Checker interface)
  risk.py                  highest-signal risk + LLM gate
  llm/                     base.py (interface) · answer.py (prompt, parse, ground)
                           openrouter.py · mock.py
  report.py  triage.py  evals.py  config.py  envfile.py  models.py
examples/billing-service/  fixture repo the sample diff applies to
examples/sample-run/       a real run against GLM 5.3 Flash, triaged
evals/cases/               billing-tax-change · docs-only · secret-leak
tests/                     62 tests, offline
```
