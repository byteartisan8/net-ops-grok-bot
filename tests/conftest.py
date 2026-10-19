"""Fixtures for the contract-level tests (owner: 01-foundation). Helpers live in netsim_testlib."""

from __future__ import annotations

import os
from collections.abc import Callable
from subprocess import CompletedProcess

import pytest
from netsim_testlib import docker_available, run_yanglint


@pytest.fixture(scope="session")
def yanglint() -> Callable[..., CompletedProcess[str]]:
    """Runs the pinned yanglint; skips when neither Docker nor $YANGLINT is available."""
    if not os.environ.get("YANGLINT") and not docker_available():
        pytest.skip("yanglint needs Docker (tools/yanglint.sh builds a pinned image) or YANGLINT=/path/to/yanglint")
    return run_yanglint
