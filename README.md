# QUAP

QUAP is a single-operator A-share research platform built on [Qlib](https://github.com/microsoft/qlib). Tokenless public A-share sources flow through immutable daily/five-minute datasets, Qlib features, trained models, shared strategy evaluation, and versioned target-weight proposals. PostgreSQL owns data, workflow state, provenance, and human decisions.

Coverage includes Shanghai/Shenzhen Main Board, STAR Market, and ChiNext. “Low price” means low **unadjusted nominal share price**, not undervaluation. Proposals preserve exact stock weights and explicit cash. Acceptance creates a model-portfolio revision—not a brokerage position or order. The platform does not collect account balances, calculate executable quantities, or send orders.

Qlib is mandatory for recommendations. Missing sources, historical coverage, qualified releases, or valid predictions block publication; there is no native recommendation fallback. Lightweight API/UI/ingestion images do not install Qlib, but the standard deployment includes four required engine workers.

Implementation and fixture validation are in progress. Live source reachability, point-in-time history, model performance, 20-session shadow qualification, and five-minute capacity remain separate activation gates. The working tree is not evidence that a running deployment has been upgraded.

## Research extensions

The working tree adds isolated supplemental sources, immutable factor/configuration revisions, three-fold Qlib development comparisons, explicit candidate freeze, asynchronous model diagnostics through Alembic `0008`, an undated-security-master allowance in `0009`, partition DDL helpers in `0010`, and a descriptive Qlib research report. Seven workspaces: research evidence appears in Data & Pipeline, Models & Validation, and Stock Research, while Qlib Report shows IC, Rank IC, forward-return quantiles, and the benchmark-relative equal-weight book.

- Collection uses the go-stock A-share degradation order—通达信 (pytdx) → 东方财富 → 新浪 → 腾讯—and records the source that actually answered. One source serves a whole capability request; a timeout, empty body, or contract-breaking field moves the request to the next source. There is no vendor token, and units are normalized at this boundary (volume in shares, turnover in CNY).
- Optional `adata==2.9.5` is research/display/cross-check only, off by default, with explicit upstream-terms acknowledgment and bounded collection. Its HTTP-only daily adapter is unavailable under the HTTPS-only policy; same-day intraday support is fixture-tested, not live-provider certified.
- Safe daily factors use a restricted expression grammar, not Python. Experiments reserve the final test, pin source/code/configuration identity, and cannot activate models. Creating a challenger still requires the existing evaluation, shadow, and manual release gates.
- Diagnostics use training-only drift baselines and calendar-aligned mature labels. Insufficient samples remain unavailable; PSI and IC never change targets or trigger retraining.
- The opt-in Compose `research` profile adds `research-data`, `qlib-research`, and `qlib-diagnostics`; it does not replace the four required production Qlib services. RD-Agent and multi-agent/paid-LLM integration remain a separately gated later stage.

## Documentation

- [用户使用手册](docs/用户使用手册.md) — 操作员视角：登录、七个工作空间、CLI、API、配置、排障
- [上线风险与修复方案](docs/上线风险与修复方案.md) — 当前实际使用中的问题、证据与修复
- [系统架构设计](docs/系统架构设计.md)
- [系统框架](docs/系统框架.md)
- [部署说明](docs/部署说明.md)
- [系统验证报告](docs/系统验证报告.md)
- [Historical roadmap](docs/内部路线图.md) (superseded native-research/ledger scope; not the current delivery contract)
- 架构图：[docs/architecture.svg](docs/architecture.svg)
- [QUAP 平台专家包](experts/quap-platform-expert/README.md) — WorkBuddy 专家包「衡工」：平台架构、Qlib 流水线排障、部署与备份恢复

## Local development

Use Python 3.11 or 3.12 and [uv](https://docs.astral.sh/uv/). The package is `quant_platform`; the CLI is `quant-platform`. From the repository root:

```bash
uv sync --frozen --python 3.11 --group dev
.venv/bin/quant-platform --help
```

See the deployment guide for secret files, isolated tests, required Qlib engine services, the opt-in research profile, and artifact-aware recovery. The `qlib` packaging extra isolates the pinned `pyqlib==0.9.7` / LightGBM runtime; it is not an optional product feature. Training and inference run separately, and report reads never trigger either. The Qlib research report also runs in an isolated subprocess, so a missing install fails only that job and leaves collection running.

Legacy reports remain read-only `legacy_native` records. Operational rollback selects a still-qualified Qlib release or monitoring-only mode; it never restores native recommendations.
