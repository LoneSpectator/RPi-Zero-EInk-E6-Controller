import pytest


@pytest.fixture(autouse=True)
def isolate_startup_network(monkeypatch):
    # Existing API/queue tests start with an empty local library. Bing startup
    # scenarios explicitly enable the feature and inject their download source.
    monkeypatch.setenv('BING_DEFAULT_WALLPAPER', 'false')
