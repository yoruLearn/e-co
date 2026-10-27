#!/bin/sh
# 容器入口：可选先跑迁移（api 容器 RUN_MIGRATIONS=1；worker 容器为 0）
set -eu

if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
  echo "[entrypoint] alembic upgrade head"
  alembic upgrade head
fi

exec "$@"
