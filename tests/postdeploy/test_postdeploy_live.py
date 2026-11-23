"""Post-deploy verification against production (https://<domain>).

EXPECTED_RELEASE (set by `cicd.release verify-or-rollback`) is the release that must be live.
Needs AWS read access for the Block Public Access check.
"""
from __future__ import annotations
import json
import os

import pytest

from tools import live_checks


@pytest.fixture(scope="module")
def call(cfg):
    return live_checks.direct_call(cfg)


@pytest.mark.parametrize("check", live_checks.CHECKS, ids=lambda c: c.__name__)
def test_live(check, call, cfg):
    results = check(call, cfg, os.environ.get("EXPECTED_RELEASE") or None)
    failed = [r for r in results if not r["passed"]]
    assert not failed, json.dumps(failed, indent=2, default=str)[:4000]
