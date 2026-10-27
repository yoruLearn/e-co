"""RAG 错误类型。

检索/索引失败一律 ``AS-5032``（依赖不可用），由上层决定降级（关键词单通道 / 兜底话术）。
"""

from __future__ import annotations

from app.api.errors import AppError, ErrorCode


class RetrievalError(AppError):
    def __init__(self, message: str, *, detail: dict[str, object] | None = None) -> None:
        super().__init__(ErrorCode.DEPENDENCY_UNAVAILABLE, message, detail=detail or {})


class IndexBuildError(AppError):
    def __init__(self, message: str, *, detail: dict[str, object] | None = None) -> None:
        super().__init__(ErrorCode.INTERNAL, message, detail=detail or {})
