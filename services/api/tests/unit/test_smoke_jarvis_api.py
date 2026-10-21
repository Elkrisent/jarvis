import jarvis_api
import pytest


@pytest.mark.unit
def test_package_importable() -> None:
    assert jarvis_api.__version__ == "0.0.0"
