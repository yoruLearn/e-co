# M3 验证记录

> 原则：只记录**实际执行**的结果；需要 staging/生产的项标 BLOCKED，不计入通过。

## 2026-10-28

| # | 命令/动作 | 结果 |
| --- | --- | --- |
| 1 | `ruff check .` / `ruff format --check .` | ✅ 180 文件（含 `scripts/loadtest.py`） |
| 2 | `mypy app` | ✅ |
| 3 | `pytest -q` | ✅ 559 passed, 15 skipped |
| 4 | YAML 解析 `rules-slo.yml` / `docker-compose.staging.yml` | ✅ 可解析 |
| 5 | `python scripts/loadtest.py --help` | ✅ 参数正常 |

## BLOCKED（未执行，不能算通过）

| 项 | 阻塞原因 |
| --- | --- |
| 并发压测（50/100/200） | 本机无 Docker/MySQL/Chroma，GitHub CI 也不跑压测 |
| 回滚演练 ≤15min | 无发布环境/上一版本镜像 |
| 备份恢复演练 RPO/RTO | 无真实数据量与实例 |
| 告警触达 | 无 alertmanager/IM 接收端 |
| 灰度放量 | 无网关/多副本 |
| 依赖故障演练 | 无 staging 拓扑 |

## 结论

- M3 的**代码与配置交付物**已完成并纳入仓库；
- M3 的**验收类工作无法在当前环境完成**，因此 M3 不能标记为"已验证达标"。
- 下一步：在有 staging 的环境按 `docs/m4/03-drills.md` 逐项执行并把结果回填本文件。
