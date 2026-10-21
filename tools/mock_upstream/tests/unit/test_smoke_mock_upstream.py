import mock_upstream
import pytest


@pytest.mark.unit
def test_package_importable() -> None:
    assert mock_upstream.__version__ == "0.0.0"
