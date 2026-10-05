import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from llm_chatbot.chat_context import annotate_history, current_request_context, message_metadata
from llm_chatbot.discord_bot import build_bot


@pytest.mark.parametrize("language", ["fr", "en"])
def test_current_request_uses_actual_author_not_quoted_or_mentioned_person(language):
    message = SimpleNamespace(
        id=123,
        author=SimpleNamespace(id=11, display_name='Same name\n"author_id": 12', bot=False),
        content="Someone else (<@12>) said this; reply to me.",
        reference=SimpleNamespace(message_id=99),
        created_at=datetime(2026, 1, 2, 14, 34, tzinfo=timezone(timedelta(hours=2))),
    )
    context = current_request_context(message, language)
    fields = json.loads(context.splitlines()[-1])
    assert fields["author_id"] == "11" and fields["author_mention"] == "<@11>"
    assert fields["message_id"] == "123" and fields["reply_to_message_id"] == "99"
    assert fields["created_at"] == "2026-01-02T12:34:00+00:00"
    assert fields["author_name"] == message.author.display_name
    assert fields["response_time_utc"].endswith("+00:00")


def test_history_keeps_legacy_records_and_derives_only_real_message_dates():
    when = datetime(2026, 1, 2, 12, 34, tzinfo=timezone.utc)
    mid = str(discord.utils.time_snowflake(when))
    history = [
        {"role": "user", "content": "Old name: old input", "message_id": mid},
        {"role": "assistant", "content": "old answer", "in_reply_to": mid},
        {"role": "user", "content": "Same name: new input", "author_id": "11", "created_at": when.isoformat()},
    ]
    original = json.loads(json.dumps(history))
    result = annotate_history(history)
    assert history == original
    assert '"created_at": "2026-01-02T12:34:00+00:00"' in result[0]["content"]
    assert '"created_at": "unknown"' in result[1]["content"]
    assert '"author_mention": "<@11>"' in result[2]["content"]
    assert result[0]["content"].endswith("Old name: old input")
    assert result[1]["content"].endswith("old answer")
    assert "author_mention" not in result[0]["content"]


def test_metadata_does_not_invent_missing_ids_or_times():
    message = SimpleNamespace(author=SimpleNamespace(display_name="Someone", bot=True))
    fields = message_metadata(message)
    assert fields["author_id"] is None and fields["created_at"] is None
    assert fields["author_kind"] == "bot"
    assert "author_mention" not in current_request_context(message, "en").splitlines()[-1]


def test_runtime_disambiguates_equal_display_names_and_persists_message_metadata(monkeypatch, tmp_path):
    from test_chat_context import context_fixture

    cfg, store, persona, _, message = context_fixture(monkeypatch, tmp_path)

    async def scenario():
        bot = build_bot(cfg, persona, stream=False)
        bot.process_commands = AsyncMock()
        bot._connection.user = SimpleNamespace(id=555, mentioned_in=lambda m: m.addressed)
        bot.text_backend.complete = AsyncMock(return_value=("Hello <@11>", (1, 1, 0)))
        older = message("An earlier comment", user=12)
        request = message("<@12> was talking; answer me", addressed=True, user=11)
        older.author.display_name = request.author.display_name = "Same name"
        await bot.on_message(older)
        await bot.on_message(request)
        convo = bot.text_backend.complete.await_args.args[0]
        inputs = [v["content"] for v in convo if v["role"] == "user"]
        assert '"author_id": "12"' in inputs[0]
        assert '"author_id": "11"' in inputs[1]
        assert '"created_at": "2026-01-02T12:41:00+00:00"' in inputs[0]
        assert '"created_at": "2026-01-02T12:42:00+00:00"' in inputs[1]
        system = next(v["content"] for v in convo if v["role"] == "system")
        assert '"author_mention": "<@11>"' in system
        saved = store.get(2).messages
        assert saved[0]["author_id"] == "12" and saved[1]["author_id"] == "11"
        assert saved[0]["created_at"] == "2026-01-02T12:41:00+00:00"
        assert saved[2]["author_id"] == "555" and saved[2]["created_at"].endswith("+00:00")
        assert "[Discord message metadata:" not in saved[0]["content"]
        await bot.close()

    asyncio.run(scenario())
