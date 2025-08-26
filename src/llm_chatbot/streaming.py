"""Streaming helpers for human-like bursts to Discord.

This module exposes two primitives:
- `stream_deltas`: bridges OpenAI Responses streaming into an async iterator of
  text deltas, returning immediately for true streaming.
- `send_stream_as_messages`: consumes deltas and emits natural message bursts
  (first ASAP, then ~2 lines), while keeping the typing indicator and handling
  Discord constraints.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from typing import Any, AsyncIterator, Dict, List, Optional

import discord
from discord.errors import HTTPException

# Boundaries where we prefer to flush chunks
BOUNDARY_NEWLINES = re.compile(r"\n+")
BOUNDARY_PUNCT_WS = re.compile(r"(?<=[.!?…])\s+")


class DeltaStream:
    """Async iterator that carries text deltas and final usage.

    The producer thread writes string chunks to an internal queue and finally
    `None` to signal completion; consumers iterate asynchronously. The final
    token usage (input, output, cached_input) is exposed via the `usage` field
    once known.
    """

    def __init__(self) -> None:
        self.q: asyncio.Queue[str | None] = asyncio.Queue()
        self.usage: tuple[int, int, int] | None = None  # (input, output, cached_input)

    def put(self, s: str) -> None:
        """Enqueue a text delta (non-empty string)."""
        self.q.put_nowait(s)

    def close(self) -> None:
        """Signal end-of-stream to consumers."""
        self.q.put_nowait(None)

    def set_usage(self, usage: tuple[int, int, int]) -> None:
        """Attach usage info after the final response is available."""
        self.usage = usage

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self.q.get()
        if item is None:
            raise StopAsyncIteration
        return item


async def stream_deltas(
    api_key: str,
    model: str,
    input_items: List[Dict[str, Any]],
    *,
    reasoning: Optional[Dict[str, Any]] = None,
    verbosity: Optional[str] = None,
    truncation: Optional[str] = None,
) -> DeltaStream:
    """Return a `DeltaStream` of text deltas and eventually usage.

    Parameters
    ----------
    api_key: str
        OpenAI API key.
    model: str
        Model name.
    input_items: list[dict]
        Responses API typed items (developer/user/assistant) already built.
    reasoning, verbosity, truncation: optional
        Extra fields passed to Responses streaming; guarded by model support.

    Notes
    -----
    The producer runs in a background executor so this function returns
    immediately, enabling true streaming for consumers.
    """
    stream_obj = DeltaStream()

    def producer() -> None:
        from openai import OpenAI  # imported here to avoid import costs if unused

        client = OpenAI(api_key=api_key)
        kwargs = {"model": model, "input": input_items}
        # Pass reasoning for GPT-5 models except chat-latest
        if reasoning is not None:
            kwargs["reasoning"] = reasoning
        # Use text.verbosity for GPT-5 (except chat-latest) if provided. This is
        # accepted by Responses.stream according to current API.
        if verbosity is not None and model.startswith("gpt-5") and model != "gpt-5-chat-latest":
            kwargs["text"] = {"format": {"type": "text"}, "verbosity": verbosity}
        if truncation:
            kwargs["truncation"] = truncation
        with client.responses.stream(**kwargs) as stream:
            for event in stream:
                if event.type == "response.output_text.delta":
                    stream_obj.put(event.delta or "")
            final = stream.get_final_response()
            try:
                usage = getattr(final, "usage", None)
                it = int(getattr(usage, "input_tokens", 0) or 0)
                ot = int(getattr(usage, "output_tokens", 0) or 0)
                cit = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
                stream_obj.set_usage((it, ot, cit))
            except Exception:
                stream_obj.set_usage((0, 0, 0))
        stream_obj.close()

    loop = asyncio.get_event_loop()
    # Run producer in background to enable true streaming (return immediately)
    fut = loop.run_in_executor(None, producer)
    # keep a reference on the stream object to avoid GC of the future
    setattr(stream_obj, "_producer_future", fut)
    return stream_obj


async def _send_chunks(channel, text: str, max_len: int, *, allowed_mentions: Optional[discord.AllowedMentions] = None) -> None:
    """Send `text` in chunks up to `max_len`, retrying on rate limits."""
    for i in range(0, len(text), max_len):
        chunk = text[i : i + max_len]
        if not chunk.strip():
            continue
        while True:
            try:
                if allowed_mentions is not None:
                    await channel.send(chunk, allowed_mentions=allowed_mentions)
                else:
                    await channel.send(chunk)
                break
            except HTTPException as e:
                if e.status == 429:
                    await asyncio.sleep(1.0)
                else:
                    raise


async def send_stream_as_messages(
    channel,
    delta_iter: AsyncIterator[str],
    *,
    rate_hz: float | None = None,
    min_first: int | None = None,
    min_next: int | None = None,
    strip_leading: Optional[List[str]] = None,
    allowed_mentions: Optional[discord.AllowedMentions] = None,
    max_total_chars: Optional[int] = None,
) -> str:
    """Send streamed text as natural bursts (no edits).

    Behavior
    - Keeps typing indicator while streaming
    - First burst ASAP after ≥1 completed line; subsequent bursts after ~2 lines
    - Respects sentence/newline boundaries and a light rate limit (~1.3 msg/s)
    - Obeys Discord's ~2000 char limit per message

    Returns the full concatenated text (not truncated), except when
    `max_total_chars` is set for an overall cap, in which case the returned
    text is truncated to that cap.
    """
    MAX_LEN = 1900
    # Defaults tuned for a more human feel; smaller bursts to better preserve lists
    MIN_FIRST = 40 if min_first is None else int(min_first)
    MIN_NEXT = 60 if min_next is None else int(min_next)
    RATE_HZ = 1.3 if rate_hz is None else float(rate_hz)
    # Line-based burst policy: send first message ASAP (>=1 completed line), then every 2 completed lines
    FIRST_LINES = 1
    NEXT_LINES = 2

    buf = ""  # carries the last incomplete segment (tail)
    unsent = ""  # accumulates complete segments not yet sent
    full = ""  # full text to return
    last_send = 0.0
    started_at = 0.0  # timestamp of first token
    FIRST_FLUSH_SEC = 0.7

    async with channel.typing():
        async for d in delta_iter:
            buf += d
            full += d
            if started_at == 0.0:
                started_at = time.monotonic()
            # Find the last boundary (newline run, or punctuation+whitespace) and
            # move completed text (preserving original whitespace) to unsent.
            boundary_idx = -1
            for m in BOUNDARY_NEWLINES.finditer(buf):
                boundary_idx = m.end()
            for m in BOUNDARY_PUNCT_WS.finditer(buf):
                if m.end() > boundary_idx:
                    boundary_idx = m.end()
            if boundary_idx > 0:
                completed = buf[:boundary_idx]
                unsent += completed
                buf = buf[boundary_idx:]
            # Enforce global character cap for streaming (truncate and finish)
            if max_total_chars is not None and len(full) >= max_total_chars:
                # we already appended this delta; calculate remaining and flush
                remain = max_total_chars - (len(full) - len(unsent) - len(buf))
                if remain < 0:
                    remain = 0
                to_send = (unsent + buf)[:remain]
                if last_send == 0.0 and strip_leading and to_send:
                    changed = True
                    while changed:
                        changed = False
                        ts = to_send.lstrip()
                        for tok in strip_leading:
                            if ts.startswith(tok):
                                ts = ts[len(tok) :].lstrip(" :,–-\u2013\u2014")
                                changed = True
                        if changed:
                            to_send = ts
                await _send_chunks(channel, to_send, MAX_LEN, allowed_mentions=allowed_mentions)
                return full[:max_total_chars]

            now = time.monotonic()
            min_len = MIN_FIRST if last_send == 0.0 else MIN_NEXT
            # Line-based threshold: first burst after >=1 completed line; subsequent after >=2 lines
            needed_lines = FIRST_LINES if last_send == 0.0 else NEXT_LINES
            completed_lines = unsent.count("\n")
            should_send_lines = completed_lines >= needed_lines
            should_send_chars = len(unsent) >= min_len
            # Rate pacing except for very first send (ASAP once a boundary reached)
            rate_ok = (now - last_send) >= (1.0 / RATE_HZ)
            should_send = (should_send_lines and (rate_ok or last_send == 0.0)) or (should_send_chars and rate_ok)
            # If first burst hasn’t met a line boundary yet, allow a small early flush of whatever we have
            if (
                not should_send
                and last_send == 0.0
                and started_at > 0.0
                and (now - started_at) >= FIRST_FLUSH_SEC
                and (len(unsent) + len(buf)) > 0
            ):
                unsent += buf
                buf = ""
                should_send = True
            # Avoid mid-line forced flushes; only send at boundaries or early-first-flush
            if should_send:
                if last_send == 0.0 and strip_leading and unsent:
                    # remove any leading self-mention tokens
                    changed = True
                    while changed:
                        changed = False
                        us = unsent.lstrip()
                        for tok in strip_leading:
                            if us.startswith(tok):
                                us = us[len(tok) :].lstrip(" :,–-\u2013\u2014")
                                changed = True
                        if changed:
                            unsent = us
                await _send_chunks(channel, unsent, MAX_LEN, allowed_mentions=allowed_mentions)
                unsent = ""
                last_send = now
                await asyncio.sleep(0.1 + random.random() * 0.3)

    # final flush
    tail = unsent + buf
    if tail:
        # If nothing was sent yet, also strip leading here
        if last_send == 0.0 and strip_leading and tail:
            changed = True
            while changed:
                changed = False
                ts = tail.lstrip()
                for tok in strip_leading:
                    if ts.startswith(tok):
                        ts = ts[len(tok) :].lstrip(" :,–-\u2013\u2014")
                        changed = True
                if changed:
                    tail = ts
        await _send_chunks(channel, tail, MAX_LEN, allowed_mentions=allowed_mentions)

    return full

