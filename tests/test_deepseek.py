from ghcr.deepseek import build_request_kwargs

_FORBIDDEN = ("temperature", "top_p", "presence_penalty", "frequency_penalty")


def test_thinking_enabled_omits_sampling_params():
    kw = build_request_kwargs("deepseek-v4-pro", "enabled", "high", "sys", "user")
    for k in _FORBIDDEN:
        assert k not in kw
    assert kw["reasoning_effort"] == "high"
    assert kw["extra_body"] == {"thinking": {"type": "enabled"}}
    assert kw["model"] == "deepseek-v4-pro"
    assert kw["messages"][0]["role"] == "system"
    assert kw["messages"][1]["content"] == "user"


def test_thinking_disabled_has_no_reasoning_effort():
    kw = build_request_kwargs("deepseek-v4-pro", "disabled", "high", "sys", "user")
    assert "reasoning_effort" not in kw
    assert kw["extra_body"] == {"thinking": {"type": "disabled"}}
    for k in _FORBIDDEN:
        assert k not in kw


def test_send_thinking_extra_body_false_drops_thinking():
    # GLM / generic OpenAI-compatible advisor: no DeepSeek-specific extra_body.
    kw = build_request_kwargs(
        "glm-5.2", "disabled", "high", "sys", "user", send_thinking_extra_body=False
    )
    assert "extra_body" not in kw
    assert "reasoning_effort" not in kw
    assert set(kw) == {"model", "messages"}
    assert kw["model"] == "glm-5.2"
