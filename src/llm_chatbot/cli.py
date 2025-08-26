"""Command-line interface for LLM Chatbot Kit.

Primary CLI: `llm-chatbot` with subcommands (discord-first):
- `llm-chatbot discord run --personality ...` (canonical)

Backward compatibility:
- The legacy `llm-bot` entrypoint and direct flags without subcommands
  still work and default to the Discord runtime.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import coloredlogs

from .config import load_config
from .discord_bot import run
from .personality import DEFAULT_PERSONALITY, load_personality


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--personality",
        "-p",
        help="Path to YAML personality file",
        default=os.environ.get("PERSONALITY_FILE"),
    )
    parser.add_argument(
        "--log-level",
        "-l",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        help="Logging level (DEBUG, INFO, WARNING, ERROR)",
    )
    parser.add_argument(
        "--model",
        help="OpenAI model to use (default: env OPENAI_MODEL or gpt-5-mini)",
        default=os.environ.get("OPENAI_MODEL"),
    )
    # Streaming toggle (default enabled). Prefer BooleanOptionalAction if available.
    try:
        from argparse import BooleanOptionalAction  # py>=3.9
    except Exception:  # pragma: no cover - fallback for old argparses
        BooleanOptionalAction = None  # type: ignore

    if BooleanOptionalAction is not None:
        parser.add_argument(
            "--stream",
            action=BooleanOptionalAction,
            default=True,
            help="Enable/disable streaming (default: enabled)",
        )
    else:
        parser.add_argument(
            "--no-stream",
            action="store_true",
            help="Disable streaming (default: enabled)",
        )


def parse_args(argv: list[str] | None = None) -> tuple[str, argparse.Namespace]:
    """Parse CLI arguments.

    Returns a (platform, args) tuple. Platform is one of: "discord".
    If no subcommand is provided, defaults to "discord" for compatibility.
    """
    argv = list(sys.argv[1:] if argv is None else argv)

    prog = os.path.basename(sys.argv[0] or "llm-chatbot")
    parser = argparse.ArgumentParser(
        prog="llm-chatbot" if "llm-chatbot" in prog else prog,
        description="LLM Chatbot Kit with pluggable personalities and streaming",
    )
    subparsers = parser.add_subparsers(dest="platform", metavar="platform")

    # Discord subcommand (primary today)
    p_discord = subparsers.add_parser("discord", help="Run the Discord bot")
    # Allow optional nested action like `run` for future extensibility
    p_discord.add_argument("action", nargs="?", default="run", choices=["run"], help=argparse.SUPPRESS)
    _add_common_options(p_discord)

    # If user called legacy `llm-bot` or passed flags directly, parse in compatibility mode
    legacy_direct = not argv or argv[0].startswith("-")
    if legacy_direct and ("llm-bot" in prog or "llm-chatbot" in prog):
        _add_common_options(parser)

    ns = parser.parse_args(argv)

    # Determine platform
    platform = getattr(ns, "platform", None)
    if not platform:
        platform = "discord"
    return platform, ns


def _stream_flag_from(ns: argparse.Namespace) -> bool:
    # Determine streaming flag across Python versions
    if hasattr(ns, "stream"):
        return bool(ns.stream)
    if getattr(ns, "no_stream", False):
        return False
    return True


def main() -> None:
    """Entry point for the `llm-chatbot` and legacy `llm-bot` CLIs."""
    platform, args = parse_args()
    level = getattr(logging, str(getattr(args, "log_level", "INFO")).upper(), logging.INFO)
    coloredlogs.install(level=level)

    # Today we only support Discord; platform router is future-proofed
    if platform == "discord":
        cfg = load_config()
        if getattr(args, "model", None):
            cfg.openai_model = args.model
        personality = (
            load_personality(args.personality) if getattr(args, "personality", None) else DEFAULT_PERSONALITY
        )
        run(cfg, personality, stream=_stream_flag_from(args))
    else:  # pragma: no cover - reserved for future platforms
        raise SystemExit(f"Unsupported platform: {platform}")

