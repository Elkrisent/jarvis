"""Service ports, the ``Services`` bundle and the admin-token dependency.

Ports are Protocols with exactly the methods the routers use. Missing rows raise
``jarvis_api.db.repositories.NotFound`` and uniqueness clashes raise ``Conflict`` at the port
boundary (including ``ApisPort.put_version`` for an unknown project); routers rely on that.
"""

from __future__ import annotations

import builtins
import hmac
from dataclasses import dataclass
from typing import Annotated, Protocol

from fastapi import Depends, Header
from jarvis_core.config import ApiDependency
from jarvis_core.errors import AuthRequiredError
from jarvis_core.keys import GeneratedKey
from jarvis_core.secretbox import AesGcmSecretBox
from starlette.requests import Request

from jarvis_api.db.config_source import StoredApi
from jarvis_api.db.repositories import KeyRecord, ProjectRecord
from jarvis_api.events import ConfigPublisher


class ProjectsPort(Protocol):
    async def create(self, name: str, slug: str) -> ProjectRecord: ...
    async def list(self) -> builtins.list[ProjectRecord]: ...
    async def get(self, id: str) -> ProjectRecord | None: ...
    async def update(self, id: str, *, name: str | None, slug: str | None) -> ProjectRecord: ...
    async def delete(self, id: str) -> None: ...


class KeysPort(Protocol):
    async def create(
        self, project_id: str, name: str, allowed_api_ids: builtins.list[str], key: GeneratedKey
    ) -> KeyRecord: ...
    async def list(self, project_id: str) -> builtins.list[KeyRecord]: ...
    async def revoke(self, key_id: str) -> KeyRecord: ...


class ApisPort(Protocol):
    async def put_version(
        self,
        project_id: str,
        api: ApiDependency,
        *,
        author_id: str | None = None,
        reason: str | None = None,
        api_id: str | None = None,
        create_only: bool = False,
        expected_version: int | None = None,
    ) -> tuple[str, int]: ...
    async def get_by_id(self, api_id: str) -> StoredApi | None: ...
    async def list_stored(self, project_id: str) -> builtins.list[StoredApi]: ...
    async def delete(self, api_id: str) -> None: ...


class SecretsPort(Protocol):
    async def add(self, box: AesGcmSecretBox, plaintext: str) -> str: ...
    async def reveal(self, box: AesGcmSecretBox, secret_id: str) -> str | None: ...
    async def delete(self, secret_id: str) -> None: ...


@dataclass
class Services:
    projects: ProjectsPort
    keys: KeysPort
    apis: ApisPort
    secrets: SecretsPort
    secret_box: AesGcmSecretBox
    publisher: ConfigPublisher


API_PREFIX = "/api/v1"

_DENIED = "a valid admin token is required"


def require_admin(request: Request, authorization: Annotated[str | None, Header()] = None) -> None:
    """Accept only ``Authorization: Bearer <admin token>`` (scheme case-insensitive)."""
    expected: str = request.app.state.admin_token
    scheme, _, presented = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise AuthRequiredError(_DENIED)
    if not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        raise AuthRequiredError(_DENIED)


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


ServicesDep = Annotated[Services, Depends(get_services)]
