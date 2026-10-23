"""JARVIS key endpoints (SPEC §4.2, §10). The full key is returned once, at creation."""

from __future__ import annotations

from fastapi import APIRouter, Response
from fastapi.exceptions import RequestValidationError
from jarvis_core.keys import generate_jarvis_key

from jarvis_api.db.repositories import NotFound
from jarvis_api.http.deps import ServicesDep
from jarvis_api.http.models import KeyCreated, KeyIn, KeyOut

router = APIRouter(tags=["keys"])

_FOREIGN_APIS = "allowed_api_ids must name APIs of this project"


@router.get("/projects/{project_id}/keys")
async def list_keys(project_id: str, services: ServicesDep) -> list[KeyOut]:
    if await services.projects.get(project_id) is None:
        raise NotFound(f"project not found: {project_id}")
    return [KeyOut.of(r) for r in await services.keys.list(project_id)]


@router.post("/projects/{project_id}/keys", status_code=201)
async def create_key(project_id: str, body: KeyIn, services: ServicesDep) -> KeyCreated:
    generated = generate_jarvis_key()
    try:
        record = await services.keys.create(project_id, body.name, body.allowed_api_ids, generated)
    except ValueError:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("body", "allowed_api_ids"), "msg": _FOREIGN_APIS}]
        ) from None
    return KeyCreated.model_validate({**KeyOut.fields_of(record), "key": generated.full})


@router.delete("/keys/{key_id}", status_code=204)
async def revoke_key(key_id: str, services: ServicesDep) -> Response:
    """Revoke a key: ``revoked_at`` is set and the row is kept. Revoking again is a no-op."""
    await services.keys.revoke(key_id)
    return Response(status_code=204)
