from llm_chatbot.personality import ContextConfig, TriggerConfig, load_personality


def test_load_personality_triggers_context_defaults(tmp_path):
    yml = tmp_path / "p.yml"
    yml.write_text(
        """
name: test
system_prompt: "sys"
developer_prompt: "dev"
"""
    )
    p = load_personality(str(yml))
    assert isinstance(p.triggers, TriggerConfig)
    assert p.triggers.enabled is False
    assert p.triggers.on_mention is True
    assert p.triggers.words is None or p.triggers.words == []
    assert p.triggers.use_regex is False
    assert isinstance(p.context, ContextConfig)
    assert p.context.include_last_n == 10
    assert p.context.include_non_addressed_messages is True


def test_load_personality_triggers_context_custom(tmp_path):
    yml = tmp_path / "p.yml"
    yml.write_text(
        """
name: test
system_prompt: "sys"
developer_prompt: "dev"
triggers:
  enabled: true
  on_mention: false
  words: ["help", "bot"]
  use_regex: false
context:
  include_last_n: 12
  include_non_addressed_messages: false
"""
    )
    p = load_personality(str(yml))
    assert p.triggers.enabled is True
    assert p.triggers.on_mention is False
    assert p.triggers.words == ["help", "bot"]
    assert p.triggers.use_regex is False
    assert p.context.include_last_n == 12
    assert p.context.include_non_addressed_messages is False
