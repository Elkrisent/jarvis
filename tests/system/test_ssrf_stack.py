"""The SSRF save-time check on the real api container (M1.7): compose wiring and messages."""

import httpx
import pytest

from tests.conftest import ADMIN_TOKEN, API_URL

pytestmark = [pytest.mark.system, pytest.mark.usefixtures("compose_stack")]


def _body(slug: str, base_url: str) -> dict[str, object]:
    return {"slug": slug, "name": slug, "base_url": base_url}


@pytest.mark.req("REQ-4.13-1", "REQ-4.13-3", "REQ-4.13-4", "REQ-4.13-5", "REQ-4.13-6")
def test_blocked_upstreams_are_rejected_and_the_allowlisted_one_saves() -> None:
    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    with httpx.Client(base_url=f"{API_URL}/api/v1", headers=admin, timeout=10) as api:
        project = api.post("/projects", json={"name": "Ssrf", "slug": "ssrf-stack-test"})
        assert project.status_code == 201, project.text
        pid = project.json()["id"]
        try:
            for index, url in enumerate(
                ["https://169.254.169.254/latest", "https://10.0.0.5", "http://example.com"]
            ):
                response = api.post(f"/projects/{pid}/apis", json=_body(f"bad-{index}", url))
                assert response.status_code == 422, (url, response.text)
                detail = response.json()["detail"]
                assert detail[0]["loc"] == ["body", "base_url"]
                assert detail[0]["msg"] == "destination is not allowed"
            # JARVIS_SSRF_ALLOWLIST=mock-upstream in the test override: plain http to it is fine.
            ok = api.post(f"/projects/{pid}/apis", json=_body("mock", "http://mock-upstream:8080"))
            assert ok.status_code == 201, ok.text
        finally:
            api.delete(f"/projects/{pid}")
