import jarvis_gateway
import pytest


@pytest.mark.unit
def test_package_importable() -> None:
    assert jarvis_gateway.__version__ == "0.0.0"
