import types

from llm_chatbot.runtime_utils import (
    _chunk_message,
    _effective_model_and_params,
    _effective_truncation,
)
from llm_chatbot.personality import Personality, ListenConfig


class FakeStore:
    def __init__(self, truncation_value=None):
        self._trunc = truncation_value

    def guild_settings(self, gid):
        if self._trunc is None:
            return {}
        return {"truncation": self._trunc}


class FakeGuild:
    def __init__(self, gid):
        self.id = gid


class FakeMessage:
    def __init__(self, gid=None):
        self.guild = FakeGuild(gid) if gid is not None else None


def test_chunk_message_splits_at_limit():
    s = "x" * 10
    out = _chunk_message(s, limit=4)
    assert out == ["xxxx", "xxxx", "xx"]


def test_effective_model_and_params_for_gpt5():
    p = Personality(name="t", system_prompt="x")
    model, reasoning, verbosity = _effective_model_and_params(
        model_default="gpt-5-mini",
        intervened=False,
        personality=p,
        verbosity_default="low",
    )
    assert model == "gpt-5-mini"
    assert isinstance(reasoning, dict) and reasoning.get("effort") == "minimal"
    assert verbosity == "low"


def test_effective_model_and_params_for_chat_latest():
    p = Personality(name="t", system_prompt="x")
    model, reasoning, verbosity = _effective_model_and_params(
        model_default="gpt-5-chat-latest",
        intervened=False,
        personality=p,
        verbosity_default="low",
    )
    assert model == "gpt-5-chat-latest"
    assert reasoning is None
    assert verbosity is None


def test_effective_truncation_prefers_guild_override():
    p = Personality(name="t", system_prompt="x", truncation="disabled")
    store = FakeStore(truncation_value="auto")
    msg = FakeMessage(gid=123)
    out = _effective_truncation(p, store, msg)
    assert out == "auto"
