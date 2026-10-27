"""Prompt Registry 测试：来源回落、缓存、$ 变量替换、基线内容守卫。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.api.errors import AppError
from app.common.config import REPO_ROOT, Settings
from app.prompt.registry import (
    DbPromptSource,
    PromptKey,
    PromptRegistry,
    PromptTemplate,
    SeedPromptSource,
)

SEED_YAML = REPO_ROOT / "seeds" / "prompts.yaml"


def write_seed(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "prompts.yaml"
    path.write_text(body, encoding="utf-8")
    return path


SAMPLE_YAML = """
prompts:
  router:
    version: 3
    content: |
      分类问题：$question
  answer_rag:
    version: 1
    content: |
      只根据资料回答，用 [[1]] 标引用。
      $context
      $question
"""


class FakeDbSource:
    name = "db"

    def __init__(self, template: PromptTemplate | None) -> None:
        self._template = template
        self.calls = 0

    async def load(self, key: str, version: int | None) -> PromptTemplate | None:
        self.calls += 1
        if self._template is None:
            return None
        if self._template.key != key:
            return None
        if version is not None and self._template.version != version:
            return None
        return self._template


async def test_seed_source_loads_key_and_version(tmp_path: Path) -> None:
    path = write_seed(tmp_path, SAMPLE_YAML)
    source = SeedPromptSource(path)
    template = await source.load("router", None)
    assert template is not None
    assert template.version == 3
    assert "分类问题" in template.content
    assert template.source == "seed"
    assert await source.load("router", 3) is not None
    assert await source.load("router", 2) is None
    assert await source.load("missing", None) is None


async def test_registry_prefers_db_over_seed(tmp_path: Path) -> None:
    db_template = PromptTemplate(key="router", version=9, content="来自数据库", source="db")
    registry = PromptRegistry(
        [FakeDbSource(db_template), SeedPromptSource(write_seed(tmp_path, SAMPLE_YAML))]
    )
    template = await registry.get("router")
    assert template.version == 9
    assert template.content == "来自数据库"
    assert registry.source_names == ["db", "seed"]


async def test_registry_falls_back_to_seed_when_db_missing(tmp_path: Path) -> None:
    registry = PromptRegistry(
        [FakeDbSource(None), SeedPromptSource(write_seed(tmp_path, SAMPLE_YAML))]
    )
    template = await registry.get(PromptKey.ROUTER)
    assert template.source == "seed"
    assert template.version == 3


class CountingSource:
    name = "count"

    def __init__(self, inner: SeedPromptSource) -> None:
        self._inner = inner
        self.calls = 0

    async def load(self, key: str, version: int | None) -> PromptTemplate | None:
        self.calls += 1
        return await self._inner.load(key, version)


async def test_registry_caches_then_invalidates(tmp_path: Path) -> None:
    source = CountingSource(SeedPromptSource(write_seed(tmp_path, SAMPLE_YAML)))
    registry = PromptRegistry([source], cache_ttl_sec=60)
    first = await registry.get("router")
    second = await registry.get("router")
    assert first is second
    assert source.calls == 1, "第二次读取应命中缓存，不应回源"

    registry.invalidate("router")
    third = await registry.get("router")
    assert third == first
    assert source.calls == 2, "invalidate 后必须回源"

    registry.invalidate()
    await registry.get("router")
    assert source.calls == 3


async def test_registry_render_uses_dollar_placeholders(tmp_path: Path) -> None:
    registry = PromptRegistry([SeedPromptSource(write_seed(tmp_path, SAMPLE_YAML))])
    rendered = await registry.render("answer_rag", context="[1] 七天无理由", question="能退吗")
    assert "[1] 七天无理由" in rendered
    assert "能退吗" in rendered
    assert "[[1]]" in rendered  # JSON 风格的方括号不受影响


async def test_registry_render_missing_variable_fails_fast(tmp_path: Path) -> None:
    registry = PromptRegistry([SeedPromptSource(write_seed(tmp_path, SAMPLE_YAML))])
    with pytest.raises(KeyError):
        await registry.render("router")


async def test_registry_missing_prompt_raises_as_5001(tmp_path: Path) -> None:
    registry = PromptRegistry([SeedPromptSource(write_seed(tmp_path, SAMPLE_YAML))])
    with pytest.raises(AppError) as excinfo:
        await registry.get("not_exist")
    assert str(excinfo.value.code) == "AS-5001"
    assert excinfo.value.detail["prompt_key"] == "not_exist"


async def test_from_settings_wires_seed_path(tmp_path: Path) -> None:
    path = write_seed(tmp_path, SAMPLE_YAML)
    settings = Settings(_env_file=None, app_env="test", prompt_seed_path=str(path))
    registry = PromptRegistry.from_settings(settings)
    assert registry.source_names == ["seed"]
    template = await registry.get("router")
    assert template.version == 3


async def test_db_source_without_session_is_top_level_import() -> None:
    """确保 DB 源可被导入（真实读取由 CI 的 RAG job 用 MySQL 验证）。"""
    assert DbPromptSource is not None


# --------------------------------------------------------------------------- #
# 基线内容守卫：prompts.yaml 必须包含四类意图与引用要求
# --------------------------------------------------------------------------- #
async def test_baseline_prompts_cover_graph_nodes() -> None:
    registry = PromptRegistry([SeedPromptSource(SEED_YAML)])
    for key in ("router", "rewrite", "answer_rag", "clarify", "reject"):
        template = await registry.get(key)
        assert template.content.strip()
        assert template.status == "published"


async def test_baseline_router_has_four_intents() -> None:
    registry = PromptRegistry([SeedPromptSource(SEED_YAML)])
    content = (await registry.get("router")).content
    for intent in ("qa", "query", "after_sales", "chitchat"):
        assert intent in content
    assert "JSON" in content


async def test_baseline_answer_requires_citations() -> None:
    registry = PromptRegistry([SeedPromptSource(SEED_YAML)])
    content = (await registry.get("answer_rag")).content
    assert "[[1]]" in content
    assert "不要编造" in content or "不得编造" in content
    assert "$context" in content and "$question" in content
