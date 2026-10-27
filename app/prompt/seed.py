"""把 ``seeds/prompts.yaml`` 灌入 ``prompt_registry`` 表（开发/CI 引导用）。

生产的发布/回滚走 ``POST /admin/prompts/{key}/publish``（M3，带评测门禁与审计）；
这里是"从 0 到 1"的初始化工具，重复执行是幂等的（同 key+version 覆盖内容）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import yaml
from sqlalchemy import select

from app.common.clients import build_clients
from app.common.config import get_settings
from app.common.logging import configure_logging
from app.db.models import PromptRegistry


def load_prompt_seeds(path: Path) -> dict[str, dict[str, object]]:
    """同步读取 YAML（文件 IO 不放在 async 函数里，避免阻塞事件循环）。"""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    prompts = raw.get("prompts") or {}
    return {str(key): item for key, item in prompts.items() if isinstance(item, dict)}


def _as_int(value: object, *, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return default


async def seed_from_yaml(
    *,
    session_factory: object,
    path: Path,
    updated_by: str = "seed",
) -> dict[str, int]:
    """把 YAML 中的 prompts 写入表；返回 ``{"inserted": n, "updated": n}``。"""
    prompts = load_prompt_seeds(path)
    inserted = 0
    updated = 0
    async with session_factory() as session:  # type: ignore[operator]
        for key, item in prompts.items():
            if "content" not in item:
                continue
            version = _as_int(item.get("version"), default=1)
            content = str(item["content"]).strip()
            status = str(item.get("status", "published"))
            row = await session.scalar(
                select(PromptRegistry).where(
                    PromptRegistry.prompt_key == str(key),
                    PromptRegistry.version == version,
                )
            )
            if row is None:
                session.add(
                    PromptRegistry(
                        prompt_key=str(key),
                        version=version,
                        content=content,
                        status=status,
                        updated_by=updated_by,
                    )
                )
                inserted += 1
            else:
                row.content = content
                row.status = status
                row.updated_by = updated_by
                updated += 1
        await session.commit()
    return {"inserted": inserted, "updated": updated}


async def _amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="灌入 Prompt 基线")
    parser.add_argument("--path", default=None, help="默认取 PROMPT_SEED_PATH")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, "console")
    path = Path(args.path or settings.prompt_seed_path)
    clients = build_clients(settings)
    try:
        result = await seed_from_yaml(
            session_factory=clients.session_factory,
            path=path,
        )
    except Exception as exc:  # CLI 输出可读错误
        print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await clients.aclose()
    print(json.dumps({"path": str(path), **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
