"""Thin wrapper over the OpenAI Chat Completions API that logs every call."""
from __future__ import annotations
import json
import time
from openai import OpenAI

from core.bus import RunLog


class LLM:
    def __init__(self, model: str, log: RunLog):
        self.client = OpenAI()          # reads OPENAI_API_KEY from the environment
        self.model = model
        self.log = log

    def chat(self, agent: str, messages: list[dict], tools: list[dict] | None = None,
             tool_choice=None, json_mode: bool = False):
        kwargs: dict = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "required"
            kwargs["parallel_tool_calls"] = False
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        t0 = time.time()
        resp = self.client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        self.log.llm({
            "agent": agent,
            "model": resp.model,
            "latency_s": round(time.time() - t0, 2),
            "usage": resp.usage.model_dump() if resp.usage else None,
            "last_input": messages[-1] if messages else None,
            "output_text": msg.content,
            "tool_calls": [{"name": tc.function.name, "arguments": tc.function.arguments}
                           for tc in (msg.tool_calls or [])],
        })
        return msg

    def json(self, agent: str, system: str, user: dict) -> dict:
        msg = self.chat(agent, [{"role": "system", "content": system},
                                {"role": "user", "content": json.dumps(user)}], json_mode=True)
        return json.loads(msg.content)
