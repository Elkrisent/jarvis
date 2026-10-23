import ipaddress
from collections.abc import Sequence

import pytest
from jarvis_core.clock import FakeClock
from jarvis_core.errors import DestinationBlockedError
from jarvis_core.ssrf import (
    DestinationChecker,
    DestinationResolver,
    HostNotResolved,
    SsrfPolicy,
    check_destination_name,
    ip_is_blocked,
    normalize_host,
    system_resolver,
)

pytestmark = pytest.mark.unit

PUBLIC = "93.184.216.34"
INTERNAL = {"gateway", "api", "worker", "postgres", "redis"}


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    return ipaddress.ip_address(text)


class StubResolver:
    def __init__(self, answers: dict[str, Sequence[str]]) -> None:
        self.answers = dict(answers)
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, host: str, port: int) -> Sequence[str]:
        self.calls.append((host, port))
        return self.answers.get(host, [])


# --- IP classification (REQ-4.13-3) -----------------------------------------------------------

BLOCKED_RANGES = [
    ("0.0.0.0/8", "0.0.0.0", "0.255.255.255"),
    ("10.0.0.0/8", "10.0.0.0", "10.255.255.255"),
    ("100.64.0.0/10", "100.64.0.0", "100.127.255.255"),
    ("127.0.0.0/8", "127.0.0.1", "127.255.255.255"),
    ("169.254.0.0/16", "169.254.169.254", "169.254.255.255"),
    ("172.16.0.0/12", "172.16.0.0", "172.31.255.255"),
    ("192.0.0.0/24", "192.0.0.1", "192.0.0.255"),
    ("192.168.0.0/16", "192.168.0.1", "192.168.255.255"),
    ("198.18.0.0/15", "198.18.0.1", "198.19.255.255"),
    ("224.0.0.0/4", "224.0.0.1", "239.255.255.255"),
    ("240.0.0.0/4", "240.0.0.1", "255.255.255.255"),
    ("::/128", "::", "::"),
    ("::1/128", "::1", "::1"),
    ("fc00::/7", "fc00::1", "fdff:ffff:ffff:ffff:ffff:ffff:ffff:ffff"),
    ("fe80::/10", "fe80::1", "febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff"),
    ("ff00::/8", "ff02::1", "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff"),
    ("2001:db8::/32", "2001:db8::1", "2001:db8:ffff:ffff:ffff:ffff:ffff:ffff"),
]


@pytest.mark.req("REQ-4.13-3")
@pytest.mark.parametrize(("network", "first", "last"), BLOCKED_RANGES)
def test_every_default_range_is_blocked(network: str, first: str, last: str) -> None:
    assert ip_is_blocked(_ip(first))
    assert ip_is_blocked(_ip(last))
    assert _ip(first) in ipaddress.ip_network(network)


@pytest.mark.req("REQ-4.13-3")
@pytest.mark.parametrize(
    "address",
    ["8.8.8.8", "1.1.1.1", PUBLIC, "100.63.255.255", "100.128.0.0", "172.15.255.255",
     "172.32.0.0", "169.253.255.255", "2606:4700:4700::1111", "2a00:1450:4001::200e"],
)  # fmt: skip
def test_public_addresses_are_allowed(address: str) -> None:
    assert not ip_is_blocked(_ip(address))


@pytest.mark.req("REQ-4.13-3")
@pytest.mark.parametrize(
    ("address", "blocked"),
    [
        ("::ffff:127.0.0.1", True),
        ("::ffff:10.0.0.1", True),
        ("::ffff:169.254.169.254", True),
        ("::ffff:8.8.8.8", False),
        ("2002:7f00:1::", True),  # 6to4 of 127.0.0.1
        ("2002:808:808::", False),  # 6to4 of 8.8.8.8
        ("64:ff9b::a9fe:a9fe", True),  # NAT64 of 169.254.169.254
        ("64:ff9b::808:808", False),
        ("::7f00:1", True),  # IPv4-compatible 127.0.0.1
    ],
)
def test_ipv6_forms_that_embed_ipv4_use_the_embedded_address(address: str, blocked: bool) -> None:
    assert ip_is_blocked(_ip(address)) is blocked


# --- host parsing -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("API.Example.COM", "api.example.com"),
        ("api.example.com.", "api.example.com"),
        ("LOCALHOST.", "localhost"),
        ("2130706433", _ip("127.0.0.1")),
        ("0177.0.0.1", _ip("127.0.0.1")),
        ("0x7f.1", _ip("127.0.0.1")),
        ("127.1", _ip("127.0.0.1")),
        ("0", _ip("0.0.0.0")),
        ("93.184.216.34", _ip(PUBLIC)),
        ("[::1]", _ip("::1")),
        ("::1", _ip("::1")),
        ("[::ffff:7f00:1]", _ip("::ffff:7f00:1")),
        ("bücher.example", "xn--bcher-kva.example"),
        ("deadbeef.example", "deadbeef.example"),
    ],
)
def test_normalize_host(host: str, expected: object) -> None:
    assert normalize_host(host) == expected


@pytest.mark.parametrize(
    "host", ["", ".", "a b", "a\x00b", "a\tb", "fe80::1%eth0", "[fe80::1%25eth0]", "a/b", "a@b"]
)
def test_malformed_hosts_are_blocked(host: str) -> None:
    with pytest.raises(DestinationBlockedError):
        normalize_host(host)


# --- name-level rules -------------------------------------------------------------------------


def test_https_to_a_public_name_needs_no_allowlist() -> None:
    assert check_destination_name("https", "api.example.com", SsrfPolicy()) is False


@pytest.mark.req("REQ-4.13-5")
@pytest.mark.parametrize("scheme", ["ftp", "file", "gopher", "ws", ""])
def test_other_schemes_are_blocked(scheme: str) -> None:
    with pytest.raises(DestinationBlockedError):
        check_destination_name(scheme, "api.example.com", SsrfPolicy())


@pytest.mark.req("REQ-4.13-5")
def test_plain_http_needs_the_host_on_the_allowlist() -> None:
    with pytest.raises(DestinationBlockedError):
        check_destination_name("http", "api.example.com", SsrfPolicy())
    policy = SsrfPolicy.from_env({"JARVIS_SSRF_ALLOWLIST": "API.example.com, 10.0.0.0/8"})
    assert check_destination_name("http", "api.example.com", policy) is True
    assert check_destination_name("http", "10.1.2.3", policy) is True  # IP literal in a CIDR
    with pytest.raises(DestinationBlockedError):
        check_destination_name("http", "other.example.com", policy)
    with pytest.raises(DestinationBlockedError):
        check_destination_name("http", "11.1.2.3", policy)


@pytest.mark.req("REQ-4.13-3")
@pytest.mark.parametrize("host", ["localhost", "LOCALHOST.", "foo.localhost", "api", "Postgres"])
def test_localhost_and_jarvis_container_names_are_blocked_even_if_allowlisted(host: str) -> None:
    entry = host.lower().strip(".")
    policy = SsrfPolicy.from_env({"JARVIS_SSRF_ALLOWLIST": f"{entry}, 0.0.0.0/0"})
    with pytest.raises(DestinationBlockedError):
        check_destination_name("https", host, policy)


# --- resolution (REQ-4.13-1, -3, -4) ----------------------------------------------------------


def _resolver(
    answers: dict[str, Sequence[str]],
    policy: SsrfPolicy | None = None,
    clock: FakeClock | None = None,
) -> tuple[DestinationResolver, StubResolver]:
    stub = StubResolver(answers)
    return DestinationResolver(policy or SsrfPolicy(), stub, clock or FakeClock()), stub


async def test_a_public_address_passes_and_is_returned() -> None:
    resolver, stub = _resolver({"api.example.com": [PUBLIC]})
    assert await resolver.resolve("api.example.com", 443, on_allowlist=False) == [PUBLIC]
    assert ("api.example.com", 443) in stub.calls


@pytest.mark.req("REQ-4.13-3")
async def test_one_blocked_address_blocks_the_whole_name() -> None:
    resolver, _ = _resolver({"mixed.example": [PUBLIC, "10.0.0.5"]})
    with pytest.raises(DestinationBlockedError):
        await resolver.resolve("mixed.example", 443, on_allowlist=False)


@pytest.mark.req("REQ-4.13-3")
@pytest.mark.parametrize("literal", ["127.0.0.1", "2130706433", "[::1]", "169.254.169.254", "0"])
async def test_ip_literals_are_checked_without_dns(literal: str) -> None:
    resolver, stub = _resolver({})
    with pytest.raises(DestinationBlockedError):
        await resolver.resolve(literal, 443, on_allowlist=False)
    assert all(host in INTERNAL for host, _ in stub.calls)  # only the internal-host lookups


@pytest.mark.req("REQ-4.13-4")
async def test_an_allowlisted_cidr_permits_matching_addresses() -> None:
    policy = SsrfPolicy.from_env({"JARVIS_SSRF_ALLOWLIST": "10.1.0.0/16"})
    resolver, _ = _resolver({"svc.internal": ["10.1.2.3"], "other.internal": ["10.2.0.1"]}, policy)
    assert await resolver.resolve("svc.internal", 443, on_allowlist=False) == ["10.1.2.3"]
    with pytest.raises(DestinationBlockedError):
        await resolver.resolve("other.internal", 443, on_allowlist=False)


@pytest.mark.req("REQ-4.13-4")
async def test_an_allowlisted_host_may_resolve_to_a_private_address() -> None:
    resolver, _ = _resolver({"svc.internal": ["192.168.5.5"]})
    assert await resolver.resolve("svc.internal", 443, on_allowlist=True) == ["192.168.5.5"]


@pytest.mark.req("REQ-4.13-3")
async def test_an_allowlist_covering_the_docker_network_does_not_unblock_jarvis_containers() -> (
    None
):
    policy = SsrfPolicy.from_env({"JARVIS_SSRF_ALLOWLIST": "172.16.0.0/12"})
    resolver, _ = _resolver(
        {"api": ["172.18.0.3"], "postgres": ["172.18.0.2"], "partner.internal": ["172.18.0.3"]},
        policy,
    )
    with pytest.raises(DestinationBlockedError):
        await resolver.resolve("partner.internal", 443, on_allowlist=False)  # same IP as `api`


async def test_a_name_with_no_addresses_is_not_resolved() -> None:
    resolver, _ = _resolver({})
    with pytest.raises(HostNotResolved):
        await resolver.resolve("nx.example", 443, on_allowlist=False)


async def test_internal_host_addresses_are_cached_for_30_seconds() -> None:
    clock = FakeClock()
    resolver, stub = _resolver({"api.example.com": [PUBLIC], "api": ["172.18.0.3"]}, clock=clock)
    await resolver.resolve("api.example.com", 443, on_allowlist=False)
    await resolver.resolve("api.example.com", 443, on_allowlist=False)
    assert [c for c in stub.calls if c[0] == "api"] == [("api", 0)]
    clock.advance(31)
    await resolver.resolve("api.example.com", 443, on_allowlist=False)
    assert [c for c in stub.calls if c[0] == "api"] == [("api", 0), ("api", 0)]


async def test_a_failing_internal_host_lookup_does_not_break_resolution() -> None:
    class Flaky(StubResolver):
        async def __call__(self, host: str, port: int) -> Sequence[str]:
            if host == "api":
                raise OSError("no such host")
            return await super().__call__(host, port)

    resolver = DestinationResolver(SsrfPolicy(), Flaky({"x.example": [PUBLIC]}), FakeClock())
    assert await resolver.resolve("x.example", 443, on_allowlist=False) == [PUBLIC]


# --- policy from the environment --------------------------------------------------------------


def test_policy_from_env_parses_cidrs_hosts_and_separators() -> None:
    policy = SsrfPolicy.from_env(
        {"JARVIS_SSRF_ALLOWLIST": "10.0.0.0/8,Svc.Internal  fd00::/8\n192.168.1.5"}
    )
    assert policy.allow_hosts == frozenset({"svc.internal"})
    assert {str(n) for n in policy.allow_networks} == {"10.0.0.0/8", "fd00::/8", "192.168.1.5/32"}


def test_policy_defaults() -> None:
    policy = SsrfPolicy.from_env({})
    assert policy.allow_networks == ()
    assert policy.allow_hosts == frozenset()
    assert policy.internal_hosts == frozenset({"gateway", "api", "worker", "postgres", "redis"})


def test_internal_hosts_can_be_overridden() -> None:
    assert SsrfPolicy.from_env({"JARVIS_INTERNAL_HOSTS": "Web, db"}).internal_hosts == frozenset(
        {"web", "db"}
    )


@pytest.mark.parametrize("bad", ["10.0.0.0/99", "a/b", "10.0.0.0/8, bad_host!", "x@y"])
def test_invalid_allowlist_entries_are_named_by_variable_without_the_value(bad: str) -> None:
    with pytest.raises(ValueError, match="JARVIS_SSRF_ALLOWLIST") as exc:
        SsrfPolicy.from_env({"JARVIS_SSRF_ALLOWLIST": bad})
    assert bad not in str(exc.value)


# --- the save-time checker --------------------------------------------------------------------


def _checker(
    answers: dict[str, Sequence[str]], policy: SsrfPolicy | None = None
) -> DestinationChecker:
    return DestinationChecker(_resolver(answers, policy)[0])


@pytest.mark.req("REQ-4.13-1")
async def test_save_accepts_a_public_host() -> None:
    await _checker({"api.example.com": [PUBLIC]}).check_save("https://api.example.com/v1")


@pytest.mark.req("REQ-4.13-1")
@pytest.mark.parametrize(
    "url",
    [
        "https://10.0.0.5",
        "https://169.254.169.254/latest",
        "https://localhost:8443",
        "https://2130706433",
        "http://api.example.com",
        "https://private.example",
        "ftp://api.example.com",
    ],
)
async def test_save_rejects_blocked_destinations(url: str) -> None:
    checker = _checker({"api.example.com": [PUBLIC], "private.example": ["10.9.9.9"]})
    with pytest.raises(DestinationBlockedError):
        await checker.check_save(url)


@pytest.mark.req("REQ-4.13-1")
async def test_save_accepts_an_unresolvable_host() -> None:
    await _checker({}).check_save("https://api.weather.example")


@pytest.mark.req("REQ-4.13-4")
async def test_save_accepts_an_allowlisted_internal_http_host() -> None:
    policy = SsrfPolicy.from_env({"JARVIS_SSRF_ALLOWLIST": "mock-upstream"})
    await _checker({"mock-upstream": ["172.18.0.9"]}, policy).check_save(
        "http://mock-upstream:8080"
    )


async def test_save_error_does_not_echo_the_address_or_host() -> None:
    checker = _checker({"private.example": ["10.9.9.9"]})
    with pytest.raises(DestinationBlockedError) as exc:
        await checker.check_save("https://private.example")
    text = str(exc.value) + str(exc.value.to_body())
    assert "10.9.9.9" not in text
    assert "private.example" not in text


@pytest.mark.parametrize(
    "url",
    [
        "https://good.example\\@127.0.0.1/",
        "https://a b.example",
        "https://x.example:99999",
        "https://",
        "not a url",
    ],
)
async def test_save_rejects_malformed_urls(url: str) -> None:
    with pytest.raises(DestinationBlockedError):
        await _checker({"good.example": [PUBLIC]}).check_save(url)


async def test_save_uses_the_scheme_default_port_for_the_lookup() -> None:
    policy = SsrfPolicy.from_env({"JARVIS_SSRF_ALLOWLIST": "svc.internal"})
    resolver, stub = _resolver({"svc.internal": ["10.0.0.1"], "ok.example": [PUBLIC]}, policy)
    checker = DestinationChecker(resolver)
    await checker.check_save("http://svc.internal")
    await checker.check_save("https://ok.example")
    await checker.check_save("https://ok.example:8443")
    assert [c for c in stub.calls if c[0] in {"svc.internal", "ok.example"}] == [
        ("svc.internal", 80),
        ("ok.example", 443),
        ("ok.example", 8443),
    ]


# --- remaining branches -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "host", ["[abc]", "not:an:ip", "a" * 64 + ".com", ("a" * 60 + ".") * 5 + "com", "a!b", "-"]
)
def test_more_malformed_hosts_are_blocked(host: str) -> None:
    if host == "-":
        assert normalize_host(host) == "-"  # a legal (if odd) label: the resolver decides
        return
    with pytest.raises(DestinationBlockedError):
        normalize_host(host)


def test_hex_looking_names_are_names() -> None:
    assert normalize_host("cafe") == "cafe"
    assert normalize_host("1.2.3.4.5") == "1.2.3.4.5"


def test_an_ip_entry_in_the_allowlist_is_a_single_address_network() -> None:
    policy = SsrfPolicy.from_env({"JARVIS_SSRF_ALLOWLIST": "127.1, ::1"})
    assert {str(n) for n in policy.allow_networks} == {"127.0.0.1/32", "::1/128"}


@pytest.mark.parametrize("bad", ["not_ok!", "10.0.0.0/8", ""])
def test_invalid_internal_host_entries_are_rejected(bad: str) -> None:
    if not bad:
        assert SsrfPolicy.from_env({"JARVIS_INTERNAL_HOSTS": bad}).internal_hosts
        return
    with pytest.raises(ValueError, match="JARVIS_INTERNAL_HOSTS") as exc:
        SsrfPolicy.from_env({"JARVIS_INTERNAL_HOSTS": bad})
    assert bad not in str(exc.value)


async def test_garbage_answers_from_a_resolver_are_ignored() -> None:
    resolver, _ = _resolver({"x.example": ["not-an-ip", PUBLIC, "fe80::1%eth0"]})
    with pytest.raises(DestinationBlockedError):
        await resolver.resolve("x.example", 443, on_allowlist=False)  # fe80::1 is link-local
    resolver, _ = _resolver({"y.example": ["not-an-ip"]})
    with pytest.raises(HostNotResolved):
        await resolver.resolve("y.example", 443, on_allowlist=False)


async def test_an_ipv4_mapped_answer_matching_an_internal_address_is_blocked() -> None:
    resolver, _ = _resolver({"api": ["172.18.0.3"], "z.example": ["::ffff:172.18.0.3"]})
    with pytest.raises(DestinationBlockedError):
        await resolver.resolve("z.example", 443, on_allowlist=True)


async def test_the_system_resolver_resolves_localhost_and_not_nonsense() -> None:
    assert "127.0.0.1" in await system_resolver("localhost", 80)
    assert await system_resolver("nx.invalid", 80) == []
