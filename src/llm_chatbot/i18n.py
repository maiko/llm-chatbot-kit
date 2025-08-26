"""Simple i18n loader for YAML message catalogs packaged with the app."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from typing import Any, Dict

import yaml

DEFAULT_LANG = "en"


@dataclass
class I18n:
    """Localized message accessor with Python `.format()` substitution."""

    lang: str
    messages: Dict[str, str]

    def t(self, key: str, **kw: Any) -> str:
        tmpl = self.messages.get(key)
        if tmpl is None:
            # Fallback to key if missing
            tmpl = key
        try:
            return tmpl.format(**kw)
        except Exception:
            return tmpl


def _load_yaml_from_package(package: str, name: str) -> Dict[str, str]:
    """Load a YAML file bundled in the package as a dict of strings."""
    try:
        with resources.files(package).joinpath(name).open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data or {}
    except FileNotFoundError:
        return {}


def load_i18n(lang: str | None, overrides: Dict[str, str] | None = None) -> I18n:
    """Load localized messages with optional overrides.

    Falls back to English and merges overrides (persona-level message tweaks).
    """
    code = (lang or DEFAULT_LANG).lower()

    base = _load_yaml_from_package("llm_chatbot.locales", f"{DEFAULT_LANG}.yml")
    if code != DEFAULT_LANG:
        base.update(_load_yaml_from_package("llm_chatbot.locales", f"{code}.yml"))

    if overrides:
        base.update(overrides)

    return I18n(lang=code, messages=base)
