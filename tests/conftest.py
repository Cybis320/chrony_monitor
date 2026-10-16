import pytest

from chrony_monitor import tempcomp


@pytest.fixture(autouse=True)
def _host_chronyd_unknown(monkeypatch):
    """Keep record() from settling on the host's chronyd having just restarted."""
    monkeypatch.setattr(tempcomp, "chronyd_age", lambda: None)
