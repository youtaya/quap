# QUAP

QUAP 是独立于 [Qlib](https://github.com/microsoft/qlib) 研究仓库的 A 股数据采集与篮子研究平台。Python 包名是 `quant_platform`，命令行入口是 `quant-platform`。主路径使用已授权的 Tushare 数据、PostgreSQL 和原生分析，安装与运行不导入 Qlib。

覆盖范围是科创板、创业板和沪深主板。系统供单操作员研究与监控：相对估值筛选给出可解释候选，持仓按成本价跟踪并给出研究建议。调度器在 Docker 保持运行时持续 tick；每个上海自然日 09:00 后向配置邮箱发送两封研究邮件（不超过 3 只估值候选，以及张江高科 SH600895 按成本 28 元的持仓建议）。审批由人工完成，不提交订单，也不承诺收益。

## 文档

- [系统架构设计](docs/系统架构设计.md)
- [系统框架](docs/系统框架.md)
- [部署说明](docs/部署说明.md)
- [系统验证报告](docs/系统验证报告.md)
- 架构图：[docs/architecture.svg](docs/architecture.svg)

## 本地开发

需要 Python 3.11 和 [uv](https://docs.astral.sh/uv/) 0.9.5。在仓库根目录：

```bash
uv sync --frozen --python 3.11 --group dev
.venv/bin/quant-platform --help
```

运行中的服务、密钥和验收步骤见部署说明。从旧监控迁移使用 `import-legacy`，由操作员提供外部旧监控运行时，只用于迁移和经批准的回滚。
