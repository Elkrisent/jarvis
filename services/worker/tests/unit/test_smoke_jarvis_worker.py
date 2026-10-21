import jarvis_worker
import pytest


@pytest.mark.unit
def test_package_importable() -> None:
    assert jarvis_worker.__version__ == "0.0.0"
