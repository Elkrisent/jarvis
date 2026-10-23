"""Test doubles for the api service."""

from __future__ import annotations

import builtins
from datetime import UTC, datetime

from jarvis_api.db.config_source import StoredApi
from jarvis_api.db.ids import new_id
from jarvis_api.db.repositories import Conflict, KeyRecord, NotFound, ProjectRecord
from jarvis_api.http.deps import Services
from jarvis_core.config import ApiDependency
from jarvis_core.keys import GeneratedKey
from jarvis_core.secretbox import AesGcmSecretBox


class RecordingPublisher:
    """A ConfigPublisher that records calls instead of talking to Redis."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def config_changed(self, api_id: str, version: int) -> None:
        self.calls.append((api_id, version))


class FakeProjects:
    """In-memory ProjectsPort with the repository's Conflict/NotFound behaviour."""

    def __init__(self) -> None:
        self.rows: dict[str, ProjectRecord] = {}

    def _check_slug(self, slug: str, *, ignore: str | None = None) -> None:
        if any(r.slug == slug and r.id != ignore for r in self.rows.values()):
            raise Conflict(f"project slug already exists: {slug}")

    async def create(self, name: str, slug: str) -> ProjectRecord:
        self._check_slug(slug)
        record = ProjectRecord(new_id("prj"), name, slug, datetime.now(UTC))
        self.rows[record.id] = record
        return record

    async def list(self) -> builtins.list[ProjectRecord]:
        return builtins.list(self.rows.values())

    async def get(self, id: str) -> ProjectRecord | None:
        return self.rows.get(id)

    async def update(self, id: str, *, name: str | None, slug: str | None) -> ProjectRecord:
        current = self.rows.get(id)
        if current is None:
            raise NotFound(f"project not found: {id}")
        if slug is not None:
            self._check_slug(slug, ignore=id)
        record = ProjectRecord(
            current.id, name or current.name, slug or current.slug, current.created_at
        )
        self.rows[id] = record
        return record

    async def delete(self, id: str) -> None:
        if self.rows.pop(id, None) is None:
            raise NotFound(f"project not found: {id}")


class FakeKeys:
    """In-memory KeysPort; given projects/apis it checks them like ``KeyRepository``."""

    def __init__(self, projects: FakeProjects | None = None, apis: FakeApis | None = None) -> None:
        self._projects = projects
        self._apis = apis
        self.rows: dict[str, KeyRecord] = {}
        self.hashes: dict[str, str] = {}

    async def create(
        self, project_id: str, name: str, allowed_api_ids: builtins.list[str], key: GeneratedKey
    ) -> KeyRecord:
        if self._projects is not None and project_id not in self._projects.rows:
            raise NotFound(f"project not found: {project_id}")
        if self._apis is not None and any(
            a not in self._apis.rows or self._apis.rows[a].project_id != project_id
            for a in allowed_api_ids
        ):
            raise ValueError("allowed_api_ids must belong to the project")
        record = KeyRecord(
            new_id("key"), project_id, name, key.prefix, tuple(allowed_api_ids), None
        )
        self.rows[record.id] = record
        self.hashes[record.id] = key.hash
        return record

    async def list(self, project_id: str) -> builtins.list[KeyRecord]:
        return [r for r in self.rows.values() if r.project_id == project_id]

    async def revoke(self, key_id: str) -> KeyRecord:
        current = self.rows.get(key_id)
        if current is None:
            raise NotFound(f"key not found: {key_id}")
        if current.revoked_at is None:
            current = KeyRecord(
                current.id,
                current.project_id,
                current.name,
                current.prefix,
                current.allowed_api_ids,
                datetime.now(UTC),
            )
            self.rows[key_id] = current
        return current


class FakeApis:
    """In-memory ApisPort: versioned APIs keyed by id, upserted by slug."""

    def __init__(self, projects: FakeProjects | None = None) -> None:
        self._projects = projects
        self.rows: dict[str, StoredApi] = {}
        self.reasons: builtins.list[str | None] = []

    async def put_version(
        self,
        project_id: str,
        api: ApiDependency,
        *,
        author_id: str | None = None,
        reason: str | None = None,
        api_id: str | None = None,
    ) -> tuple[str, int]:
        if self._projects is not None and project_id not in self._projects.rows:
            raise NotFound(f"unknown project: {project_id}")
        if api_id is None:
            api_id = next(
                (
                    r.id
                    for r in self.rows.values()
                    if r.project_id == project_id and r.config.slug == api.slug
                ),
                None,
            )
        elif api_id not in self.rows or self.rows[api_id].project_id != project_id:
            raise NotFound(f"api not found: {api_id}")
        elif any(
            r.project_id == project_id and r.config.slug == api.slug and r.id != api_id
            for r in self.rows.values()
        ):
            raise Conflict(f"api slug already exists in project: {api.slug}")
        previous = self.rows.get(api_id) if api_id else None
        api_id = api_id or new_id("api")
        version = previous.version + 1 if previous else 1
        self.rows[api_id] = StoredApi(api_id, project_id, version, api, datetime.now(UTC))
        self.reasons.append(reason)
        return api_id, version

    async def get_by_id(self, api_id: str) -> StoredApi | None:
        return self.rows.get(api_id)

    async def list_stored(self, project_id: str) -> builtins.list[StoredApi]:
        return [r for r in self.rows.values() if r.project_id == project_id]

    async def delete(self, api_id: str) -> None:
        if self.rows.pop(api_id, None) is None:
            raise NotFound(f"api not found: {api_id}")


class FakeSecrets:
    """In-memory SecretsPort that keeps plaintext (tests only)."""

    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    async def add(self, box: AesGcmSecretBox, plaintext: str) -> str:
        secret_id = new_id("sec")
        self.rows[secret_id] = plaintext
        return secret_id

    async def reveal(self, box: AesGcmSecretBox, secret_id: str) -> str | None:
        return self.rows.get(secret_id)

    async def delete(self, secret_id: str) -> None:
        self.rows.pop(secret_id, None)


ADMIN_TOKEN = "s3cret-admin-token-" + "x" * 20
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def fake_services() -> Services:
    """A Services bundle of fakes whose keys and APIs check their project like the repositories."""
    projects = FakeProjects()
    apis = FakeApis(projects)
    return Services(
        projects=projects,
        keys=FakeKeys(projects, apis),
        apis=apis,
        secrets=FakeSecrets(),
        secret_box=AesGcmSecretBox(bytes(range(32))),
        publisher=RecordingPublisher(),
    )
