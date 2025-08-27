import json
import logging
import sys

from llm_chatbot.logging_setup import (
    JsonFormatter,
    RedactionFilter,
    _level_from_flags,
    setup_logging,
)


def test_level_precedence_verbose_quiet_and_explicit(monkeypatch):
    monkeypatch.delenv("LLM_LOG_LEVEL", raising=False)
    lvl = _level_from_flags(None, 0, True, None)
    assert lvl == logging.WARNING
    lvl = _level_from_flags(None, 2, False, None)
    assert lvl == logging.DEBUG
    lvl = _level_from_flags("error", 2, False, None)
    assert lvl == logging.ERROR
    lvl = _level_from_flags(None, 0, False, "debug")
    assert lvl == logging.DEBUG


def test_json_formatter_stable_keys(capsys):
    logger = logging.getLogger("t")
    logger.handlers = []
    h = logging.StreamHandler(stream=sys.stdout)
    h.setFormatter(JsonFormatter())
    logger.addHandler(h)
    logger.setLevel(logging.INFO)
    logger.info("hello world", extra={"foo": "bar"})
    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert set(data.keys()).issuperset({"ts", "level", "logger", "msg"})
    assert data["level"] == "INFO"
    assert data["logger"] == "t"
    assert data["msg"] == "hello world"
    assert "extra" in data and data["extra"]["foo"] == "bar"


def test_redaction(monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-abc123SECRET")
    f = RedactionFilter()
    logger = logging.getLogger("r")
    logger.handlers = []
    h = logging.StreamHandler(stream=sys.stdout)
    h.addFilter(f)
    h.setFormatter(JsonFormatter())
    logger.addHandler(h)
    logger.setLevel(logging.INFO)
    logger.info("token=%s", "sk-abc123SECRET")
    out = capsys.readouterr().out
    assert "***REDACTED***" in out and "sk-abc123SECRET" not in out


def test_setup_logging_env_parity(monkeypatch, capsys):
    monkeypatch.setenv("LLM_LOG_LEVEL", "ERROR")
    monkeypatch.setenv("LLM_LOG_FORMAT", "json")
    setup_logging(None, None, 0, False)
    logger = logging.getLogger("p")
    logger.error("boom")
    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert data["level"] == "ERROR"
