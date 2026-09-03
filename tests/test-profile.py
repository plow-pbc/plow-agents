import argparse
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "bin/plow-agents"
spec = importlib.util.spec_from_loader("plow_agents", SourceFileLoader("plow_agents", str(SOURCE)))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_profile_sends_name_and_photo(monkeypatch, capsys):
    seen = {}

    def fake_call(method, base, path, **kwargs):
        seen.update(method=method, path=path, **kwargs)
        return kwargs["body"]

    monkeypatch.setattr(module, "account_token", lambda _args: "account-token")
    monkeypatch.setattr(module, "call", fake_call)
    module.profile(argparse.Namespace(name="Ada", photo="https://example.com/ada.jpg", show=False, api_base="https://api.plow.co"))

    assert seen["method"] == "PATCH"
    assert seen["path"] == "/v1/auth/profile"
    assert seen["body"] == {"display_name": "Ada", "photo_url": "https://example.com/ada.jpg"}
    assert '"display_name": "Ada"' in capsys.readouterr().out
