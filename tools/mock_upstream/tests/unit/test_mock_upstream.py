import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from mock_upstream.app import Rule, create_app

pytestmark = pytest.mark.unit


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def sleeper() -> FakeSleep:
    return FakeSleep()


@pytest.fixture
async def client(sleeper: FakeSleep) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(seed=123, sleep=sleeper)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as c:
        yield c


async def _rules(client: httpx.AsyncClient, *rules: dict[str, object]) -> None:
    r = await client.post("/__admin/rules", json=list(rules))
    assert r.status_code == 200


# ----------------------------------------------------------------------- default echo


async def test_default_is_200_echo_of_method_path_query_headers(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/things?a=1&a=2&b=x", content=b"hello", headers={"X-Test": "yes"})
    assert r.status_code == 200
    body = r.json()
    assert body["method"] == "POST"
    assert body["path"] == "/v1/things"
    assert body["query"] == [["a", "1"], ["a", "2"], ["b", "x"]]
    assert body["headers"]["x-test"] == "yes"
    assert body["body_size"] == 5


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def test_every_method_is_served(client: httpx.AsyncClient, method: str) -> None:
    assert (await client.request(method, "/any/path")).status_code == 200


async def test_head_is_served(client: httpx.AsyncClient) -> None:
    r = await client.head("/x")
    assert r.status_code == 200
    assert r.content == b""


async def test_root_path_is_served(client: httpx.AsyncClient) -> None:
    assert (await client.get("/")).json()["path"] == "/"


# ------------------------------------------------------------------------ admin: rules


async def test_rules_can_be_set_as_one_object_or_a_list_and_read_back(
    client: httpx.AsyncClient,
) -> None:
    assert (await client.post("/__admin/rules", json={"status": 201})).json() == {"rules": 1}
    assert (
        await client.post("/__admin/rules", json=[{"status": 202}, {"status": 203}])
    ).json() == {"rules": 2}
    assert [r["status"] for r in (await client.get("/__admin/rules")).json()] == [202, 203]


async def test_setting_rules_replaces_the_previous_set(client: httpx.AsyncClient) -> None:
    await _rules(client, {"status": 418})
    await _rules(client, {"status": 200})
    assert (await client.get("/x")).status_code == 200


@pytest.mark.parametrize(
    "bad",
    [
        {"status": 99},
        {"status": 600},
        {"surprise": 1},
        {"latency_ms": -1},
        {"latency_range_ms": [10, 5]},
        {"latency_range_ms": [-1, 5]},
        {"errors": [{"status": 200, "rate": 0.5}]},
        {"errors": [{"status": 500, "rate": 1.5}]},
        {"errors": [{"status": 500, "rate": 0.6}, {"status": 503, "rate": 0.6}]},
        {"body_bytes": -1},
    ],
)
async def test_invalid_rules_are_rejected(
    client: httpx.AsyncClient, bad: dict[str, object]
) -> None:
    assert (await client.post("/__admin/rules", json=bad)).status_code == 422


async def test_first_matching_rule_wins(client: httpx.AsyncClient) -> None:
    await _rules(
        client,
        {"path_prefix": "/a/b", "status": 201},
        {"path_prefix": "/a", "status": 202},
        {"status": 203},
    )
    assert (await client.get("/a/b/c")).status_code == 201
    assert (await client.get("/a/x")).status_code == 202
    assert (await client.get("/z")).status_code == 203


async def test_rule_can_be_limited_by_method(client: httpx.AsyncClient) -> None:
    await _rules(client, {"methods": ["post", "PUT"], "status": 409})
    assert (await client.post("/x")).status_code == 409
    assert (await client.put("/x")).status_code == 409
    assert (await client.get("/x")).status_code == 200


async def test_unmatched_requests_fall_back_to_the_default(client: httpx.AsyncClient) -> None:
    await _rules(client, {"path_prefix": "/only", "status": 500})
    assert (await client.get("/other")).status_code == 200


# ----------------------------------------------------------------------- status and errors


async def test_fixed_status(client: httpx.AsyncClient) -> None:
    await _rules(client, {"status": 404})
    assert (await client.get("/x")).status_code == 404


async def test_error_rate_of_one_always_fails_with_that_status(client: httpx.AsyncClient) -> None:
    await _rules(client, {"errors": [{"status": 503, "rate": 1.0}]})
    for _ in range(5):
        r = await client.get("/x")
        assert r.status_code == 503
        assert r.json() == {"error": "injected", "status": 503}


async def test_error_rate_of_zero_never_fails(client: httpx.AsyncClient) -> None:
    await _rules(client, {"errors": [{"status": 503, "rate": 0.0}]})
    for _ in range(20):
        assert (await client.get("/x")).status_code == 200


async def test_error_rate_is_roughly_the_requested_fraction(client: httpx.AsyncClient) -> None:
    await _rules(client, {"errors": [{"status": 500, "rate": 0.3}, {"status": 503, "rate": 0.2}]})
    statuses = [(await client.get("/x")).status_code for _ in range(1000)]
    assert 230 < statuses.count(500) < 370
    assert 130 < statuses.count(503) < 270
    assert set(statuses) <= {200, 500, 503}


@pytest.mark.parametrize("status", [429, 503])
async def test_retry_after_header(client: httpx.AsyncClient, status: int) -> None:
    await _rules(client, {"status": status, "retry_after_s": 7})
    r = await client.get("/x")
    assert r.status_code == status
    assert r.headers["retry-after"] == "7"


# ------------------------------------------------------------------------------- headers


async def test_set_cookie_cache_control_and_custom_headers(client: httpx.AsyncClient) -> None:
    await _rules(
        client,
        {"set_cookie": "sid=1; Path=/", "cache_control": "max-age=30", "headers": {"X-A": "b"}},
    )
    r = await client.get("/x")
    assert r.headers["set-cookie"] == "sid=1; Path=/"
    assert r.headers["cache-control"] == "max-age=30"
    assert r.headers["x-a"] == "b"


async def test_no_extra_headers_by_default(client: httpx.AsyncClient) -> None:
    r = await client.get("/x")
    assert "set-cookie" not in r.headers
    assert "cache-control" not in r.headers
    assert "retry-after" not in r.headers


# ----------------------------------------------------------------------------- redirects


async def test_redirect_defaults_to_302(client: httpx.AsyncClient) -> None:
    await _rules(client, {"redirect_to": "http://169.254.169.254/latest"})
    r = await client.get("/x")
    assert r.status_code == 302
    assert r.headers["location"] == "http://169.254.169.254/latest"


@pytest.mark.parametrize("status", [301, 307, 308])
async def test_redirect_keeps_a_3xx_status(client: httpx.AsyncClient, status: int) -> None:
    await _rules(client, {"redirect_to": "/elsewhere", "status": status})
    assert (await client.get("/x")).status_code == status


# ------------------------------------------------------------------------------ body size


@pytest.mark.parametrize("size", [0, 1, 65_535, 65_536, 65_537, 200_000])
async def test_oversized_body_has_exact_size(client: httpx.AsyncClient, size: int) -> None:
    await _rules(client, {"body_bytes": size})
    r = await client.get("/x")
    assert len(r.content) == size
    assert r.headers["content-length"] == str(size)


# ------------------------------------------------------------------------------- latency


async def test_fixed_latency(client: httpx.AsyncClient, sleeper: FakeSleep) -> None:
    await _rules(client, {"latency_ms": 250})
    await client.get("/x")
    assert sleeper.calls == [0.25]


async def test_no_latency_means_no_sleep(client: httpx.AsyncClient, sleeper: FakeSleep) -> None:
    await client.get("/x")
    assert sleeper.calls == []


async def test_random_latency_stays_in_range(client: httpx.AsyncClient, sleeper: FakeSleep) -> None:
    await _rules(client, {"latency_range_ms": [40, 60]})
    for _ in range(50):
        await client.get("/x")
    assert len(sleeper.calls) == 50
    assert all(0.040 <= s <= 0.060 for s in sleeper.calls)
    assert len(set(sleeper.calls)) > 1


async def test_hang_never_answers_but_the_call_is_recorded(client: httpx.AsyncClient) -> None:
    await _rules(client, {"hang": True})
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(client.get("/stuck"), timeout=0.2)
    assert (await client.get("/__admin/calls")).json()["count"] == 1


# ---------------------------------------------------------------------------------- calls


async def test_calls_record_method_path_query_headers_in_order(client: httpx.AsyncClient) -> None:
    await client.get("/a?x=1", headers={"Idempotency-Key": "k1"})
    await client.post("/b", content=b"1234")
    data = (await client.get("/__admin/calls")).json()
    assert data["count"] == 2
    first, second = data["calls"]
    assert (first["index"], first["method"], first["path"]) == (0, "GET", "/a")
    assert first["query"] == [["x", "1"]]
    assert first["headers"]["idempotency-key"] == "k1"
    assert (second["index"], second["method"], second["body_size"]) == (1, "POST", 4)


async def test_calls_record_which_rule_matched(client: httpx.AsyncClient) -> None:
    await _rules(client, {"path_prefix": "/hit", "status": 200})
    await client.get("/hit")
    await client.get("/miss")
    calls = (await client.get("/__admin/calls")).json()["calls"]
    assert [c["rule"] for c in calls] == [0, None]


async def test_calls_can_be_filtered_by_path_prefix(client: httpx.AsyncClient) -> None:
    for path in ("/a/1", "/a/2", "/b/1"):
        await client.get(path)
    assert (await client.get("/__admin/calls", params={"path_prefix": "/a"})).json()["count"] == 2


async def test_admin_requests_are_not_recorded(client: httpx.AsyncClient) -> None:
    await client.get("/__admin/rules")
    await client.get("/__admin/health")
    assert (await client.get("/__admin/calls")).json() == {"count": 0, "calls": []}


async def test_duplicate_request_headers_are_joined(client: httpx.AsyncClient) -> None:
    await client.get("/x", headers=[("X-Multi", "a"), ("X-Multi", "b")])
    call = (await client.get("/__admin/calls")).json()["calls"][0]
    assert call["headers"]["x-multi"] == "a, b"


# --------------------------------------------------------------------- reset and seeding


async def test_reset_clears_rules_and_calls(client: httpx.AsyncClient) -> None:
    await _rules(client, {"status": 500})
    await client.get("/x")
    assert (await client.post("/__admin/reset")).json() == {"status": "reset"}
    assert (await client.get("/__admin/rules")).json() == []
    assert (await client.get("/__admin/calls")).json()["count"] == 0
    assert (await client.get("/x")).status_code == 200


async def _outcomes(client: httpx.AsyncClient, seed: int | None = None) -> list[int]:
    await client.post("/__admin/reset", params={"seed": seed} if seed is not None else {})
    await _rules(client, {"errors": [{"status": 503, "rate": 0.5}]})
    return [(await client.get("/x")).status_code for _ in range(40)]


async def test_same_seed_gives_the_same_sequence_after_every_reset(
    client: httpx.AsyncClient,
) -> None:
    first = await _outcomes(client)
    assert first == await _outcomes(client)
    assert {200, 503} <= set(first)


async def test_different_seeds_give_different_sequences(client: httpx.AsyncClient) -> None:
    assert await _outcomes(client, seed=1) != await _outcomes(client, seed=2)


async def test_reset_with_a_seed_sticks_for_later_resets(client: httpx.AsyncClient) -> None:
    first = await _outcomes(client, seed=9)
    assert first == await _outcomes(client)


async def test_two_apps_with_the_same_seed_agree() -> None:
    results = []
    for _ in range(2):
        app = create_app(seed=77)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://m"
        ) as c:
            await c.post("/__admin/rules", json={"errors": [{"status": 500, "rate": 0.5}]})
            results.append([(await c.get("/x")).status_code for _ in range(30)])
    assert results[0] == results[1]


# --------------------------------------------------------------------------- misc


def test_rule_matches_uppercases_methods() -> None:
    assert Rule(methods=["get"]).matches("GET", "/x")
    assert not Rule(methods=["get"]).matches("POST", "/x")


async def test_health(client: httpx.AsyncClient) -> None:
    assert (await client.get("/__admin/health")).json() == {"status": "ok"}


def test_app_from_env_reads_the_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    from mock_upstream.app import create_app_from_env

    monkeypatch.setenv("MOCK_SEED", "5")
    assert create_app_from_env().state.mock.initial_seed == 5
    monkeypatch.delenv("MOCK_SEED")
    assert create_app_from_env().state.mock.initial_seed is None
