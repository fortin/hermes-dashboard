"""Shared pytest fixtures for dashboard tests."""

from __future__ import annotations

import pytest

from app.localtime import configure_timezone


@pytest.fixture(autouse=True)
def _pin_utc_timezone() -> None:
    configure_timezone("UTC")
