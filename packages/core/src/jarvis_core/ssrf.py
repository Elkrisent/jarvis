"""SSRF protection rules (SPEC §4.13), shared by the management API and the gateway.

Nothing here opens a connection. The API uses ``DestinationChecker`` when an API is saved; the
gateway's transport uses ``check_destination_name`` and ``DestinationResolver`` at connect time
and connects to the addresses ``resolve`` returns, so the checked address is the connected one.
Error messages are fixed text: never the host, an address or the allowlist.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from jarvis_core.clock import Clock, SystemClock
from jarvis_core.errors import DestinationBlockedError

log = logging.getLogger("jarvis_core.ssrf")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
Resolver = Callable[[str, int], Awaitable[Sequence[str]]]

MESSAGE = "destination is not allowed"
DEFAULT_INTERNAL_HOSTS = frozenset({"gateway", "api", "worker", "postgres", "redis"})
INTERNAL_TTL_S = 30.0
INTERNAL_LOOKUP_TIMEOUT_S = 2.0
MAX_HOST_CHARS = 253

_BLOCKED = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "2001::/32",  # Teredo: embeds an obfuscated IPv4 address
        "2001:db8::/32",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
)
# The gateway's and api's own listeners. A wide allowlist entry (``0.0.0.0/0``) never covers
# these; only an entry that lies inside them does (``127.0.0.0/8``, ``::1/128``).
_LOOPBACK = tuple(
    ipaddress.ip_network(n) for n in ("0.0.0.0/8", "127.0.0.0/8", "::/128", "::1/128")
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")
_NUMERIC_HOST = re.compile(r"[0-9a-fA-FxX.]+")
_HOSTNAME = re.compile(r"[a-z0-9_.-]+")
_SPLIT = re.compile(r"[,\s]+")


class HostNotResolved(Exception):
    """The name has no addresses. Not a policy decision: callers choose what that means."""


def _blocked(reason: str) -> DestinationBlockedError:
    log.debug("destination blocked: %s", reason)  # a fixed reason code, never the destination
    return DestinationBlockedError(MESSAGE)


async def system_resolver(host: str, port: int) -> list[str]:
    """Addresses from the operating system's resolver, de-duplicated, in resolver order."""
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    return list(dict.fromkeys(str(info[4][0]).split("%")[0] for info in infos))


# --- host parsing and IP classification -------------------------------------------------------


def normalize_host(host: str) -> str | IPAddress:
    """An IP address object, or the lower-cased ASCII host name. Raises if the host is malformed.

    Numeric hosts are read the way the operating system would (``2130706433``, ``0177.0.0.1``,
    ``0x7f.1`` and ``127.1`` are all 127.0.0.1), so no spelling gets past the IP rules.
    """
    if not host or "%" in host or any(ord(c) <= 0x20 or ord(c) == 0x7F for c in host):
        raise _blocked("host-form")  # includes IPv6 zone ids, which ``ipaddress`` would accept
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
        if ":" not in host:
            raise _blocked("host-form")
    if ":" in host:
        try:
            return ipaddress.ip_address(host)
        except ValueError:
            raise _blocked("host-form") from None
    name = host.rstrip(".") if host != "." else ""
    if not name:
        raise _blocked("host-form")
    try:
        name = name.encode("idna").decode("ascii").lower()  # nameprep also folds look-alike dots
    except UnicodeError:
        raise _blocked("host-form") from None
    name = name.rstrip(".")
    if not name or len(name) > MAX_HOST_CHARS or not _HOSTNAME.fullmatch(name):
        raise _blocked("host-form")
    if _NUMERIC_HOST.fullmatch(name):
        try:
            return ipaddress.IPv4Address(socket.inet_ntoa(socket.inet_aton(name)))
        except OSError:
            pass  # not a number: an ordinary name such as ``cafe``
    return name


def ip_is_blocked(ip: IPAddress) -> bool:
    """True for every address SPEC §4.13 blocks by default. IPv6 forms that embed an IPv4
    address are judged by the embedded address."""
    embedded = _embedded_ipv4(ip)
    if embedded is not None:
        return ip_is_blocked(embedded)
    return any(ip.version == net.version and ip in net for net in _BLOCKED)


def _embedded_ipv4(ip: IPAddress) -> ipaddress.IPv4Address | None:
    if isinstance(ip, ipaddress.IPv4Address):
        return None
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip in _NAT64 or (ip in _IPV4_COMPATIBLE and int(ip) > 1):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def _canonical(ip: IPAddress) -> IPAddress:
    """The IPv4 address an IPv6 form embeds (mapped, 6to4, NAT64, compatible), else ``ip``."""
    return _embedded_ipv4(ip) or ip


def _in(ip: IPAddress, networks: Sequence[IPNetwork]) -> bool:
    return any(ip.version == net.version and ip in net for net in networks)


# --- policy -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SsrfPolicy:
    allow_networks: tuple[IPNetwork, ...] = ()
    allow_hosts: frozenset[str] = frozenset()
    internal_hosts: frozenset[str] = field(default=DEFAULT_INTERNAL_HOSTS)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> SsrfPolicy:
        networks: list[IPNetwork] = []
        hosts: set[str] = set()
        for index, entry in enumerate(_entries(env.get("JARVIS_SSRF_ALLOWLIST", ""))):
            try:
                if "/" in entry:
                    networks.append(ipaddress.ip_network(entry, strict=False))
                    continue
                normalized = normalize_host(entry)
            except (ValueError, DestinationBlockedError):
                raise ValueError(
                    f"JARVIS_SSRF_ALLOWLIST entry {index + 1} is not a CIDR or host name"
                ) from None
            if isinstance(normalized, str):
                hosts.add(normalized)
            else:
                networks.append(ipaddress.ip_network(normalized))
        internal = DEFAULT_INTERNAL_HOSTS
        raw_internal = env.get("JARVIS_INTERNAL_HOSTS")
        if raw_internal:
            names: set[str] = set()
            for index, entry in enumerate(_entries(raw_internal)):
                try:
                    name = normalize_host(entry)
                except DestinationBlockedError:
                    name = None
                if not isinstance(name, str):
                    raise ValueError(f"JARVIS_INTERNAL_HOSTS entry {index + 1} is not a host name")
                names.add(name)
            if not names:
                raise ValueError("JARVIS_INTERNAL_HOSTS has no entries")
            internal = frozenset(names)
        return cls(tuple(networks), frozenset(hosts), internal)

    def host_allowed(self, host: str | IPAddress) -> bool:
        if isinstance(host, str):
            return host in self.allow_hosts
        return _in(host, self.allow_networks)


def _entries(raw: str) -> list[str]:
    return [e for e in _SPLIT.split(raw.strip()) if e]


def check_destination_name(scheme: str, host: str, policy: SsrfPolicy) -> bool:
    """The rules that need no DNS. Returns whether the host is on the allowlist; raises
    ``DestinationBlockedError`` for a bad scheme, a malformed host, ``localhost``, a JARVIS
    container name, or plain ``http`` to a host that is not on the allowlist."""
    scheme = scheme.lower()
    if scheme not in ("https", "http"):
        raise _blocked("scheme")
    normalized = normalize_host(host)
    if isinstance(normalized, str) and (
        normalized == "localhost"
        or normalized.endswith(".localhost")
        or normalized in policy.internal_hosts
    ):
        raise _blocked("name")
    on_allowlist = policy.host_allowed(normalized)
    if scheme == "http" and not on_allowlist:
        raise _blocked("plain-http")
    return on_allowlist


class DestinationResolver:
    """Resolve a host and validate every address; the caller connects to what comes back."""

    def __init__(
        self,
        policy: SsrfPolicy,
        resolver: Resolver = system_resolver,
        clock: Clock | None = None,
    ) -> None:
        self.policy = policy
        self._resolver = resolver
        self._clock = SystemClock() if clock is None else clock
        self._internal: frozenset[IPAddress] = frozenset()
        self._internal_until = float("-inf")
        self._loopback_allowed = tuple(
            net
            for net in policy.allow_networks
            if any(net.version == lb.version and net.subnet_of(lb) for lb in _LOOPBACK)  # type: ignore[arg-type]
        )

    async def resolve(self, host: str, port: int, *, on_allowlist: bool) -> list[str]:
        normalized = normalize_host(host)
        if isinstance(normalized, str):
            addresses = _parse_addresses(await self._resolver(normalized, port))
            if not addresses:
                raise HostNotResolved
        else:
            addresses = [normalized]
        internal, complete = await self._internal_addresses()
        for ip in addresses:
            canonical = _canonical(ip)
            if canonical in internal:
                raise _blocked("internal-host")
            if _in(canonical, _LOOPBACK):
                if not _in(canonical, self._loopback_allowed):
                    raise _blocked("loopback")
                continue
            if not ip_is_blocked(ip):
                continue
            if not (on_allowlist or _in(canonical, self.policy.allow_networks)):
                raise _blocked("blocked-address")
            if not complete:
                # Allowed only by the allowlist, and the JARVIS containers' addresses could not be
                # looked up: this might be one of them, so fail closed.
                raise _blocked("internal-unverified")
        return [str(ip) for ip in addresses]

    async def _internal_addresses(self) -> tuple[frozenset[IPAddress], bool]:
        """The JARVIS containers' addresses, and whether every lookup worked. A lookup that failed
        or timed out keeps the last known addresses, and nothing is cached until all succeed."""
        now = self._clock.monotonic()
        if now < self._internal_until:
            return self._internal, True

        async def lookup(name: str) -> Sequence[str]:
            return await asyncio.wait_for(self._resolver(name, 0), INTERNAL_LOOKUP_TIMEOUT_S)

        results = await asyncio.gather(
            *(lookup(n) for n in sorted(self.policy.internal_hosts)), return_exceptions=True
        )
        complete = not any(isinstance(r, BaseException) for r in results)
        found: set[IPAddress] = set() if complete else set(self._internal)
        for result in results:
            if not isinstance(result, BaseException):
                found.update(_canonical(ip) for ip in _parse_addresses(result))
        self._internal = frozenset(found)
        if complete:
            self._internal_until = now + INTERNAL_TTL_S
        return self._internal, complete


def _parse_addresses(raw: Sequence[str]) -> list[IPAddress]:
    parsed: list[IPAddress] = []
    for text in raw:
        try:
            parsed.append(ipaddress.ip_address(text.split("%")[0]))
        except ValueError:
            continue
    return parsed


class DestinationChecker:
    """The save-time check: the same rules as connect time, but an unresolvable host is accepted
    (connect time is the enforcement)."""

    def __init__(self, destinations: DestinationResolver) -> None:
        self._destinations = destinations

    async def check_save(self, base_url: str) -> None:
        if any(ord(c) <= 0x20 or ord(c) == 0x7F or c == "\\" for c in base_url):
            raise _blocked("url-form")
        try:
            parts = urlsplit(base_url)
            port = parts.port
        except ValueError:
            raise _blocked("url-form") from None
        host = parts.hostname or ""
        on_allowlist = check_destination_name(parts.scheme, host, self._destinations.policy)
        try:
            await self._destinations.resolve(
                host,
                port or (80 if parts.scheme.lower() == "http" else 443),
                on_allowlist=on_allowlist,
            )
        except HostNotResolved:
            return
