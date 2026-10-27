"""真实网关联调（默认跳过；本地 ``.env`` 指向 openai 时才跑）。

CI 永远不跑这里（没有 key，且不该为每次 PR 付模型费用）；它的价值是：
在接网关的真实环境里，用**最小 token** 验证协议、推理分流、JSON 模式、延迟量级。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from app.common.config import REPO_ROOT, Settings
from app.llm.embeddings import FastEmbedEmbedding, cosine
from app.llm.factory import build_chat_model
from app.llm.types import ChatMessage, LLMError

pytestmark = pytest.mark.integration_llm


@pytest.fixture(scope="module")
def live_settings() -> Settings:
    settings = Settings()
    if settings.llm_provider != "openai":
        pytest.skip("LLM_PROVIDER 不是 openai（.env 未接真实网关）")
    if not settings.llm_api_key_file:
        pytest.skip("未配置 LLM_API_KEY_FILE")
    key_path = Path(settings.llm_api_key_file)
    if not key_path.is_absolute():
        key_path = REPO_ROOT / key_path
    if not key_path.exists() or not settings.llm_api_key():
        pytest.skip(f"key 文件不存在或为空：{key_path}")
    return settings


async def test_live_complete_returns_real_answer(live_settings: Settings) -> None:
    model = build_chat_model(live_settings, cassette_dir="evals/cassettes")
    try:
        result = await model.complete(
            [ChatMessage(role="user", content="1+1 等于几？只回答数字。")],
            max_tokens=64,
            tag="live.test",
        )
    finally:
        await model.aclose()
    assert "2" in result.content
    assert result.usage.total_tokens > 0


async def test_live_stream_separates_reasoning(live_settings: Settings) -> None:
    model = build_chat_model(live_settings, cassette_dir="evals/cassettes")
    started = time.perf_counter()
    first_content: float | None = None
    content: list[str] = []
    reasoning_len = 0
    try:
        async for delta in model.stream(
            [ChatMessage(role="user", content="用一句话说明七天无理由退货。")],
            max_tokens=200,
            tag="live.stream",
        ):
            reasoning_len += len(delta.reasoning)
            if delta.content:
                content.append(delta.content)
                if first_content is None:
                    first_content = time.perf_counter() - started
    finally:
        await model.aclose()
    assert content, "流式未返回任何正文"
    assert first_content is not None
    # 推理模型首字约 1-3s；这里只做量级断言，SLO 门槛由压测负责
    assert first_content < 15.0
    assert reasoning_len > 0, "推理模型应产生 reasoning_content（仅观测，不推给用户）"


async def test_live_json_mode_returns_parsable_json(live_settings: Settings) -> None:
    model = build_chat_model(live_settings, cassette_dir="evals/cassettes")
    try:
        result = await model.complete(
            [
                ChatMessage(role="system", content="你是意图分类器，只输出 JSON，不要解释。"),
                ChatMessage(
                    role="user",
                    content=(
                        "把用户问题分到 qa / query / after_sales / chitchat 之一。"
                        '用户问题：我的退款到哪了。输出 JSON：{"intent": "..."}'
                    ),
                ),
            ],
            json_mode=True,
            max_tokens=200,
            tag="live.json",
        )
    finally:
        await model.aclose()
    payload = json.loads(result.content)
    assert payload["intent"] in {"qa", "query", "after_sales", "chitchat"}


async def test_live_missing_key_fails_closed(live_settings: Settings) -> None:
    model = build_chat_model(live_settings, cassette_dir="evals/cassettes")
    if not hasattr(model, "_api_key"):
        pytest.skip("mock provider")
    model._api_key = ""  # type: ignore[attr-defined] - 故意制造缺 key
    try:
        with pytest.raises(LLMError):
            await model.complete([ChatMessage(role="user", content="hi")])
    finally:
        await model.aclose()


@pytest.mark.local_embed
@pytest.mark.skipif(
    os.environ.get("RUN_LOCAL_EMBED") != "1",
    reason="需要下载本地模型；RUN_LOCAL_EMBED=1 时运行",
)
async def test_live_fastembed_model_download_and_similarity() -> None:
    pytest.importorskip("fastembed")
    model = FastEmbedEmbedding(
        model_name="BAAI/bge-small-zh-v1.5",
        dim=512,
        hf_endpoint="https://hf-mirror.com",
    )
    vectors = await model.embed(["退款多久到账", "退款到账时间是多久", "物流单号在哪里查"])
    query, related, unrelated = vectors
    assert len(query) == 512
    assert cosine(query, related) > cosine(query, unrelated)
