import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def isolated_registry():
    """Let a test register throwaway tools without leaking them into other tests."""
    from ai import security

    saved = dict(security.REGISTRY)
    yield security.REGISTRY
    security.REGISTRY.clear()
    security.REGISTRY.update(saved)
