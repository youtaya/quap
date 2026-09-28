"""Contracts shared by the control plane and the mandatory Qlib engine."""

import math
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from quant_platform.domain import CN, StrictModel, symbol

ENGINE_VERSION = "qlib-v1"
DATA_CONTRACT = "qlib-adjusted-v1"
Frequency = Literal["day", "5min"]

# 数据契约里各数据源能力的**显示名**。它服务于「阻塞文案」这一条链路：`pipeline.py` 用它拼就绪
# 阻塞文案，前端工作台持有一份同值副本（`frontend/src/domain/labels.ts`）用于「数据源能力」表。
# 放在 domain 而不是 `pipeline.py`，是因为词表属于数据契约本身，而 `pipeline` 会拖进数据库依赖——
# 任何想拿这份显示名的调用方都不该被迫先连上 PostgreSQL。
CAPABILITY_LABELS = {
    "securities": "证券目录",
    "calendar": "交易日历",
    "history": "历史采集",
    "factors": "复权因子",
    "benchmark": "基准指数",
    "constraints": "涨跌停推导",
    "security_history": "风险状态",
    "quotes": "行情采集",
    "minutes": "分钟线",
}

# 「今日」判决面的门禁链。`docs/DESIGN.md` 附录 A.3 是这张表的权威定义，代码只是它的副本：
# **阶段一三项互相独立**（都直接对原始状态判定，不存在谁依赖谁），**阶段二起顺序依赖**。
#
# `depends_on` 不只是文档，它是呈现规则的输入。依赖未通过时，下游门禁**不得**显示自身失败状态，
# 只能显示「等待上游」——否则操作员会看到一个自己无法行动的红点，误以为要先处理它，于是又开始了
# 那趟「Overview → 数据 → 模型 → 回 Overview」的往返。这正是这次重构要消灭的东西。
GATE_STAGES = {"foundation": "基础就绪", "data-model": "数据与模型", "output": "产出"}

# 动作类型三分（附录 A.4），决定卡点下方出现什么：
#   auto   可自解 —— 平台内有对应操作，给主按钮 + 预计耗时；
#   manual 半自解 —— 需改配置 / 重启平台进程，给「改什么」+「我已处理，重新检测」；
#   ops    需运维 —— 卡点在平台进程之外，给可复制指令 + 复检，**禁止**配一个点了不起作用的按钮。
GATE_ACTIONS = ("auto", "manual", "ops")

# 顺序即呈现顺序。`day_baseline` 判的是**日线**模型，所以它虽然只在五分钟频率下出现，却不挂在
# 五分钟自己的链上（`depends_on` 为空）——它是独立卡点，不该被上游挡住。
GATE_SPECS = (
    {"key": "capability", "stage": "foundation", "depends_on": (), "action": "auto"},
    {"key": "scope", "stage": "foundation", "depends_on": (), "action": "manual"},
    {"key": "engine", "stage": "foundation", "depends_on": (), "action": "ops"},
    {"key": "generation", "stage": "data-model", "depends_on": ("capability", "scope", "engine"), "action": "auto"},
    {"key": "model", "stage": "data-model", "depends_on": ("generation",), "action": "manual"},
    {"key": "day_baseline", "stage": "data-model", "depends_on": (), "action": "ops", "frequencies": ("5min",)},
)

# ⑥「今日建议」不是就绪条件，而是**产出状态**：它问的不是「能不能跑」，是「跑出来的东西在不在」。
# 所以它不进 `GATE_SPECS`（不参与 `ready` 判定），由 `readiness` 的 `output` 字段单独承载，
# 依赖关系固定为 `model`。
OUTPUT_GATE = {"key": "recommendation", "stage": "output", "depends_on": ("model",), "action": "auto"}


class PipelineBlocked(RuntimeError):
    """A missing prerequisite, never permission to use a substitute model."""


class ScanPolicy(StrictModel):
    price_ceiling: float = Field(10, gt=0)
    minimum_sessions: int = Field(120, ge=60, le=2000)
    minimum_turnover: float = Field(20_000_000, ge=0)
    top_n: int = Field(20, ge=1, le=20)
    max_weight: float = Field(0.05, gt=0, le=0.05)
    minimum_cash: float = Field(0.05, ge=0.05, le=1)
    change_band: float = Field(0.005, ge=0, le=0.05)
    minimum_coverage: float = Field(0.95, ge=0.95, le=1)
    exclude_risk_names: Literal[True] = True


class PolicyChange(StrictModel):
    expected_revision: int = Field(ge=0)
    policy: ScanPolicy


class PortfolioInput(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    weights: dict[str, float] = Field(default_factory=dict, max_length=300)
    cash_weight: float = Field(1, ge=0, le=1)
    expected_revision: int = Field(0, ge=0)

    @field_validator("name")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("A portfolio name is required.")
        return value.strip()

    @field_validator("weights")
    @classmethod
    def identities(cls, value):
        result = {}
        for code, weight in value.items():
            code = symbol(code)
            if code in result or not math.isfinite(weight) or not 0 < weight <= 1:
                raise ValueError("Weights must be unique, finite, positive fractions.")
            result[code] = weight
        return dict(sorted(result.items()))

    @model_validator(mode="after")
    def total(self):
        if not math.isclose(sum(self.weights.values()) + self.cash_weight, 1, abs_tol=1e-8):
            raise ValueError("Stock weights plus cash must equal one; weights are never normalized silently.")
        return self


class WatchlistInput(StrictModel):
    symbols: list[str] = Field(default_factory=list, max_length=300)
    expected_revision: int = Field(0, ge=0)

    @field_validator("symbols")
    @classmethod
    def identities(cls, value):
        parsed = [symbol(item) for item in value]
        if len(set(parsed)) != len(parsed):
            raise ValueError("Duplicate watchlist identities.")
        return sorted(parsed)


class RunInput(StrictModel):
    purpose: Literal["inference", "training", "shadow"] = "inference"
    frequency: Frequency = "day"
    model_id: UUID | None = None
    training_configuration_id: UUID | None = None
    as_of: datetime | None = None
    portfolio_id: UUID | None = None
    request_key: str = Field(min_length=1, max_length=120)

    @field_validator("as_of")
    @classmethod
    def aware(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("An explicit time zone is required.")
        return value

    @model_validator(mode="after")
    def shadow_model(self):
        if (self.purpose == "shadow") != (self.model_id is not None):
            raise ValueError("Only shadow requests require an explicit challenger model ID.")
        if self.training_configuration_id is not None and self.purpose != "training":
            raise ValueError("Training configurations are accepted only for explicit training requests.")
        return self


class Acceptance(StrictModel):
    portfolio_id: UUID | None = None
    selected_symbols: list[str] | None = Field(None, min_length=1, max_length=300)

    @field_validator("selected_symbols")
    @classmethod
    def selections(cls, value):
        return WatchlistInput.identities(value) if value is not None else None

    name: str = Field(min_length=1, max_length=120)
    expected_revision: int = Field(ge=0)
    expected_policy_revision: int = Field(ge=0)
    request_key: str = Field(min_length=1, max_length=120)


class ReleaseAction(StrictModel):
    expected_active_id: UUID | None = None


def five_minute_end(at):
    local = at.astimezone(CN)
    minute = local.hour * 60 + local.minute
    if not (575 <= minute <= 690 or 785 <= minute <= 900):
        return None
    return local.replace(minute=local.minute // 5 * 5, second=0, microsecond=0)


def intraday_window(cutoff, available):
    cutoff, available = cutoff.astimezone(CN), available.astimezone(CN)
    if available < cutoff or available > cutoff + timedelta(seconds=120):
        raise PipelineBlocked("Intraday publication deadline missed.")
    effective = cutoff + timedelta(minutes=5)
    session_end = cutoff.replace(hour=11 if cutoff.hour < 12 else 15, minute=30 if cutoff.hour < 12 else 0)
    if effective >= session_end:
        raise PipelineBlocked("No future execution-reference interval in this session.")
    return effective, min(cutoff + timedelta(minutes=7), session_end)


def action_for(previous, target, band=0.005):
    if target is None:
        return "unavailable"
    if previous == 0 and target == 0:
        return "watch"
    if previous > 0 and target == 0:
        return "exit"
    if abs(target - previous) < band:
        return "hold"
    return "add" if previous == 0 else "increase" if target > previous else "reduce"
