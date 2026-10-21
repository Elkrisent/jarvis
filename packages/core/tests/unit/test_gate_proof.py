import pytest

pytestmark = pytest.mark.unit


def test_deliberately_failing_to_prove_the_merge_gate() -> None:
    """Throwaway (M0.4 proof). This PR is closed unmerged."""
    assert 1 == 2
