"""Bounded async Chat Completions adapter for configured text models."""

from __future__ import annotations

import asyncio
import json
import ssl
from urllib.parse import urlsplit

import httpx


class ChatCompletionsClient:
    def __init__(self, base_url: str, api_key: str, model: str, ca_file: str | None = None, transport=None):
        parts = urlsplit(base_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("TEXT_API_BASE_URL must be an HTTP(S) base URL without credentials/query/fragment")
        if not api_key or not model:
            raise ValueError("TEXT_API_KEY and TEXT_MODEL are required for the configured text backend")
        self.model = model
        self.lock = asyncio.Lock()
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": "Bearer " + api_key},
            verify=ssl.create_default_context(cafile=ca_file) if ca_file else True,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(120, connect=10),
            limits=httpx.Limits(max_connections=1),
        )

    def payload(self, messages: list[dict], stream: bool = False) -> dict:
        # Put persona instructions first, dropping kit-only context annotations.
        systems = [m["content"] for m in messages if m["role"] in {"system", "developer"}]
        history = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] in {"user", "assistant"}]
        return {
            "model": self.model,
            "messages": [{"role": "system", "content": "\n\n".join(systems)}] + history,
            "max_tokens": 512,
            "temperature": 0.6,
            "stream": stream,
        }

    async def complete_message(self, messages: list[dict], tools: list[dict] | None = None) -> tuple[dict, tuple[int, int, int]]:
        if self.lock.locked():
            raise RuntimeError("text_backend_busy")
        async with self.lock:
            # No automatic fallback/retry: an ambiguous request must not produce another answer.
            payload = self.payload(messages)
            if tools:
                payload.update(tools=tools, tool_choice="auto", parallel_tool_calls=False, max_tokens=1024)
            async with self.http.stream("POST", "chat/completions", json=payload) as response:
                if response.status_code != 200:
                    raise RuntimeError("text_backend_backend_unavailable")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 1024 * 1024:
                        raise RuntimeError("text_backend_response_too_large")
                data = json.loads(body)
                usage = data.get("usage", {})
                if data["choices"][0].get("finish_reason") == "length":
                    raise RuntimeError("text_backend_response_truncated")
                return data["choices"][0]["message"], (
                    int(usage.get("prompt_tokens", 0)),
                    int(usage.get("completion_tokens", 0)),
                    0,
                )

    async def complete(self, messages: list[dict]) -> tuple[str, tuple[int, int, int]]:
        message, usage = await self.complete_message(messages)
        if message.get("tool_calls") or not isinstance(message.get("content"), str):
            raise RuntimeError("text_backend_unexpected_response")
        return message["content"], usage

    async def deltas(self, messages: list[dict]):
        if self.lock.locked():
            raise RuntimeError("text_backend_busy")
        async with self.lock:
            async with self.http.stream("POST", "chat/completions", json=self.payload(messages, stream=True)) as response:
                if response.status_code != 200:
                    raise RuntimeError("text_backend_backend_unavailable")
                total = 0
                async for line in response.aiter_lines():
                    total += len(line)
                    if total > 1024 * 1024:
                        raise RuntimeError("text_backend_response_too_large")
                    if not line.startswith("data:"):
                        continue
                    value = line[5:].strip()
                    if value == "[DONE]":
                        return
                    event = json.loads(value)
                    for choice in event.get("choices", []):
                        delta = choice.get("delta", {}).get("content")
                        if delta:
                            yield delta
                raise RuntimeError("text_backend_stream_incomplete")

    async def judge(self, messages: list[dict], threshold: float) -> tuple[bool, str, float]:
        response, _ = await self.complete(
            messages
            + [
                {
                    "role": "system",
                    "content": (
                        "Should you briefly participate in this conversation? Return only JSON: "
                        '{"intervene": true/false, "intent": "help", "confidence": 0.0}.'
                    ),
                }
            ]
        )
        try:
            data = json.loads(response)
            confidence = float(data["confidence"])
            return bool(data["intervene"]) and confidence >= threshold, str(data.get("intent", "help")), confidence
        except (ValueError, TypeError, KeyError):
            return False, "help", 0.0

    async def close(self) -> None:
        await self.http.aclose()
