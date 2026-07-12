import dataclasses

import ghcr.wizard
from ghcr.cli import _build, main
from ghcr.config import AdvisorConfig
from ghcr.cost import Prices
from ghcr.deepseek import DeepSeekClient
from tests.fakes import make_config


def test_advisor_provider_openai_builds_distinct_deepseek_client(tmp_path):
    advisor_cfg = AdvisorConfig(
        api_key="z", base_url="https://api.z.ai/api/paas/v4", model="glm-5.2",
        request_timeout_seconds=600, prices=Prices(0.0, 0.0), send_thinking_extra_body=False,
    )
    cfg = dataclasses.replace(
        make_config(db_path=str(tmp_path / "s.db"), advisor_provider="openai"),
        advisor=advisor_cfg,
    )
    _gh, ds, _store, orch = _build(cfg)
    assert isinstance(orch.advisor, DeepSeekClient)
    assert orch.advisor is not ds  # distinct from the worker → tokens report separately
    assert orch.advisor.model == "glm-5.2"
    assert orch.advisor.send_thinking_extra_body is False
    assert orch.advisor.prices == Prices(0.0, 0.0)


def test_init_dispatches_to_wizard_without_loading_config(tmp_path, monkeypatch):
    calls = {}

    def fake_wizard(path, env=None, **kw):
        calls["path"] = path
        return 0

    monkeypatch.setattr(ghcr.wizard, "run_wizard", fake_wizard)

    missing = tmp_path / "does-not-exist.yaml"
    rc = main(["--config", str(missing), "init"])

    assert rc == 0
    assert calls["path"] == str(missing)  # ran the wizard; never tried to load config
