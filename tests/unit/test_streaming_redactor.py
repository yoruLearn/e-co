"""流式脱敏测试：号码跨 chunk 时不得泄露半截 PII。"""

from __future__ import annotations

from app.prompt.guard import StreamingRedactor


def _feed_all(redactor: StreamingRedactor, chunks: list[str]) -> str:
    output = ""
    for chunk in chunks:
        output += redactor.feed(chunk)
    output += redactor.flush()
    return output


def test_phone_split_across_chunks_is_never_leaked() -> None:
    redactor = StreamingRedactor(holdback=8)
    chunks = ["请联系 138", "1234", "5678 处理。"]
    output = _feed_all(redactor, chunks)
    assert "13812345678" not in output
    assert "138****5678" in output


def test_output_streams_by_sentence() -> None:
    redactor = StreamingRedactor(holdback=4)
    first = redactor.feed("第一句。")
    assert first == "" or first.endswith("。")
    second = redactor.feed("第二句。")
    combined = first + second + redactor.flush()
    assert "第一句。" in combined and "第二句。" in combined


def test_long_text_flushes_progressively() -> None:
    redactor = StreamingRedactor(holdback=8)
    text = "退款时效说明：余额一个工作日到账。银行卡三到七个工作日。"
    emitted = redactor.feed(text)
    assert emitted, "超过 holdback 且含句末标点时应能先发一段"
    emitted += redactor.flush()
    assert emitted == text


def test_redaction_hits_are_counted() -> None:
    redactor = StreamingRedactor(holdback=4)
    _feed_all(redactor, ["身份证 110101199003071234 已核验。"])
    assert redactor.hits.get("id_card") == 1


def test_empty_stream_is_noop() -> None:
    redactor = StreamingRedactor()
    assert redactor.feed("") == ""
    assert redactor.flush() == ""
