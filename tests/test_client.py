import httpx
import pytest

from fuel_prices_mcp import fuelfinder


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    fuelfinder._token_cache.update({"token": None, "expires_at": 0.0})
    monkeypatch.setenv("FUEL_FINDER_CLIENT_ID", "cid")
    monkeypatch.setenv("FUEL_FINDER_CLIENT_SECRET", "secret")


def make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def token_response():
    return httpx.Response(
        200,
        json={"success": True, "data": {"access_token": "tok", "expires_in": 3600}},
    )


def test_fetches_all_batches_until_empty():
    def handler(request):
        if "generate_access_token" in str(request.url):
            return token_response()
        assert request.headers["Authorization"] == "Bearer tok"
        batch = int(request.url.params["batch-number"])
        if batch <= 2:
            return httpx.Response(200, json=[{"node_id": f"b{batch}"}])
        return httpx.Response(200, json=[])

    records = fuelfinder.fetch_stations(client=make_client(handler))
    assert [r["node_id"] for r in records] == ["b1", "b2"]


def test_token_cached_across_fetches():
    calls = {"token": 0}

    def handler(request):
        if "generate_access_token" in str(request.url):
            calls["token"] += 1
            return token_response()
        return httpx.Response(200, json=[])

    client = make_client(handler)
    fuelfinder.fetch_stations(client=client)
    fuelfinder.fetch_prices(client=client)
    assert calls["token"] == 1


def test_bad_credentials_actionable():
    def handler(request):
        return httpx.Response(401, json={"success": False, "message": "Invalid"})

    with pytest.raises(fuelfinder.ApiError, match="Register"):
        fuelfinder.fetch_stations(client=make_client(handler))


def test_auth_lockout_message():
    def handler(request):
        return httpx.Response(429, json={"error": "Too many failed attempts"})

    with pytest.raises(fuelfinder.ApiError, match="wait 5 minutes"):
        fuelfinder.fetch_stations(client=make_client(handler))


def test_missing_credentials(monkeypatch):
    monkeypatch.delenv("FUEL_FINDER_CLIENT_ID")
    with pytest.raises(fuelfinder.ApiError, match="not configured"):
        fuelfinder.fetch_stations(client=make_client(lambda r: httpx.Response(500)))


def test_404_past_last_batch_ends_pagination():
    def handler(request):
        if "generate_access_token" in str(request.url):
            return token_response()
        batch = int(request.url.params["batch-number"])
        if batch <= 3:
            return httpx.Response(200, json=[{"node_id": f"b{batch}"}])
        return httpx.Response(404)

    records = fuelfinder.fetch_stations(client=make_client(handler))
    assert len(records) == 3


def test_404_on_first_batch_is_an_error():
    def handler(request):
        if "generate_access_token" in str(request.url):
            return token_response()
        return httpx.Response(404)

    with pytest.raises(fuelfinder.ApiError, match="404"):
        fuelfinder.fetch_stations(client=make_client(handler))
