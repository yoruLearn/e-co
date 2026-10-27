# 电商售后管家 · 开发命令（Windows 下建议用 scripts/*.ps1）
SHELL := /bin/sh
COMPOSE := docker compose -f deploy/docker-compose.yml
PY := uv run

.PHONY: help install lint format type test cov ci up down status logs compose-config migrate smoke

help: ## 列出命令
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## 安装依赖（含 dev）
	uv sync --extra dev

lint: ## ruff 静态检查
	$(PY) ruff check .

format: ## ruff 自动修复 + 格式化
	$(PY) ruff check --fix . && $(PY) ruff format .

type: ## mypy 类型检查
	$(PY) mypy app

test: ## 单测
	$(PY) pytest -q

cov: ## 单测 + 覆盖率门禁
	$(PY) pytest -q --cov=app --cov-report=term-missing

ci: ## 与 CI 一致的全量门禁
	pwsh -File scripts/ci.ps1

up: ## 起开发栈（含构建）
	pwsh -File scripts/up.ps1 -Build

down: ## 停开发栈
	pwsh -File scripts/down.ps1

status: ## 健康矩阵
	pwsh -File scripts/status.ps1

logs: ## 跟踪 api 日志
	$(COMPOSE) logs -f api

compose-config: ## 校验 compose 文件
	$(COMPOSE) --env-file deploy/.env.example config -q

migrate: ## 本地执行迁移（需可达 MySQL）
	$(PY) alembic upgrade head

smoke: ## 本地起服务并打探针（无依赖时 /readyz 应 503）
	$(PY) python scripts/smoke.py
