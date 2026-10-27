"""LLM / Embedding 类型与错误（M1）。

设计约束（来自 Spec §6.2 / §14 / §19.1）：

- 上游模型故障必须归一成 ``AS-5033``（503），并由调用方决定降级（话术兜底/转人工）；
- 推理模型的 ``reasoning_content`` 与用户可见正文**严格分离**，不进入 SSE ``delta``；
- 错误信息不得包含 API Key / 请求头（只保留 request_id 与状态码）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.api.errors import AppError, ErrorCode

Role = Literal["system", "user", "assistant"]


def _as_int(value: object) -> int:
    """上游 usage 字段类型不可控（int/str/None），统一安全转换。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


class LLMError(AppError):
    """上游模型调用失败（超时/鉴权/5xx/协议异常）。"""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        request_id: str | None = None,
        model: str | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        merged: dict[str, object] = {}
        if status is not None:
            merged["upstream_status"] = status
        if request_id:
            merged["request_id"] = request_id
        if model:
            merged["model"] = model
        merged.update(detail or {})
        super().__init__(
            ErrorCode.UPSTREAM_MODEL_UNAVAILABLE,
            message,
            detail=merged,
        )
        self.status = status
        self.request_id = request_id


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Role
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_api(cls, raw: dict[str, object] | None) -> Usage:
        if not raw:
            return cls()
        details = raw.get("completion_tokens_details")
        reasoning = _as_int(details.get("reasoning_tokens")) if isinstance(details, dict) else 0
        return cls(
            prompt_tokens=_as_int(raw.get("prompt_tokens")),
            completion_tokens=_as_int(raw.get("completion_tokens")),
            reasoning_tokens=reasoning,
            total_tokens=_as_int(raw.get("total_tokens")),
        )

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


@dataclass(frozen=True, slots=True)
class ChatResult:
    content: str
    model: str
    usage: Usage = field(default_factory=Usage)
    reasoning: str = ""
    finish_reason: str | None = None

    @property
    def empty(self) -> bool:
        return not self.content.strip()


@dataclass(frozen=True, slots=True)
class ChatDelta:
    """流式增量。``reasoning`` 只用于观测，不推给用户。"""

    content: str = ""
    reasoning: str = ""
    finish_reason: str | None = None
    usage: Usage | None = None

    @property
    def has_content(self) -> bool:
        return bool(self.content)
