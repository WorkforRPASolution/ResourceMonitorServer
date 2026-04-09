"""Smoke test to verify project skeleton + pytest + asyncio setup."""
import pytest


@pytest.mark.unit
def test_skeleton_imports_src():
    """src package is importable (pythonpath configured)."""
    import src  # noqa: F401


@pytest.mark.unit
def test_skeleton_mock_fixture(mock_infra):
    """mock_infra fixture is available."""
    assert mock_infra.es is not None
    assert mock_infra.mongo is not None
    assert mock_infra.zk is not None


@pytest.mark.unit
async def test_skeleton_asyncio_auto_mode():
    """asyncio_mode=auto allows direct async test functions."""
    assert True
