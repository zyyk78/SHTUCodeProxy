"""Regression test for 🤔 Thinking text-block → thinking block conversion.

Reproduces the gap where a model returns reasoning embedded in `content` as a
"🤔 Thinking\n```...```" text block (typically imitating the proxy's own
previous output). Before the fix, proxy had no parser for this format and
forwarded it verbatim as plain text. Now split_thinking_text_block + the
non-streaming assembly routes it into a real thinking block / reasoning item
when the client requested thinking.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from transformer import (  # noqa: E402
    split_thinking_text_block,
    chat_completion_json_to_responses,
    responses_json_to_anthropic_message,
)


def test_split_three_backticks_with_body():
    text = "🤔 Thinking\n```\nlet me think\n```\n\nthe answer"
    reasoning, rest = split_thinking_text_block(text)
    assert reasoning == "let me think", reasoning
    assert rest == "the answer", rest


def test_split_four_backticks_no_body():
    text = "🤔 Thinking\n````\nonly reasoning\n````"
    reasoning, rest = split_thinking_text_block(text)
    assert reasoning == "only reasoning", reasoning
    assert rest == "", rest


def test_split_no_block_passthrough():
    text = "plain answer, no thinking block"
    reasoning, rest = split_thinking_text_block(text)
    assert reasoning == ""
    assert rest == text


def test_split_leading_whitespace_and_case():
    text = "  🤔 thinking\n```\nr\n```\n\nbody"
    reasoning, rest = split_thinking_text_block(text)
    assert reasoning == "r", reasoning
    assert rest == "body", rest


def test_chat_completion_to_responses_reasoning_item_when_thinking():
    """content carries a 🤔 block; thinking requested → reasoning output item."""
    payload = {
        "choices": [{
            "message": {
                "content": "🤔 Thinking\n````\nmy reasoning\n````\n\nfinal answer",
            }
        }],
        "_thinking_requested": True,
    }
    out = chat_completion_json_to_responses(payload, "m", 10)
    types = [o.get("type") for o in out["output"]]
    assert "reasoning" in types, types
    msg = next(o for o in out["output"] if o.get("type") == "message")
    assert msg["content"][0]["text"] == "final answer", msg
    rsn = next(o for o in out["output"] if o.get("type") == "reasoning")
    assert rsn["summary"][0]["text"] == "my reasoning", rsn


def test_chat_completion_to_responses_text_merge_when_no_thinking():
    """No thinking requested → keep 🤔 text merge (clients without thinking UI)."""
    payload = {
        "choices": [{
            "message": {
                "content": "🤔 Thinking\n````\nmy reasoning\n````\n\nfinal answer",
            }
        }],
    }
    out = chat_completion_json_to_responses(payload, "m", 10)
    types = [o.get("type") for o in out["output"]]
    assert "reasoning" not in types, types
    text = out["output"][0]["content"][0]["text"]
    assert "🤔 Thinking" in text and "final answer" in text, text


def test_responses_to_anthropic_thinking_block_when_thinking():
    """🤔 block in message text; thinking requested → Anthropic thinking block."""
    converted = {
        "output": [{
            "id": "x", "type": "message", "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "text": "🤔 Thinking\n````\nr\n````\n\nans"}],
        }],
        "_thinking_requested": True,
    }

    class _MC:
        model_id = "m"

    msg = responses_json_to_anthropic_message(converted, _MC())
    block_types = [b.get("type") for b in msg["content"]]
    assert "thinking" in block_types, block_types
    tblock = next(b for b in msg["content"] if b.get("type") == "thinking")
    assert tblock["thinking"] == "r", tblock
    text_blocks = [b for b in msg["content"] if b.get("type") == "text"]
    assert text_blocks and text_blocks[0]["text"] == "ans", msg["content"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"OK: {fn.__name__}")
    print(f"all {len(fns)} passed")
