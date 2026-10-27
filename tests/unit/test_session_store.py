"""会话记忆测试：窗口截断、摘要触发、Redis 故障降级。"""

from __future__ import annotations

from typing import Any

from app.session.store import SessionStore
from app.session.summarize import SessionSummarizer, transcript_of


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.fail = False

    async def get(self, key: str) -> Any:
        if self.fail:
            raise ConnectionError("down")
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        if self.fail:
            raise ConnectionError("down")
        self.data[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        for key in keys:
            self.data.pop(key, None)
        return len(keys)


class FakeSummarizer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, transcript: str) -> str:
        self.calls.append(transcript)
        return "用户想退蓝牙耳机，已确认质量问题。"


async def test_append_creates_turns_and_trims_window() -> None:
    store = SessionStore(FakeRedis(), window_turns=2)
    for index in range(5):
        state = await store.append("s1", user_message=f"q{index}", assistant_message=f"a{index}")
    assert len(state.turns) == 4  # 2 轮
    assert state.turns[0]["content"] == "q3"
    assert state.turns[-1]["content"] == "a4"


async def test_summary_triggered_after_window_overflow() -> None:
    summarizer = FakeSummarizer()
    store = SessionStore(FakeRedis(), window_turns=2, summarizer=summarizer)
    for index in range(4):
        state = await store.append("s1", user_message=f"q{index}", assistant_message=f"a{index}")
    assert summarizer.calls, "超过窗口 2 倍 + 2 条后应触发摘要"
    assert state.summary
    assert "蓝牙耳机" in state.summary
    history = state.history()
    assert history[0]["role"] == "system"
    assert "历史摘要" in history[0]["content"]


async def test_summary_failure_keeps_conversation() -> None:
    async def broken(_transcript: str) -> str:
        raise RuntimeError("llm down")

    store = SessionStore(FakeRedis(), window_turns=1, summarizer=broken)
    for index in range(3):
        state = await store.append("s1", user_message=f"q{index}", assistant_message=f"a{index}")
    assert state.turns  # 对话保留
    assert state.summary == ""


async def test_redis_failure_degrades_to_stateless() -> None:
    redis = FakeRedis()
    redis.fail = True
    store = SessionStore(redis)
    state = await store.load("s1")
    assert state.turns == []
    result = await store.append("s1", user_message="q", assistant_message="a")
    assert result.turns  # 内存里仍有内容，只是没落 Redis
    await store.clear("s1")  # 不抛异常


async def test_load_handles_corrupted_payload() -> None:
    redis = FakeRedis()
    redis.data["sess:s1"] = "{not json"
    store = SessionStore(redis)
    assert (await store.load("s1")).turns == []


async def test_summarizer_prompt_render_missing_key_falls_back(monkeypatch) -> None:
    class FakePrompts:
        async def render(self, key: str, **values: Any) -> str:
            if key != "summary":
                raise KeyError(key)
            return f"总结：{values['history']}"

    class FakeChat:
        async def complete(self, messages: Any, **kwargs: Any) -> Any:
            from app.llm.types import ChatResult

            return ChatResult(content="摘要内容", model="fake")

        async def aclose(self) -> None:
            return None

    summarizer = SessionSummarizer(FakeChat(), FakePrompts())  # type: ignore[arg-type]
    assert await summarizer("user: 我要退货") == "摘要内容"
    assert await summarizer("   ") == ""


def test_transcript_of_joins_turns() -> None:
    turns = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]
    assert transcript_of(turns) == "user: q1\nassistant: a1"
