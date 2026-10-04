import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from llm_chatbot.image_client import ImageError
from llm_chatbot.image_config import ImageConfig
from llm_chatbot.image_jobs import JobStore
from llm_chatbot.image_tools import image_tool_receipt
from test_images import admit, image_env, settings, tool_response


def test_disabled_daily_quota_survives_restart_and_keeps_capacity_rules(tmp_path):
    cfg = settings(tmp_path, daily_limit=0, queue_limit=1)
    store = JobStore(cfg)
    for job_id in range(100, 112):
        admit(store, job_id)
        with pytest.raises(ImageError, match="user_busy"):
            admit(store, job_id + 100)
        with pytest.raises(ImageError, match="queue_full"):
            admit(store, job_id + 200, user=11)
        assert store.cancel(str(job_id), 10, 1)
    store.close()
    store = JobStore(cfg)
    assert admit(store, 999)["id"] == "999"
    assert store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 13
    store.close()


@pytest.mark.parametrize("limit", [0, -1])
def test_daily_quota_env_zero_is_unlimited_and_negative_invalid(monkeypatch, tmp_path, limit):
    image_env(monkeypatch, tmp_path)
    monkeypatch.setenv("IMAGE_DAILY_LIMIT", str(limit))
    if limit < 0:
        with pytest.raises(ValueError, match="quota"):
            ImageConfig.from_env()
    else:
        assert ImageConfig.from_env().daily_limit == 0


@pytest.mark.parametrize("language", ["fr", "en"])
@pytest.mark.parametrize("code", ["invalid_preset", "invalid_size", "daily_limit", "user_busy"])
def test_tool_reports_specific_rejection_without_prompt_logging(language, code, caplog):
    async def scenario():
        feature = SimpleNamespace(language=language, from_message=AsyncMock(side_effect=ImageError(code)))
        response = tool_response()
        response["tool_calls"][0]["function"]["arguments"] = '{"prompt":"PRIVATE_PROMPT_MARKER"}'
        reply = await image_tool_receipt(feature, response, None)
        assert "image_tool_request_rejected" not in reply
        assert "PRIVATE_PROMPT_MARKER" not in reply + caplog.text
        assert f"rejected code={code} exception=ImageError" in caplog.text
        assert reply.startswith("Génération non lancée" if language == "fr" else "Generation not started")
        feature.from_message.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize("exception", [ImageError("PRIVATE_ERROR_MARKER"), ValueError("PRIVATE_ERROR_MARKER")])
def test_tool_never_discloses_untrusted_exception_text(exception, caplog):
    async def scenario():
        feature = SimpleNamespace(language="fr", from_message=AsyncMock(side_effect=exception))
        reply = await image_tool_receipt(feature, tool_response(), None)
        assert "PRIVATE_ERROR_MARKER" not in reply + caplog.text
        assert "rejected code=" in caplog.text

    asyncio.run(scenario())
