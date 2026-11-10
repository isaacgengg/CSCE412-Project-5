"""Base agent: an LLM that reasons over a task and calls an allowlisted set of tools.

Flow for one agent run:
  1. The task (a JSON payload from the bus) is given to the model with the agent's
     system prompt and tool schemas.
  2. The model must call a tool every turn. Python executes the tool, logs the call
     and result, and feeds the result back.
  3. Tools marked `consequential` pause for human approval before running.
  4. The model ends by calling `submit_report`, whose arguments are the agent's
     structured output. That report is what gets sent to the next agent.
"""
from __future__ import annotations
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from core.bus import Bus, RunLog
from core.config import Config
from core.llm import LLM

MAX_RESULT_CHARS = 6000


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Any]
    consequential: bool = False   # requires human approval

    def spec(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


def no_params() -> dict:
    return {"type": "object", "properties": {}, "additionalProperties": False}


class Approver:
    """Human-in-the-loop gate. Denies automatically when there is no terminal."""

    def __init__(self, log: RunLog, auto_deny: bool = False):
        self.log = log
        self.auto_deny = auto_deny

    def ask(self, agent: str, tool: str, args: dict, rationale: str) -> bool:
        print("\n" + "=" * 70)
        print(f"APPROVAL REQUIRED  agent={agent}  tool={tool}")
        print(f"arguments: {json.dumps(args)}")
        print(f"rationale: {rationale or '(none given)'}")
        print("=" * 70)
        if self.auto_deny or not sys.stdin.isatty():
            answer = "n"
            print("No interactive terminal or --auto-deny set: denying.")
        else:
            answer = input("Apply this change? [y/N] ").strip().lower()
        approved = answer in ("y", "yes")
        self.log.approval({"agent": agent, "tool": tool, "arguments": args,
                           "rationale": rationale, "decision": "approved" if approved else "denied"})
        return approved


@dataclass
class Context:
    cfg: Config
    log: RunLog
    bus: Bus
    llm: LLM
    approver: Approver


@dataclass
class Agent:
    name: str
    system_prompt: str
    tools: list[Tool]
    report_schema: dict
    ctx: Context
    tool_index: dict = field(init=False)

    def __post_init__(self):
        self.tool_index = {t.name: t for t in self.tools}

    # ---- tool execution (also used directly for deterministic steps) ----
    def invoke(self, tool_name: str, args: dict | None = None, rationale: str = "",
               requested_by: str = "llm") -> Any:
        args = args or {}
        tool = self.tool_index[tool_name]
        record = {"agent": self.name, "tool": tool_name, "arguments": args,
                  "requested_by": requested_by, "consequential": tool.consequential}
        if tool.consequential and not self.ctx.approver.ask(self.name, tool_name, args, rationale):
            result = {"status": "denied_by_human"}
            self.ctx.log.tool({**record, "status": "denied", "result": result})
            self.ctx.log.agent_log(self.name, f"tool {tool_name} DENIED by human")
            return result
        t0 = time.time()
        try:
            result = tool.fn(**args)
            status = "ok"
        except Exception as e:  # tool errors go back to the model as data
            result = {"error": type(e).__name__, "message": str(e)}
            status = "error"
        self.ctx.log.tool({**record, "status": status,
                           "duration_s": round(time.time() - t0, 2), "result": result})
        self.ctx.log.agent_log(self.name, f"tool {tool_name}({json.dumps(args)}) -> {status}")
        return result

    # ---- LLM loop ----
    def run(self, task: dict) -> dict:
        log = self.ctx.log
        log.agent_log(self.name, f"received task: {json.dumps(task)[:300]}")
        submit = Tool("submit_report", "Submit your final structured report. Call this once, last.",
                      self.report_schema, lambda **kw: kw)
        specs = [t.spec() for t in self.tools] + [submit.spec()]
        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt +
             "\n\nYou must call exactly one tool per turn. Base every claim on tool results, "
             "never on assumptions. When done, call submit_report."},
            {"role": "user", "content": json.dumps(task, default=str)},
        ]
        max_steps = self.ctx.cfg.max_agent_steps
        for step in range(max_steps):
            force = {"type": "function", "function": {"name": "submit_report"}} \
                if step == max_steps - 1 else "required"
            msg = self.ctx.llm.chat(self.name, messages, specs, tool_choice=force)
            if not msg.tool_calls:
                messages.append({"role": "assistant", "content": msg.content or ""})
                messages.append({"role": "user", "content": "Call a tool or submit_report."})
                continue
            tc = msg.tool_calls[0]
            messages.append({"role": "assistant", "content": msg.content, "tool_calls": [{
                "id": tc.id, "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments}}]})
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            name = tc.function.name
            if name == "submit_report":
                log.agent_log(self.name, f"report: {json.dumps(args)[:400]}")
                return args
            if name not in self.tool_index:
                result = {"error": f"unknown tool {name}"}
            else:
                rationale = args.pop("rationale", "") if isinstance(args, dict) else ""
                result = self.invoke(name, args, rationale=rationale)
            content = json.dumps(result, default=str)
            if len(content) > MAX_RESULT_CHARS:
                content = content[:MAX_RESULT_CHARS] + "...[truncated]"
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})
        raise RuntimeError(f"{self.name} did not submit a report")


def with_rationale(schema: dict) -> dict:
    """Adds a required `rationale` field so the model must justify consequential actions."""
    s = json.loads(json.dumps(schema))
    s.setdefault("properties", {})["rationale"] = {
        "type": "string", "description": "Why this change is needed, shown to the human approver."}
    s["required"] = list(dict.fromkeys([*s.get("required", []), "rationale"]))
    return s
