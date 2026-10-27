"""Mock Chat 实现：录制回放（CI/离线），未命中即失败。

为什么"未命中即失败"而不是生成假答案：CI 里的评测门禁必须能真失败。
如果 mock 悄悄兜底，评测就是自欺欺人。

Cassette 文件格式（``evals/cassettes/<suite>.json``）::

    {"entries": [
      {"request": {"model": "deepseek-flash", "messages": [...], "temperature": 0.0,
                   "max_tokens": 800, "json_mode": false},
       "response": {"content": "...", "reasoning": "...", "finish_reason": "stop",
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20}}}
    ]}

匹配键 = sha256(canonical(request))，因此 prompt 改一个字就会 miss——这是刻意的。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, cast

from app.common.logging import get_logger
from app.common.metrics import LLM_TOKENS
from app.llm.types import ChatDelta, ChatMessage, ChatResult, LLMError, Usage

logger = get_logger("app.llm.mock")

MOCK_CHUNK_SIZE = 12
MOCK_MODEL_NAME = "mock"


def request_key(
    *,
    model: str,
    messages: Sequence[ChatMessage],
    temperature: float,
    max_tokens: int | None,
    json_mode: bool,
) -> str:
    canonical = {
        "model": model,
        "messages": [message.as_dict() for message in messages],
        "temperature": round(float(temperature), 4),
        "max_tokens": max_tokens,
        "json_mode": bool(json_mode),
    }
    digest = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return digest


def _entry_key(entry: dict[str, Any]) -> str:
    request = entry.get("request") or {}
    messages = [
        ChatMessage(role=item.get("role", "user"), content=item.get("content", ""))
        for item in request.get("messages", [])
    ]
    return request_key(
        model=str(request.get("model", MOCK_MODEL_NAME)),
        messages=messages,
        temperature=float(request.get("temperature", 0.0)),
        max_tokens=request.get("max_tokens"),
        json_mode=bool(request.get("json_mode", False)),
    )


class MockChatModel:
    """按键回放；``record`` 时把真实调用写回文件（供后续离线重放）。"""

    name = MOCK_MODEL_NAME

    def __init__(
        self,
        entries: dict[str, dict[str, Any]] | None = None,
        *,
        source: Path | None = None,
        allow_record: bool = False,
    ) -> None:
        self._entries = dict(entries or {})
        self._source = source
        self._allow_record = allow_record
        self.calls: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    @classmethod
    def from_dir(cls, directory: Path | str) -> MockChatModel:
        """加载目录下全部 cassette（同 key 后者覆盖前者，便于专项覆盖）。"""
        path = Path(directory)
        entries: dict[str, dict[str, Any]] = {}
        if path.exists():
            for file in sorted(path.glob("*.json")):
                payload = json.loads(file.read_text(encoding="utf-8"))
                for entry in payload.get("entries", []):
                    entries[_entry_key(entry)] = entry
        return cls(entries, source=path)

    def add(
        self,
        *,
        messages: Sequence[ChatMessage],
        response: dict[str, Any],
        **request: Any,
    ) -> None:
        key = request_key(
            model=str(request.get("model", MOCK_MODEL_NAME)),
            messages=messages,
            temperature=float(request.get("temperature", 0.0)),
            max_tokens=request.get("max_tokens"),
            json_mode=bool(request.get("json_mode", False)),
        )
        self._entries[key] = {
            "request": {
                "model": request.get("model", MOCK_MODEL_NAME),
                "messages": [message.as_dict() for message in messages],
                "temperature": float(request.get("temperature", 0.0)),
                "max_tokens": request.get("max_tokens"),
                "json_mode": bool(request.get("json_mode", False)),
            },
            "response": response,
        }

    # ------------------------------------------------------------------ #
    def _lookup(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float,
        max_tokens: int | None,
        json_mode: bool,
    ) -> dict[str, Any]:
        key = request_key(
            model=self.name,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )
        entry = self._entries.get(key)
        if entry is None:
            raise LLMError(
                "mock cassette miss：该请求没有录制记录（CI 必须显式录制后才能回放）",
                model=self.name,
                detail={
                    "cassette_key": key,
                    "messages": [message.as_dict() for message in messages],
                },
            )
        self.calls.append(entry["request"])
        response = entry.get("response")
        if not isinstance(response, dict):
            raise LLMError(
                "cassette response 格式非法（必须是 JSON object）",
                model=self.name,
                detail={"cassette_key": key},
            )
        return cast("dict[str, Any]", response)

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        json_mode: bool = False,
        reasoning_effort: str | None = None,  # noqa: ARG002 - mock 不区分推理档位
        tag: str = "llm",
    ) -> ChatResult:
        response = self._lookup(
            messages, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode
        )
        usage = Usage.from_api(response.get("usage"))
        LLM_TOKENS.labels(node=tag).inc(usage.total_tokens)
        return ChatResult(
            content=str(response.get("content", "")),
            model=str(response.get("model", self.name)),
            usage=usage,
            reasoning=str(response.get("reasoning", "")),
            finish_reason=response.get("finish_reason", "stop"),
        )

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,  # noqa: ARG002 - mock 不区分推理档位
        tag: str = "llm",
    ) -> AsyncIterator[ChatDelta]:
        response = self._lookup(
            messages, temperature=temperature, max_tokens=max_tokens, json_mode=False
        )
        content = str(response.get("content", ""))
        reasoning = str(response.get("reasoning", ""))
        for start in range(0, len(reasoning), MOCK_CHUNK_SIZE):
            yield ChatDelta(reasoning=reasoning[start : start + MOCK_CHUNK_SIZE])
        for start in range(0, len(content), MOCK_CHUNK_SIZE):
            yield ChatDelta(content=content[start : start + MOCK_CHUNK_SIZE])
        usage = Usage.from_api(response.get("usage"))
        LLM_TOKENS.labels(node=tag).inc(usage.total_tokens)
        yield ChatDelta(
            finish_reason=response.get("finish_reason", "stop"),
            usage=usage,
        )

    async def aclose(self) -> None:
        return None
