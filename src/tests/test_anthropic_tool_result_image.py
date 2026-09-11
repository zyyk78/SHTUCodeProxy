"""Regression coverage: Anthropic tool_result images must reach upstream.

Claude Code's Read tool returns image files as a tool_result whose content is a
list containing an `image` block. The Anthropic Messages conversion used to
flatten that through anthropic_content_to_text() -> literal "[image]", losing
the pixels, while the equivalent Responses/Codex path preserved them. These
tests lock in parity.
"""
import base64

from transformer import (
    anthropic_message_to_chat_messages,
    anthropic_tool_results_visible_content,
    content_to_chat_content,
    tool_result_content_to_chat_content,
)

IMG = base64.b64encode(b"\x89PNG\r\n\x1a\nFAKEPIXELS").decode()
IMAGE_PART = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": IMG}}


def _tool_result(content, is_error=False):
    part = {"type": "tool_result", "tool_use_id": "t1", "content": content}
    if is_error:
        part["is_error"] = True
    return {"role": "user", "content": [part]}


def test_image_only_tool_result_preserves_pixels_in_tool_message():
    msg = anthropic_message_to_chat_messages(_tool_result([IMAGE_PART]))
    tool = next(m for m in msg if m["role"] == "tool")
    assert isinstance(tool["content"], list)
    assert any(p.get("type") == "image_url" and IMG in p["image_url"]["url"] for p in tool["content"])


def test_image_only_tool_result_preserves_pixels_in_visible_fallback():
    msg = anthropic_message_to_chat_messages(_tool_result([IMAGE_PART]))
    user = next(m for m in msg if m["role"] == "user")
    assert isinstance(user["content"], list)
    joined = json_dumps(user["content"])
    assert IMG in joined
    assert any(p.get("type") == "image_url" for p in user["content"])


def test_text_plus_image_tool_result_keeps_both():
    content = [{"type": "text", "text": "screenshot:"}, IMAGE_PART]
    msg = anthropic_message_to_chat_messages(_tool_result(content))
    tool = next(m for m in msg if m["role"] == "tool")
    assert any(p.get("type") == "text" and p["text"] == "screenshot:" for p in tool["content"])
    assert any(p.get("type") == "image_url" for p in tool["content"])


def test_text_only_tool_result_keeps_string_contract():
    msg = anthropic_message_to_chat_messages(_tool_result("alpha.py\nbeta.py"))
    tool = next(m for m in msg if m["role"] == "tool")
    assert tool["content"] == "alpha.py\nbeta.py"
    user = next(m for m in msg if m["role"] == "user")
    assert isinstance(user["content"], str)
    assert "<tool_results>" in user["content"]


def test_error_prefix_with_image():
    msg = anthropic_message_to_chat_messages(_tool_result([IMAGE_PART], is_error=True))
    tool = next(m for m in msg if m["role"] == "tool")
    assert tool["content"][0] == {"type": "text", "text": "[ERROR] "}


def test_helper_returns_string_when_no_media():
    out = anthropic_tool_results_visible_content([{"tool_use_id": "t1", "content": "plain"}])
    assert isinstance(out, str)


def test_disabled_visible_fallback_still_keeps_image_in_tool_message():
    msg = anthropic_message_to_chat_messages(_tool_result([IMAGE_PART]), tool_result_visible_fallback=False)
    tool = next(m for m in msg if m["role"] == "tool")
    assert any(p.get("type") == "image_url" for p in tool["content"])
    assert not any(m["role"] == "user" for m in msg)


def json_dumps(value):
    import json
    return json.dumps(value, ensure_ascii=False)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"OK: {fn.__name__}")
    print(f"all {len(fns)} passed")
