"""DeepSeek chat-completion client (thinking-aware), OpenAI-compatible.

When thinking is enabled the API forbids ``temperature``/``top_p``/penalty
params — so request kwargs are built conditionally. ``build_request_kwargs`` is
a module function (no SDK import) so the param rules can be unit-tested without
network or the ``openai`` package.
"""

from __future__ import annotations

from .models import ReviewResult, Usage


class DeepSeekError(Exception):
    pass


def build_request_kwargs(
    model: str,
    thinking: str,
    reasoning_effort: str,
    system_prompt: str,
    user_prompt: str,
) -> dict:
    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if thinking == "enabled":
        # Sampling params are intentionally omitted — unsupported with thinking.
        kwargs["reasoning_effort"] = reasoning_effort
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
    else:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    return kwargs


class DeepSeekClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        thinking: str = "enabled",
        reasoning_effort: str = "high",
        timeout: int = 600,
    ):
        from openai import OpenAI  # lazy: keeps pure modules importable without the SDK

        self.model = model
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    def review(self, system_prompt: str, user_prompt: str, *, thinking: str | None = None) -> ReviewResult:
        """One chat-completion call. ``thinking`` overrides the client default for
        this call only — the planner pass runs "disabled" (symbol listing needs no
        deep reasoning, and reasoning tokens bill as output)."""
        kwargs = build_request_kwargs(
            self.model, thinking or self.thinking, self.reasoning_effort, system_prompt, user_prompt
        )
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as e:  # network / API errors normalized for the orchestrator
            raise DeepSeekError(str(e)) from e

        msg = resp.choices[0].message
        content = (getattr(msg, "content", None) or "").strip()
        if not content:
            raise DeepSeekError("model returned empty content")

        u = resp.usage
        usage = Usage(
            prompt_tokens=int(getattr(u, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(u, "completion_tokens", 0) or 0),
            total_tokens=int(getattr(u, "total_tokens", 0) or 0),
        )
        return ReviewResult(content=content, usage=usage, model=self.model)
