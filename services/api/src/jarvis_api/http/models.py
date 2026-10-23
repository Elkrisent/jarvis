"""Request and response models of /api/v1. Every input model forbids unknown fields."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from jarvis_core.config import ApiDependency, HeaderName, Slug
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationInfo,
    model_validator,
)

from jarvis_api.db.config_source import StoredApi
from jarvis_api.db.repositories import KeyRecord, ProjectRecord

Name = Annotated[str, Field(min_length=1, max_length=200)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectIn(_In):
    name: Name
    slug: Slug


class ProjectPatch(_In):
    name: Name | None = None
    slug: Slug | None = None


class ProjectOut(BaseModel):
    id: str
    name: str
    slug: str
    created_at: datetime

    @classmethod
    def of(cls, record: ProjectRecord) -> ProjectOut:
        return cls(id=record.id, name=record.name, slug=record.slug, created_at=record.created_at)


class KeyIn(_In):
    name: Name
    allowed_api_ids: list[str] = []


class KeyOut(BaseModel):
    """A JARVIS key as listed: the display prefix only, never the key or its hash."""

    id: str
    project_id: str
    name: str
    prefix: str
    allowed_api_ids: list[str]
    revoked_at: datetime | None

    @classmethod
    def fields_of(cls, record: KeyRecord) -> dict[str, object]:
        return {
            "id": record.id,
            "project_id": record.project_id,
            "name": record.name,
            "prefix": record.prefix,
            "allowed_api_ids": list(record.allowed_api_ids),
            "revoked_at": record.revoked_at,
        }

    @classmethod
    def of(cls, record: KeyRecord) -> KeyOut:
        return cls.model_validate(cls.fields_of(record))


class KeyCreated(KeyOut):
    """The creation response: the only place the full key is ever returned."""

    key: str


# Validation context flag: the API already has a stored secret, so omitting one keeps it.
SECRET_STORED = "secret_stored"

CredentialMode = Literal["bearer", "header", "query", "none"]


class CredentialIn(_In):
    """A credential as sent by a client. ``secret`` is write-only plaintext; refs never come in."""

    mode: CredentialMode = "none"
    header_name: HeaderName | None = None
    param_name: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    secret: Annotated[SecretStr, Field(min_length=1)] | None = None  # writeOnly in OpenAPI

    @model_validator(mode="after")
    def _check_mode_fields(self, info: ValidationInfo) -> Self:
        stored = bool(info.context and info.context.get(SECRET_STORED))
        if self.mode == "none":
            if self.secret is not None:
                raise ValueError("secret is not allowed when mode is 'none'")
        elif self.secret is None and not stored:
            raise ValueError(f"secret is required when mode is '{self.mode}'")
        # The same rules as jarvis_core.config.Credential, so errors point at the request body.
        if self.mode == "header" and self.header_name is None:
            raise ValueError("header_name is required when mode is 'header'")
        if self.mode == "query" and self.param_name is None:
            raise ValueError("param_name is required when mode is 'query'")
        if self.mode != "header" and self.header_name is not None:
            raise ValueError("header_name is only allowed when mode is 'header'")
        if self.mode != "query" and self.param_name is not None:
            raise ValueError("param_name is only allowed when mode is 'query'")
        return self


class CredentialOut(BaseModel):
    """A credential as shown: whether a secret is stored, never the secret or its reference."""

    mode: CredentialMode
    header_name: str | None
    param_name: str | None
    has_secret: bool


class ApiIn(ApiDependency):
    """Every ``ApiDependency`` field, with ``CredentialIn`` in place of the stored credential."""

    credential: CredentialIn = CredentialIn()  # type: ignore[assignment]
    reason: Annotated[str, Field(max_length=500)] | None = None

    def to_dependency(self, secret_ref: str | None) -> ApiDependency:
        """The config to store: the plaintext secret replaced by the sealed secret's id."""
        data = self.model_dump(mode="json", exclude={"credential", "reason"})
        credential = self.credential
        data["credential"] = {
            "mode": credential.mode,
            "header_name": credential.header_name,
            "param_name": credential.param_name,
            "secret_ref": secret_ref,
        }
        return ApiDependency.model_validate(data)


class ApiOut(ApiDependency):
    """The current version of an API, with ``credential.has_secret`` in place of the secret."""

    model_config = ConfigDict(extra="ignore")

    id: str
    project_id: str
    version: int
    created_at: datetime
    credential: CredentialOut  # type: ignore[assignment]

    @classmethod
    def of(cls, stored: StoredApi) -> ApiOut:
        data = stored.config.model_dump(mode="json", exclude={"credential"})
        credential = stored.config.credential
        return cls.model_validate(
            {
                **data,
                "id": stored.id,
                "project_id": stored.project_id,
                "version": stored.version,
                "created_at": stored.created_at,
                "credential": {
                    "mode": credential.mode,
                    "header_name": credential.header_name,
                    "param_name": credential.param_name,
                    "has_secret": credential.secret_ref is not None,
                },
            }
        )
