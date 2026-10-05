from types import SimpleNamespace

import pytest

from llm_chatbot.runtime_utils import _chunk_message, repair_truncated_mentions

KNOWN = "222233334444555566"
TRUNCATED = "2222333344445566"


def event(*others):
    return SimpleNamespace(
        author=SimpleNamespace(id=int(KNOWN)),
        guild=SimpleNamespace(members=[SimpleNamespace(id=int(v)) for v in others]),
        mentions=[],
    )


@pytest.mark.parametrize("token", [f"<@{TRUNCATED}>", f"<@!{TRUNCATED}>", "<@22223333444455566>"])
def test_only_unambiguous_omitted_digits_are_restored(token):
    assert repair_truncated_mentions("Hello " + token, event()) == f"Hello <@{KNOWN}>"


def test_exact_members_ambiguous_candidates_and_other_errors_are_preserved():
    token = f"<@{TRUNCATED}>"
    assert repair_truncated_mentions(token, event(TRUNCATED)) == token
    assert repair_truncated_mentions(token, event("222233334444775566")) == token
    for token in ["<@2222333344445569>", "<@222233334445566>", "<@2222333344445555669>", "<@12>", f"<@{KNOWN}>"]:
        assert repair_truncated_mentions(token, event()) == token


def test_member_cache_and_explicit_mentions_supply_candidates_but_codes_and_escapes_are_untouched():
    message = event()
    message.author.id = 42
    message.mentions = [SimpleNamespace(id=int(KNOWN))]
    raw = f"`<@{TRUNCATED}>` ```\n<@{TRUNCATED}>\n``` \\<@{TRUNCATED}> <@{TRUNCATED}>"
    expected = raw.rsplit(" ", 1)[0] + f" <@{KNOWN}>"
    assert repair_truncated_mentions(raw, message) == expected
    message.mentions = []
    message.guild.members = [SimpleNamespace(id=int(KNOWN))]
    assert repair_truncated_mentions(f"<@{TRUNCATED}>", message) == f"<@{KNOWN}>"


def test_reply_chunks_preserve_full_member_mentions():
    mention = f"<@{KNOWN}>"
    text = "x" * 1985 + mention + "tail"
    chunks = _chunk_message(text)
    assert "".join(chunks) == text
    assert all(len(chunk) <= 1990 for chunk in chunks)
    assert chunks[1] == mention + "tail"
