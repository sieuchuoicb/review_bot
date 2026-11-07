import io
import json
import urllib.error

import pytest

from review_brief import pipeline
from review_brief.envfile import load_env
from review_brief.llm.answer import AnswerFormatError, answer_question, build_prompt, ground, parse_reply
from review_brief.llm.base import LLMConfigError, LLMError
from review_brief.llm.mock import MockLLM
from review_brief.llm.openrouter import OpenRouterClient
from review_brief.models import ContextBlock, Question
from tests.conftest import DOCS_ONLY_DIFF, FIXTURE_REPO, ROOT, SAMPLE_DIFF

ANSWERS = ROOT / "evals/cases/billing-tax-change/llm_answers.json"


def question() -> Question:
    return Question(
        id="behaviour_change:billing/invoice.py:Invoice.total",
        kind="behaviour_change",
        target="Invoice.total",
        file="billing/invoice.py",
        line=12,
        trigger="Invoice.total() changed",
        ask="Does it break callers?",
        context=[
            ContextBlock("before: Invoice.total", "billing/invoice.py", 12, "def total(self):\n    return 1",
                         version="base"),
            ContextBlock("caller: monthly_revenue", "billing/reports.py", 10, "a\nb\nrevenue += inv.total()"),
        ],
    )


def concern(file, line, claim="x is wrong"):
    return {"claim": claim, "file": file, "line": line, "why": "because", "confidence": "high"}


# --- prompt and parsing -------------------------------------------------------


def test_prompt_marks_code_as_untrusted_with_a_random_fence_and_line_numbers():
    system, user = build_prompt(question(), ["tests/x.py:3 Test regression"], max_concerns=3)
    assert "untrusted" in system and "At most 3 concerns" in system
    marker = user.split("<<<", 1)[1].split(" ", 1)[0]
    assert marker.startswith("CODE-")
    assert user.count(f"<<<{marker} block") == 2 and user.count(f"\n{marker}>>>") == 2
    assert "   12 | def total(self):" in user
    assert "| before the PR |" in user and "| after the PR |" in user
    assert "- tests/x.py:3 Test regression" in user
    _, user2 = build_prompt(question(), [], max_concerns=3)
    assert marker not in user2  # fresh fence per call; PR code cannot guess it


@pytest.mark.parametrize(
    "reply",
    [
        '{"concerns": []}',
        '```json\n{"concerns": []}\n```',
        'Here you go:\n{"concerns": [], "no_concern_reason": "fine"}\nThanks!',
    ],
)
def test_parse_reply_tolerates_fences_and_chatter(reply):
    assert parse_reply(reply)["concerns"] == []


@pytest.mark.parametrize("reply", ["no json here", "{not json}", '{"concerns": "nope"}'])
def test_parse_reply_rejects_unusable_replies(reply):
    with pytest.raises(AnswerFormatError):
        parse_reply(reply)


# --- grounding ------------------------------------------------------------------


def test_grounding_keeps_only_claims_that_cite_a_line_we_showed():
    data = {
        "concerns": [
            concern("billing/reports.py", 12, "double tax"),  # inside the caller block: kept
            concern("./billing/reports.py", 11),  # path variant: kept
            concern("billing/reports.py", 99),  # outside the block: dropped
            concern("payments/charge.py", 4),  # never shown: dropped
            concern("billing/invoice.py", 12),  # only in the *before* block: dropped
            {"file": "billing/reports.py", "line": 12},  # no claim: dropped
        ]
    }
    findings, dropped = ground(question(), data, source="llm:test/m", max_concerns=5)
    assert [(f.location, f.evidence_level) for f in findings] == [
        ("billing/reports.py:12", "hypothesis"),
        ("billing/reports.py:11", "hypothesis"),
    ]
    assert findings[0].evidence == "revenue += inv.total()"  # the real cited code, not model text
    assert dropped == 4


def test_grounding_caps_concerns_per_question():
    data = {"concerns": [concern("billing/reports.py", 10 + i % 3) for i in range(5)]}
    findings, dropped = ground(question(), data, source="s", max_concerns=2)
    assert len(findings) == 2 and dropped == 3


def test_llm_failure_fails_the_question_not_the_review():
    q = question()
    outcome = answer_question(MockLLM({q.id: {"__error__": "HTTP 503"}}), q, [], 3)
    assert outcome.status == "failed" and "HTTP 503" in outcome.note
    outcome = answer_question(MockLLM({q.id: "I think it is fine"}), q, [], 3)
    assert outcome.status == "failed" and "unusable reply" in outcome.note


# --- pipeline with an LLM ---------------------------------------------------------


def test_pipeline_adds_grounded_hypotheses_but_risk_comes_from_rules_only(policy):
    llm = MockLLM.from_file(ANSWERS)
    audit: list = []
    brief = pipeline.review(
        SAMPLE_DIFF.read_text(), diff_source="d", repo=FIXTURE_REPO, policy=policy, llm=llm, audit=audit
    )
    hyps = {f.location for f in brief.hypotheses}
    assert hyps == {"billing/reports.py:12", "billing/invoice.py:34", "billing/invoice.py:40"}
    assert brief.llm["calls"] == 2 and brief.llm["dropped_claims"] == 1
    assert [q.status for q in brief.questions] == ["answered", "answered"]
    assert len(audit) == 2 and "reply" in audit[0]
    # The LLM saw what the tools proved, so it is not asked to repeat it.
    assert "Test regression" in llm.calls[0]["user"]


def test_low_risk_change_makes_no_llm_call(policy):
    llm = MockLLM()
    brief = pipeline.review(DOCS_ONLY_DIFF, diff_source="d", repo=FIXTURE_REPO, policy=policy, llm=llm)
    assert llm.calls == [] and brief.llm["calls"] == 0


# --- OpenRouter client (no network: urlopen is faked) -------------------------------


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def ok_body(content='{"concerns": []}'):
    return FakeResponse(
        json.dumps(
            {"model": "z-ai/test", "choices": [{"message": {"content": content}}],
             "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        ).encode()
    )


def http_error(code, body=b'{"error": "nope"}'):
    return urllib.error.HTTPError("https://openrouter.ai", code, "err", {}, io.BytesIO(body))


def client_with(responses, sleeps=None):
    calls = []

    def urlopen(request, timeout):
        calls.append(request)
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client = OpenRouterClient("sk-or-secret", "z-ai/test", urlopen=urlopen,
                              sleep=(sleeps.append if sleeps is not None else lambda s: None))
    return client, calls


def test_openrouter_sends_a_deterministic_json_request():
    client, calls = client_with([ok_body()])
    resp = client.complete("sys", "user", question_id="q1")
    assert resp.text == '{"concerns": []}' and resp.usage["prompt_tokens"] == 10
    body = json.loads(calls[0].data)
    assert body["model"] == "z-ai/test" and body["temperature"] == 0
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert calls[0].get_header("Authorization") == "Bearer sk-or-secret"


def test_openrouter_retries_rate_limits_and_server_errors():
    sleeps: list = []
    client, calls = client_with([http_error(429), http_error(502), ok_body()], sleeps)
    assert client.complete("s", "u", question_id="q").text
    assert len(calls) == 3 and sleeps == [1.5, 3.0]


def test_openrouter_does_not_retry_auth_errors_and_never_leaks_the_key():
    client, calls = client_with([http_error(401, b'{"error": "bad key"}')])
    with pytest.raises(LLMError) as exc:
        client.complete("s", "u", question_id="q")
    assert len(calls) == 1 and "401" in str(exc.value) and "sk-or-secret" not in str(exc.value)


def test_openrouter_gives_up_after_retries():
    client, _ = client_with([http_error(500)] * 3)
    with pytest.raises(LLMError, match="after 3 attempt"):
        client.complete("s", "u", question_id="q")


def test_openrouter_config_comes_from_env():
    with pytest.raises(LLMConfigError, match="OPEN_ROUTER_API_KEY and LLM_MODEL_NAME"):
        OpenRouterClient.from_env({})
    client = OpenRouterClient.from_env({"OPENROUTER_API_KEY": "k", "LLM_MODEL_NAME": "m"})
    assert client.model == "m"


def test_env_file_loads_without_overriding_the_environment(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        '# comment\nexport A="quoted"\nB=plain # trailing comment\nC=from-file\nbroken line\n'
    )
    environ = {"C": "from-env"}
    assert load_env(env_file, environ) == ["A", "B"]
    assert environ == {"A": "quoted", "B": "plain", "C": "from-env"}
