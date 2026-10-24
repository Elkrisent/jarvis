"""Route templates per API (SPEC §4.14), kept in step with the config snapshot."""

from __future__ import annotations

from jarvis_core.route_template import RouteTemplater

from jarvis_gateway.config.snapshot import ApiEntry, Snapshot


class TemplateRegistry:
    """One ``RouteTemplater`` per ``(api_id, version)``, built on first use.

    A new API version starts with a fresh templater, so its fallback-template budget is not
    shared with the old one. ``retain`` is a ``ConfigStore`` listener.
    """

    def __init__(self) -> None:
        self._templaters: dict[tuple[str, int], RouteTemplater] = {}

    @property
    def size(self) -> int:
        return len(self._templaters)

    def templater_for(self, api: ApiEntry) -> RouteTemplater:
        key = (api.api_id, api.version)
        templater = self._templaters.get(key)
        if templater is None:
            templater = RouteTemplater([r.pattern for r in api.config.routes])
            self._templaters[key] = templater
        return templater

    def route_for(self, api: ApiEntry, path: str) -> str:
        return self.templater_for(api).template(path)

    def retain(self, snapshot: Snapshot) -> None:
        live = {(a.api_id, a.version) for a in snapshot.apis.values()}
        for key in [k for k in self._templaters if k not in live]:
            del self._templaters[key]
