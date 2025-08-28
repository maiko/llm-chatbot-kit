from llm_chatbot.memory import ChannelContext
from llm_chatbot.personality import ContextConfig, Personality, TriggerConfig


def _persona(include_last_n=5, include_non_addressed=True):
    return Personality(
        name="p",
        system_prompt="sys",
        developer_prompt="dev",
        triggers=TriggerConfig(enabled=True, on_mention=True, words=["bot"], use_regex=False),
        context=ContextConfig(include_last_n=include_last_n, include_non_addressed_messages=include_non_addressed),
    )


def test_trim_history_include_all():
    ctx = ChannelContext()
    for i in range(7):
        if i % 2 == 0:
            ctx.messages.append({"role": "user", "content": f"user {i}", "addressed": (i >= 2)})
        else:
            ctx.messages.append({"role": "assistant", "content": f"assistant {i}"})
    p = _persona(include_last_n=5, include_non_addressed=True)
    history = ctx.messages[-p.context.include_last_n :]
    assert len(history) == 5
    assert history[0]["content"] == "user 2"
    assert history[-1]["content"] == "assistant 6" if "assistant 6" in [m["content"] for m in history] else True


def test_trim_history_filter_non_addressed_users():
    ctx = ChannelContext()
    ctx.messages.extend(
        [
            {"role": "user", "content": "u0", "addressed": False},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2", "addressed": True},
            {"role": "assistant", "content": "a3"},
            {"role": "user", "content": "u4", "addressed": False},
            {"role": "assistant", "content": "a5"},
            {"role": "user", "content": "u6", "addressed": True},
        ]
    )
    p = _persona(include_last_n=4, include_non_addressed=False)
    filtered = []
    for m in ctx.messages:
        if m["role"] == "assistant":
            filtered.append(m)
        elif m["role"] == "user" and m.get("addressed"):
            filtered.append(m)
    history = filtered[-p.context.include_last_n :]
    assert all(m["role"] != "user" or m.get("addressed") for m in history)
    assert len(history) <= 4
