"""文档分块与索引构建（Spec §7.1）。

M0 只锁定**规格常量与元数据契约**，实现留给 M1：

- 分块 500 字 / 80 overlap，按标题层级切分；
- 每个 chunk 保留 ``doc_id / title / section / version`` 元数据；
- collection 命名 ``aftersales_v{N}``，与 ``index_version`` 表联动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

CHUNK_SIZE: Final[int] = 500
CHUNK_OVERLAP: Final[int] = 80
EMBEDDING_BATCH_SIZE: Final[int] = 32
COLLECTION_PREFIX: Final[str] = "aftersales_v"


@dataclass(frozen=True, slots=True)
class Chunk:
    """可入库的最小检索单元。"""

    doc_id: str
    title: str
    section: str
    version: str
    text: str
    index: int = 0
    metadata: dict[str, str] = field(default_factory=dict)

    def to_metadata(self) -> dict[str, str]:
        merged = {
            "doc_id": self.doc_id,
            "title": self.title,
            "section": self.section,
            "version": self.version,
            "index": str(self.index),
        }
        merged.update(self.metadata)
        return merged


def collection_name(index_version: str) -> str:
    """``v3`` → ``aftersales_v3``。"""
    cleaned = index_version.strip()
    if not cleaned:
        msg = "索引版本不能为空"
        raise ValueError(msg)
    return f"{COLLECTION_PREFIX}{cleaned.lstrip('vV')}"


def chunk_document(text: str, *, doc_id: str, title: str, version: str) -> list[Chunk]:
    """按标题层级切分并做 500/80 滑窗。

    TODO(M1): 见 ``docs/m0/02-tickets.md`` 的 M1 阶段任务；此处刻意不实现，
    避免 M0 引入半成品的切分逻辑污染评测基线。
    """
    raise NotImplementedError("RAG 分块将在 M1 实现（Spec §7.1）")
