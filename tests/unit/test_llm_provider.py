"""LLM provider 测试：请求形状、推理内容分流、重试/降级、错误映射（全部离线）。

用 ``httpx.MockTransport`` 精确断言发往网关的 payload——这是"真实网关行为"的可重复替身。
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from app.common.config import Settings
from app.llm.mock import MockChatModel, request_key
from app.llm.openai import OpenAICompatibleChatModel, parse_sse_line
from app.llm.types import ChatMessage, LLMError, Usage

Handler = Callable[[httpx.Request], httpx.Response]


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "app_env": "test",
        "llm_provider": "openai",
        "llm_model": "test-model",
        "llm_fallback_model": "fallback-model",
        "llm_base_url": "https://llm.example/v1",
        "llm_api_key_file": None,
        "llm_reasoning_effort": "low",
        "llm_max_output_tokens": 128,
        "llm_retry": 1,
    }
    base.update(overrides)
    return Settings(**base)


def build(
    handler: Handler, settings: Settings, *, api_key: str = "sk-unit-test"
) -> OpenAICompatibleChatModel:
    return OpenAICompatibleChatModel(
        settings, api_key=api_key, transport=httpx.MockTransport(handler)
    )


def _ok_body(content: str = "你好", model: str = "test-model") -> dict[str, object]:
    return {
        "model": model,
        "choices": [
            {
                "message": {"content": content, "reasoning_content": "内部推理"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
            "completion_tokens_details": {"reasoning_tokens": 3},
        },
    }


async def test_complete_sends_expected_payload_and_parses_result() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_ok_body())

    model = build(handler, make_settings())
    result = await model.complete([ChatMessage(role="user", content="你好")], tag="router")

    assert len(captured) == 1
    request = captured[0]
    assert str(request.url) == "https://llm.example/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-unit-test"
    payload = json.loads(request.content)
    assert payload["model"] == "test-model"
    assert payload["messages"] == [{"role": "user", "content": "你好"}]
    assert payload["temperature"] == 0.0
    assert payload["max_tokens"] == 128
    assert payload["reasoning_effort"] == "low"
    assert payload["stream"] is False
    assert "response_format" not in payload

    assert result.content == "你好"
    assert result.reasoning == "内部推理"
    assert result.finish_reason == "stop"
    assert result.usage == Usage(
        prompt_tokens=11, completion_tokens=7, reasoning_tokens=3, total_tokens=18
    )
    await model.aclose()


async def test_json_mode_requires_json_keyword_and_sets_response_format() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_ok_body('{"intent": "qa"}'))

    model = build(handler, make_settings())
    with pytest.raises(LLMError, match="json"):
        await model.complete([ChatMessage(role="user", content="分类这句话")], json_mode=True)
    assert calls == []

    await model.complete(
        [ChatMessage(role="user", content='输出 JSON：{"intent": "qa"}')], json_mode=True
    )
    payload = json.loads(calls[-1].content)
    assert payload["response_format"] == {"type": "json_object"}
    await model.aclose()


async def test_401_is_mapped_to_as_5033_without_leaking_key() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": {
                    "message": "Authentication Fails, Your api key is invalid",
                    "request_id": "req_abc",
                    "code": "invalid_request_error",
                }
            },
        )

    model = build(handler, make_settings())
    with pytest.raises(LLMError) as excinfo:
        await model.complete([ChatMessage(role="user", content="hi")])
    error = excinfo.value
    assert str(error.code) == "AS-5033"
    assert error.status == 401
    assert error.detail["request_id"] == "req_abc"
    dumped = json.dumps(error.to_payload("tr_x"), ensure_ascii=False)
    assert "sk-unit-test" not in dumped
    await model.aclose()


async def test_retry_then_success_counts_attempts() -> None:
    attempts: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(500, json={"error": {"message": "boom"}})
        return httpx.Response(200, json=_ok_body("ok"))

    model = build(handler, make_settings(llm_retry=1))
    result = await model.complete([ChatMessage(role="user", content="hi")])
    assert result.content == "ok"
    assert len(attempts) == 2
    await model.aclose()


async def test_falls_back_to_secondary_model_after_primary_failures() -> None:
    models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        models.append(payload["model"])
        if payload["model"] == "test-model":
            return httpx.Response(503, json={"error": {"message": "down"}})
        return httpx.Response(200, json=_ok_body("from-fallback", model="fallback-model"))

    model = build(handler, make_settings(llm_retry=0))
    result = await model.complete([ChatMessage(role="user", content="hi")])
    assert result.content == "from-fallback"
    assert models == ["test-model", "fallback-model"]
    await model.aclose()


async def test_4xx_is_not_retried() -> None:
    attempts: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    model = build(handler, make_settings(llm_retry=3))
    with pytest.raises(LLMError) as excinfo:
        await model.complete([ChatMessage(role="user", content="hi")])
    assert excinfo.value.status == 400
    assert len(attempts) == 1  # 参数错误重试无意义
    await model.aclose()


async def test_missing_api_key_fails_before_network() -> None:
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=_ok_body())

    model = build(handler, make_settings(), api_key="")
    with pytest.raises(LLMError, match="LLM API Key"):
        await model.complete([ChatMessage(role="user", content="hi")])
    assert calls == []
    await model.aclose()


SSE_BODY = (
    'data: {"choices":[{"delta":{"reasoning_content":"想"}}]}\n'
    'data: {"choices":[{"delta":{"reasoning_content":"一下"}}]}\n'
    'data: {"choices":[{"delta":{"content":"七天"}}]}\n'
    'data: {"choices":[{"delta":{"content":"无理由"}}]}\n'
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":5,"completion_tokens":7,"total_tokens":12,'
    '"completion_tokens_details":{"reasoning_tokens":2}}}\n'
    "data: [DONE]\n"
)


async def test_stream_separates_reasoning_from_content() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=SSE_BODY.encode(),
            headers={"content-type": "text/event-stream"},
        )

    model = build(handler, make_settings())
    reasoning: list[str] = []
    content: list[str] = []
    finish: str | None = None
    usage: Usage | None = None
    async for delta in model.stream([ChatMessage(role="user", content="七天无理由")]):
        if delta.reasoning:
            reasoning.append(delta.reasoning)
        if delta.content:
            content.append(delta.content)
        if delta.finish_reason:
            finish = delta.finish_reason
        if delta.usage:
            usage = delta.usage
    assert "".join(reasoning) == "想一下"
    assert "".join(content) == "七天无理由"
    assert "想" not in "".join(content)  # 推理绝不混入正文
    assert finish == "stop"
    assert usage is not None and usage.total_tokens == 12
    await model.aclose()


async def test_stream_error_status_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    model = build(handler, make_settings())
    with pytest.raises(LLMError) as excinfo:
        async for _delta in model.stream([ChatMessage(role="user", content="hi")]):
            pass
    assert excinfo.value.status == 429
    await model.aclose()


@pytest.mark.parametrize(
    ("line", "expected_content", "expected_reasoning"),
    [
        ('data: {"choices":[{"delta":{"content":"a"}}]}', "a", ""),
        ('data: {"choices":[{"delta":{"reasoning_content":"r"}}]}', "", "r"),
        ("data: [DONE]", None, None),
        ("", None, None),
        (": keep-alive", None, None),
        ("data: not-json", None, None),
    ],
)
def test_parse_sse_line(
    line: str, expected_content: str | None, expected_reasoning: str | None
) -> None:
    delta = parse_sse_line(line)
    if expected_content is None:
        assert delta is None
        return
    assert delta is not None
    assert delta.content == expected_content
    assert delta.reasoning == expected_reasoning


async def test_mock_model_replays_and_fails_loudly_on_miss() -> None:
    messages = [ChatMessage(role="user", content="退款多久到账")]
    model = MockChatModel()
    model.add(
        messages=messages,
        response={"content": "3-5 个工作日", "usage": {"total_tokens": 9}, "finish_reason": "stop"},
        temperature=0.0,
        max_tokens=128,
        json_mode=False,
    )
    result = await model.complete(messages, max_tokens=128)
    assert result.content == "3-5 个工作日"
    assert result.usage.total_tokens == 9

    key = request_key(
        model="mock", messages=messages, temperature=0.0, max_tokens=128, json_mode=False
    )
    assert key  # 稳定键存在

    with pytest.raises(LLMError, match="cassette miss"):
        await model.complete([ChatMessage(role="user", content="没录过的问题")])


async def test_mock_stream_chunks_content() -> None:
    messages = [ChatMessage(role="user", content="q")]
    model = MockChatModel()
    model.add(
        messages=messages,
        response={"content": "一二三四五六七八九十十一十二十三", "usage": {"total_tokens": 4}},
        temperature=0.0,
        max_tokens=None,
        json_mode=False,
    )
    chunks = [delta.content async for delta in model.stream(messages) if delta.content]
    assert len(chunks) > 1
    assert "".join(chunks) == "一二三四五六七八九十十一十二十三"
