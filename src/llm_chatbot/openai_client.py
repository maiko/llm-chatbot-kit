"""Minimal wrappers around OpenAI SDK usage for this bot.

Highlights
- Responses API first with typed items (developer/user/assistant) for GPT‑5.
- Guarding of `reasoning` and `text.verbosity` to the supported models only.
- Compact helpers to translate chat messages to Responses input and extract
  usage information consistently across SDK variants.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple


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
        resp = client.responses.create(**rkw)
        out = _extract_responses_output(resp) or ""
        usage = extract_usage(resp)
        return out, usage
    except Exception as e:
        last_err = e

    # Fallback new chat completions
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        ck = {"model": model, "messages": messages}
        resp = client.chat.completions.create(**ck)
        out = resp.choices[0].message.content or ""
        usage = extract_usage(resp)
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
    logger = logging.getLogger("llm-chatbot-kit")

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
            resp = client.responses.create(**kwargs)
            out = _extract_responses_output(resp) or "{}"
            intervene, intent, conf = _parse_json(out)
            return (intervene and conf >= threshold), intent, conf
        except Exception as e_responses:
            logger.info("listen-judge: responses failed for model=%s err=%s", model, e_responses)
            # Try chat.completions with the same model (may still fail if model unsupported there)
            try:
                prompt = [{"role": "system", "content": instruction}]
                prompt.extend(context_messages[-5:])
                # Omit temperature to satisfy models that only allow the default (1)
                resp_cc = client.chat.completions.create(model=model, messages=prompt)
                txt = resp_cc.choices[0].message.content or "{}"
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
            resp_fb = client.responses.create(**kwargs_fb)
            out_fb = _extract_responses_output(resp_fb) or "{}"
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
        resp = client.moderations.create(model=model, input=text)
        result = resp.results[0]
        # Block if flagged
        flagged = getattr(result, "flagged", False)
        return not flagged
    except Exception:
        # Fail open (allow) if moderation endpoint not available
        return True

