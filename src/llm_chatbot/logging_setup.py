from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any, Dict

SECRET_ENV_PREFIXES = ("SLACK_",)
SECRET_ENV_NAMES = ("OPENAI_API_KEY", "DISCORD_TOKEN")

_TRACE_OPENAI_MODE = "meta"


def _level_from_flags(explicit: str | None, vcount: int, quiet: bool, env_level: str | None) -> int:
    if explicit:
        return getattr(logging, explicit.upper(), logging.INFO)
    if env_level:
        env_lvl = getattr(logging, env_level.upper(), None)
    else:
        env_lvl = None
    base = logging.INFO
    if quiet:
        base = logging.WARNING
    if vcount >= 2:
        base = logging.DEBUG
    elif vcount == 1:
        base = logging.INFO
    return env_lvl if env_lvl is not None else base


def parse_log_levels(spec: str | None) -> Dict[str, int]:
    out: Dict[str, int] = {}
    if not spec:
        return out
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    for p in parts:
        if "=" not in p:
            continue
        name, lvl = p.split("=", 1)
        name = name.strip()
        lvl = lvl.strip().upper()
        if not name:
            continue
        val = getattr(logging, lvl, None)
        if isinstance(val, int):
            out[name] = val
    return out


def apply_module_levels(levels: Dict[str, int]) -> None:
    for name, lvl in levels.items():
        logging.getLogger(name).setLevel(lvl)


def get_trace_openai_mode() -> str:
    return _TRACE_OPENAI_MODE


class RedactionFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self.secret_keys = set()
        for name in SECRET_ENV_NAMES:
            val = os.environ.get(name)
            if val:
                self.secret_keys.add(val)
        for k, v in os.environ.items():
            if any(k.startswith(p) for p in SECRET_ENV_PREFIXES) and v:
                self.secret_keys.add(v)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if record.args:
                try:
                    formatted = str(record.msg) % record.args
                except Exception:
                    formatted = str(record.msg)
            else:
                formatted = str(record.msg)
            redacted = self._redact_text(formatted)
            record.msg = redacted
            record.args = ()
        except Exception:
            pass
        return True

    def _looks_like_secret(self, s: str) -> bool:
        if len(s) >= 20 and any(c.isalpha() for c in s) and any(c.isdigit() for c in s):
            return True
        return False

    def _redact_text(self, s: str) -> str:
        out = s
        for val in list(self.secret_keys):
            if val and val in out:
                out = out.replace(val, "***REDACTED***")
        return out

    def _redact_dict(self, d: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for k, v in d.items():
            if isinstance(v, str):
                if k.upper() in SECRET_ENV_NAMES or any(k.upper().startswith(p) for p in SECRET_ENV_PREFIXES) or self._looks_like_secret(v):
                    out[k] = "***REDACTED***"
                else:
                    out[k] = self._redact_text(v)
            else:
                out[k] = v
        return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra: Dict[str, Any] = {}
        for k, v in record.__dict__.items():
            if k in (
                "name",
                "msg",
                "args",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "exc_info",
                "exc_text",
                "stack_info",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
            ):
                continue
            extra[k] = v
        if extra:
            data["extra"] = extra
        try:
            return json.dumps(data, ensure_ascii=False)
        except Exception:
            return json.dumps({"ts": data["ts"], "level": data["level"], "logger": data["logger"], "msg": data["msg"]})


def _text_formatter() -> logging.Formatter:
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    datefmt = "%H:%M:%S"
    return logging.Formatter(fmt=fmt, datefmt=datefmt)


def setup_logging(
    format_str: str | None,
    explicit_level: str | None,
    vcount: int,
    quiet: bool,
    *,
    log_levels: Dict[str, int] | None = None,
    trace_openai: str | None = None,
) -> None:
    global _TRACE_OPENAI_MODE
    env_level = os.environ.get("LLM_LOG_LEVEL")
    env_format = os.environ.get("LLM_LOG_FORMAT")
    env_levels = os.environ.get("LLM_LOG_LEVELS")
    env_trace = os.environ.get("LLM_TRACE_OPENAI")
    fmt = (format_str or env_format or "text").lower()
    level = _level_from_flags(explicit_level, vcount, quiet, env_level)

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(stream=sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(_text_formatter())

    handler.addFilter(RedactionFilter())
    root.addHandler(handler)
    root.setLevel(level)

    levels_map = log_levels if log_levels is not None else parse_log_levels(env_levels)
    if levels_map:
        apply_module_levels(levels_map)

    mode = (trace_openai or env_trace or "meta").lower()
    if mode not in ("off", "meta", "full"):
        mode = "meta"
    _TRACE_OPENAI_MODE = mode


def add_logging_cli_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-v", "--verbose", action="count", default=0, help="Increase verbosity (-v INFO, -vv DEBUG)")
    parser.add_argument("-q", "--quiet", action="store_true", help="Quiet mode (WARNING)")
    parser.add_argument("--log-level", "-l", default=os.environ.get("LOG_LEVEL"), help="Explicit log level (DEBUG|INFO|WARNING|ERROR)")
    parser.add_argument("--log-format", choices=["text", "json"], default=None, help="Log format (default: text; env LLM_LOG_FORMAT)")
    parser.add_argument(
        "--log-levels",
        default=None,
        help=("Per-module levels (exact match): module=LEVEL[,module=LEVEL...] " "or env LLM_LOG_LEVELS"),
    )
    parser.add_argument(
        "--trace-openai",
        choices=["off", "meta", "full"],
        default=None,
        help="OpenAI tracing (default: env LLM_TRACE_OPENAI or meta)",
    )
