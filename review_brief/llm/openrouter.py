"""OpenRouter client (OpenAI-compatible chat completions), stdlib only.

Config from the environment (or .env):
  OPEN_ROUTER_API_KEY   (OPENROUTER_API_KEY also accepted)
  LLM_MODEL_NAME        OpenRouter model slug

Retries 429s, 5xx errors and network errors with backoff. Any other error
fails this one question, not the whole review. The API key never appears in
errors or in the audit trail.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from review_brief.llm.base import LLMConfigError, LLMError, LLMResponse

URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterClient:
    provider = "openrouter"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout: float = 60.0,
        retries: int = 2,
        backoff: float = 1.5,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._api_key = api_key
        self.model = model
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self._urlopen = urlopen
        self._sleep = sleep

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> OpenRouterClient:
        key = environ.get("OPEN_ROUTER_API_KEY") or environ.get("OPENROUTER_API_KEY")
        model = environ.get("LLM_MODEL_NAME")
        required = (("OPEN_ROUTER_API_KEY", key), ("LLM_MODEL_NAME", model))
        missing = [name for name, value in required if not value]
        if missing:
            raise LLMConfigError(f"set {' and '.join(missing)} in .env or the environment")
        return cls(key, model)  # type: ignore[arg-type]

    def complete(self, system: str, user: str, *, question_id: str) -> LLMResponse:
        body = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            }
        ).encode()
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-Title": "review-brief",
        }
        last_error = ""
        for attempt in range(self.retries + 1):
            if attempt:
                self._sleep(self.backoff * 2 ** (attempt - 1))
            request = urllib.request.Request(URL, data=body, headers=headers, method="POST")
            try:
                with self._urlopen(request, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
                last_error = f"HTTP {exc.code}: {detail}"
                if exc.code == 429 or exc.code >= 500:
                    continue
                raise LLMError(f"OpenRouter {last_error}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = f"network error: {getattr(exc, 'reason', exc)}"
                continue
            except json.JSONDecodeError as exc:
                raise LLMError("OpenRouter returned a non-JSON response") from exc

            if isinstance(data, dict) and data.get("error"):
                err = data["error"]
                code = err.get("code") if isinstance(err, dict) else None
                last_error = f"API error {code}: {err.get('message') if isinstance(err, dict) else err}"
                if code == 429 or (isinstance(code, int) and code >= 500):
                    continue
                raise LLMError(f"OpenRouter {last_error}")
            try:
                text = data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError) as exc:
                raise LLMError("OpenRouter response had no choices[0].message.content") from exc
            return LLMResponse(text=text, model=data.get("model", self.model), usage=data.get("usage") or {},
                               raw=data)
        raise LLMError(f"OpenRouter failed after {self.retries + 1} attempt(s): {last_error}")
