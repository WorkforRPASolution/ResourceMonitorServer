"""Guards for v2 constant cleanup."""
import pytest

from src.config import constants

pytestmark = pytest.mark.unit


def test_coll_rule_removed():
    """v2 merges rules into RESOURCE_MONITOR_PROFILE — the separate RULE
    collection constant must be gone (it was never imported anywhere)."""
    assert not hasattr(constants, "COLL_RULE")


def test_coll_profile_present():
    assert constants.COLL_PROFILE == "RESOURCE_MONITOR_PROFILE"
