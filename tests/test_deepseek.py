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
