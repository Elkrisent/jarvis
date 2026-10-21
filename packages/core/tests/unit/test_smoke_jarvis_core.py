import jarvis_core
import pytest


@pytest.mark.unit
def test_package_importable() -> None:
    assert jarvis_core.__version__ == "0.0.0"
