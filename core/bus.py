"""Message bus and run logger.

Every agent-to-agent message, tool call, LLM call and human approval is appended
as one JSON line to files under runs/<run_id>/. These files are the execution logs.
"""
from __future__ import annotations
import json
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.redact import redact


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class Message:
    id: str
    timestamp: str
    sender: str
    recipient: str
    type: str          # task | report | proposal | approval | result
    payload: dict
    in_reply_to: str | None = None


class RunLog:
    def __init__(self, root: Path, label: str):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_id = f"{stamp}-{label}"
        self.dir = root / self.run_id
        (self.dir / "agents").mkdir(parents=True, exist_ok=True)
        (self.dir / "evidence").mkdir(exist_ok=True)
        self.seq = 0

    def _append(self, name: str, record: dict) -> None:
        self.seq += 1
        record = {"seq": self.seq, **record}
        line = redact(json.dumps(record, default=str))
        with open(self.dir / name, "a") as f:
            f.write(line + "\n")

    def agent_log(self, agent: str, text: str) -> None:
        line = f"[{now()}] [{agent}] {text}"
        print(redact(line))
        with open(self.dir / "agents" / f"{agent}.log", "a") as f:
            f.write(redact(line) + "\n")
        with open(self.dir / "timeline.log", "a") as f:
            f.write(redact(line) + "\n")

    def tool(self, record: dict) -> None:
        self._append("tools.jsonl", {"timestamp": now(), **record})

    def llm(self, record: dict) -> None:
        self._append("llm_calls.jsonl", {"timestamp": now(), **record})

    def approval(self, record: dict) -> None:
        self._append("approvals.jsonl", {"timestamp": now(), **record})

    def evidence(self, name: str, data: Any) -> Path:
        p = self.dir / "evidence" / name
        p.write_text(redact(json.dumps(data, indent=2, default=str)))
        return p


class Bus:
    def __init__(self, log: RunLog):
        self.log = log
        self.messages: list[Message] = []

    def send(self, sender: str, recipient: str, type: str, payload: dict,
             in_reply_to: str | None = None) -> Message:
        msg = Message(id=f"msg-{uuid.uuid4().hex[:8]}", timestamp=now(), sender=sender,
                      recipient=recipient, type=type, payload=payload, in_reply_to=in_reply_to)
        self.messages.append(msg)
        self.log._append("messages.jsonl", asdict(msg))
        self.log.agent_log("bus", f"{sender} -> {recipient} [{type}] {msg.id}")
        return msg
