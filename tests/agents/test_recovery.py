"""Rollback is approval-gated and its outcome is recorded from evidence, not from the model."""
import pytest

import orchestrator
from agents import recovery_agent
from core.agent import Approver, Context
from core.bus import Bus, RunLog
from core.config import load_config
from tools.aws_tools import AWS
from tools.registry import FIX_ACTIONS, aws_write_tools

A, B = "a" * 40, "b" * 40


@pytest.fixture
def ctx(tmp_path):
    log = RunLog(tmp_path, "test")
    return Context(cfg=load_config(), log=log, bus=Bus(log), llm=None, approver=Approver(log, auto_deny=True))


def test_upload_tool_is_retired_and_rollback_is_consequential():
    assert "s3_sync_site" not in FIX_ACTIONS
    tools = aws_write_tools(AWS(load_config()))       # constructing AWS makes no network calls
    assert all(t.consequential for t in tools.values())
    assert "rollback_to_known_good" in tools


def test_denied_approval_never_calls_rollback(ctx, monkeypatch):
    called = []
    monkeypatch.setattr("cicd.release.rollback_to_known_good", lambda: called.append(1))
    agent = recovery_agent.build_fix(ctx, AWS(ctx.cfg), "rollback_to_known_good")
    assert agent.invoke("rollback_to_known_good", {}, rationale="test") == {"status": "denied_by_human"}
    assert called == []


class FakeAgent:
    def __init__(self, effect, report):
        self.effect, self.report = effect, report

    def run(self, task):
        self.effect()
        return self.report


def run_rollback(ctx, monkeypatch, *, switch: bool, verified: bool):
    live = {"id": A}
    records = []
    monkeypatch.setattr(orchestrator.release, "current_release", lambda: live["id"])
    monkeypatch.setattr(orchestrator.release, "record", lambda rid, status, **kw: records.append((rid, status, kw)))
    monkeypatch.setattr(orchestrator.recovery_agent, "build_rollback", lambda *a: FakeAgent(
        lambda: live.update(id=B) if switch else None,
        {"status": "rolled_back" if switch else "not_needed", "actions_taken": [], "notes": ""}))
    monkeypatch.setattr(orchestrator, "step_verify", lambda *a, **kw: {"all_passed": verified})
    return orchestrator.cmd_rollback(ctx, None, None), records


def test_rollback_records_outcome_after_verification(ctx, monkeypatch):
    ok, records = run_rollback(ctx, monkeypatch, switch=True, verified=True)
    assert ok and records == [(A, "rolled_back", {"rolled_back_to": B, "by": "recovery_coordinator",
                                                  "run_id": ctx.log.run_id})]


def test_rollback_failure_is_recorded(ctx, monkeypatch):
    ok, records = run_rollback(ctx, monkeypatch, switch=True, verified=False)
    assert not ok and records[0][1] == "rollback_failed"


def test_no_switch_means_nothing_recorded(ctx, monkeypatch):
    ok, records = run_rollback(ctx, monkeypatch, switch=False, verified=True)
    assert ok and records == []
