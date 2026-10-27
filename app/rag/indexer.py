"""离线索引构建（worker 执行，Spec §7.1）。

M0 只定义构建计划与版本状态机，实际 embedding / 入库在 M1：

``building → build_done → (评测抽样) → active → deprecated / rolled_back``
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from app.rag.chunker import collection_name


class IndexStatus(StrEnum):
    BUILDING = "building"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True, slots=True)
class IndexBuildPlan:
    """一次索引构建的输入。"""

    version: str
    source_dir: Path
    collection: str
    embedding_model: str
    chunk_size: int
    chunk_overlap: int

    @classmethod
    def for_version(
        cls,
        version: str,
        source_dir: Path,
        embedding_model: str,
        *,
        chunk_size: int = 500,
        chunk_overlap: int = 80,
    ) -> IndexBuildPlan:
        return cls(
            version=version,
            source_dir=source_dir,
            collection=collection_name(version),
            embedding_model=embedding_model,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )


def build_index(plan: IndexBuildPlan) -> int:
    """构建索引并返回文档数。

    TODO(M1): 文档解析 → 分块 → 批量向量化（32/批）→ 写 Chroma → 更新 ``index_version``。
    """
    raise NotImplementedError("离线索引构建将在 M1 实现（Spec §7.1）")
