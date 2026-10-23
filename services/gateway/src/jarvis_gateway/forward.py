"""Pure helpers for forwarding: header filtering, credential injection, target URL (SPEC §4.1/4.5).

No I/O and no secrets are kept here; ``secret`` arguments are used once and returned only inside
the headers or URL they were asked to build.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from urllib.parse import quote_from_bytes, unquote_plus

from jarvis_core.config import Credential

Headers = Sequence[tuple[str, str]]

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
# Dropped from requests only: the HTTP client sets the first two again for the new request.
_REQUEST_ONLY = frozenset({"host", "content-length", "expect"})
_PATH_SAFE = "/:@!$&'()*+,;=-._~%"
_QUERY_SAFE = "/?:@!$&'()*+,;=-._~%"
_BAD_PERCENT = re.compile(rb"%(?![0-9A-Fa-f]{2})")


def _named_by_connection(headers: Headers) -> set[str]:
    named: set[str] = set()
    for name, value in headers:
        if name.lower() == "connection":
            named.update(t.strip().lower() for t in value.split(",") if t.strip())
    return named


def _hop_by_hop(name: str, named: set[str]) -> bool:
    lowered = name.lower()
    return lowered in HOP_BY_HOP or lowered.startswith("proxy-") or lowered in named


def filter_request_headers(headers: Headers) -> list[tuple[str, str]]:
    """The client's headers minus hop-by-hop headers, ``Host``, ``Content-Length``, ``Expect``."""
    named = _named_by_connection(headers)
    return [
        (n, v) for n, v in headers if n.lower() not in _REQUEST_ONLY and not _hop_by_hop(n, named)
    ]


def filter_response_headers(headers: Headers) -> list[tuple[str, str]]:
    """The upstream's headers without hop-by-hop headers; order and repeats are kept."""
    named = _named_by_connection(headers)
    return [(n, v) for n, v in headers if not _hop_by_hop(n, named)]


def inject_credential_header(
    headers: Headers, credential: Credential, secret: str | None
) -> list[tuple[str, str]]:
    """Remove any client value in the credential's header slot, then set the real one."""
    if credential.mode == "bearer":
        name, value = "Authorization", f"Bearer {secret}"
    elif credential.mode == "header" and credential.header_name is not None:
        name, value = credential.header_name, str(secret)
    else:
        return list(headers)
    kept = [(n, v) for n, v in headers if n.lower() != name.lower()]
    kept.append((name, value))
    return kept


def _encode(text: str, safe: str) -> str:
    """Percent-encode the raw (latin-1 view of the) bytes; keep valid ``%XX``, fix stray ``%``."""
    raw = _BAD_PERCENT.sub(b"%25", text.encode("latin-1", errors="replace"))
    return quote_from_bytes(raw, safe=safe)


def _clean_path(raw_path: str) -> str:
    """Resolve dot segments (also ``%2e``), collapse empty segments, never climb above the root."""
    stack: list[str] = []
    for segment in raw_path.split("/"):
        folded = segment.replace("%2e", ".").replace("%2E", ".")
        if segment == "" or folded == ".":
            continue
        if folded == "..":
            if stack:
                stack.pop()
            continue
        stack.append(segment)
    if stack:
        return "/" + "/".join(stack) + ("/" if raw_path.endswith("/") else "")
    return "/" if raw_path.endswith("/") else ""


def build_target_url(
    base_url: str, raw_path: str, raw_query: str, credential: Credential, secret: str | None
) -> str:
    """``{base_url}{path}?{query}``, with the credential added to the query in ``query`` mode.

    The path keeps its original percent-encoding. The query is kept segment by segment as sent;
    only segments for the credential parameter (any spelling of its name) are dropped.
    """
    path = _encode(_clean_path(raw_path), _PATH_SAFE)
    query = raw_query
    if credential.mode == "query" and credential.param_name is not None:
        param = credential.param_name
        segments = raw_query.split("&") if raw_query else []
        kept = [s for s in segments if unquote_plus(s.split("=", 1)[0]) != param]
        name = quote_from_bytes(param.encode(), safe="")
        kept.append(f"{name}={quote_from_bytes(str(secret).encode(), safe='')}")
        query = "&".join(kept)
    query = _encode(query, _QUERY_SAFE)
    return f"{base_url}{path}" + (f"?{query}" if query else "")


def wants_stream(accept: str | None) -> bool:
    """True if the client asks for server-sent events (rejected in v1, SPEC D11)."""
    if not accept:
        return False
    return any(
        part.split(";")[0].strip().lower() == "text/event-stream" for part in accept.split(",")
    )
