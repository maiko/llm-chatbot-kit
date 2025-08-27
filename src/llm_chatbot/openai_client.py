from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from .logging_setup import get_trace_openai_mode

logger = logging.getLogger(__name__)

"""Minimal wrappers around OpenAI SDK usage for this bot.

Highlights
- Responses API first with typed items (developer/user/assistant) for GPT‑5.
- Guarding of `reasoning` and `text.verbosity` to the supported models only.
- Compact helpers to translate chat messages to Responses input and extract
  usage information consistently across SDK variants.
"""


def _now() -> float:
    return time.perf_counter()


def _trace_meta(
    path: str,
    model: str,
    started: float,
    resp_obj: object,
    usage: dict | tuple[int, int, int] | None = None,
    phase: str | None = None,
) -> None:
    if get_trace_openai_mode() == "off":
        return
    try:
        dur_ms = int((time.perf_counter() - started) * 1000)
        req_id = getattr(resp_obj, "id", None)
        tin = 0
        tout = 0
        tcached = 0
        if isinstance(usage, tuple) and len(usage) >= 2:
            tin, tout = int(usage[0] or 0), int(usage[1] or 0)
            tcached = int((usage[2] if len(usage) > 2 else 0) or 0)
        else:
            u = usage or {}
            try:
                tin = int(u.get("input_tokens", 0) or 0)  # type: ignore[call-arg]
                tout = int(u.get("output_tokens", 0) or 0)  # type: ignore[call-arg]
                tcached = int(u.get("cache_creation_input_tokens", 0) or 0)  # type: ignore[call-arg]
            except Exception:
                tin = tin
                tout = tout
                tcached = tcached
        msg = "trace-openai: path=%s model=%s latency_ms=%d input=%d output=%d cached=%d req_id=%s"
        logger.info(
            msg,
            path,
            model,
            dur_ms,
            tin,
            tout,
            tcached,
            str(req_id) if req_id is not None else "",
            extra={
                "trace": {
                    "type": "openai",
                    "mode": "meta",
                    "path": path,
                    "phase": phase or "",
                    "model": model,
                    "latency_ms": dur_ms,
                    "request_id": req_id,
                    "usage": {"input": tin, "output": tout, "cached": tcached},
                }
            },
        )
    except Exception:
        pass


def _trace_full(path: str, model: str, inputs: object | None = None, outputs: object | None = None, phase: str | None = None) -> None:
    if get_trace_openai_mode() != "full":
        return
    try:
        ilen = 0
        olen = 0
        try:
            ilen = len(str(inputs)) if inputs is not None else 0
        except Exception:
            ilen = 0
        try:
            olen = len(str(outputs)) if outputs is not None else 0
        except Exception:
            olen = 0
        logger.info(
            "trace-openai-full: path=%s model=%s inputs_len=%d outputs_len=%d phase=%s inputs=%r outputs=%r",
            path,
            model,
            ilen,
            olen,
            phase or "",
            inputs,
            outputs,
        )
    except Exception:
        pass


def _extract_responses_output(resp) -> Optional[str]:
    """Best-effort extraction of text from a Responses API result object."""
    # Prefer convenience property if present
    text = getattr(resp, "output_text", None)
    if isinstance(text, str) and text:
        return text
    # Generic traversal
    try:
        outputs = getattr(resp, "output", None) or getattr(resp, "choices", None)
        if outputs and len(outputs) > 0:
            content = getattr(outputs[0], "content", None)
            if content and len(content) > 0:
                txt = getattr(content[0], "text", None)
                if txt is not None:
                    val = getattr(txt, "value", None)
                    if isinstance(val, str) and val:
                        return val
    except Exception:
        pass
    return None


def _is_gpt5_chat_latest(model: str) -> bool:
    """Return True if the model is the special `gpt-5-chat-latest`."""
    return (model or "").lower() == "gpt-5-chat-latest"


def _supports_gpt5_reasoning_and_verbosity(model: str) -> bool:
    """Return True if model supports reasoning + text.verbosity (GPT‑5 except chat-latest)."""
    m = (model or "").lower()
    return m.startswith("gpt-5") and not _is_gpt5_chat_latest(model)


def _messages_to_responses_payload(messages: List[dict]) -> List[Dict[str, Any]]:
    """Translate chat messages to Responses API "input" items.

    Rules
    - Any "system" or "developer" roles are concatenated into a single
      developer item placed first.
    - "user" maps to `input_text`, "assistant" maps to `output_text`.
    - Unknown roles are coerced to "user".
    """
    developer_parts: List[str] = []
    items: List[Dict[str, Any]] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        if role == "system" or role == "developer":
            if content:
                developer_parts.append(content)
            continue
        use_role = role if role in ("user", "assistant") else "user"
        items.append(
            {
                "role": use_role,
                "content": [
                    {
                        "type": "input_text" if use_role == "user" else "output_text",
                        "text": content,
                    }
                ],
            }
        )
    if developer_parts:
        items.insert(
            0,
            {
                "role": "developer",
                "content": [
                    {
                        "type": "input_text",
                        "text": "\n\n".join(developer_parts),
                    }
                ],
            },
        )
    return items


def _build_responses_kwargs(
    model: str,
    input_items: List[Dict[str, Any]],
    *,
    reasoning: Optional[Dict[str, Any]] = None,
    verbosity: Optional[str] = None,
    truncation: Optional[str] = None,
) -> Dict[str, Any]:
    """Build kwargs for `client.responses.create()` consistently.

    Adds `reasoning` and `text.verbosity` only for GPT‑5 models (but not
    `gpt-5-chat-latest`). Adds `truncation` when provided.
    """
    kwargs: Dict[str, Any] = {"model": model, "input": input_items}
    if _supports_gpt5_reasoning_and_verbosity(model):
        if reasoning is not None:
            kwargs["reasoning"] = reasoning
        if verbosity is not None:
            kwargs["text"] = {"format": {"type": "text"}, "verbosity": verbosity}
    if truncation:
        kwargs["truncation"] = truncation
    return kwargs


def chat_complete_with_usage(
    api_key: str,
    model: str,
    messages: List[dict],
    reasoning: Optional[Dict[str, Any]] = None,
    verbosity: Optional[str] = None,
    truncation: Optional[str] = None,
) -> Tuple[str, Tuple[int, int, int]]:
    """Return assistant text and token usage.

    Returns
    -------
    tuple[str, tuple[int, int, int]]
        (text, (input_tokens, output_tokens, cached_input_tokens))
    """
    # Try new Responses API
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        input_items = _messages_to_responses_payload(messages)
        rkw: Dict[str, Any] = {"model": model, "input": input_items}
        if _supports_gpt5_reasoning_and_verbosity(model):
            if reasoning is not None:
                rkw["reasoning"] = reasoning
            if verbosity is not None:
                rkw["text"] = {"format": {"type": "text"}, "verbosity": verbosity}
        if truncation:
            rkw["truncation"] = truncation
        _t0 = time.perf_counter()
        resp = client.responses.create(**rkw)
        out = _extract_responses_output(resp) or ""
        usage = extract_usage(resp)
        try:
            _trace_meta("responses.create", model, _t0, resp, usage, phase="chat")
            _trace_full("responses.create", model, inputs=rkw, outputs=out, phase="chat")
        except Exception:
            pass
        return out, usage
    except Exception as e:
        last_err = e

    # Fallback new chat completions
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        ck = {"model": model, "messages": messages}
        _t1 = time.perf_counter()
        resp = client.chat.completions.create(**ck)
        out = resp.choices[0].message.content or ""
        usage = extract_usage(resp)
        try:
            _trace_meta("chat.completions.create", model, _t1, resp, usage, phase="chat")
            _trace_full("chat.completions.create", model, inputs=ck, outputs=out, phase="chat")
        except Exception:
            pass
        return out, usage
    except Exception as e:
        raise RuntimeError(f"OpenAI request failed: {e if e else last_err}")


def extract_usage(resp: Any) -> Tuple[int, int, int]:
    """Extract token usage from Responses or Chat Completions results."""
    # Responses API
    try:
        usage = getattr(resp, "usage", None)
        if usage is not None:
            it = int(getattr(usage, "input_tokens", 0) or 0)
            ot = int(getattr(usage, "output_tokens", 0) or 0)
            cit = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
            return it, ot, cit
    except Exception:
        pass
    # Chat Completions new client
    try:
        usage = getattr(resp, "usage", None)
        if usage and isinstance(usage, dict):
            return int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)), 0
    except Exception:
        pass
    # Legacy
    try:
        return int(resp["usage"]["prompt_tokens"]), int(resp["usage"]["completion_tokens"]), 0
    except Exception:
        return 0, 0, 0


def judge_intervention(api_key: str, model: str, context_messages: List[Dict[str, str]], threshold: float) -> Tuple[bool, str, float]:
    """Decide if the bot should intervene cheaply with a small model.

    Returns
    -------
    (bool, str, float)
        (intervene, intent, confidence)
    """
    logger = logging.getLogger(__name__)

    # Strategy: Prefer Responses API; if model is invalid or unsupported, fallback to a small widely-available model.
    # Always try to return a parsed decision rather than failing silently.
    def _parse_json(s: str) -> Tuple[bool, str, float]:
        try:
            data = json.loads(s.strip().strip("`"))
        except Exception:
            return False, "help", 0.0
        intervene = bool(data.get("intervene", False))
        intent = str(data.get("intent", "help"))
        conf = float(data.get("confidence", 0.0))
        return intervene, intent, conf

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        instruction = (
            "You are a strict classifier for a Discord bot. Decide if the bot should proactively intervene. "
            'Return ONLY compact JSON: {"intervene": true|false, "intent": "help|joke|snark", "confidence": 0..1}.'
        )
        judge_msgs = [{"role": "developer", "content": instruction}] + context_messages[-5:]
        items = _messages_to_responses_payload(judge_msgs)

        # First try with given model via Responses API
        try:
            kwargs = _build_responses_kwargs(
                model,
                items,
                reasoning={"effort": "minimal"},
                verbosity="low",
                truncation=None,
            )
            started = _now()
            resp = client.responses.create(**kwargs)
            out = _extract_responses_output(resp) or "{}"
            try:
                _trace_meta("responses.create", model, started, resp, extract_usage(resp), phase="judge")
                _trace_full("responses.create", model, inputs=kwargs, outputs=out, phase="judge")
            except Exception:
                pass
            intervene, intent, conf = _parse_json(out)
            return (intervene and conf >= threshold), intent, conf
        except Exception as e_responses:
            logger.info("listen-judge: responses failed for model=%s err=%s", model, e_responses)
            # Try chat.completions with the same model (may still fail if model unsupported there)
            try:
                prompt = [{"role": "system", "content": instruction}]
                prompt.extend(context_messages[-5:])
                # Omit temperature to satisfy models that only allow the default (1)
                started_cc = _now()
                resp_cc = client.chat.completions.create(model=model, messages=prompt)
                txt = resp_cc.choices[0].message.content or "{}"
                try:
                    _trace_meta("chat.completions.create", model, started_cc, resp_cc, extract_usage(resp_cc), phase="judge")
                    _trace_full("chat.completions.create", model, inputs={"model": model, "messages": prompt}, outputs=txt, phase="judge")
                except Exception:
                    pass
                intervene, intent, conf = _parse_json(txt)
                return (intervene and conf >= threshold), intent, conf
            except Exception as e_cc:
                logger.info("listen-judge: chat.completions failed for model=%s err=%s", model, e_cc)

        # Fallback to a small widely-available model
        fallback_model = "gpt-5-nano"
        try:
            kwargs_fb = _build_responses_kwargs(
                fallback_model,
                items,
                reasoning={"effort": "minimal"},
                verbosity="low",
                truncation=None,
            )
            started_fb = _now()
            resp_fb = client.responses.create(**kwargs_fb)
            out_fb = _extract_responses_output(resp_fb) or "{}"
            try:
                _trace_meta("responses.create", fallback_model, started_fb, resp_fb, extract_usage(resp_fb), phase="judge-fallback")
                _trace_full("responses.create", fallback_model, inputs=kwargs_fb, outputs=out_fb, phase="judge-fallback")
            except Exception:
                pass
            intervene, intent, conf = _parse_json(out_fb)
            logger.info("listen-judge: fallback model=%s used", fallback_model)
            return (intervene and conf >= threshold), intent, conf
        except Exception as e_fb:
            logger.warning("listen-judge: all judge attempts failed err=%s", e_fb)
            return False, "help", 0.0
    except Exception as outer:
        logger.warning("listen-judge: unexpected failure err=%s", outer)
        return False, "help", 0.0


def moderate_text(api_key: str, model: str, text: str) -> bool:
    """Return True if the text is allowed, False if it should be blocked."""
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        started = _now()
        resp = client.moderations.create(model=model, input=text)
        try:
            _trace_meta("moderations.create", model, started, resp, extract_usage(resp), phase="moderate")
            _trace_full("moderations.create", model, inputs={"model": model, "input": text}, outputs=None, phase="moderate")
        except Exception:
            pass
        result = resp.results[0]
        flagged = getattr(result, "flagged", False)
        return not flagged
    except Exception:
        # Fail open (allow) if moderation endpoint not available
        return True
