import ghcr.wizard
from ghcr.cli import main


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
