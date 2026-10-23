"""API dependency endpoints with sealed provider secrets (SPEC §5.1, §5.2, §10, §11.2).

Save flow: validate, seal and store a supplied secret, ``put_version`` (commits), then publish
``config:changed``. A secret stored for a save that fails is deleted again, so none is orphaned.
A PATCH saves with ``expected_version`` (409 if another save committed since its read), and a
POST with ``create_only`` (409 for an existing slug), so concurrent saves never overwrite each
other or leave the live config pointing at a deleted secret.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, Response
from fastapi.exceptions import RequestValidationError
from jarvis_core.config import ApiDependency
from jarvis_core.errors import NotImplementedYetError
from pydantic import SecretStr, ValidationError

from jarvis_api.db.config_source import StoredApi, diff_configs
from jarvis_api.db.repositories import NotFound
from jarvis_api.http.deps import Services, ServicesDep
from jarvis_api.http.models import SECRET_STORED, ApiIn, ApiOut

router = APIRouter(tags=["apis"])
logger = logging.getLogger(__name__)

TEST_PLACEHOLDER = "API test requests through the gateway pipeline arrive in M4.10"

# Diffs of stored configs: top-level keys, secret refs excluded. ``put_version`` records it.
config_diff = diff_configs


def merge_patch(target: Any, patch: Any) -> Any:
    """Apply a JSON merge patch (RFC 7386) without modifying ``target``."""
    if not isinstance(patch, dict):
        return patch
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def _invalid(exc: ValidationError) -> RequestValidationError:
    """A body validation error with ``body``-rooted field paths and no input values."""
    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    return RequestValidationError(
        [{"type": e["type"], "loc": ("body", *e["loc"]), "msg": e["msg"]} for e in errors]
    )


async def _stored(services: Services, api_id: str) -> StoredApi:
    stored = await services.apis.get_by_id(api_id)
    if stored is None:
        raise NotFound(f"api not found: {api_id}")
    return stored


async def _seal(services: Services, secret: SecretStr | None) -> str | None:
    if secret is None:
        return None
    return await services.secrets.add(services.secret_box, secret.get_secret_value())


async def _save(
    services: Services,
    project_id: str,
    body: ApiIn,
    secret_ref: str | None,
    *,
    new_secret_ref: str | None,
    api_id: str | None = None,
    create_only: bool = False,
    expected_version: int | None = None,
) -> tuple[str, int]:
    try:
        api: ApiDependency = body.to_dependency(secret_ref)
        saved = await services.apis.put_version(
            project_id,
            api,
            reason=body.reason,
            api_id=api_id,
            create_only=create_only,
            expected_version=expected_version,
        )
    except Exception:
        if new_secret_ref is not None:
            await services.secrets.delete(new_secret_ref)
        raise
    return saved


async def _discard_secret(services: Services, secret_id: str) -> None:
    """Delete a secret the current version no longer uses. Failure only leaves an orphan row."""
    try:
        await services.secrets.delete(secret_id)
    except Exception as exc:
        logger.warning("could not delete an unused provider secret (%s)", type(exc).__name__)


@router.get("/projects/{project_id}/apis")
async def list_apis(project_id: str, services: ServicesDep) -> list[ApiOut]:
    if await services.projects.get(project_id) is None:
        raise NotFound(f"project not found: {project_id}")
    return [ApiOut.of(s) for s in await services.apis.list_stored(project_id)]


@router.post("/projects/{project_id}/apis", status_code=201)
async def create_api(project_id: str, body: ApiIn, services: ServicesDep) -> ApiOut:
    if await services.projects.get(project_id) is None:
        raise NotFound(f"project not found: {project_id}")
    secret_ref = await _seal(services, body.credential.secret)
    # create_only: an existing slug, even one created concurrently, is a 409 and not an upsert.
    api_id, version = await _save(
        services, project_id, body, secret_ref, new_secret_ref=secret_ref, create_only=True
    )
    await services.publisher.config_changed(api_id, version)
    return ApiOut.of(await _stored(services, api_id))


@router.get("/apis/{api_id}")
async def get_api(api_id: str, services: ServicesDep) -> ApiOut:
    return ApiOut.of(await _stored(services, api_id))


@router.patch(
    "/apis/{api_id}",
    openapi_extra={
        "requestBody": {
            "description": (
                "A JSON merge patch (RFC 7386) over the current config, validated as a whole. "
                "Omit credential.secret to keep the stored secret; send one to replace it."
            ),
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ApiIn"}}},
        }
    },
)
async def update_api(
    api_id: str, patch: Annotated[dict[str, Any], Body()], services: ServicesDep
) -> ApiOut:
    stored = await _stored(services, api_id)
    current = stored.config.model_dump(mode="json")
    stored_ref: str | None = current["credential"].pop("secret_ref", None)
    credential_patch = patch.get("credential", {})
    # An explicit null (for the secret or the whole credential) removes the stored secret.
    secret_cleared = credential_patch is None or (
        isinstance(credential_patch, dict)
        and "secret" in credential_patch
        and credential_patch["secret"] is None
    )
    try:
        body = ApiIn.model_validate(
            merge_patch(current, patch),
            context={SECRET_STORED: stored_ref is not None and not secret_cleared},
        )
    except ValidationError as exc:
        raise _invalid(exc) from None

    new_ref = await _seal(services, body.credential.secret)
    keep = body.credential.mode != "none" and not secret_cleared
    secret_ref = (new_ref or stored_ref) if keep else None
    # expected_version: a save that committed since our read makes this one a 409, so a stale
    # merge never overwrites it and never resurrects a secret that save already deleted.
    _, version = await _save(
        services,
        stored.project_id,
        body,
        secret_ref,
        new_secret_ref=new_ref,
        api_id=api_id,
        expected_version=stored.version,
    )
    await services.publisher.config_changed(api_id, version)
    if stored_ref is not None and stored_ref != secret_ref:
        await _discard_secret(services, stored_ref)
    return ApiOut.of(await _stored(services, api_id))


@router.delete("/apis/{api_id}", status_code=204)
async def delete_api(api_id: str, services: ServicesDep) -> Response:
    """Delete an API with its versions and its sealed secret."""
    stored = await _stored(services, api_id)
    await services.apis.delete(api_id)
    if stored.config.credential.secret_ref is not None:
        await _discard_secret(services, stored.config.credential.secret_ref)
    return Response(status_code=204)


@router.post("/apis/{api_id}/test", status_code=501)
async def test_api(api_id: str, services: ServicesDep) -> None:
    """Placeholder: sending a test request through the gateway pipeline arrives in M4.10."""
    await _stored(services, api_id)
    raise NotImplementedYetError(TEST_PLACEHOLDER)
