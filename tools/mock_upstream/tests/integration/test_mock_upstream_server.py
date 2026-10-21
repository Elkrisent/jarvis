"""Behaviours that need a real socket: connection drop, hang, streaming, redirects."""

import asyncio
import socket
from collections.abc import AsyncIterator

import httpx
import pytest
import uvicorn
from mock_upstream.app import create_app

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def base_url() -> AsyncIterator[str]:
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(seed=1),
            host="127.0.0.1",
            port=port,
            log_level="critical",
            timeout_graceful_shutdown=1,
        )
    )
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "mock upstream did not start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@pytest.fixture
async def client(base_url: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=base_url, timeout=5) as c:
        yield c


async def test_echo_over_a_real_connection(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/current", params={"city": "Bangalore"})
    assert r.status_code == 200
    assert r.json()["query"] == [["city", "Bangalore"]]


async def test_connection_reset_surfaces_as_an_incomplete_response(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/__admin/rules", json={"reset": True})
    with pytest.raises(httpx.RemoteProtocolError):
        await client.get("/x")
    # the call was still recorded, and the server keeps serving afterwards
    assert (await client.get("/__admin/calls")).json()["count"] == 1
    await client.post("/__admin/rules", json=[])
    assert (await client.get("/y")).status_code == 200


async def test_hang_triggers_the_clients_read_timeout(client: httpx.AsyncClient) -> None:
    await client.post("/__admin/rules", json={"hang": True})
    with pytest.raises(httpx.ReadTimeout):
        await client.get("/x", timeout=httpx.Timeout(5, read=0.3))
    # a hung connection does not block other requests
    await client.post("/__admin/rules", json=[])
    assert (await client.get("/y")).status_code == 200


async def test_hung_calls_are_recorded(client: httpx.AsyncClient) -> None:
    await client.post("/__admin/rules", json={"hang": True})
    with pytest.raises(httpx.ReadTimeout):
        await client.get("/x", timeout=httpx.Timeout(5, read=0.3))
    assert (await client.get("/__admin/calls")).json()["count"] == 1


async def test_oversized_body_streams_in_full(client: httpx.AsyncClient) -> None:
    size = 20 * 1024 * 1024
    await client.post("/__admin/rules", json={"body_bytes": size})
    total = 0
    async with client.stream("GET", "/big") as r:
        assert int(r.headers["content-length"]) == size
        async for chunk in r.aiter_bytes():
            total += len(chunk)
    assert total == size


async def test_redirects_are_not_followed_unless_asked(client: httpx.AsyncClient) -> None:
    await client.post("/__admin/rules", json={"path_prefix": "/old", "redirect_to": "/new"})
    r = await client.get("/old")
    assert (r.status_code, r.headers["location"]) == (302, "/new")
    followed = await client.get("/old", follow_redirects=True)
    assert followed.status_code == 200
    assert followed.json()["path"] == "/new"


async def test_real_latency(client: httpx.AsyncClient) -> None:
    await client.post("/__admin/rules", json={"latency_ms": 150})
    loop = asyncio.get_running_loop()
    start = loop.time()
    await client.get("/x")
    assert loop.time() - start >= 0.14


async def test_set_cookie_and_retry_after_on_the_wire(client: httpx.AsyncClient) -> None:
    await client.post(
        "/__admin/rules", json={"status": 429, "retry_after_s": 3, "set_cookie": "a=b"}
    )
    r = await client.get("/x")
    assert (r.status_code, r.headers["retry-after"], r.headers["set-cookie"]) == (429, "3", "a=b")


async def test_concurrent_requests_are_all_recorded(client: httpx.AsyncClient) -> None:
    await asyncio.gather(*(client.get(f"/c/{i}") for i in range(50)))
    data = (await client.get("/__admin/calls")).json()
    assert data["count"] == 50
    assert sorted(c["path"] for c in data["calls"]) == sorted(f"/c/{i}" for i in range(50))
