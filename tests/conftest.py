import pytest


@pytest.fixture(autouse=True)
def isolated_landing_home(monkeypatch):
    """Keep a developer's real landing state out of the test run.

    `StateStore` honours LANDING_HOME/AGENT_LANDING_HOME. Anyone who actually uses
    Agent Landing has one exported, which would otherwise redirect every store in
    the suite -- including two that are supposed to be independent -- into one
    directory, and would write test junk into their live `.landing/`.
    """
    monkeypatch.delenv("LANDING_HOME", raising=False)
    monkeypatch.delenv("AGENT_LANDING_HOME", raising=False)
