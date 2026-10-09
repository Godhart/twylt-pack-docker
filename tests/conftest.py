import pytest
@pytest.fixture(autouse=True)
def shared_policy(monkeypatch):
    monkeypatch.setenv("TWYLT_GUARDRAILS", "1")
    monkeypatch.setenv("TWYLT_DISABLE_NETWORK", "0")
