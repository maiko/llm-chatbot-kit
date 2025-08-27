import json
import logging
import sys

from llm_chatbot.logging_setup import JsonFormatter, RedactionFilter, parse_log_levels, setup_logging
from llm_chatbot.openai_client import _trace_full, _trace_meta  # type: ignore[attr-defined]


def test_parse_log_levels_exact_names():
    m = parse_log_levels("llm_chatbot.openai_client=DEBUG,llm_chatbot.streaming=WARNING,foo=INVALID")
    assert m["llm_chatbot.openai_client"] == logging.DEBUG
    assert m["llm_chatbot.streaming"] == logging.WARNING
    assert "foo" not in m or not isinstance(m.get("foo"), str)


def test_apply_module_levels_and_trace_env(monkeypatch):
    monkeypatch.setenv("LLM_LOG_LEVELS", "llm_chatbot.openai_client=DEBUG")
    monkeypatch.setenv("LLM_TRACE_OPENAI", "meta")
    setup_logging("json", None, 0, False)
    logger = logging.getLogger("llm_chatbot.openai_client")
    assert logger.getEffectiveLevel() == logging.DEBUG


def test_trace_meta_json_shape(capsys, monkeypatch):
    setup_logging("json", "INFO", 0, False)
    logger = logging.getLogger("llm_chatbot.openai_client")
    logger.handlers = []
    logger.propagate = False
    h = logging.StreamHandler(stream=sys.stdout)
    h.addFilter(RedactionFilter())
    h.setFormatter(JsonFormatter())
    logger.addHandler(h)
    logger.setLevel(logging.INFO)

    class R:
        def __init__(self):
            self.id = "req_123"

    _trace_meta(
        "responses.create",
        "gpt-5-mini",
        0.0,
        R(),
        {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0},
        phase="chat",
    )
    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert "extra" in data and "trace" in data["extra"]
    tr = data["extra"]["trace"]
    assert tr["type"] == "openai" and tr["path"] == "responses.create" and tr["model"] == "gpt-5-mini"
    assert "latency_ms" in tr and "usage" in tr and tr["usage"]["input"] == 10 and tr["usage"]["output"] == 5


def test_trace_full_redaction(monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-SECRETKEY-1234567890")
    monkeypatch.setenv("LLM_TRACE_OPENAI", "full")
    setup_logging("json", "INFO", 0, False)
    logger = logging.getLogger("llm_chatbot.openai_client")
    logger.handlers = []
    logger.propagate = False
    h = logging.StreamHandler(stream=sys.stdout)
    h.addFilter(RedactionFilter())
    h.setFormatter(JsonFormatter())
    logger.addHandler(h)
    logger.setLevel(logging.INFO)
    _trace_full("responses.create", "gpt-5-mini", inputs={"api_key": "sk-SECRETKEY-1234567890"}, outputs="ok", phase="chat")
    out = capsys.readouterr().out
    assert "***REDACTED***" in out and "sk-SECRETKEY-1234567890" not in out
