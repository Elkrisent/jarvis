"""Route templates: the metrics key for a request path (SPEC §4.14).

Explicit per-API patterns are matched first; otherwise numeric, UUID and long token-like
segments become ``{id}``. A per-API cap on distinct fallback templates keeps metric
cardinality bounded, and the overflow goes to ``{other}``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

OTHER = "{other}"
DEFAULT_MAX_TEMPLATES = 500
ID = "{id}"
MAX_PATH_CHARS = 2048
MAX_SEGMENTS = 32
LONG_TOKEN_MIN = 25  # "longer than 24 characters"

_NUMERIC = re.compile(r"[0-9]+")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_TOKEN = re.compile(r"[A-Za-z0-9_+=-]+")  # hex and base64 / base64url alphabets
_PLACEHOLDER = re.compile(r"\{[^{}/]+\}")


def _strip(path: str) -> str:
    """The path without any query or fragment."""
    cut = min((i for i in (path.find("?"), path.find("#")) if i != -1), default=len(path))
    return path[:cut]


def _segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def _is_id(segment: str) -> bool:
    if _NUMERIC.fullmatch(segment) or _UUID.fullmatch(segment):
        return True
    return len(segment) >= LONG_TOKEN_MIN and _TOKEN.fullmatch(segment) is not None


class _Pattern:
    __slots__ = ("literals", "template", "wildcards")

    def __init__(self, pattern: str) -> None:
        parts = _segments(_strip(pattern))
        self.template = "/" + "/".join(parts)
        # None marks a placeholder segment, which matches any one segment.
        self.wildcards: tuple[str | None, ...] = tuple(
            None if _PLACEHOLDER.fullmatch(p) else p for p in parts
        )
        self.literals = sum(1 for p in self.wildcards if p is not None)

    def matches(self, segments: list[str]) -> bool:
        return all(
            want is None or want == got for want, got in zip(self.wildcards, segments, strict=True)
        )


class RouteTemplater:
    """One per API. ``template`` never raises and keeps at most ``max_templates`` fallback keys."""

    def __init__(
        self, patterns: Sequence[str] = (), *, max_templates: int = DEFAULT_MAX_TEMPLATES
    ) -> None:
        self._max = max_templates
        self._seen: set[str] = set()
        by_length: dict[int, list[_Pattern]] = {}
        for pattern in patterns:
            compiled = _Pattern(pattern)
            by_length.setdefault(len(compiled.wildcards), []).append(compiled)
        # Most literal segments first; sorted() is stable, so ties keep the configured order.
        self._patterns = {
            n: sorted(group, key=lambda p: -p.literals) for n, group in by_length.items()
        }

    def template(self, path: str) -> str:
        path = _strip(path)
        if len(path) > MAX_PATH_CHARS:
            return OTHER
        segments = _segments(path)
        if len(segments) > MAX_SEGMENTS:
            return OTHER
        for pattern in self._patterns.get(len(segments), ()):
            if pattern.matches(segments):
                return pattern.template
        template = "/" + "/".join(ID if _is_id(s) else s for s in segments)
        if template in self._seen:
            return template
        if len(self._seen) >= self._max:
            return OTHER
        self._seen.add(template)
        return template
