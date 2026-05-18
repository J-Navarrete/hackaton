"""Thin synchronous wrapper around the MiniMax chat completions API.

Uses only stdlib (urllib) — no extra dependencies needed.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import urllib.request
from typing import Any

_BASE_URL = "https://api.minimaxi.chat/v1"
DEFAULT_MODEL = "MiniMax-M2.7-highspeed"
_SSL_CTX = ssl.create_default_context()


def _api_key() -> str:
    key = os.environ.get("MINIMAX_API_KEY", "")
    if not key:
        raise ValueError("MINIMAX_API_KEY no configurada en .env")
    return key


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def to_openai_tool(anthropic_tool: dict) -> dict:
    """Convert Anthropic tool format → OpenAI/MiniMax function format."""
    return {
        "type": "function",
        "function": {
            "name": anthropic_tool["name"],
            "description": anthropic_tool.get("description", ""),
            "parameters": anthropic_tool.get("input_schema", {}),
        },
    }


def to_openai_tool_choice(name: str) -> dict:
    """Convert Anthropic forced tool_choice → OpenAI format."""
    return {"type": "function", "function": {"name": name}}


def chat(
    messages: list[dict],
    *,
    system: str | None = None,
    tools: list[dict] | None = None,
    tool_choice: dict | None = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 3000,
) -> dict:
    """Synchronous chat completion. Returns the message dict from choices[0].

    tools/tool_choice must already be in OpenAI format.
    Raises urllib.error.HTTPError or ValueError on failure.
    """
    full_messages: list[dict] = []
    if system:
        full_messages.append({"role": "system", "content": system})
    full_messages.extend(messages)

    payload: dict[str, Any] = {
        "model": model,
        "messages": full_messages,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
    if tool_choice:
        payload["tool_choice"] = tool_choice

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{_BASE_URL}/chat/completions",
        data=data,
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, context=_SSL_CTX, timeout=120) as r:
        resp = json.loads(r.read().decode("utf-8"))
    return resp["choices"][0]["message"]


def get_text(msg: dict) -> str:
    """Extract clean text from a message, stripping <think> tags."""
    return _strip_think(msg.get("content") or "")


def get_tool_args(msg: dict, tool_name: str) -> dict | None:
    """Extract parsed arguments dict for a given tool call, or None."""
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        if fn.get("name") == tool_name:
            return json.loads(fn.get("arguments", "{}"))
    return None
