from __future__ import annotations

import re

import pytest
from jarvis_api.db.config_source import api_from_config_json, config_json_from_api
from jarvis_api.db.ids import new_id
from jarvis_api.db.migrate import to_async_url
from jarvis_core.config import ApiDependency, Credential, Route

pytestmark = pytest.mark.unit


def test_new_id_has_the_prefix_and_is_unique() -> None:
    first, second = new_id("prj"), new_id("prj")
    assert re.fullmatch(r"prj_[0-9a-f]{32}", first)
    assert first != second


@pytest.mark.parametrize("scheme", ["postgresql", "postgres", "postgresql+psycopg"])
def test_to_async_url_selects_the_asyncpg_driver(scheme: str) -> None:
    url = to_async_url(f"{scheme}://jarvis:s3cret@db:5432/jarvis")
    assert url == "postgresql+asyncpg://jarvis:s3cret@db:5432/jarvis"


def test_to_async_url_keeps_a_percent_encoded_password_intact() -> None:
    url = to_async_url("postgresql://jarvis:p%40ss%2Fword@db/jarvis")
    assert url == "postgresql+asyncpg://jarvis:p%40ss%2Fword@db/jarvis"


def test_api_config_round_trips_through_json() -> None:
    api = ApiDependency(
        slug="weather",
        name="Weather",
        base_url="https://api.weather.example",
        credential=Credential(mode="bearer", secret_ref="sec_1"),
        routes=(Route(pattern="/v1/forecast", cache_ttl_s=60),),
    )
    assert api_from_config_json(config_json_from_api(api)) == api


def test_stored_config_holds_a_secret_reference_never_a_secret() -> None:
    api = ApiDependency(
        slug="weather",
        name="Weather",
        base_url="https://api.weather.example",
        credential=Credential(mode="bearer", secret_ref="sec_1"),
    )
    assert config_json_from_api(api)["credential"]["secret_ref"] == "sec_1"
