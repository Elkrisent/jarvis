import re

import pytest
from jarvis_gateway.request_id import new_request_id

pytestmark = pytest.mark.unit

ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


@pytest.mark.req("REQ-4.3-1")
def test_shape_is_a_26_char_crockford_ulid() -> None:
    assert ULID.match(new_request_id())


def test_later_time_sorts_later() -> None:
    assert new_request_id(now_ms=1_000) < new_request_id(now_ms=2_000)


def test_time_prefix_encodes_the_millisecond_clock() -> None:
    assert new_request_id(now_ms=0)[:10] == "0000000000"


def test_ids_are_unique() -> None:
    assert len({new_request_id() for _ in range(1000)}) == 1000
