from llm_chatbot.openai_client import _messages_to_responses_payload


def test_messages_to_responses_payload_shapes_and_roles():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "developer", "content": "dev"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "tool", "content": "ignored as user"},
    ]
    items = _messages_to_responses_payload(msgs)
    assert items[0]["role"] == "developer"
    dev_chunk = items[0]["content"][0]
    assert dev_chunk["type"] == "input_text"
    assert "sys" in dev_chunk["text"] and "dev" in dev_chunk["text"]

    # user mapped to input_text
    assert items[1]["role"] == "user"
    assert items[1]["content"][0]["type"] == "input_text"
    # assistant mapped to output_text
    assert items[2]["role"] == "assistant"
    assert items[2]["content"][0]["type"] == "output_text"
    # unknown role coerced to user
    assert items[3]["role"] == "user"
