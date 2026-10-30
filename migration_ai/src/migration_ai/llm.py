"""Client for the LLM served on OpenShift AI (KServe + vLLM, OpenAI-compatible API).

The model is used for reasoning and writing only. Every action goes through
explicit Python *tools* with guardrails, so the model can never run arbitrary
commands. When no model is configured or reachable, callers fall back to
deterministic behaviour, which keeps every pipeline usable without a GPU.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from migration_ai.config import LLMSettings

log = logging.getLogger(__name__)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Any]

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


def _strip_thinking(text: str) -> str:
    """Remove <think>...</think> blocks some models (Qwen3) emit."""
    if not text:
        return ""
    while "<think>" in text and "</think>" in text:
        start = text.index("<think>")
        end = text.index("</think>") + len("</think>")
        text = text[:start] + text[end:]
    return text.strip()


class LLM:
    def __init__(self, settings: LLMSettings):
        self.settings = settings
        self._client = None

    @property
    def configured(self) -> bool:
        return self.settings.configured

    def client(self):
        if self._client is None:
            import httpx
            from openai import OpenAI

            self._client = OpenAI(
                base_url=self.settings.base_url.rstrip("/"),
                api_key=self.settings.api_key or "not-needed",
                timeout=self.settings.timeout,
                max_retries=0,
                http_client=httpx.Client(verify=self.settings.verify_tls, timeout=self.settings.timeout),
            )
        return self._client

    def available(self) -> bool:
        if not self.configured:
            return False
        try:
            ids = [m.id for m in self.client().models.list().data]
            if self.settings.model not in ids:
                log.warning("model %s not served by %s (served: %s)", self.settings.model,
                            self.settings.base_url, ids)
                return False
            return True
        except Exception as exc:
            log.warning("LLM endpoint not reachable: %s", exc)
            return False

    def chat(self, messages: list[dict], tools: list[Tool] | None = None,
             max_tokens: int | None = None, timeout: float | None = None) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.settings.temperature,
            "max_tokens": max_tokens or self.settings.max_tokens,
        }
        if tools:
            kwargs["tools"] = [t.schema() for t in tools]
            kwargs["tool_choice"] = "auto"
        if self.settings.extra_body:
            kwargs["extra_body"] = self.settings.extra_body
        client = self.client().with_options(timeout=timeout) if timeout else self.client()
        start = time.monotonic()
        resp = client.chat.completions.create(**kwargs)
        usage = getattr(resp, "usage", None)
        log.info("LLM call: %.1fs, prompt %s tokens, answer %s tokens (max %s)", time.monotonic() - start,
                 getattr(usage, "prompt_tokens", "?"), getattr(usage, "completion_tokens", "?"), kwargs["max_tokens"])
        return resp.choices[0].message

    def complete(self, system: str, user: str, max_tokens: int | None = None, timeout: float | None = None) -> str:
        msg = self.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                        max_tokens=max_tokens, timeout=timeout)
        return _strip_thinking(msg.content or "")

    def run_tools(self, system: str, user: str, tools: list[Tool], max_turns: int = 30,
                  on_event: Callable[[dict], None] | None = None,
                  history: list[dict] | None = None) -> tuple[str, list[dict]]:
        """Classic tool-calling loop. Returns (final answer, transcript)."""
        by_name = {t.name: t for t in tools}
        messages: list[dict] = [{"role": "system", "content": system}, *(history or []),
                                {"role": "user", "content": user}]
        for turn in range(max_turns):
            msg = self.chat(messages, tools)
            calls = msg.tool_calls or []
            content = _strip_thinking(msg.content or "")
            if not calls:
                if on_event:
                    on_event({"type": "answer", "turn": turn, "content": content})
                messages.append({"role": "assistant", "content": content})
                return content, messages
            messages.append({
                "role": "assistant", "content": msg.content or "",
                "tool_calls": [{"id": c.id, "type": "function",
                                "function": {"name": c.function.name, "arguments": c.function.arguments}}
                               for c in calls],
            })
            for call in calls:
                name = call.function.name
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if on_event:
                    on_event({"type": "tool_call", "turn": turn, "tool": name, "args": args})
                if name not in by_name:
                    result: Any = {"error": f"unknown tool {name}", "available": list(by_name)}
                else:
                    try:
                        result = by_name[name].fn(**args)
                    except TypeError as exc:
                        result = {"error": f"bad arguments for {name}: {exc}"}
                    except Exception as exc:
                        log.exception("tool %s failed", name)
                        result = {"error": f"{type(exc).__name__}: {exc}"}
                text = result if isinstance(result, str) else json.dumps(result, default=str)
                if on_event:
                    on_event({"type": "tool_result", "turn": turn, "tool": name, "result": text[:2000]})
                messages.append({"role": "tool", "tool_call_id": call.id, "content": text[:12000]})
        return "Stopped: maximum number of reasoning steps reached.", messages
