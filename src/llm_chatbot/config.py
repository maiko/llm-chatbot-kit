"""Configuration and simple JSON persistence helpers."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    """Runtime configuration sourced from environment variables."""

    discord_token: str
    openai_api_key: str
    openai_model: str
    openai_verbosity: str | None
    owner_id: str | None
    command_prefix: str
    max_turns: int
    store_path: Path
    text_enabled: bool = True
    image_enabled: bool = False
    text_api_base_url: str | None = None
    text_ca_file: str | None = None
    text_guild_ids: frozenset[int] = frozenset()
    text_channel_ids: frozenset[int] = frozenset()
    text_role_ids: frozenset[int] = frozenset()
    serialize_backends: bool = False
    image_tools_enabled: bool = False
    ready_file: Path | None = None
    text_api_key: str | None = None


def _maybe_migrate_cache(new_dir: Path, new_store: Path) -> None:
    """Migrate context store from legacy path if present and new path missing."""
    try:
        old_dir = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "discord-llm-bot"
        old_store = old_dir / "context.json"
        if not new_store.exists() and old_store.exists():
            new_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old_store, new_store)
    except Exception:
        # Best-effort; ignore migration failures
        pass


def load_config() -> Config:
    """Load configuration from environment variables and defaults.

    Creates the cache directory if needed and resolves the context store path.
    Performs a one-time migration from the legacy cache path if applicable.
    """
    mode = os.getenv("BOT_MODE", "chat").lower()
    if mode not in {"chat", "image", "both"}:
        raise ValueError("BOT_MODE must be chat, image or both")
    cache_dir = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "llm-chatbot-kit"
    cache_dir.mkdir(parents=True, exist_ok=True)
    store_path = Path(os.environ.get("CONTEXT_STORE_PATH", cache_dir / "context.json"))

    # Only attempt migration if using the default location
    if str(store_path) == str(cache_dir / "context.json"):
        _maybe_migrate_cache(cache_dir, store_path)

    return Config(
        discord_token=os.environ.get("DISCORD_TOKEN", ""),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        # Default to mini for generation (user request)
        openai_model=os.environ.get("TEXT_MODEL", os.environ.get("OPENAI_MODEL", "gpt-5-mini")),
        openai_verbosity=os.environ.get("OPENAI_VERBOSITY", "low"),
        owner_id=os.environ.get("DISCORD_OWNER_ID"),
        command_prefix=os.environ.get("COMMAND_PREFIX", "~"),
        max_turns=int(os.environ.get("MAX_TURNS", "20")),
        store_path=store_path,
        text_enabled=mode in {"chat", "both"},
        image_enabled=mode in {"image", "both"},
        text_api_base_url=os.getenv("TEXT_API_BASE_URL") or None,
        text_api_key=os.getenv("TEXT_API_KEY") or None,
        text_ca_file=os.getenv("TEXT_CA_FILE") or None,
        text_guild_ids=frozenset(int(v) for v in os.getenv("TEXT_GUILD_IDS", "").split(",") if v.strip()),
        text_channel_ids=frozenset(int(v) for v in os.getenv("TEXT_CHANNEL_IDS", "").split(",") if v.strip()),
        text_role_ids=frozenset(int(v) for v in os.getenv("TEXT_ROLE_IDS", "").split(",") if v.strip()),
        serialize_backends=os.getenv("SERIALIZE_BACKENDS", "false").lower() == "true",
        image_tools_enabled=os.getenv("IMAGE_TOOLS_ENABLED", "false").lower() == "true",
        ready_file=Path(os.environ["BOT_READY_FILE"]) if os.getenv("BOT_READY_FILE") else None,
    )


def read_json(path: Path) -> dict:
    """Read JSON from `path` safely and return a dict (empty on failure)."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read context store: {path}") from exc


def write_json(path: Path, data: dict) -> None:
    """Atomically write JSON to `path` by first writing to a temp file."""
    temp = path.with_suffix(".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    with temp.open("w") as output:
        temp.chmod(0o600)
        output.write(json.dumps(data, ensure_ascii=False))
    temp.replace(path)
