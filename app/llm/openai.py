"""OpenAI 兼容 Chat 实现（DeepSeek / 任意兼容网关）。

关键点（都是实测踩出来的）：

1. **推理模型**：DeepSeek-V4 系列先输出 ``reasoning_content`` 再输出 ``content``。
   首字延迟约 2.2s（``reasoning_effort=low``），因此推理内容只用于观测与计费，
   绝不进入 SSE ``delta``；同时支持 ``reasoning_effort`` 降档。
2. **JSON 模式**：DeepSeek 要求 prompt 中出现 "json" 字样，否则 400；这里在发送前校验，
   属于编程错误，直接 fail-fast。
3. **重试与降级**：网络错误/429/5xx 重试 ``llm_retry`` 次，仍失败则切 ``LLM_FALLBACK_MODEL``
   再试一次；4xx（鉴权/参数）不重试。
4. **密钥零泄漏**：错误信息只带 ``request_id`` 与状态码，不带 header/body 原文。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from app.common.config import Settings
from app.common.logging import get_logger, log_warning
from app.common.metrics import LLM_TOKENS
from app.llm.types import ChatDelta, ChatMessage, ChatResult, LLMError, Usage

logger = get_logger("app.llm.openai")

_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_MAX_ERROR_BODY = 300


def _parse_error_body(raw: bytes, status: int, model: str) -> LLMError:
    """从上游错误响应体提取可读信息（只保留 message 与 request_id）。"""
    message = f"上游模型返回 {status}"
    request_id: str | None = None
    try:
        body: Any = json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            request_id = str(error.get("request_id") or "") or None
            upstream = error.get("message")
            if isinstance(upstream, str) and upstream:
                message = upstream[:_MAX_ERROR_BODY]
    return LLMError(message, status=status, request_id=request_id, model=model)


class OpenAICompatibleChatModel:
    """``POST {base}/chat/completions``。"""

    def __init__(
        self,
        settings: Settings,
        *,
        name: str | None = None,
        fallback_model: str | None = None,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str | None = None,
    ) -> None:
        self._settings = settings
        self.name = name or settings.llm_model
        self._fallback = (
            fallback_model if fallback_model is not None else settings.llm_fallback_model
        )
        self._base_url = (base_url or settings.llm_base_url).rstrip("/")
        self._url = f"{self._base_url}/chat/completions"
        self._api_key = api_key if api_key is not None else settings.llm_api_key()
        timeout = httpx.Timeout(settings.llm_timeout_ms / 1000, connect=10.0)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout, transport=transport)

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise LLMError(
                "缺少 LLM API Key：请配置 LLM_API_KEY_FILE（文件不存在或为空）",
                model=self.name,
            )
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _payload(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        temperature: float,
        max_tokens: int | None,
        json_mode: bool,
        reasoning_effort: str | None,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [message.as_dict() for message in messages],
            "temperature": temperature,
            "max_tokens": max_tokens or self._settings.llm_max_output_tokens,
            "stream": stream,
        }
        effort = (
            reasoning_effort
            if reasoning_effort is not None
            else self._settings.llm_reasoning_effort
        )
        if effort:
            payload["reasoning_effort"] = effort
        if json_mode:
            joined = " ".join(message.content for message in messages).lower()
            if "json" not in joined:
                raise LLMError(
                    'json_mode 要求 prompt 中出现 "json"（DeepSeek 兼容约束）',
                    model=model,
                )
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        json_mode: bool = False,
        reasoning_effort: str | None = None,
        tag: str = "llm",
    ) -> ChatResult:
        models = [self.name]
        if self._fallback and self._fallback != self.name:
            models.append(self._fallback)

        last_error: LLMError | None = None
        for index, model in enumerate(models):
            payload = self._payload(
                messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=json_mode,
                reasoning_effort=reasoning_effort,
                stream=False,
            )
            attempts = self._settings.llm_retry + 1
            for attempt in range(attempts):
                if index > 0 or attempt > 0:
                    log_warning(
                        logger,
                        "llm.retry",
                        model=model,
                        attempt=attempt,
                        fallback=index > 0,
                    )
                try:
                    return await self._post_once(payload, model=model, tag=tag)
                except LLMError as exc:
                    last_error = exc
                    retryable = exc.status is None or exc.status in _RETRYABLE_STATUS
                    if not retryable:
                        # 参数/鉴权类错误换模型也没用，直接抛出，避免把 400 放大成两次调用
                        raise
                    if attempt == attempts - 1:
                        break
        if last_error is None:  # pragma: no cover - 循环至少跑一次
            raise LLMError("上游模型调用失败", model=self.name)
        raise last_error

    async def _post_once(
        self,
        payload: dict[str, Any],
        *,
        model: str,
        tag: str,
    ) -> ChatResult:
        try:
            response = await self._client.post(self._url, json=payload, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise LLMError(
                f"上游模型超时（>{self._settings.llm_timeout_ms}ms）", model=model
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"上游模型网络异常：{type(exc).__name__}", model=model) from exc

        if response.status_code >= 400:
            raise _parse_error_body(response.content, response.status_code, model)

        try:
            body: dict[str, Any] = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise LLMError(
                "上游模型返回非 JSON 响应", model=model, status=response.status_code
            ) from exc

        choices = body.get("choices") or []
        if not choices:
            raise LLMError("上游模型返回空 choices", model=model, status=response.status_code)
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        reasoning = message.get("reasoning_content") if isinstance(message, dict) else None
        usage = Usage.from_api(body.get("usage") if isinstance(body.get("usage"), dict) else None)
        LLM_TOKENS.labels(node=tag).inc(usage.total_tokens)
        return ChatResult(
            content=content if isinstance(content, str) else "",
            model=str(body.get("model") or model),
            usage=usage,
            reasoning=reasoning if isinstance(reasoning, str) else "",
            finish_reason=choice.get("finish_reason") if isinstance(choice, dict) else None,
        )

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        tag: str = "llm",
    ) -> AsyncIterator[ChatDelta]:
        payload = self._payload(
            messages,
            model=self.name,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=False,
            reasoning_effort=reasoning_effort,
            stream=True,
        )
        usage = Usage()
        try:
            async with self._client.stream(
                "POST", self._url, json=payload, headers=self._headers()
            ) as response:
                if response.status_code >= 400:
                    raw = await response.aread()
                    raise _parse_error_body(raw, response.status_code, self.name)
                async for line in response.aiter_lines():
                    delta = parse_sse_line(line)
                    if delta is None:
                        continue
                    if delta.usage:
                        usage = delta.usage
                    yield delta
        except httpx.TimeoutException as exc:
            raise LLMError("上游模型流式超时", model=self.name) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"上游模型流式网络异常：{type(exc).__name__}", model=self.name) from exc
        finally:
            LLM_TOKENS.labels(node=tag).inc(usage.total_tokens)
            logger.debug("llm.stream.done", extra={"model": self.name, "tag": tag})

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def parse_sse_line(line: str) -> ChatDelta | None:
    """解析一行 SSE；非 data 行、空行、``[DONE]`` 返回 None。"""
    stripped = line.strip()
    if not stripped.startswith("data:"):
        return None
    data = stripped[5:].strip()
    if not data or data == "[DONE]":
        return None
    try:
        event: dict[str, Any] = json.loads(data)
    except json.JSONDecodeError:
        return None
    choices = event.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    raw_delta = choice.get("delta") or {}
    content = raw_delta.get("content") if isinstance(raw_delta, dict) else None
    reasoning = raw_delta.get("reasoning_content") if isinstance(raw_delta, dict) else None
    usage_raw = event.get("usage")
    usage = Usage.from_api(usage_raw) if isinstance(usage_raw, dict) else None
    return ChatDelta(
        content=content if isinstance(content, str) else "",
        reasoning=reasoning if isinstance(reasoning, str) else "",
        finish_reason=choice.get("finish_reason") if isinstance(choice, dict) else None,
        usage=usage,
    )
