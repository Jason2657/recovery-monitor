"""Shared test fixtures.

The self-correction loop mutates the process-global ``RISK_THRESHOLD``. Reset it
before every test so the correction-loop side effect can't couple tests together.
"""

from __future__ import annotations

import pytest

from recovery_monitor import config


@pytest.fixture(autouse=True)
def _reset_risk_threshold():
    config.set_risk_threshold(config.DEFAULT_RISK_THRESHOLD)
    yield
    config.set_risk_threshold(config.DEFAULT_RISK_THRESHOLD)
