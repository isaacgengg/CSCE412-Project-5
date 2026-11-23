"""Shared fixtures. Pre-deploy tests run against the BUILT site (SITE_DIR, default build/site):
run `python -m cicd.release build` first. Generated tests in tests/generated/ use these too."""
from __future__ import annotations
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import load_config  # noqa: E402


@pytest.fixture(scope="session")
def cfg():
    return load_config()


@pytest.fixture(scope="session")
def site_dir() -> Path:
    d = Path(os.environ.get("SITE_DIR", ROOT / "build" / "site"))
    if not d.is_dir():
        pytest.exit(f"{d} not found; run `python -m cicd.release build` first", returncode=2)
    return d


@pytest.fixture(scope="session")
def expected_sha() -> str:
    from cicd.release import expected_sha
    return expected_sha()
