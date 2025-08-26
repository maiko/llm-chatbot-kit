from llm_chatbot.listener import should_intervene
from llm_chatbot.personality import Personality, ListenConfig


def _persona(min_len=1, triggers=None):
    lc = ListenConfig(
        enabled=True,
        min_len=min_len,
        trigger_keywords=triggers or ["hey"],
        cooldown_channel_seconds=0,
        cooldown_user_seconds=0,
    )
    return Personality(name="p", system_prompt="s", listen=lc)


def test_should_intervene_by_question_mark():
    p = _persona(min_len=0)
    gs = {"listen_enabled": True}
    ok, intent = should_intervene(p, gs, channel_id=1, channel_name="general", author_id=42, author_is_bot=False, content="Are you there?")
    assert ok is True and intent == "help"


def test_should_intervene_by_trigger_keyword():
    p = _persona(min_len=3, triggers=["kappa"])
    gs = {"listen_enabled": True}
    ok, intent = should_intervene(p, gs, channel_id=1, channel_name="general", author_id=42, author_is_bot=False, content="kappa!!!")
    assert ok is True and intent in ("help", "joke")
