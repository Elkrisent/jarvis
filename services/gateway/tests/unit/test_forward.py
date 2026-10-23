from collections.abc import Sequence

import pytest
from jarvis_core.config import Credential
from jarvis_gateway.forward import (
    build_target_url,
    filter_request_headers,
    filter_response_headers,
    inject_credential_header,
    wants_stream,
)

pytestmark = pytest.mark.unit

Headers = Sequence[tuple[str, str]]
NONE = Credential()
BEARER = Credential(mode="bearer", secret_ref="sec_1")
CUSTOM = Credential(mode="header", header_name="X-Api-Key", secret_ref="sec_1")
QUERY = Credential(mode="query", param_name="appid", secret_ref="sec_1")
SECRET = "S3cr3t value/+="


def _names(headers: Headers) -> list[str]:
    return [n for n, _ in headers]


# --- request headers --------------------------------------------------------------------------


def test_hop_by_hop_and_per_connection_headers_are_removed() -> None:
    headers = [
        ("Accept", "*/*"),
        ("Connection", "X-Foo, Keep-Alive"),
        ("X-Foo", "1"),
        ("Keep-Alive", "timeout=5"),
        ("Proxy-Authorization", "Basic abc"),
        ("proxy-x-thing", "1"),
        ("TE", "trailers"),
        ("Trailer", "Expires"),
        ("Transfer-Encoding", "chunked"),
        ("Upgrade", "websocket"),
        ("Host", "gateway:8080"),
        ("Content-Length", "5"),
        ("Expect", "100-continue"),
        ("X-Keep", "yes"),
    ]
    assert filter_request_headers(headers) == [("Accept", "*/*"), ("X-Keep", "yes")]


def test_ordinary_headers_keep_their_case_order_and_repeats() -> None:
    headers = [("Cookie", "a=1"), ("X-Dup", "1"), ("cookie", "b=2"), ("X-Dup", "2")]
    assert filter_request_headers(headers) == headers


def test_connection_tokens_from_several_connection_headers_apply() -> None:
    headers = [
        ("connection", "x-a"),
        ("Connection", "x-b"),
        ("X-A", "1"),
        ("x-b", "2"),
        ("X-C", "3"),
    ]
    assert filter_request_headers(headers) == [("X-C", "3")]


# --- credential injection (REQ-4.5-2 to -6) ---------------------------------------------------


@pytest.mark.req("REQ-4.5-2", "REQ-4.5-6")
def test_bearer_replaces_every_client_authorization_header() -> None:
    headers = [("authorization", "Bearer client"), ("AUTHORIZATION", "Basic x"), ("Accept", "*/*")]
    assert inject_credential_header(headers, BEARER, SECRET) == [
        ("Accept", "*/*"),
        ("Authorization", f"Bearer {SECRET}"),
    ]


@pytest.mark.req("REQ-4.5-3", "REQ-4.5-6")
def test_custom_header_replaces_every_client_value_in_any_case() -> None:
    headers = [("x-api-key", "client"), ("X-API-KEY", "client2"), ("Authorization", "Bearer keep")]
    assert inject_credential_header(headers, CUSTOM, SECRET) == [
        ("Authorization", "Bearer keep"),
        ("X-Api-Key", SECRET),
    ]


@pytest.mark.req("REQ-4.5-5")
def test_mode_none_and_query_leave_the_headers_alone() -> None:
    headers = [("Authorization", "Bearer client"), ("X-Api-Key", "client")]
    assert inject_credential_header(headers, NONE, None) == headers
    assert inject_credential_header(headers, QUERY, SECRET) == headers


# --- target URL (REQ-4.1-1, REQ-4.5-4, REQ-4.5-6) ---------------------------------------------


def url(
    base: str, path: str, query: str = "", cred: Credential = NONE, secret: str | None = None
) -> str:
    return build_target_url(base, path, query, cred, secret)


@pytest.mark.req("REQ-4.1-1")
@pytest.mark.parametrize(
    ("base", "path", "query", "expected"),
    [
        ("https://api.x.com", "/v1/a", "city=B", "https://api.x.com/v1/a?city=B"),
        ("https://api.x.com/v1", "/a/b", "", "https://api.x.com/v1/a/b"),
        ("https://api.x.com", "", "", "https://api.x.com"),
        ("https://api.x.com", "/", "", "https://api.x.com/"),
        ("https://api.x.com", "/a/", "x=1", "https://api.x.com/a/?x=1"),
        ("https://api.x.com", "//a///b", "", "https://api.x.com/a/b"),
        ("https://api.x.com", "/a%2Fb/c", "", "https://api.x.com/a%2Fb/c"),
        ("https://api.x.com", "/q", "a=1&&b=%20&c", "https://api.x.com/q?a=1&&b=%20&c"),
        ("https://api.x.com", "/q", "a=1&b=2&a=3", "https://api.x.com/q?a=1&b=2&a=3"),
    ],
)
def test_path_and_query_are_forwarded_as_sent(
    base: str, path: str, query: str, expected: str
) -> None:
    assert url(base, path, query) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/a/./b", "/v1/a/b"),
        ("/a/../b", "/v1/b"),
        ("/../../etc/passwd", "/v1/etc/passwd"),
        ("/a/%2e%2e/%2E%2E/b", "/v1/b"),
        ("/a/%2e/b", "/v1/a/b"),
        ("/..", "/v1"),
        ("/a/..", "/v1"),
        ("/a/../", "/v1/"),
    ],
)
def test_dot_segments_are_resolved_and_never_climb_above_the_base_path(
    path: str, expected: str
) -> None:
    assert url("https://api.x.com/v1", path) == f"https://api.x.com{expected}"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/a b", "/a%20b"),
        ("/caf\xc3\xa9", "/caf%C3%A9"),  # the raw UTF-8 bytes, read as latin-1
        ("/a\x00b", "/a%00b"),
        ("/a\r\nb", "/a%0D%0Ab"),
        ("/100%", "/100%25"),
        ("/%zz", "/%25zz"),
        ("/ok%41", "/ok%41"),
        ("/a?b#c", "/a%3Fb%23c"),
    ],
)
def test_bytes_that_are_not_url_safe_are_percent_encoded(path: str, expected: str) -> None:
    assert url("https://api.x.com", path) == f"https://api.x.com{expected}"


@pytest.mark.req("REQ-4.5-4")
def test_query_credential_is_appended_url_encoded_after_the_existing_parameters() -> None:
    got = url("https://api.x.com", "/v1/w", "city=B&units=m", QUERY, "a b&c=d")
    assert got == "https://api.x.com/v1/w?city=B&units=m&appid=a%20b%26c%3Dd"


@pytest.mark.req("REQ-4.5-4")
def test_query_credential_alone_has_no_leading_ampersand() -> None:
    assert url("https://api.x.com", "/w", "", QUERY, "k") == "https://api.x.com/w?appid=k"


@pytest.mark.req("REQ-4.5-6")
@pytest.mark.parametrize(
    "query",
    [
        "appid=client",
        "appid=a&appid=b",
        "x=1&appid=client&y=2",
        "app%69d=client",
        "appid",
        "appid=",
    ],
)
def test_client_values_for_the_credential_parameter_are_removed_in_any_spelling(
    query: str,
) -> None:
    got = url("https://api.x.com", "/w", query, QUERY, "REAL")
    assert got.count("appid=") == 1
    assert got.endswith("appid=REAL")
    assert "client" not in got
    assert "app%69d" not in got


def test_other_parameters_survive_the_credential_rewrite_byte_for_byte() -> None:
    got = url("https://api.x.com", "/w", "a=%20x&&b&appid=c&d=1+2", QUERY, "k")
    assert got == "https://api.x.com/w?a=%20x&&b&d=1+2&appid=k"


def test_a_query_with_unsafe_bytes_is_encoded() -> None:
    assert (
        url("https://api.x.com", "/w", "q=a b\x00\xc3\xa9")
        == "https://api.x.com/w?q=a%20b%00%C3%A9"
    )


def test_the_credential_parameter_is_only_added_in_query_mode() -> None:
    assert url("https://api.x.com", "/w", "appid=client", BEARER, SECRET) == (
        "https://api.x.com/w?appid=client"
    )


# --- response headers -------------------------------------------------------------------------


@pytest.mark.req("REQ-4.4-7")
def test_response_headers_drop_only_hop_by_hop_and_keep_order_and_repeats() -> None:
    headers = [
        ("Content-Type", "application/json"),
        ("Set-Cookie", "a=1"),
        ("Content-Length", "10"),
        ("Content-Encoding", "gzip"),
        ("Transfer-Encoding", "chunked"),
        ("Connection", "close, X-Hop"),
        ("X-Hop", "1"),
        ("Keep-Alive", "timeout=5"),
        ("Set-Cookie", "b=2"),
        ("Retry-After", "7"),
    ]
    assert filter_response_headers(headers) == [
        ("Content-Type", "application/json"),
        ("Set-Cookie", "a=1"),
        ("Content-Length", "10"),
        ("Content-Encoding", "gzip"),
        ("Set-Cookie", "b=2"),
        ("Retry-After", "7"),
    ]


# --- streaming requests (D11) -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("accept", "expected"),
    [
        ("text/event-stream", True),
        ("TEXT/EVENT-STREAM", True),
        ("text/event-stream;q=0.9, */*", True),
        ("application/json, text/event-stream", True),
        ("application/json", False),
        ("*/*", False),
        ("text/event-streams", False),
        ("", False),
        (None, False),
    ],
)
def test_wants_stream(accept: str | None, expected: bool) -> None:
    assert wants_stream(accept) is expected
