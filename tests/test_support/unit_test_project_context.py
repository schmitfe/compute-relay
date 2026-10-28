import pytest

@pytest.fixture
def context_live_server(settings, request):
    settings.STATIC_URL = "/static/"
    return request.getfixturevalue("live_server")
